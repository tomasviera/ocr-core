<?php
/**
 * ocr-core/motores/lib_agy.php   (CORE — semilla: prensadelplata/WEB)
 *
 * Wrapper PHP que invoca el `transcribir_agy.py` *sibling* (mismo dir) como
 * subprocess para transcribir UNA imagen vía Antigravity CLI (agy) — sucesor
 * oficial de Gemini CLI (suscripción flat, sin costo por token).
 *
 * Motor agnóstico al dominio (regla dura del core): cero SQL, no toca BD. Loguea
 * por la costura `coreLog()` (cada proyecto define `coreLogSink`). Los paths
 * efímeros (sandbox, debug, workdir) entran por argumento — no se derivan de
 * ningún PROJECT_ROOT del proyecto consumidor.
 *
 * Análogo a `lib_aistudio.php::ejecutarAiStudio()` y a
 * `lib_gemini_cli.php::ejecutarGeminiCLI()`, con la misma shape de retorno
 * para que el worker pueda usarlos indistintamente según `proveedor`.
 *
 * Diferencias clave vs aistudio_web:
 *   - No usa CDP (agy es un proceso local lanzado bajo ConPTY por el .py).
 *   - El sandbox de agy es PRE-TRUSTED y PERSISTENTE por slot (lo entrega
 *     `agyReclamarSlot()` en lib_agy_cuentas.php — P5). Acá NO se crea ni
 *     se borra: solo se valida que exista.
 *   - El workdir efímero del wrapper (`temp/agy_subprocess/job<id>_<ts>/`)
 *     contiene prompt.md, salida.json y los logs del subprocess; SE BORRA
 *     en el éxito limpio y se conserva en error/debug para inspección.
 *   - El .py decodifica .b64 e instala imagen.jpg + prompt.md + .agents/
 *     settings.json en el sandbox por sí mismo. Acá NO duplicamos eso.
 *   - Token usage: vía statusLine side-channel (opcional, setup manual).
 *     Sin setup → tokens_*=0. tokens_thought lo expone el .py (no se fuerza
 *     a 0 como en aistudio_web). Sin costo (flat).
 *   - One-shot estricto: la política de reintento vive ENTERA en PHP a
 *     nivel worker (lib_worker_policy.php).
 *
 * Funciones expuestas:
 *   - agyPython(): ?string                  Resuelve binario python
 *   - agyBorrarWorkdir(string): bool        Borra workdir efímero del wrapper
 *   - agyArchivarWorkdir($wd, $archive)     Mueve workdir conservado a OneDrive (v18+)
 *   - agyLimpiarWorkdirsHuerfanos(...): int GC defensivo — manual; ya no se llama auto
 *   - ejecutarAgy(...): array               Equivalente a ejecutarAiStudio()
 *
 * Política de captura y lifecycle (v18+):
 *   - El `.py` escribe SIEMPRE el bundle forense en `<workdir>/debug/`
 *     (console_raw, history_text, extracted_*, metrics.json, agy_logfile.log).
 *     Sin gating, ok o !ok.
 *   - `ejecutarAgy()` NO toca el workdir post-corrida. Devuelve `sandbox_path =
 *     $workdir` (path local). El caller PHP es el ÚNICO que decide:
 *       - Job OK + QA limpia  → agyBorrarWorkdir($workdir)
 *       - !OK o QA grave      → agyArchivarWorkdir($workdir, $archiveDir)
 *     Esto permite capturar logs para QA post-hoc (SIN_FIN, CAPTURA_TIMEOUT,
 *     etc.) que sólo se conocen DESPUÉS de la corrida.
 *   - El GC defensivo `agyLimpiarWorkdirsHuerfanos` ya NO se llama desde
 *     `ejecutarAgy`/`chequearUsageAgy`. En el modelo nuevo, un huérfano en local
 *     es señal de crash del worker (raro): conservalo para inspección, no
 *     barrerlo en silencio. La función sigue exportada por si se necesita
 *     limpieza manual puntual.
 */

declare(strict_types=1);

/**
 * Costura logger del core (README §"Artefacto #2 — Costura logger").
 *
 * El motor loguea SIEMPRE por acá. El core NO conoce tabla ni columnas: cada
 * proyecto define `coreLogSink($engine, $nivel, $mensaje, $detalle)` en su
 * `core_bootstrap.php`, ruteándolo a su log de eventos:
 *   - prensadelplata → logDebug()  → transcripcion_debug_log
 *   - manuscritos-v3 → logEvento() → eventos
 *
 * Best-effort: nunca propaga. Guardado con `function_exists` para que un segundo
 * motor del core que también lo defina (Fase 2+) no provoque redeclare.
 */
if (!function_exists('coreLog')) {
    function coreLog(string $engine, string $nivel, string $mensaje, array $detalle = []): void
    {
        try {
            if (function_exists('coreLogSink')) {
                coreLogSink($engine, $nivel, $mensaje, $detalle);
            }
        } catch (Throwable $e) {
            // Logging best-effort: nunca propagar.
        }
    }
}

/**
 * Mensaje `-i` que se manda a agy en modo SIN IMAGEN (postproceso). El default
 * del `.py` ("Transcribí la imagen @imagen.jpg…") es la instrucción equivocada
 * para una tarea de texto puro: hacía que el modelo intentara transcribir la
 * imagen dummy 1×1 en vez de seguir el prompt. Sólo aplica al modo sin-imagen
 * (manuscritos-v3 postproceso); prensadelplata nunca corre sin-imagen.
 */
const AGY_CMD_I_SIN_IMAGEN =
    'Seguí al pie de la letra las instrucciones de @prompt.md y devolvé únicamente '
    . 'lo que ahí se pide. No transcribas ninguna imagen. No uses búsqueda web.';

// =====================================================================
// HELPERS
// =====================================================================

/**
 * Resuelve el path absoluto al binario python que ejecutará el wrapper.
 * Preferir el del PATH (`where python` en Windows).
 */
function agyPython(): ?string
{
    if (PHP_OS_FAMILY === 'Windows') {
        $out = []; $code = 0;
        @exec('where python 2>nul', $out, $code);
        if ($code !== 0 || empty($out)) return null;
        foreach ($out as $linea) {
            $cand = trim($linea);
            if ($cand !== '' && is_file($cand)) return $cand;
        }
        return null;
    }
    $out = []; $code = 0;
    @exec('which python3 2>/dev/null', $out, $code);
    if ($code === 0 && !empty($out)) return trim($out[0]);
    @exec('which python 2>/dev/null', $out, $code);
    if ($code === 0 && !empty($out)) return trim($out[0]);
    return null;
}

/**
 * Borra un workdir efímero del wrapper. Por seguridad, sólo opera si el path
 * contiene `/agy_subprocess/` en su realpath. NO toca el sandbox PRE-TRUSTED
 * del slot (ese vive en `temp/agy_sandbox_web/` y NUNCA lo borra este wrapper).
 */
function agyBorrarWorkdir(string $dirAbs): bool
{
    if (!is_dir($dirAbs)) return true;
    $real = realpath($dirAbs);
    if ($real === false || strpos(str_replace('\\', '/', $real), '/agy_subprocess/') === false) {
        return false;
    }
    $ok = true;
    $dirsParaBorrar = [];
    $it = new RecursiveIteratorIterator(
        new RecursiveDirectoryIterator($real, RecursiveDirectoryIterator::SKIP_DOTS),
        RecursiveIteratorIterator::CHILD_FIRST
    );
    foreach ($it as $item) {
        $path = $item->getPathname();
        if ($item->isDir()) {
            $dirsParaBorrar[] = $path;
        } else {
            if (!@unlink($path)) $ok = false;
        }
    }
    unset($it, $item);
    foreach ($dirsParaBorrar as $d) {
        clearstatcache(true, $d);
        if (is_dir($d) && !@rmdir($d)) $ok = false;
    }
    clearstatcache(true, $real);
    if (is_dir($real) && !@rmdir($real)) $ok = false;
    return $ok;
}

/**
 * Limpia workdirs huérfanos > N horas. Defensivo: cubre crashes del worker
 * que dejen `temp/agy_subprocess/job<id>_<ts>/` sin borrar.
 */
function agyLimpiarWorkdirsHuerfanos(string $baseDir, float $maxEdadHoras = 2.0): int
{
    if (!is_dir($baseDir)) return 0;
    $cortar = time() - (int)($maxEdadHoras * 3600);
    $borrados = 0;
    foreach (new DirectoryIterator($baseDir) as $entry) {
        if ($entry->isDot() || !$entry->isDir()) continue;
        try {
            $vacio = !(new FilesystemIterator($entry->getPathname(), FilesystemIterator::SKIP_DOTS))->valid();
        } catch (Throwable $e) {
            continue; // no legible: saltear
        }
        $graciaSecs = 600;
        $suficientementeViejo = $entry->getMTime() < (time() - $graciaSecs);
        if (($vacio && $suficientementeViejo) || $entry->getMTime() < $cortar) {
            if (agyBorrarWorkdir($entry->getPathname())) $borrados++;
        }
    }
    return $borrados;
}

// =====================================================================
// EJECUCIÓN — un intento puro (sin retries)
// =====================================================================

/**
 * Lanza el wrapper Python una sola vez y devuelve el JSON parseado + stdout/
 * stderr del subprocess + datos del proceso. NO maneja retries.
 *
 * @return array{
 *   ok: bool,                       // del JSON del wrapper
 *   data: array,                    // contenido del salida.json (puede estar vacío)
 *   stdout: string,
 *   stderr: string,
 *   exit_code: int,
 *   timed_out: bool,
 *   duracion_seg: float,
 * }
 */
function _agyEjecutarUnIntento(
    string  $pythonBin,
    string  $scriptPath,
    string  $imagenPath,
    string  $promptPath,
    string  $salidaJsonPath,
    string  $sandboxDir,
    int     $timeoutRespuesta,
    int     $procTimeout,
    ?string $homeDir,
    ?string $modeloAgy,
    ?int    $cols,
    ?int    $rows,
    ?float  $grace,
    ?string $agyBin,
    ?string $cmdI,
    ?string $cmdMode
): array {
    $cmd = [
        $pythonBin, '-u', $scriptPath,
        '--imagen',      $imagenPath,
        '--prompt',      $promptPath,
        '--salida-json', $salidaJsonPath,
        '--sandbox-dir', $sandboxDir,
        '--timeout',     (string)$timeoutRespuesta,
    ];
    if ($homeDir !== null && $homeDir !== '') {
        $cmd[] = '--home-dir';
        $cmd[] = $homeDir;
    }
    if ($modeloAgy !== null && $modeloAgy !== '') {
        $cmd[] = '--modelo-agy';
        $cmd[] = $modeloAgy;
    }
    if ($cols !== null && $cols > 0) {
        $cmd[] = '--cols';
        $cmd[] = (string)$cols;
    }
    if ($rows !== null && $rows > 0) {
        $cmd[] = '--rows';
        $cmd[] = (string)$rows;
    }
    if ($grace !== null && $grace > 0) {
        $cmd[] = '--grace';
        $cmd[] = (string)$grace;
    }
    if ($agyBin !== null && $agyBin !== '') {
        $cmd[] = '--agy-bin';
        $cmd[] = $agyBin;
    }
    if ($cmdI !== null && $cmdI !== '') {
        $cmd[] = '--cmd-i';
        $cmd[] = $cmdI;
    }
    // Modo de invocación de agy: 'interactive' (-i, legacy) o 'print' (-p, sin
    // inflar tablas). El .py default-ea a interactive si no se pasa → degradación
    // segura si un vendor viejo no manda este flag.
    if ($cmdMode !== null && $cmdMode !== '') {
        $cmd[] = '--cmd-mode';
        $cmd[] = $cmdMode;
    }

    $workdir    = dirname($salidaJsonPath);
    $stdoutFile = $workdir . DIRECTORY_SEPARATOR . 'stdout.log';
    $stderrFile = $workdir . DIRECTORY_SEPARATOR . 'stderr.log';
    @unlink($stdoutFile);
    @unlink($stderrFile);
    @unlink($salidaJsonPath);

    $descriptorSpec = [
        0 => ['pipe', 'r'],
        1 => ['file', $stdoutFile, 'w'],
        2 => ['file', $stderrFile, 'w'],
    ];

    $env = array_merge($_SERVER, $_ENV, ['PYTHONIOENCODING' => 'utf-8']);
    $envFiltrado = [];
    foreach ($env as $k => $v) {
        if (is_string($k) && is_string($v)) $envFiltrado[$k] = $v;
    }

    $t0 = microtime(true);
    $exitCode = -1;
    $timedOut = false;

    $proc = @proc_open($cmd, $descriptorSpec, $pipes, $workdir, $envFiltrado);
    if ($proc === false) {
        return [
            'ok' => false, 'data' => [],
            'stdout' => '', 'stderr' => 'proc_open() falló',
            'exit_code' => -1, 'timed_out' => false,
            'duracion_seg' => 0.0,
        ];
    }
    if (isset($pipes[0])) fclose($pipes[0]);

    while (true) {
        $status = proc_get_status($proc);
        if (!$status['running']) {
            $exitCode = $status['exitcode'];
            break;
        }
        if (microtime(true) - $t0 > $procTimeout) {
            $pid = $status['pid'] ?? 0;
            if ($pid > 0 && PHP_OS_FAMILY === 'Windows') {
                @exec("taskkill /F /T /PID {$pid} 2>nul");
            }
            proc_terminate($proc);
            for ($i = 0; $i < 30; $i++) {
                usleep(100_000);
                if (!proc_get_status($proc)['running']) break;
            }
            $timedOut = true;
            break;
        }
        usleep(300_000);
    }
    proc_close($proc);

    $stdout = @file_get_contents($stdoutFile) ?: '';
    $stderr = @file_get_contents($stderrFile) ?: '';
    $duracion = microtime(true) - $t0;

    $data = [];
    if (is_file($salidaJsonPath)) {
        $raw = @file_get_contents($salidaJsonPath);
        if ($raw !== false && $raw !== '') {
            $parsed = @json_decode($raw, true);
            if (is_array($parsed)) $data = $parsed;
        }
    }

    return [
        'ok' => !empty($data['ok']),
        'data' => $data,
        'stdout' => $stdout, 'stderr' => $stderr,
        'exit_code' => $exitCode, 'timed_out' => $timedOut,
        'duracion_seg' => round($duracion, 2),
    ];
}

// =====================================================================
// EJECUCIÓN PRINCIPAL (un intento — la política de reintento vive en PHP/worker)
// =====================================================================

/**
 * Análogo de `ejecutarAiStudio()` / `ejecutarGeminiCLI()`. Transcribe una
 * imagen vía Antigravity CLI (agy).
 *
 * @param string  $promptCompleto    Prompt completo ya renderizado por
 *                                   renderPromptParaJob() (familia 'antigravity':
 *                                   trae mención `@imagen.jpg` y los marcadores
 *                                   INICIO/FIN heredados de la base global).
 * @param string  $imagenPath        Ruta absoluta a la imagen (.jpg/.png/.b64).
 *                                   El .py decodifica .b64 al sandbox.
 * @param int     $jobId             ID del job (para naming del workdir efímero).
 * @param string  $imagenStem        Nombre base de la imagen sin extensión.
 * @param string  $resultadosDir     Dir base del proyecto para efímeros. NO se
 *                                   usa para el sandbox (ese lo entrega P5);
 *                                   sí es el fallback #2 de la base del workdir
 *                                   efímero (ver 'workdir_base').
 * @param array   $agyConfig         Config del slot + flags:
 *                                     'sandbox_dir'           (REQUERIDO, P5)
 *                                     'home_dir'              (opcional, v1 omitido)
 *                                     'modelo_agy'            (string mapeado por
 *                                                              agyMapearModelo)
 *                                     'workdir_base'          (opcional: base del
 *                                                              workdir efímero; si
 *                                                              falta cae a $resultadosDir
 *                                                              y luego a sys_get_temp_dir)
 *                                     'timeout_respuesta_seg' (default $timeout)
 *                                     'cols','rows','grace','agy_bin' (overrides
 *                                                              opcionales del .py)
 *                                     'cmd_mode'              ('print' = -p sin inflar
 *                                                              tablas; 'interactive' = -i
 *                                                              legacy. Default .py: interactive)
 *
 * NOTA v18+: el workdir efímero NO se borra ni se mueve adentro de esta
 * función. Queda en su path local y se devuelve en `sandbox_path` del shape.
 * El caller decide el lifecycle post-QA: `agyBorrarWorkdir()` (OK + QA limpia)
 * o `agyArchivarWorkdir($workdir, $archiveDir)` (!OK o QA grave).
 * @param int     $timeout           Compat: timeout por intento (s). Default 300.
 * @param int     $maxIntentos       NO se usa: corre 1 intento. La política de
 *                                   reintento vive en el worker (lib_worker_policy.php).
 *
 * @return array  Shape compatible con ejecutarAiStudio()/ejecutarGeminiCLI() —
 *                ver _agyShapeRespuesta() abajo.
 */
function ejecutarAgy(
    string $promptCompleto,
    string $imagenPath,
    int    $jobId,
    string $imagenStem,
    string $resultadosDir,
    array  $agyConfig = [],
    int    $timeout = 300,
    int    $maxIntentos = 1,
    bool   $sinImagen = false
): array {
    $t0Total = microtime(true);

    $sandboxDir = (string)($agyConfig['sandbox_dir'] ?? '');
    $workdirBaseCfg = isset($agyConfig['workdir_base']) && $agyConfig['workdir_base'] !== ''
                    ? (string)$agyConfig['workdir_base'] : null;
    $homeDir    = isset($agyConfig['home_dir']) && $agyConfig['home_dir'] !== ''
                    ? (string)$agyConfig['home_dir'] : null;
    $modeloAgy  = isset($agyConfig['modelo_agy']) ? (string)$agyConfig['modelo_agy'] : null;
    $tResp      = (int)($agyConfig['timeout_respuesta_seg'] ?? $timeout);
    $cols       = isset($agyConfig['cols']) ? (int)$agyConfig['cols'] : null;
    $rows       = isset($agyConfig['rows']) ? (int)$agyConfig['rows'] : null;
    $grace      = isset($agyConfig['grace']) ? (float)$agyConfig['grace'] : null;
    $agyBin     = isset($agyConfig['agy_bin']) ? (string)$agyConfig['agy_bin'] : null;
    // Chat-input (-i) de agy. En modo sin-imagen (postproceso) override-eamos el
    // default del .py (transcripción de imagen) por una instrucción de texto puro.
    // El caller puede forzar uno propio vía $agyConfig['cmd_i'].
    $cmdI       = isset($agyConfig['cmd_i']) && $agyConfig['cmd_i'] !== ''
                ? (string)$agyConfig['cmd_i']
                : ($sinImagen ? AGY_CMD_I_SIN_IMAGEN : null);
    // Modo de invocación: 'print' (-p, markdown crudo sin inflar tablas) o
    // 'interactive' (-i, legacy). Si el caller no lo setea, el .py default-ea a
    // interactive (compat: v3 sigue en -i, no setea cmd_mode).
    $cmdMode    = isset($agyConfig['cmd_mode']) && $agyConfig['cmd_mode'] !== ''
                ? (string)$agyConfig['cmd_mode'] : null;

    // ── 1. Validar precondiciones ──
    $pythonBin = agyPython();
    if ($pythonBin === null) {
        return _agyShapeError('python_no_encontrado: instalar Python y agregarlo al PATH', $t0Total);
    }

    // El .py es sibling de este archivo dentro de motores/ del core (vendorizado
    // a core_vendor/motores/). Ya NO se deriva de un PROJECT_ROOT del proyecto.
    $scriptPath = __DIR__ . DIRECTORY_SEPARATOR . 'transcribir_agy.py';
    if (!is_file($scriptPath)) {
        return _agyShapeError("script_no_encontrado: $scriptPath", $t0Total);
    }

    // Postproceso (v3): modo sin imagen real. agy igual necesita un image.jpg
    // copiable en su sandbox aunque el prompt no lo referencie → el motor crea
    // una dummy 1×1 en el workdir (antes esto vivía en el caller de v3). El .py
    // no cambia: recibe una imagen real (mínima).
    if (!$sinImagen && !is_file($imagenPath)) {
        return _agyShapeError("imagen_no_existe: $imagenPath", $t0Total);
    }

    if ($sandboxDir === '' || !is_dir($sandboxDir)) {
        return _agyShapeError("sandbox_dir_invalido: '$sandboxDir' (lo entrega agyReclamarSlot)", $t0Total);
    }

    // ── 2. Crear workdir efímero del wrapper ──
    // (El sandbox PRE-TRUSTED del slot NO se toca acá: el .py instala adentro
    // imagen.jpg + prompt.md + .agents/settings.json por sí mismo.)
    //
    // Regla dura del core (c): los paths efímeros entran por argumento, nunca
    // hardcodeados. Base del workdir, en orden:
    //   1) $agyConfig['workdir_base'] explícito;
    //   2) $resultadosDir (lo pasa el worker; en prensa = WEB/temp);
    //   3) fallback: directorio temporal del sistema.
    // Sobre la base se cuelga SIEMPRE 'agy_subprocess/' (agyBorrarWorkdir exige
    // ese segmento en el realpath como salvaguarda anti-borrado accidental).
    $workdirRoot = $workdirBaseCfg
        ?? ((is_string($resultadosDir) && $resultadosDir !== '' && is_dir($resultadosDir))
              ? $resultadosDir
              : sys_get_temp_dir());
    $workdirBase = rtrim(str_replace(['/', '\\'], DIRECTORY_SEPARATOR, $workdirRoot), DIRECTORY_SEPARATOR)
                 . DIRECTORY_SEPARATOR . 'agy_subprocess';
    @mkdir($workdirBase, 0755, true);
    // v18+: NO se llama agyLimpiarWorkdirsHuerfanos acá. Un huérfano en local
    // = crash del worker (raro); conservalo para inspección manual.

    $ts = date('Hisv');
    $workdir = $workdirBase . DIRECTORY_SEPARATOR . "job{$jobId}_{$imagenStem}_{$ts}";
    if (!@mkdir($workdir, 0755, true) && !is_dir($workdir)) {
        return _agyShapeError("workdir_no_se_pudo_crear: $workdir", $t0Total);
    }

    // Modo sin imagen: materializar la dummy 1×1 dentro del workdir efímero y
    // apuntar $imagenPath a ella. Así el resto del flujo (y el .py) es idéntico.
    if ($sinImagen) {
        $imagenPath = $workdir . DIRECTORY_SEPARATOR . '_dummy.jpg';
        if (@file_put_contents($imagenPath, _agyDummyJpgBytes()) === false) {
            agyBorrarWorkdir($workdir);
            return _agyShapeError("dummy_jpg_escritura_fallo: $imagenPath", $t0Total);
        }
    }

    // El .py recibe el prompt como ruta y lo copia al sandbox por sí mismo
    // (preparar_sandbox() → sandbox_dir/prompt.md). Lo escribimos a workdir/.
    $promptPath     = $workdir . DIRECTORY_SEPARATOR . 'prompt.md';
    $salidaJsonPath = $workdir . DIRECTORY_SEPARATOR . 'salida.json';

    if (@file_put_contents($promptPath, $promptCompleto) === false) {
        agyBorrarWorkdir($workdir);
        return _agyShapeError("prompt_escritura_fallo: $promptPath", $t0Total);
    }

    // Bundle forense (v18+): el .py lo escribe SIEMPRE dentro de $workdir/debug/.
    // Esta función NO toca el workdir post-corrida — el lifecycle lo decide PHP
    // post-QA usando agyArchivarWorkdir() o agyBorrarWorkdir().

    $procTimeout = $tResp + 120; // margen para arranque ConPTY + barrido zombis

    // ── 3. UN solo intento. La política de reintentos vive ENTERA en el worker
    //    (lib_worker_policy.php + procesarJobTranscripcionAgy). El wrapper NO
    //    reintenta por su cuenta — espejo de la decisión hecha para AI Studio
    //    (ver lib_aistudio.php:545-553 y BITACORA 2026-06-03). ──
    $intentos = 1;
    coreLog('agy', 'INFO',
        $sinImagen
            ? "Enviando prompt a agy (Antigravity CLI, sin imagen). Espera ~40–90s mientras agy procesa."
            : "Enviando imagen a agy (Antigravity CLI). Espera ~40–90s mientras agy procesa.",
        ['imagen' => $sinImagen ? '(sin imagen)' : basename($imagenPath), 'modelo' => $modeloAgy ?? '(global)', 'intento' => $intentos]);

    $resp = _agyEjecutarUnIntento(
        $pythonBin, $scriptPath,
        $imagenPath, $promptPath, $salidaJsonPath, $sandboxDir,
        $tResp, $procTimeout,
        $homeDir, $modeloAgy,
        $cols, $rows, $grace, $agyBin, $cmdI, $cmdMode
    );

    $data      = $resp['data'] ?? [];
    $veredicto = $data['veredicto'] ?? null;
    $cuota     = ($veredicto === 'CUOTA');

    // v18+: el workdir queda intacto en su path local. El caller decide post-QA
    // si llamarlo agyBorrarWorkdir($workdir) o agyArchivarWorkdir($workdir, ...).
    return _agyShapeRespuesta($resp, $workdir, $intentos, $t0Total,
        conservarWorkdir: true, cuotaAgotada: $cuota, erroresIntentos: []);
}

/**
 * Mueve un workdir efímero a `<archiveDir>/<basename(workdir)>/`. Devuelve el
 * path final o null si no se pudo mover (en cuyo caso el workdir queda
 * intacto en su ubicación original; el caller usa eso como fallback).
 *
 * v18+: lo llama el caller PHP post-QA. Casos típicos: veredicto != OK, o
 * `qa_sospecha != 0` (SIN_FIN, CAPTURA_TIMEOUT, WEBSEARCH, EXPLORACION_AGY, etc).
 *
 * Si el destino ya existe (colisión de timestamp), se le agrega sufijo `_dupN`.
 * Cross-volume safe: usa rename() si misma raíz (mismo disco), recursive copy
 * + delete si no (Tier 3: local `C:\prensa_runtime` → OneDrive `E:\OneDrive\...`).
 */
function agyArchivarWorkdir(string $workdir, string $archiveDir): ?string
{
    if (!is_dir($workdir)) return null;
    if (!@mkdir($archiveDir, 0755, true) && !is_dir($archiveDir)) {
        coreLog('agy', 'WARN', "archive_dir no se pudo crear: $archiveDir — workdir queda en local: $workdir", []);
        return null;
    }
    $base = basename($workdir);
    $dest = rtrim($archiveDir, DIRECTORY_SEPARATOR . '/') . DIRECTORY_SEPARATOR . $base;
    $n = 1;
    while (is_dir($dest)) {
        $dest = rtrim($archiveDir, DIRECTORY_SEPARATOR . '/') . DIRECTORY_SEPARATOR . $base . "_dup{$n}";
        if (++$n > 50) {
            coreLog('agy', 'WARN', "archive: demasiadas colisiones para $base; queda en local", []);
            return null;
        }
    }
    // 1) Intento atómico mismo volumen.
    if (@rename($workdir, $dest)) {
        return $dest;
    }
    // 2) Cross-volume (típico Tier 3: local C:\ → OneDrive E:\): copy + delete.
    if (!_agyCopiarDirRecursivo($workdir, $dest)) {
        coreLog('agy', 'WARN', "archive: copia recursiva falló de $workdir a $dest — workdir queda en local", []);
        @rmdir($dest); // limpiar parcial
        return null;
    }
    // Borramos el original. Reusamos agyBorrarWorkdir (exige /agy_subprocess/ en
    // el realpath, que se cumple porque $workdir sigue siendo el original).
    if (!agyBorrarWorkdir($workdir)) {
        coreLog('agy', 'WARN', "archive: copia OK pero no pude borrar el original $workdir — quedó duplicado", []);
    }
    return $dest;
}

/** Copia recursiva de directorio (cross-volume; usado por agyArchivarWorkdir). */
function _agyCopiarDirRecursivo(string $src, string $dst): bool
{
    if (!is_dir($src)) return false;
    if (!@mkdir($dst, 0755, true) && !is_dir($dst)) return false;
    $it = new RecursiveIteratorIterator(
        new RecursiveDirectoryIterator($src, RecursiveDirectoryIterator::SKIP_DOTS),
        RecursiveIteratorIterator::SELF_FIRST
    );
    foreach ($it as $item) {
        $rel = substr($item->getPathname(), strlen($src) + 1);
        $target = $dst . DIRECTORY_SEPARATOR . $rel;
        if ($item->isDir()) {
            if (!@mkdir($target, 0755, true) && !is_dir($target)) return false;
        } else {
            if (!@copy($item->getPathname(), $target)) return false;
        }
    }
    return true;
}

// =====================================================================
// CHEQUEO DE CUOTA via /usage (rama distinta a la transcripción)
// =====================================================================

/**
 * Pide a agy el reporte de cuota (`/usage` por stdin del TUI) y devuelve un
 * shape con los datos parseados del grupo GEMINI MODELS.
 *
 * Análogo a ejecutarAgy() pero MUCHO más simple: el .py corre con
 * `--modo=usage`, abre la TUI sin args, manda `/usage\r`, parsea el snapshot
 * y devuelve un JSON con account_email/plan_tier/weekly/h5. NO toca scratch ni
 * sandbox (no rompe la próxima transcripción), NO necesita prompt ni imagen.
 *
 * Reutiliza la maquinaria de _agyEjecutarUnIntento via un helper interno
 * dedicado (firma corta: sólo lo que /usage necesita).
 *
 * Shape de retorno:
 *   [
 *     'ok'               => bool,
 *     'veredicto'        => 'OK'|'ERROR',
 *     'error'            => ?string,
 *     'account_email'    => ?string,
 *     'plan_tier'        => ?string,
 *     'weekly_pct_usado' => ?float,    // 0–100, NULL si no se pudo parsear
 *     'weekly_reset_seg' => ?int,      // segundos hasta el reset (0 = Quota available)
 *     'h5_pct_usado'     => ?float,
 *     'h5_reset_seg'     => ?int,
 *     'raw_screen'       => string,    // snapshot crudo del TUI tras /usage
 *     'duracion_seg'     => float,
 *     'estado_captura'   => ?string,   // OK_QUIESCENT | READY_TIMEOUT | POST_TIMEOUT | …
 *     'bytes_total'      => int,
 *     'exit_code'        => ?int,
 *     'parser_notes'     => string[],
 *     'engine'           => 'agy',
 *   ]
 *
 * @param string  $sandboxDir Sandbox PRE-TRUSTED del slot agy. Sólo se usa
 *                            como cwd del subprocess. NO se modifica.
 * @param ?string $homeDir    Override del HOME (mismo gesto que ejecutarAgy).
 *                            NULL = HOME del usuario Windows que corre el worker.
 * @param int     $timeoutSeg Tope total del subprocess. Cubre cold start
 *                            (~80s la 1ra del proceso) + round del /usage (~6s)
 *                            + margen. Default 120.
 * @param ?string $workdirBase Base del workdir efímero del wrapper. Si null,
 *                             cae al sys temp.
 *
 * NOTA v18+: el workdir efímero NO se borra ni se mueve adentro. Queda en su
 * path local y el caller decide post-corrida con `agyBorrarWorkdir()` o
 * `agyArchivarWorkdir()`. El bundle forense (usage_snapshot, usage_history,
 * usage_metrics) se escribe siempre en `<workdir>/debug/`.
 */
function chequearUsageAgy(
    string  $sandboxDir,
    ?string $homeDir   = null,
    int     $timeoutSeg = 120,
    ?string $workdirBase = null
): array {
    $t0Total = microtime(true);

    $pythonBin = agyPython();
    if ($pythonBin === null) {
        return _agyShapeUsageError('python_no_encontrado: instalar Python y agregarlo al PATH', $t0Total);
    }
    $scriptPath = __DIR__ . DIRECTORY_SEPARATOR . 'transcribir_agy.py';
    if (!is_file($scriptPath)) {
        return _agyShapeUsageError("script_no_encontrado: $scriptPath", $t0Total);
    }
    if ($sandboxDir === '' || !is_dir($sandboxDir)) {
        return _agyShapeUsageError("sandbox_dir_invalido: '$sandboxDir' (lo entrega agyReclamarSlot)", $t0Total);
    }

    // Workdir efímero (mismo patrón que ejecutarAgy).
    $workdirRoot = ($workdirBase !== null && $workdirBase !== '' && is_dir($workdirBase))
        ? $workdirBase
        : sys_get_temp_dir();
    $workdirBaseDir = rtrim(str_replace(['/', '\\'], DIRECTORY_SEPARATOR, $workdirRoot), DIRECTORY_SEPARATOR)
                    . DIRECTORY_SEPARATOR . 'agy_subprocess';
    @mkdir($workdirBaseDir, 0755, true);
    // v18+: ver nota en ejecutarAgy sobre GC.

    $ts = date('Hisv');
    $workdir = $workdirBaseDir . DIRECTORY_SEPARATOR . "usage_check_{$ts}";
    if (!@mkdir($workdir, 0755, true) && !is_dir($workdir)) {
        return _agyShapeUsageError("workdir_no_se_pudo_crear: $workdir", $t0Total);
    }
    $salidaJsonPath = $workdir . DIRECTORY_SEPARATOR . 'salida.json';

    coreLog('agy', 'INFO', "Chequeando cuota agy via /usage (sandbox={$sandboxDir})", [
        'sandbox_dir' => $sandboxDir, 'home_dir' => $homeDir, 'timeout' => $timeoutSeg,
    ]);

    $cmd = [
        $pythonBin, '-u', $scriptPath,
        '--modo', 'usage',
        '--salida-json', $salidaJsonPath,
        '--sandbox-dir', $sandboxDir,
        '--timeout',     (string)$timeoutSeg,
    ];
    if ($homeDir !== null && $homeDir !== '') {
        $cmd[] = '--home-dir';
        $cmd[] = $homeDir;
    }
    if ($debugCaptureOk) {
        $cmd[] = '--debug-capture-ok';
    }

    $stdoutFile = $workdir . DIRECTORY_SEPARATOR . 'stdout.log';
    $stderrFile = $workdir . DIRECTORY_SEPARATOR . 'stderr.log';
    @unlink($stdoutFile);
    @unlink($stderrFile);
    @unlink($salidaJsonPath);

    $descriptorSpec = [
        0 => ['pipe', 'r'],
        1 => ['file', $stdoutFile, 'w'],
        2 => ['file', $stderrFile, 'w'],
    ];
    $env = array_merge($_SERVER, $_ENV, ['PYTHONIOENCODING' => 'utf-8']);
    $envFiltrado = [];
    foreach ($env as $k => $v) {
        if (is_string($k) && is_string($v)) $envFiltrado[$k] = $v;
    }

    // Cap del proceso: timeout del subprocess (cold start cubierto en el .py
    // como READY_TIMEOUT) + margen para barrido de zombies + escritura del JSON.
    $procTimeout = $timeoutSeg + 60;

    $t0 = microtime(true);
    $exitCode = -1;
    $timedOut = false;

    $proc = @proc_open($cmd, $descriptorSpec, $pipes, $workdir, $envFiltrado);
    if ($proc === false) {
        return _agyShapeUsageError("proc_open_fallo", $t0Total);
    }
    if (isset($pipes[0])) fclose($pipes[0]);

    while (true) {
        $status = proc_get_status($proc);
        if (!$status['running']) {
            $exitCode = $status['exitcode'];
            break;
        }
        if (microtime(true) - $t0 > $procTimeout) {
            $pid = $status['pid'] ?? 0;
            if ($pid > 0 && PHP_OS_FAMILY === 'Windows') {
                @exec("taskkill /F /T /PID {$pid} 2>nul");
            }
            proc_terminate($proc);
            for ($i = 0; $i < 30; $i++) {
                usleep(100_000);
                if (!proc_get_status($proc)['running']) break;
            }
            $timedOut = true;
            break;
        }
        usleep(300_000);
    }
    proc_close($proc);

    $duracion = microtime(true) - $t0;
    $data = [];
    if (is_file($salidaJsonPath)) {
        $raw = @file_get_contents($salidaJsonPath);
        if ($raw !== false && $raw !== '') {
            $parsed = @json_decode($raw, true);
            if (is_array($parsed)) $data = $parsed;
        }
    }

    // v18+: NO se toca el workdir acá; el caller decide post-respuesta.
    $ok = !empty($data['ok']);

    return [
        'ok'               => $ok,
        'sandbox_path'     => $workdir,
        'veredicto'        => $data['veredicto'] ?? ($timedOut ? 'ERROR' : 'ERROR'),
        'error'            => $data['error'] ?? ($timedOut ? 'proc_timeout' : ($exitCode === 0 ? null : "exit_code={$exitCode} sin_salida_json")),
        'account_email'    => $data['account_email']    ?? null,
        'plan_tier'        => $data['plan_tier']        ?? null,
        'weekly_pct_usado' => isset($data['weekly_pct_usado']) ? (float) $data['weekly_pct_usado'] : null,
        'weekly_reset_seg' => isset($data['weekly_reset_seg']) ? (int) $data['weekly_reset_seg'] : null,
        'h5_pct_usado'     => isset($data['h5_pct_usado']) ? (float) $data['h5_pct_usado'] : null,
        'h5_reset_seg'     => isset($data['h5_reset_seg']) ? (int) $data['h5_reset_seg'] : null,
        'raw_screen'       => (string) ($data['raw_screen'] ?? ''),
        'duracion_seg'     => round(microtime(true) - $t0Total, 2),
        'estado_captura'   => $data['estado_captura'] ?? null,
        'bytes_total'      => (int) ($data['bytes_total'] ?? 0),
        'exit_code'        => $exitCode,
        'parser_notes'     => $data['parser_notes'] ?? [],
        'engine'           => 'agy',
    ];
}

/** Shape de error preflight para chequearUsageAgy (sin workdir). */
function _agyShapeUsageError(string $errorMsg, float $t0Total): array
{
    return [
        'ok'               => false,
        'sandbox_path'     => null,
        'veredicto'        => 'ERROR_PREFLIGHT',
        'error'            => $errorMsg,
        'account_email'    => null,
        'plan_tier'        => null,
        'weekly_pct_usado' => null,
        'weekly_reset_seg' => null,
        'h5_pct_usado'     => null,
        'h5_reset_seg'     => null,
        'raw_screen'       => '',
        'duracion_seg'     => round(microtime(true) - $t0Total, 2),
        'estado_captura'   => null,
        'bytes_total'      => 0,
        'exit_code'        => null,
        'parser_notes'     => [],
        'engine'           => 'agy',
    ];
}

/**
 * Bytes de un JPEG 1×1 (dummy). Lo usa el modo $sinImagen: agy exige un
 * image.jpg copiable en su sandbox aunque el prompt no lo referencie (postproceso
 * de v3). Antes vivía en el caller (lib_postprocesador::_postprocDummyJpg).
 */
function _agyDummyJpgBytes(): string
{
    return base64_decode(
        '/9j/2wCEAAEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEB' .
        'AQEBAQEBAQEBAQEBAQEBAQEBAQEBAQH/2wBDAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEB' .
        'AQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQH/wgARCAABAAEDASIA' .
        'AhEBAxEB/8QAFQABAQAAAAAAAAAAAAAAAAAAAAH/xAAUAQEAAAAAAAAAAAAAAAAAAAAA/9oA' .
        'DAMBAAIQAxAAAAEH/8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABBQI//8QAFBEBAAAA' .
        'AAAAAAAAAAAAAAAAAP/aAAgBAwEBPwE//8QAFBEBAAAAAAAAAAAAAAAAAAAAAP/aAAgBAgEB' .
        'PwE//8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQAGPwI//8QAFBABAAAAAAAAAAAAAAAA' .
        'AAAAAP/aAAgBAQABPyE//9oADAMBAAIAAwAAABAA/8QAFBEBAAAAAAAAAAAAAAAAAAAAAP/a' .
        'AAgBAwEBPxA//8QAFBEBAAAAAAAAAAAAAAAAAAAAAP/aAAgBAgEBPxA//8QAFBABAAAAAAAA' .
        'AAAAAAAAAAAAAP/aAAgBAQABPxA//9k='
    );
}

// =====================================================================
// HELPERS DE SHAPE DE RETORNO
// =====================================================================

/**
 * Devuelve el shape de error preflight (sin workdir).
 */
function _agyShapeError(string $errorMsg, float $t0Total): array
{
    return [
        'ok' => false,
        'error' => $errorMsg,
        'response' => '',
        'stats' => [],
        'tools' => null,
        'tokens_input' => 0, 'tokens_output' => 0, 'tokens_thought' => 0,
        'tokens_cached' => 0, 'tokens_total' => 0,
        'session_id' => null,
        'stdout_raw' => '', 'stderr_raw' => '',
        'intentos' => 0,
        'exit_code' => null,
        'duracion_seg' => round(microtime(true) - $t0Total, 2),
        'sandbox_path' => null,
        'cuota_agotada' => false,
        'engine' => 'agy',
        'veredicto' => 'ERROR_PREFLIGHT',
    ];
}

/**
 * Mapea el resultado de un intento al shape compatible con `ejecutarAiStudio()`
 * / `ejecutarGeminiCLI()` + extras agy-específicos.
 *
 * Campos del shape común (idénticos a _aistudioShapeRespuesta):
 *   ok, response, error, stats, tools, tokens_input/output/thought/cached/total,
 *   session_id, stdout_raw, stderr_raw, intentos, exit_code, duracion_seg,
 *   sandbox_path, cuota_agotada, engine.
 *
 * Extras agy (consumidos por procesarJobTranscripcionAgy):
 *   veredicto, fuente_response, fin_presente, websearch_detectado,
 *   websearch_patrones, websearch_fuente, tools_used, longitud_sospechosa,
 *   stdout_largo_sospechoso, estado_captura, bytes_leidos, zombis_barridos,
 *   modelo_pedido, statusline_disponible, context_window_size,
 *   used_percentage, plan_tier, imagen_cargada_ok (bool|null, bump v28).
 *
 * `fuente_response` (agregado 2026-06-21) indica de dónde salió `response`
 * para que el worker decida QA bits post-hoc:
 *   - "ini_fin"  : INICIO y FIN visibles (camino feliz prensa).
 *   - "ini_only" : INICIO sin FIN (truncado; QA_BIT_SIN_FIN).
 *   - "history"  : sin INICIO/FIN; cayó al history limpio de pyte (v2 con
 *                  prompt `[tipo:]`, o prensa con instruction-following raro).
 *   - "screen"   : fallback al viewport visible (caso muy raro).
 *   - "vacio"    : ningún dato útil; `ok=false`.
 */
function _agyShapeRespuesta(
    array  $intento,
    string $workdir,
    int    $intentos,
    float  $t0Total,
    bool   $conservarWorkdir,
    bool   $cuotaAgotada,
    array  $erroresIntentos = []
): array {
    $data = $intento['data'] ?? [];
    $ok = !empty($data['ok']);
    $sandboxPath = $workdir;
    if (!$conservarWorkdir && is_dir($workdir)) {
        if (agyBorrarWorkdir($workdir)) {
            $sandboxPath = null;
        }
    }

    $extras = [];
    if (!empty($erroresIntentos))            $extras['errores_intentos']         = $erroresIntentos;
    if (isset($data['veredicto']))           $extras['veredicto']                = $data['veredicto'];
    if (isset($data['fuente_response']))     $extras['fuente_response']          = (string)$data['fuente_response'];
    if (array_key_exists('fin_presente', $data))
                                             $extras['fin_presente']             = (bool)$data['fin_presente'];
    if (array_key_exists('websearch_detectado', $data))
                                             $extras['websearch_detectado']      = (bool)$data['websearch_detectado'];
    if (isset($data['websearch_patrones']))  $extras['websearch_patrones']       = $data['websearch_patrones'];
    if (isset($data['websearch_fuente']))    $extras['websearch_fuente']         = $data['websearch_fuente'];
    if (isset($data['tools_used']))          $extras['tools_used']               = $data['tools_used'];
    if (!empty($data['longitud_sospechosa']))
                                             $extras['longitud_sospechosa']      = true;
    if (!empty($data['stdout_largo_sospechoso']))
                                             $extras['stdout_largo_sospechoso']  = true;
    if (isset($data['estado_captura']))      $extras['estado_captura']           = $data['estado_captura'];
    if (isset($data['bytes_leidos']))        $extras['bytes_leidos']             = (int)$data['bytes_leidos'];
    if (isset($data['zombis_barridos']))     $extras['zombis_barridos']          = (int)$data['zombis_barridos'];
    if (isset($data['modelo_pedido']))       $extras['modelo_pedido']            = $data['modelo_pedido'];
    if (array_key_exists('statusline_disponible', $data))
                                             $extras['statusline_disponible']    = (bool)$data['statusline_disponible'];
    if (isset($data['context_window_size'])) $extras['context_window_size']      = (int)$data['context_window_size'];
    if (isset($data['used_percentage']))     $extras['used_percentage']          = (float)$data['used_percentage'];
    if (isset($data['plan_tier']))           $extras['plan_tier']                = $data['plan_tier'];
    // Cuota en tiempo real del statusLine (bloque `quota` gemini-weekly/5h). Mismo
    // shape que produce el /usage (`chequearUsageAgy`) para que el consumidor PHP
    // (feed del worker) no discrimine origen. `null` — no 0/'' — cuando falta,
    // para distinguir "no hay statusLine" de "0% usado"; el feed gatea con !== null.
    $extras['weekly_pct_usado'] = isset($data['quota_weekly_pct_usado']) ? (float)$data['quota_weekly_pct_usado'] : null;
    $extras['weekly_reset_seg'] = isset($data['quota_weekly_reset_seg']) ? (int)$data['quota_weekly_reset_seg'] : null;
    $extras['h5_pct_usado']     = isset($data['quota_h5_pct_usado'])     ? (float)$data['quota_h5_pct_usado'] : null;
    $extras['h5_reset_seg']     = isset($data['quota_h5_reset_seg'])     ? (int)$data['quota_h5_reset_seg'] : null;
    $extras['account_email']    = $data['account_email'] ?? null;
    // Segundos hasta el reset de cuota (parseado del 429 "Resets in 13m27s" en
    // la .db de la conversación). 0 si no se pudo parsear → el worker usará el
    // default `agy_cooldown_seg` como fallback.
    if (isset($data['cuota_reset_seg']))     $extras['cuota_reset_seg']          = (int) $data['cuota_reset_seg'];
    // Motivo del fallo de arranque transitorio (veredicto=TRANSITORIO): backend
    // 500 al resolver modelo, auth/keyring timeout, etc. El worker rutea el
    // reintento por `veredicto`; este campo es para el error_msg/forense.
    if (isset($data['transitorio_motivo']))  $extras['transitorio_motivo']       = (string) $data['transitorio_motivo'];
    // Chequeo fáctico "agy adjuntó imagen.jpg al contexto multimodal" (bump v28,
    // 2026-07-23). Firma unívoca en la .db de conversación (ver
    // `_parsear_conversacion_db` del .py + notas/motor_agy.md §"Bump v28").
    // Tri-estado (bool | null): true=cargada, false=NO cargada (DB legible sin
    // firma → posible alucinación), null=unknown (DB no legible → NO disparar
    // QA para evitar falsos positivos). El worker inyecta `QA_BIT_NO_CARGO_IMAGEN`
    // sólo en `imagen_cargada_ok === false`.
    $extras['imagen_cargada_ok'] = array_key_exists('imagen_cargada_ok', $data)
        ? $data['imagen_cargada_ok']
        : null;

    // Token usage del statusLine side-channel (leído por el .py tras cerrar agy).
    // Si el setup manual del statusLine no se hizo, todos quedan en 0. A
    // diferencia de aistudio_web, `tokens_thought` NO se fuerza a 0: el .py lo
    // expone tal cual venga del statusLine (en la práctica statusLine no separa
    // thought de output, así que vendrá 0 igual — pero no lo cableamos acá).
    $tokIn   = isset($data['tokens_input'])   ? (int)$data['tokens_input']   : 0;
    $tokOut  = isset($data['tokens_output'])  ? (int)$data['tokens_output']  : 0;
    $tokTh   = isset($data['tokens_thought']) ? (int)$data['tokens_thought'] : 0;
    $tokCach = isset($data['tokens_cached'])  ? (int)$data['tokens_cached']  : 0;
    $tokTot  = isset($data['tokens_total'])   ? (int)$data['tokens_total']   : 0;

    return array_merge([
        'ok' => $ok,
        'response' => (string)($data['response'] ?? ''),
        'error' => $ok ? null : (string)($data['error'] ?? 'falla_sin_detalle'),
        'stats' => [],   // agy no expone stats granulares
        'tools' => null,
        'tokens_input' => $tokIn, 'tokens_output' => $tokOut, 'tokens_thought' => $tokTh,
        'tokens_cached' => $tokCach, 'tokens_total' => $tokTot,
        'session_id' => null,
        'stdout_raw' => (string)($intento['stdout'] ?? ''),
        'stderr_raw' => (string)($intento['stderr'] ?? ''),
        'intentos' => $intentos,
        'exit_code' => $intento['exit_code'] ?? null,
        'duracion_seg' => round(microtime(true) - $t0Total, 2),
        'sandbox_path' => $sandboxPath,
        'cuota_agotada' => $cuotaAgotada,
        'engine' => 'agy',
    ], $extras);
}
