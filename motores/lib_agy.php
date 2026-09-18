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
 *   - chequearUsageAgy(...): array          /usage → cuota parseada
 *
 * Lock de motor por host (v41): `ejecutarAgy` y `chequearUsageAgy` serializan el
 * lanzamiento de `agy` en el host con un lockfile (`%LOCALAPPDATA%\agy_motor.lock`
 * por default, override con `$agyConfig['lock_dir']`). Es ORTOGONAL al slot de
 * cola de la BD, que se libera temprano a propósito. Ver el bloque
 * "LOCK DE MOTOR POR HOST" abajo antes de tocarlo.
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
// LOCK DE MOTOR POR HOST (F5, core v41)
// =====================================================================
//
// PROBLEMA. El slot de cola vive en la BD y se libera TEMPRANO en el camino de
// éxito (rev. 140 de prensa: el tail de persistencia corre sin slot) para no
// dejar el motor ocioso. Pero el *motor* no es el slot: cuando el worker suelta
// el slot, el árbol `agy*` de esa corrida puede seguir vivo unos segundos, y el
// job siguiente arranca otro `agy` encima. Dos `agy` a la vez en un host se
// pisan el portapapeles (modo `paste`: recurso GLOBAL sin lock — ver
// notas/motor_agy.md §Bump v34) y el sandbox del slot.
//
// SOLUCIÓN. Un lock de ARCHIVO por host, ortogonal al slot de BD: se toma antes
// de lanzar el subproceso y se suelta cuando el árbol `agy*` está
// verificadamente muerto. El archivo vive FUERA de los árboles de proyecto
// (%LOCALAPPDATA% por default) porque prensa y transcriptor-manuscritos-v3
// vendorizan copias separadas del core y tienen que colisionar en el MISMO
// archivo.
//
// SALVEDADES MEDIDAS (V2, 2026-08-07, PHP 8.4 / Win 10 — exclusión 1-de-6 y
// liberación instantánea al morir el dueño). No "simplificar" ninguna:
//   (a) el handle tiene que sobrevivir toda la sección crítica ⇒ el helper lo
//       RETORNA y el caller lo retiene hasta su `finally`. Si fuera variable
//       local del helper, PHP lo cerraría al salir y el lock se soltaría solo.
//   (b) el lockfile NO SE BORRA JAMÁS, ni al liberar: el delete-pending de
//       Windows lo deja inadquirible hasta que muera el último dueño.
//   (c) NO se escribe el PID (ni nada) dentro del lockfile: `flock` en Windows
//       es MANDATORY y bloquearía la lectura ajena. Si alguna vez se quiere
//       diagnóstico, va en un archivo aparte (`agy_motor.owner`).
//   (d) si el path primario no abre → fallback a `sys_get_temp_dir()`; si ese
//       tampoco → WARN y NO-OP. Compatibilidad sobre estrictez: v3 corre este
//       mismo core y no puede quedar bloqueado por un lock que no se puede
//       crear.
//
// Todo entra por `$agyConfig['lock_dir']` (opcional). Ningún parámetro nuevo
// obligatorio: v3 no setea nada y funciona con el default.

/** Sentinela de lock degradado: "no se pudo lockear, seguimos igual". */
const AGY_LOCK_MOTOR_NOOP = 'agy_lock_motor_noop';

/** Segundos que se espera el lock antes de rendirse (no bloqueante, poll). */
const AGY_LOCK_MOTOR_ESPERA_SEG = 10.0;

/** Segundos que se espera a que muera todo `agy*` antes de soltar el lock. */
const AGY_LOCK_MOTOR_DRENAJE_SEG = 15.0;

/** Path del lockfile. `lock_dir` > %LOCALAPPDATA% > temp del sistema. */
function _agyLockMotorPath(array $agyConfig): string
{
    $dir = isset($agyConfig['lock_dir']) && $agyConfig['lock_dir'] !== ''
         ? (string)$agyConfig['lock_dir']
         : (string)(getenv('LOCALAPPDATA') ?: sys_get_temp_dir());
    return rtrim(str_replace(['/', '\\'], DIRECTORY_SEPARATOR, $dir), DIRECTORY_SEPARATOR)
         . DIRECTORY_SEPARATOR . 'agy_motor.lock';
}

/**
 * ¿Queda algún proceso cuyo nombre empiece con `agy` vivo en el host?
 *
 * Mismo criterio de prefijo que `_barrido_zombis` del `.py` (`name.lower()
 * .startswith("agy")`), a propósito: los dos tienen que estar de acuerdo sobre
 * qué es "el motor". BAJO EL LOCK, cualquier `agy*` vivo es de esta corrida, así
 * que el criterio host-global es correcto — y NO depende de la invariante
 * mutable "1 slot agy por host" (`agy_slots_*` son flags operativos).
 */
function _agyHayProcesoAgyVivo(): bool
{
    if (PHP_OS_FAMILY !== 'Windows') return false;
    $out = []; $code = 0;
    @exec('tasklist /FO CSV /NH 2>nul', $out, $code);
    if ($code !== 0) return false;   // no pudimos mirar ⇒ no colgamos al worker
    foreach ($out as $linea) {
        if (!preg_match('/^"([^"]*)"/', (string)$linea, $m)) continue;
        if (stripos($m[1], 'agy') === 0) return true;
    }
    return false;
}

/**
 * Toma el lock de motor del host.
 *
 * @return resource|string|null  resource = lock tomado (retenerlo hasta el
 *                               `finally` y pasarlo a _agyLiberarLockMotor);
 *                               AGY_LOCK_MOTOR_NOOP = degradado, se sigue sin
 *                               lock; null = OCUPADO tras agotar los reintentos.
 */
function _agyTomarLockMotor(array $agyConfig, float $esperaMaxSeg = AGY_LOCK_MOTOR_ESPERA_SEG)
{
    $paths    = [_agyLockMotorPath($agyConfig)];
    $fallback = rtrim(sys_get_temp_dir(), DIRECTORY_SEPARATOR . '/')
              . DIRECTORY_SEPARATOR . 'agy_motor.lock';
    if ($fallback !== $paths[0]) $paths[] = $fallback;

    $fh = false; $pathUsado = '';
    foreach ($paths as $p) {
        $dir = dirname($p);
        if (!is_dir($dir)) @mkdir($dir, 0755, true);
        // 'c': crea si falta, NO trunca, y no exige que el archivo tenga nada
        // adentro (salvedad (c): el lockfile queda vacío para siempre).
        $fh = @fopen($p, 'c');
        if ($fh !== false) { $pathUsado = $p; break; }
        coreLog('agy', 'WARN', "lock_motor: no pude abrir el lockfile ($p)", ['path' => $p]);
    }
    if ($fh === false) {
        coreLog('agy', 'WARN',
            'lock_motor: ningún path abrible — sigo SIN lock (no-op). '
            . 'Riesgo: dos agy concurrentes en el host.', ['paths' => $paths]);
        return AGY_LOCK_MOTOR_NOOP;
    }

    $t0 = microtime(true);
    while (true) {
        if (@flock($fh, LOCK_EX | LOCK_NB)) {
            return $fh;
        }
        if ((microtime(true) - $t0) >= $esperaMaxSeg) break;
        usleep(500_000);
    }
    @fclose($fh);   // (b) NO se borra el archivo, sólo se cierra ESTE handle.
    coreLog('agy', 'WARN',
        "lock_motor: ocupado tras " . round(microtime(true) - $t0, 1) . "s ($pathUsado)",
        ['path' => $pathUsado]);
    return null;
}

/**
 * Suelta el lock, pero recién cuando el motor está verificadamente libre.
 *
 * La espera activa es lo que convierte "el subproceso Python retornó" en "no
 * queda nada de `agy` corriendo": cubre el camino de timeout + `taskkill` del
 * wrapper, donde el árbol puede sobrevivir al `proc_close`. Si expira, se
 * loguea WARN y se suelta IGUAL — colgar al worker sería peor que un
 * solapamiento.
 *
 * @param resource|string|null $fh Lo que devolvió _agyTomarLockMotor.
 */
function _agyLiberarLockMotor($fh, float $esperaMaxSeg = AGY_LOCK_MOTOR_DRENAJE_SEG): void
{
    if (!is_resource($fh)) return;   // AGY_LOCK_MOTOR_NOOP / null → nada que soltar
    $t0 = microtime(true);
    while (_agyHayProcesoAgyVivo()) {
        if ((microtime(true) - $t0) >= $esperaMaxSeg) {
            coreLog('agy', 'WARN',
                "lock_motor: todavía hay procesos agy* tras {$esperaMaxSeg}s — suelto igual",
                []);
            break;
        }
        usleep(500_000);
    }
    @flock($fh, LOCK_UN);
    @fclose($fh);
    // (b) El lockfile NO se borra: con delete-pending de Windows quedaría
    //     inadquirible para el próximo job.
}

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
    ?string $cmdMode,
    ?callable $latido = null,
    int     $latidoCadaSeg = 30
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
    // Modo de invocación de agy: 'interactive' (-i, legacy), 'print' (-p, sin
    // inflar tablas) o 'paste' (bump v34). El .py default-ea a interactive si no
    // se pasa → degradación segura si un vendor viejo no manda este flag.
    //
    // WORKAROUND TEMPORAL — bug upstream agy #735 (LS 1.1.10 manda `inline_data`
    // de longitud 0 cuando el modelo abre una imagen con `view_file` →
    // INVALID_ARGUMENT 400). https://github.com/google-antigravity/antigravity-cli/issues/735
    // 'paste' abre el TUI SIN -i/-p y adjunta la imagen como media del mensaje
    // vía portapapeles, evitando el converter roto. El valor viaja OPACO: acá no
    // hay validación de valores (la hace el `choices` del argparse del .py), así
    // que sumar el modo no requirió tocar el armado del comando.
    // Si el bug se arregla upstream: evaluar volver a 'print'.
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
    $tUltimoLatido = $t0;   // primer latido a los $latidoCadaSeg del arranque

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
        // Latido opcional del caller (ver docblock de ejecutarAgy). El consumidor
        // queda BLOQUEADO acá hasta $procTimeout y no puede refrescar sus propias
        // marcas de vida; sin esto, un supervisor que las vigile lo da por muerto
        // y re-despacha su trabajo. Best-effort: nunca voltea la transcripción.
        if ($latido !== null && (microtime(true) - $tUltimoLatido) >= $latidoCadaSeg) {
            $tUltimoLatido = microtime(true);
            try { $latido(); } catch (Throwable $eLat) { /* ignorado a propósito */ }
        }
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
 *                                                              legacy; 'paste' = TUI con la
 *                                                              imagen adjuntada por
 *                                                              portapapeles, workaround del
 *                                                              bug upstream agy #735 — bump
 *                                                              v34. Default .py: interactive)
 *                                     'lock_dir'              (opcional, v41: directorio del
 *                                                              lockfile de motor por host.
 *                                                              Default %LOCALAPPDATA% y, si no
 *                                                              existe, el temp del sistema. Tiene
 *                                                              que quedar FUERA de los árboles de
 *                                                              proyecto para que prensa y v3
 *                                                              colisionen en el mismo archivo)
 *                                     'latido'                (callable|null, v42: callback
 *                                                              sin argumentos que el loop de
 *                                                              poll invoca cada
 *                                                              'latido_cada_seg' mientras el
 *                                                              subprocess corre. Ver NOTA v42)
 *                                     'latido_cada_seg'       (int, default 30, mínimo 5)
 *
 * NOTA v42 — por qué existe `latido`: el consumidor queda BLOQUEADO adentro de
 * esta función hasta `timeout_respuesta_seg` (+120 s de margen ConPTY), y en ese
 * lapso no puede refrescar sus propias marcas de vida. Si algo lo vigila por
 * timeout —en prensa, el supervisor: `workers_registrados.last_heartbeat` a los
 * 5 min y `trabajos_api.started_at` a los 30— lo da por muerto y re-despacha su
 * trabajo, quedando DOS ejecuciones del mismo job. Eso es el incidente #72025 de
 * prensa (2026-08-07): el segundo intento murió con `imagen_no_existe` porque el
 * primero ya había borrado la imagen, y su cierre en `error` pisó el `completado`
 * del primero, que ya había guardado la transcripción. El callback es opcional y
 * best-effort (una excepción adentro se traga y NO voltea la transcripción), así
 * que un caller que no lo pase se comporta igual que en v41.
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
    // Latido opcional (ver docblock). Ausente/no-callable = null → el loop de poll
    // se comporta exactamente como antes: retrocompatible con cualquier caller
    // que no lo pase (transcriptor-manuscritos-v3 incluido).
    $latido     = (isset($agyConfig['latido']) && is_callable($agyConfig['latido']))
                ? $agyConfig['latido'] : null;
    $latidoCada = max(5, (int)($agyConfig['latido_cada_seg'] ?? 30));

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

    // ── 3.b Lock de motor por host (F5, core v41) ──
    // Va DESPUÉS de las precondiciones (no tiene sentido retener el motor para
    // fallar por un sandbox inexistente) y ANTES del lanzamiento. La liberación
    // vive en el `finally` y espera a que el árbol `agy*` esté muerto — eso es
    // lo que hace que el próximo job no arranque encima de éste.
    $lockFh = _agyTomarLockMotor($agyConfig);
    if ($lockFh === null) {
        // Ocupado tras agotar los reintentos NB. Esperar bloqueando sería peor:
        // el worker retiene su slot de BD mientras espera → livelock. Se devuelve
        // un TRANSITORIO para que la política de reintento del worker lo re-encole.
        agyBorrarWorkdir($workdir);
        return _agyShapeError(
            'lock_motor_ocupado: otro proceso tiene el motor agy de este host',
            $t0Total, 'TRANSITORIO', 'lock_motor_ocupado');
    }
    try {
        $resp = _agyEjecutarUnIntento(
            $pythonBin, $scriptPath,
            $imagenPath, $promptPath, $salidaJsonPath, $sandboxDir,
            $tResp, $procTimeout,
            $homeDir, $modeloAgy,
            $cols, $rows, $grace, $agyBin, $cmdI, $cmdMode,
            $latido, $latidoCada
        );
    } finally {
        _agyLiberarLockMotor($lockFh);
    }

    $data      = $resp['data'] ?? [];
    $veredicto = $data['veredicto'] ?? null;
    // v37: la cuota es un HECHO de la corrida, no un veredicto. Desde el core
    // v37 el `.py` puede devolver `veredicto=OK` (transcripción rescatada) CON
    // `cuota_agotada=true` — el 429 llegó DESPUÉS de que el modelo terminó de
    // generar (job69869: `OK_FIN` + 21.812 chars y el 429 en el step siguiente).
    // Leer sólo el veredicto ataría los dos efectos y se perdería uno: o el
    // texto (como hasta v36) o el cooldown de la cuenta. El caller PHP tiene que
    // poder aplicar los dos. Core viejo: la clave siempre venía alineada con el
    // veredicto, así que el OR es retrocompatible.
    $cuota     = ($veredicto === 'CUOTA') || !empty($data['cuota_agotada']);

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
 * @param array   $agyConfig  (v41, opcional) Sólo se lee `lock_dir` — el lock de
 *                            motor por host, compartido con `ejecutarAgy`. Vacío
 *                            = default (%LOCALAPPDATA%). Los callers viejos no lo
 *                            pasan y siguen funcionando.
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
    ?string $workdirBase = null,
    array   $agyConfig = []
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

    // ── Lock de motor por host (F5, core v41) ──
    // El /usage abre el MISMO binario `agy` que una transcripción y ocupa el
    // motor igual: sin esto, un check podría arrancar encima de un job en vuelo
    // (o al revés). Comparte helpers y lockfile con `ejecutarAgy`.
    $lockFh = _agyTomarLockMotor($agyConfig);
    if ($lockFh === null) {
        // El worker re-encola los usage checks con retry corto; que el motivo
        // sea explícito alcanza para diagnosticarlo en el log.
        return _agyShapeUsageError(
            'lock_motor_ocupado: otro proceso tiene el motor agy de este host', $t0Total);
    }
    try {
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
    } finally {
        _agyLiberarLockMotor($lockFh);
    }

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
 *
 * v41: `$veredicto` y `$transitorioMotivo` son OPCIONALES y sólo los usa el
 * camino `lock_motor_ocupado` (F5), que no es un fallo del job sino del host:
 * sale como `veredicto='TRANSITORIO'` + `transitorio_motivo='lock_motor_ocupado'`
 * para que el worker lo re-encole por el harness que ya existe. Con los defaults
 * el shape es EXACTAMENTE el histórico (la clave `transitorio_motivo` ni
 * aparece), así que los 7 callers preflight anteriores no cambian.
 */
function _agyShapeError(
    string $errorMsg,
    float  $t0Total,
    string $veredicto = 'ERROR_PREFLIGHT',
    string $transitorioMotivo = ''
): array
{
    $extra = $transitorioMotivo !== '' ? ['transitorio_motivo' => $transitorioMotivo] : [];
    return $extra + [
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
        'veredicto' => $veredicto,
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
 *   used_percentage, plan_tier, imagen_cargada_ok (bool|null, bump v28; la
 *   SEÑAL que lo puebla cambió en v34 — ver `_resolver_imagen_cargada` del .py),
 *   citation_urls (string[], bump v30 — atribución/recitación, NO web),
 *   paste_chip_detectado / paste_reintentos / clipboard_restaurado /
 *   media_en_log (bump v34, modo `paste`).
 *
 * `fuente_response` (agregado 2026-06-21) indica de dónde salió `response`
 * para que el worker decida QA bits post-hoc:
 *   - "ini_fin"  : INICIO y FIN visibles (camino feliz prensa).
 *   - "ini_only" : INICIO sin FIN (truncado; QA_BIT_SIN_FIN).
 *   - "history"  : sin INICIO/FIN; cayó al history limpio de pyte (v2 con
 *                  prompt `[tipo:]`, o prensa con instruction-following raro).
 *   - "screen"   : fallback al viewport visible (caso muy raro).
 *   - "db"       : bump v34, sólo `cmd_mode='paste'`. El texto sale del último
 *                  step de la conversation.db, con los tags `<ilegible>`/
 *                  `<dudoso>` intactos (el TUI los destruye al renderizar). En
 *                  paste NO hay fallback a pantalla: sin texto en la .db el
 *                  veredicto es TRANSITORIO (`db_sin_texto_paste`).
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
    // Citations del checker de atribución/recitación de Google (bump v30,
    // 2026-07-24). Lista de URIs de `CitationSource` halladas en la .db de esta
    // corrida (ver `_CITATION_URI_RE` del .py). Semántica:
    //   []      → sin citations (o .db ilegible) → sin acción.
    //   [urls…] → el backend atribuyó tramos del texto generado a material
    //             indexado ⇒ recitación de memoria. **NO es acceso web** (los
    //             hosts de citation se excluyen de `web_urls` en el mismo bump).
    // El worker inyecta `QA_BIT_RECITATION` y registra `paginas_bloqueo_gemini`
    // cuando viene no-vacía. Core viejo (v≤29) no manda la clave → [] → no-op.
    // Ver notas/bloqueo_qa_recurrente.md §"Evidencia forense".
    $extras['citation_urls'] = (array_key_exists('citation_urls', $data) && is_array($data['citation_urls']))
        ? array_values(array_filter(array_map('strval', $data['citation_urls'])))
        : [];
    // LOOP degenerado de salida (bump v31, 2026-07-31). El `.py` cortó la
    // captura porque agy quedó repitiendo una unidad corta (`producing`) DESPUÉS
    // de terminar de generar — bug del CLI, no del modelo. Semántica:
    //   false → camino normal.
    //   true + veredicto=TRANSITORIO → no quedaba nada rescatable; el worker
    //     re-encola por el harness de `AgyTransitorioException`
    //     (`transitorio_motivo='loop_salida_agy'`).
    //   true + ok=true → SÍ quedaba transcripción: `response` viene ya SIN el
    //     sufijo del loop y el worker inyecta el QA grave `looping`.
    // `loop_chars_response` es cuánto se le recortó al `response` persistido;
    // `loop_chars`/`loop_repeticiones` miden el stream crudo completo (forense).
    // Core viejo (v≤30) no manda las claves → false/0 → no-op.
    // Ver notas/motor_agy.md §"Bump v31".
    $extras['loop_detectado']      = !empty($data['loop_detectado']);
    $extras['loop_unidad']         = isset($data['loop_unidad']) ? (string) $data['loop_unidad'] : '';
    $extras['loop_repeticiones']   = isset($data['loop_repeticiones']) ? (int) $data['loop_repeticiones'] : 0;
    $extras['loop_chars']          = isset($data['loop_chars']) ? (int) $data['loop_chars'] : 0;
    $extras['loop_chars_response'] = isset($data['loop_chars_response']) ? (int) $data['loop_chars_response'] : 0;
    // MODO `paste` (bump v34, 2026-08-05).
    // WORKAROUND TEMPORAL — bug upstream agy #735 (LS 1.1.10 manda `inline_data`
    // de longitud 0 cuando el modelo abre una imagen con `view_file` →
    // INVALID_ARGUMENT 400). https://github.com/google-antigravity/antigravity-cli/issues/735
    // El paste del TUI adjunta la imagen como media del mensaje y evita el
    // converter roto. Semántica de las claves (siempre presentes con default,
    // igual que el bloque de loop → core viejo (v≤33) las omite y quedan
    // false/0/null, no-op para el worker):
    //   paste_chip_detectado → el TUI mostró el chip `📎 N media attached`. Si
    //     es false Y `estado_captura === 'PASTE_SIN_CHIP'`, el mensaje NUNCA se
    //     envió (cero cuota) y el veredicto es TRANSITORIO con
    //     `transitorio_motivo='paste_sin_chip'` → re-encolable.
    //   paste_reintentos     → veces que hubo que re-pegar (0 ó 1).
    //   clipboard_restaurado → el portapapeles del usuario volvió a su texto
    //     previo (el .py lo pisa por la ventana mínima; es un recurso global).
    //   media_en_log         → `media=N` del `--log-file` de agy. `>= 1` confirma
    //     que la imagen viajó como media del mensaje; null = no verificable. Es
    //     una de las 3 señales que componen `imagen_cargada_ok` desde v34.
    // Ver notas/motor_agy.md §"Bump v34".
    $extras['paste_chip_detectado'] = !empty($data['paste_chip_detectado']);
    $extras['paste_reintentos']     = isset($data['paste_reintentos']) ? (int) $data['paste_reintentos'] : 0;
    $extras['clipboard_restaurado'] = !empty($data['clipboard_restaurado']);
    $extras['media_en_log']         = isset($data['media_en_log']) ? (int) $data['media_en_log'] : null;
    // RESCATE DE TEXTO (core v37, proyecto `agy_texto_descartado`).
    //   rescate_motivo != '' ⇒ el texto que viene en `response` NO salió por el
    //     camino normal: entró por uno que hasta v36 lo descartaba (429 posterior
    //     a la generación, timeout con el texto sólo en la .db, excepción de
    //     fase), o trae una propiedad que un humano tiene que confirmar
    //     (`parcial_sin_fin`, `thinking_mezclado`). Se combinan con `+`.
    //     El worker lo convierte en el QA grave `texto_rescatado` ⇒ la página va
    //     a revisión y el bundle forense se archiva en vez de borrarse.
    //     v48 (proyecto `bloqueo_filtro_gemini`, F4) suma la parte
    //     `bloqueo_filtro`: Gemini pegó su mensaje de rechazo al final del texto
    //     y el `.py` lo cortó en ORIGEN (el crudo entero sigue en el bundle).
    //     Esa parte NO va al `texto_rescatado` genérico: el worker de prensa la
    //     mapea a su propio bit `bloqueo_filtro`, porque la política de cola
    //     cuenta los bloqueos por motivo. Si es la única parte, `texto_rescatado`
    //     no se prende. Detalle del recorte y del catálogo de firmas (dos copias,
    //     una por repo, con contrato de equivalencia byte a byte): ver el bloque
    //     §"BLOQUEO POR EL FILTRO DE GEMINI" de `transcribir_agy.py`.
    //   bloqueo_filtro_variante / _firma / _chars: forense del recorte. `_chars`
    //     es lo que se le sacó al `response`; 0 con la variante poblada = el
    //     response era SÓLO el mensaje y NO se recortó (caso degenerado: vaciar
    //     el response rompería el invariante "OK ⇒ response no vacío").
    //   db_identidad_ok tri-estado: ¿la .db de conversación era de ESTA corrida?
    //     (cascade_id == uuid del `--log-file`). Con != true el `.py` no
    //     persiste texto NI propaga imagen_cargada/websearch/citations: viajan
    //     en neutro. Forense — el worker no decide nada con esto hoy.
    //   db_rechazo: por qué NO se aceptó el candidato de la .db ('' si se aceptó).
    // Core viejo (v≤36) no manda las claves → ''/null → no-op.
    // Ver notas/motor_agy.md §"Bump v37".
    $extras['rescate_motivo']   = isset($data['rescate_motivo']) ? (string) $data['rescate_motivo'] : '';
    $extras['db_identidad_ok']  = array_key_exists('db_identidad_ok', $data)
        ? $data['db_identidad_ok']
        : null;
    $extras['db_rechazo']       = isset($data['db_rechazo']) ? (string) $data['db_rechazo'] : '';
    $extras['db_marcas_pagina'] = isset($data['db_marcas_pagina']) ? (int) $data['db_marcas_pagina'] : 0;
    // Core viejo (v≤47) no manda estas claves → ''/0 → no-op para el worker.
    $extras['bloqueo_filtro_variante'] = isset($data['bloqueo_filtro_variante']) ? (string) $data['bloqueo_filtro_variante'] : '';
    $extras['bloqueo_filtro_firma']    = isset($data['bloqueo_filtro_firma'])    ? (string) $data['bloqueo_filtro_firma']    : '';
    $extras['bloqueo_filtro_chars']    = isset($data['bloqueo_filtro_chars'])    ? (int)    $data['bloqueo_filtro_chars']    : 0;

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
