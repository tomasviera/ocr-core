<?php
/**
 * web/includes/lib_aistudio.php
 *
 * Wrapper PHP que invoca `scripts/transcribir_aistudio.py` como subprocess para
 * transcribir UNA imagen vía Google AI Studio (web UI con Playwright + CDP).
 *
 * Análogo a `lib_gemini_cli.php::ejecutarGeminiCLI()`, con la misma shape de
 * retorno para que `correrPipeline()` pueda usarlos indistintamente según
 * `$config['motor']`.
 *
 * Diferencias clave:
 *   - No usa perfiles OAuth (la sesión vive en el Chrome del usuario, en
 *     E:\chrome-cdp-profile).
 *   - Sandbox vive en `temp/aistudio_sandbox/job<jobId>_<stem>_<ts>/`.
 *   - Token usage: el wrapper lee el tooltip del contador de AI Studio tras una
 *     respuesta aceptable y devuelve tokens_input/output/total. `output` incluye
 *     thoughts (AI Studio no los separa) → tokens_thought=0. Sin costo (flat).
 *   - Retries por modo de falla:
 *       internal_error  → 2 retries inmediatos
 *       sin_respuesta   → 1 retry con 60s backoff
 *       cuota_agotada   → 0 retries (devuelve cuota_agotada=true)
 *       degradada/dudosa → 0 retries (devuelve ok=true, deja que el orquestador
 *                                     decida vía sin_marcador / Reintento_parsing)
 *
 * Funciones expuestas:
 *   - aistudioPython(): string|null         Resuelve binario python
 *   - aistudioBorrarSandbox(string): bool
 *   - aistudioHealthCheckCDP(string): array Verifica que CDP responda
 *   - ejecutarAiStudio(...): array          Equivalente a ejecutarGeminiCLI()
 *   - aistudioStripChatInputMarker(string): string
 */

declare(strict_types=1);

/**
 * Costura de logging del core (convención + function_exists). El proyecto
 * consumidor define coreLogSink() (prensa → logDebug; v2 → logEvento). Guardado
 * por function_exists: si lib_agy.php ya lo definió al cargar primero, se omite.
 * Defensivo: un fallo de logging nunca tumba un job.
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

// =====================================================================
// HELPERS
// =====================================================================

/**
 * Resuelve el path absoluto al binario python que ejecutará el wrapper.
 * Preferir el del PATH (`where python` en Windows).
 */
function aistudioPython(): ?string
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
 * Verifica que Chrome con CDP esté escuchando. Devuelve:
 *   ['ok' => bool, 'detalle' => string, 'browser' => string|null]
 */
function aistudioHealthCheckCDP(string $cdpUrl, int $timeoutSeg = 5): array
{
    $url = rtrim($cdpUrl, '/') . '/json/version';
    $ctx = stream_context_create([
        'http' => ['method' => 'GET', 'timeout' => $timeoutSeg, 'ignore_errors' => true],
    ]);
    $raw = @file_get_contents($url, false, $ctx);
    if ($raw === false) {
        return ['ok' => false, 'detalle' => "CDP no responde en $url", 'browser' => null];
    }
    $data = @json_decode($raw, true);
    if (!is_array($data)) {
        return ['ok' => false, 'detalle' => "Respuesta CDP no es JSON: " . substr($raw, 0, 200), 'browser' => null];
    }
    $browser = $data['Browser'] ?? null;
    return ['ok' => true, 'detalle' => 'OK', 'browser' => $browser];
}

/**
 * Probe de login: verifica si el Chrome de esta cuenta tiene sesión activa en
 * AI Studio y, best-effort, con qué email. Invoca scripts/aistudio_check_login.py
 * (Playwright + CDP) como subprocess y captura su JSON por stdout (pipe en
 * memoria, NO archivos: evita el bug de la rev. 89.1 donde proc_open con
 * stdout a un archivo dentro de OneDrive fallaba con Permission denied).
 *
 * Es más caro que aistudioHealthCheckCDP (navega de verdad, ~5-10s) → NUNCA
 * llamarlo en el poll de la UI; sólo on-demand (botón), al arrancar el
 * supervisor, o lazy en el reclamo de slot bajo advisory lock.
 *
 * @param string  $cdpUrl        http://localhost:PUERTO de la cuenta.
 * @param ?string $emailEsperado google_email esperado (para comparar). null = no comparar.
 * @param int     $timeoutSeg    Margen total para el subprocess (default 45).
 * @return array{
 *   ok: bool,                  // el probe pudo correr (CDP respondió + navegó)
 *   logueado: ?bool,           // true=sesión activa, false=redirigió a login, null=indeterminado
 *   email: ?string,            // email detectado (best-effort) o null
 *   email_coincide: ?bool,     // true/false si se pudo comparar; null si no
 *   email_raw: ?string,        // crudo de donde salió el email (auditar selector)
 *   url_final: ?string,
 *   detalle: string,
 *   duracion_seg: float
 * }
 */
function aistudioCheckLoginCDP(string $cdpUrl, ?string $emailEsperado = null, int $timeoutSeg = 70): array
{
    $t0 = microtime(true);
    $shape = static function (bool $ok, ?bool $logueado, string $detalle, array $extra = []) use ($t0): array {
        return array_merge([
            'ok' => $ok, 'logueado' => $logueado, 'email' => null,
            'email_coincide' => null, 'email_raw' => null, 'url_final' => null,
            'detalle' => $detalle, 'duracion_seg' => round(microtime(true) - $t0, 2),
        ], $extra);
    };

    $pythonBin = aistudioPython();
    if ($pythonBin === null) {
        return $shape(false, null, 'python_no_encontrado: instalar Python y agregarlo al PATH');
    }
    $scriptPath = __DIR__ . DIRECTORY_SEPARATOR . 'aistudio_check_login.py';
    if (!is_file($scriptPath)) {
        return $shape(false, null, "script_no_encontrado: $scriptPath");
    }

    $cmd = [$pythonBin, '-u', $scriptPath, '--cdp', $cdpUrl, '--timeout', '30'];
    if ($emailEsperado !== null && $emailEsperado !== '') {
        $cmd[] = '--email-esperado';
        $cmd[] = $emailEsperado;
    }

    // stdout/stderr por pipe (memoria), nunca a archivo (OneDrive, rev. 89.1).
    $descriptors = [0 => ['pipe', 'r'], 1 => ['pipe', 'w'], 2 => ['pipe', 'w']];
    $env = [];
    foreach (array_merge($_SERVER, $_ENV, ['PYTHONIOENCODING' => 'utf-8']) as $k => $v) {
        if (is_string($k) && is_string($v)) $env[$k] = $v;
    }

    $proc = @proc_open($cmd, $descriptors, $pipes, __DIR__, $env);
    if (!is_resource($proc)) {
        return $shape(false, null, 'proc_open() falló al lanzar el probe de login');
    }
    fclose($pipes[0]);
    stream_set_blocking($pipes[1], false);
    stream_set_blocking($pipes[2], false);

    $stdout = ''; $stderr = '';
    $deadline = microtime(true) + $timeoutSeg;
    while (true) {
        $stdout .= stream_get_contents($pipes[1]);
        $stderr .= stream_get_contents($pipes[2]);
        $st = proc_get_status($proc);
        if (!$st['running']) break;
        if (microtime(true) > $deadline) {
            $pid = $st['pid'] ?? 0;
            if ($pid > 0 && PHP_OS_FAMILY === 'Windows') @exec("taskkill /F /T /PID {$pid} 2>nul");
            proc_terminate($proc);
            $stdout .= stream_get_contents($pipes[1]);
            $stderr .= stream_get_contents($pipes[2]);
            fclose($pipes[1]); fclose($pipes[2]); proc_close($proc);
            return $shape(false, null, "timeout del probe de login (> {$timeoutSeg}s)");
        }
        usleep(150_000);
    }
    $stdout .= stream_get_contents($pipes[1]);
    $stderr .= stream_get_contents($pipes[2]);
    fclose($pipes[1]); fclose($pipes[2]);
    proc_close($proc);

    // El script imprime el JSON a stdout. Tomar el último objeto JSON por si hay
    // ruido previo (warnings de playwright, etc.).
    $json = null;
    $ini = strpos($stdout, '{');
    $fin = strrpos($stdout, '}');
    if ($ini !== false && $fin !== false && $fin > $ini) {
        $json = @json_decode(substr($stdout, $ini, $fin - $ini + 1), true);
    }
    if (!is_array($json)) {
        return $shape(false, null, 'salida del probe no parseable: ' . substr(trim($stderr ?: $stdout), 0, 300));
    }

    return [
        'ok'             => (bool)($json['ok'] ?? false),
        'logueado'       => array_key_exists('logueado', $json) ? $json['logueado'] : null,
        'email'          => $json['email'] ?? null,
        'email_coincide' => array_key_exists('email_coincide', $json) ? $json['email_coincide'] : null,
        'email_raw'      => $json['email_raw'] ?? null,
        'url_final'      => $json['url_final'] ?? null,
        'detalle'        => (string)($json['detalle'] ?? ''),
        'duracion_seg'   => (float)($json['duracion_seg'] ?? round(microtime(true) - $t0, 2)),
    ];
}

/**
 * Elimina la línea con el marcador <<<CHAT_INPUT>>> del prompt completo.
 * Lo usa el orquestador cuando engine='cli' para pasarle al CLI el prompt
 * sin marcador (el CLI lo ignora pero queda más limpio).
 */
function aistudioStripChatInputMarker(string $prompt): string
{
    return preg_replace('/^[\s]*<<<CHAT_INPUT>>>[\s]*$\R?/m', '', $prompt) ?? $prompt;
}

/**
 * Borra un sandbox de AI Studio. Por seguridad, sólo opera si el path contiene
 * `/aistudio_sandbox/` en su realpath.
 */
function aistudioBorrarSandbox(string $dirAbs): bool
{
    if (!is_dir($dirAbs)) return true;
    $real = realpath($dirAbs);
    if ($real === false || strpos(str_replace('\\', '/', $real), '/aistudio_sandbox/') === false) {
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
 * Limpia sandboxes huérfanos > N horas.
 */
function aistudioLimpiarSandboxesHuerfanos(string $baseDir, float $maxEdadHoras = 2.0): int
{
    if (!is_dir($baseDir)) return 0;
    $cortar = time() - (int)($maxEdadHoras * 3600);
    $borrados = 0;
    foreach (new DirectoryIterator($baseDir) as $entry) {
        if ($entry->isDot() || !$entry->isDir()) continue;
        // Vacío + al menos 5 min de antigüedad: borrar (sin datos de debug, y la
        // ventana de gracia evita borrar un sandbox recién creado por otro worker
        // antes de que haya escrito sus archivos).
        // Con contenido: respetar el threshold de antigüedad normal.
        try {
            $vacio = !(new FilesystemIterator($entry->getPathname(), FilesystemIterator::SKIP_DOTS))->valid();
        } catch (Throwable $e) {
            continue; // no legible (lock OneDrive u otro): saltear
        }
        $graciaSecs = 600; // 10 min — más que suficiente para mkdir→escritura
        $suficientementeViejo = $entry->getMTime() < (time() - $graciaSecs);
        if (($vacio && $suficientementeViejo) || $entry->getMTime() < $cortar) {
            if (aistudioBorrarSandbox($entry->getPathname())) $borrados++;
        }
    }
    return $borrados;
}

// =====================================================================
// EJECUCIÓN — un intento puro (sin retries)
// =====================================================================

/**
 * Lanza el wrapper Python una sola vez y devuelve el JSON parseado + stdout/stderr
 * en bruto + datos del proceso. NO maneja retries.
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
function _aistudioEjecutarUnIntento(
    string $pythonBin,
    string $scriptPath,
    string $imagenSandbox,
    string $promptSandbox,
    string $salidaJsonPath,
    string $cdpUrl,
    string $modelo,
    int    $timeoutRespuesta,
    string $mediaResolution,
    ?string $screenshotErrorPath,
    int    $procTimeout,
    bool   $noCerrarTabError = false,
    string $thinkingLevel = ''
): array {
    $cmd = [
        $pythonBin, '-u', $scriptPath,
        '--imagen', $imagenSandbox,
        '--prompt', $promptSandbox,
        '--salida-json', $salidaJsonPath,
        '--cdp', $cdpUrl,
        '--modelo', $modelo,
        '--timeout-respuesta', (string)$timeoutRespuesta,
        '--media-resolution', $mediaResolution,
    ];
    // thinking_level vacío = no pasar el flag → el wrapper no toca el dropdown.
    if ($thinkingLevel !== '') {
        $cmd[] = '--thinking-level';
        $cmd[] = $thinkingLevel;
    }
    if ($screenshotErrorPath !== null) {
        $cmd[] = '--screenshot-error';
        $cmd[] = $screenshotErrorPath;
    }
    if ($noCerrarTabError) {
        $cmd[] = '--no-cerrar-tab-error';
    }

    $sandboxDir = dirname($salidaJsonPath);
    $stdoutFile = $sandboxDir . DIRECTORY_SEPARATOR . 'stdout.log';
    $stderrFile = $sandboxDir . DIRECTORY_SEPARATOR . 'stderr.log';
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

    $proc = @proc_open($cmd, $descriptorSpec, $pipes, $sandboxDir, $envFiltrado);
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

    // Leer JSON si lo escribió
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
// EJECUCIÓN PRINCIPAL CON RETRIES
// =====================================================================

/**
 * Análogo de `ejecutarGeminiCLI()`. Transcribe una imagen vía AI Studio web.
 *
 * @param string  $promptTexto       Prompt completo (puede contener marcador <<<CHAT_INPUT>>>)
 * @param string  $imagenPath        Ruta absoluta a la imagen
 * @param int     $jobId             ID del job (para naming del sandbox)
 * @param string  $imagenStem        Nombre base de la imagen sin extensión
 * @param string  $resultadosDir     Ruta absoluta a resultados/ (no se usa para sandbox; se mantiene por shape)
 * @param array   $aistudioConfig    settings['aistudio']: cdp_url, modelo, timeout_respuesta_seg, media_resolution, screenshot_error_dir
 * @param int     $timeout           Compatibilidad: timeout por intento (segundos)
 * @param int     $maxIntentos       NO se usa: corre 1 intento. La política de reintento vive en el worker (lib_worker_policy.php). Reservado para compat de firma.
 *
 * @return array  Shape compatible con ejecutarGeminiCLI() — ver lib_gemini_cli.php phpdoc.
 */
function ejecutarAiStudio(
    string $promptTexto,
    string $imagenPath,
    int    $jobId,
    string $imagenStem,
    string $resultadosDir,
    array  $aistudioConfig = [],
    int    $timeout = 300,
    int    $maxIntentos = 1
): array {
    $t0Total = microtime(true);
    // CORE: la lib vive en core_vendor/motores/ — NO se deriva el root del
    // proyecto de __DIR__. Se reconstruye del $resultadosDir que pasa el worker
    // (siempre <root_proyecto>/temp), así sirve a prensa (root=WEB) y a v2
    // (root=web/) sin tocar el caller. Sólo se usa para resolver sandbox/errores
    // efímeros; el script .py es sibling (__DIR__), abajo.
    $proyectoRoot = dirname(rtrim($resultadosDir, "/\\"));

    $cdpUrl    = (string)($aistudioConfig['cdp_url'] ?? 'http://localhost:9222');
    $modelo    = (string)($aistudioConfig['modelo'] ?? 'gemini-3.1-pro-preview');
    $tResp     = (int)($aistudioConfig['timeout_respuesta_seg'] ?? $timeout);
    $mediaRes  = (string)($aistudioConfig['media_resolution'] ?? 'High');
    $thinking  = (string)($aistudioConfig['thinking_level'] ?? '');
    $shotDir   = (string)($aistudioConfig['screenshot_error_dir'] ?? 'temp/aistudio_errors');
    if (!preg_match('#^([A-Za-z]:\\\\|/)#', $shotDir)) {
        $shotDir = $proyectoRoot . DIRECTORY_SEPARATOR . $shotDir;
    }
    // Debug: si está activo, no cerrar el tab de Chrome cuando el intento falla,
    // para poder inspeccionar el estado del UI (recitation block, etc.). Lo
    // setea procesarJobTranscripcionAiStudio según el flag system_flags
    // 'aistudio_debug'. En éxito el tab se cierra igual (lo decide el wrapper).
    $noCerrarTab = !empty($aistudioConfig['no_cerrar_tab_error']);

    // ── 1. Validar precondiciones ──
    $pythonBin = aistudioPython();
    if ($pythonBin === null) {
        return _aistudioShapeError('python_no_encontrado: instalar Python y agregarlo al PATH', $t0Total);
    }

    $scriptPath = __DIR__ . DIRECTORY_SEPARATOR . 'transcribir_aistudio.py';
    if (!is_file($scriptPath)) {
        return _aistudioShapeError("script_no_encontrado: $scriptPath", $t0Total);
    }

    if (!is_file($imagenPath)) {
        return _aistudioShapeError("imagen_no_existe: $imagenPath", $t0Total);
    }

    // Health check rápido del CDP (5s)
    $cdp = aistudioHealthCheckCDP($cdpUrl, 5);
    if (!$cdp['ok']) {
        return _aistudioShapeError("cdp_unreachable: {$cdp['detalle']}", $t0Total);
    }

    // ── 2. Crear sandbox efímero ──
    $sandboxBase = $proyectoRoot . DIRECTORY_SEPARATOR . 'temp' . DIRECTORY_SEPARATOR . 'aistudio_sandbox';
    aistudioLimpiarSandboxesHuerfanos($sandboxBase, 2.0);

    $ts = date('Hisv');
    $sandboxDir = $sandboxBase . DIRECTORY_SEPARATOR . "job{$jobId}_{$imagenStem}_{$ts}";
    if (!@mkdir($sandboxDir, 0755, true) && !is_dir($sandboxDir)) {
        return _aistudioShapeError("sandbox_no_se_pudo_crear: $sandboxDir", $t0Total);
    }

    $imagenSandbox  = $sandboxDir . DIRECTORY_SEPARATOR . 'image.jpg';
    $promptSandbox  = $sandboxDir . DIRECTORY_SEPARATOR . 'prompt.md';
    $salidaJsonPath = $sandboxDir . DIRECTORY_SEPARATOR . 'salida.json';

    // Resolver la imagen al sandbox como image.jpg. El formato canónico en la
    // cola del WEB es .b64 (base64 del PNG renderizado; lo escriben tanto
    // process_page.php como procesarJobRenderYEncolar). Si el path termina en
    // .b64 lo decodificamos directo al sandbox; si es una imagen real, se copia.
    // (Mismo contrato que ejecutarGeminiCLI(), que también decodifica .b64.)
    if (strtolower(substr($imagenPath, -4)) === '.b64') {
        $b64Data = @file_get_contents($imagenPath);
        if ($b64Data === false) {
            aistudioBorrarSandbox($sandboxDir);
            return _aistudioShapeError("imagen_b64_no_leible: $imagenPath", $t0Total);
        }
        // Tolerar prefijo data:URL ("data:image/png;base64,....").
        if (strpos($b64Data, ',') !== false) {
            $b64Data = substr($b64Data, strpos($b64Data, ',') + 1);
        }
        $decoded = base64_decode(trim($b64Data), true);
        if ($decoded === false || $decoded === '') {
            aistudioBorrarSandbox($sandboxDir);
            return _aistudioShapeError("imagen_b64_decode_fallo: $imagenPath", $t0Total);
        }
        if (@file_put_contents($imagenSandbox, $decoded) === false) {
            aistudioBorrarSandbox($sandboxDir);
            return _aistudioShapeError("imagen_b64_escritura_fallo: $imagenSandbox", $t0Total);
        }
    } elseif (!@copy($imagenPath, $imagenSandbox)) {
        aistudioBorrarSandbox($sandboxDir);
        return _aistudioShapeError("imagen_copia_fallo: $imagenPath → $imagenSandbox", $t0Total);
    }
    file_put_contents($promptSandbox, $promptTexto);

    @mkdir($shotDir, 0755, true);
    $screenshotErrorPath = $shotDir . DIRECTORY_SEPARATOR . "job{$jobId}_{$imagenStem}_{$ts}.png";

    $procTimeout = $tResp + 120;  // margen para arranque playwright + cleanup

    // ── 3. UN solo intento. La política de reintentos vive ENTERA en PHP a
    //    nivel worker (procesarJobTranscripcionAiStudio + lib_worker_policy.php):
    //    el wrapper YA NO reintenta por su cuenta. Antes este loop re-corría el
    //    subprocess hasta 3 veces sobre el MISMO Chrome / la MISMA cuenta ante
    //    INTERNAL_ERROR/RESPUESTA_CHROME/etc., lo que pisaba la regla "una misma
    //    cuenta no toma el mismo trabajo dos veces" (el worker recién veía el
    //    fallo tras 3 corridas iguales). Ahora corremos una vez y devolvemos el
    //    veredicto + metadata; el worker decide guardar / cuota / bloqueo /
    //    reintentar (cross-cuenta) / error. Ver BITACORA 2026-06-03.
    $intentos = 1;
    coreLog('aistudio_web', 'INFO',
        "Enviando imagen al modelo (AI Studio web). Espera ~60–90s mientras carga UI, sube imagen y el modelo responde.",
        ['imagen' => basename($imagenPath), 'modelo' => $modelo, 'intento' => $intentos]);

    $resp = _aistudioEjecutarUnIntento(
        $pythonBin, $scriptPath, $imagenSandbox, $promptSandbox, $salidaJsonPath,
        $cdpUrl, $modelo, $tResp, $mediaRes, $screenshotErrorPath, $procTimeout,
        $noCerrarTab, $thinking
    );

    $data      = $resp['data'] ?? [];
    $veredicto = $data['veredicto'] ?? null;

    // Conservar el sandbox (stderr.log, screenshot) para inspección en TODOS los
    // casos salvo el éxito limpio (OK_SANA/DUDOSA): así un REVISAR, un DEGRADADA o
    // cualquier transitorio/error deja rastro para debug. CUOTA además marca
    // cuotaAgotada para que el worker active el cooldown de la cuenta.
    $cuota     = ($veredicto === 'CUOTA');
    $conservar = !in_array($veredicto, ['OK_SANA', 'DUDOSA'], true);

    return _aistudioShapeRespuesta($resp, $sandboxDir, $intentos, $t0Total,
        conservarSandbox: $conservar, cuotaAgotada: $cuota, erroresIntentos: []);
}

// =====================================================================
// HELPERS DE SHAPE DE RETORNO
// =====================================================================

/**
 * Devuelve el shape de error preflight (sin sandbox).
 */
function _aistudioShapeError(string $errorMsg, float $t0Total): array
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
        'engine' => 'aistudio_web',
        'veredicto' => 'ERROR_PREFLIGHT',
    ];
}

/**
 * Mapea el resultado de un intento al shape compatible con `ejecutarGeminiCLI()`.
 */
function _aistudioShapeRespuesta(
    array $intento,
    string $sandboxDir,
    int $intentos,
    float $t0Total,
    bool $conservarSandbox,
    bool $cuotaAgotada,
    array $erroresIntentos = []
): array {
    $data = $intento['data'] ?? [];
    $ok = !empty($data['ok']);
    $sandboxPath = $sandboxDir;
    if (!$conservarSandbox && is_dir($sandboxDir)) {
        if (aistudioBorrarSandbox($sandboxDir)) {
            $sandboxPath = null;
        }
    }

    $extras = [];
    if (!empty($erroresIntentos)) $extras['errores_intentos'] = $erroresIntentos;
    if (isset($data['heuristicas'])) $extras['heuristicas'] = $data['heuristicas'];
    if (isset($data['fuente_dom'])) $extras['fuente_dom'] = $data['fuente_dom'];
    if (isset($data['veredicto'])) $extras['veredicto'] = $data['veredicto'];
    if (isset($data['error_sospechado'])) $extras['error_sospechado'] = $data['error_sospechado'];
    if (isset($data['modelo_verificado'])) $extras['modelo_verificado'] = $data['modelo_verificado'];
    if (isset($data['media_resolution'])) $extras['media_resolution'] = $data['media_resolution'];
    // thinking_level: nivel EFECTIVAMENTE aplicado y verificado por el wrapper
    // (Medium/High/...); null si no se pudo verificar (quedó el default de cuenta).
    // array_key_exists (no isset) para propagar también el null explícito.
    if (array_key_exists('thinking_level', $data)) $extras['thinking_level'] = $data['thinking_level'];
    if (isset($data['modo_prompt'])) $extras['modo_prompt'] = $data['modo_prompt'];
    if (!empty($data['incompleto'])) $extras['incompleto'] = true;
    if (!empty($data['longitud_sospechosa'])) $extras['longitud_sospechosa'] = true;

    // Token usage del tooltip de AI Studio (leído por el wrapper Python tras una
    // respuesta aceptable). `output` INCLUYE los thought tokens: AI Studio no los
    // separa, por eso tokens_thought queda en 0 (no comparable 1:1 con la ruta
    // Google API, donde output excluye thoughts). Null/ausente → 0.
    $tokIn  = isset($data['tokens_input'])  ? (int)$data['tokens_input']  : 0;
    $tokOut = isset($data['tokens_output']) ? (int)$data['tokens_output'] : 0;
    $tokTot = isset($data['tokens_total'])  ? (int)$data['tokens_total']  : 0;

    return array_merge([
        'ok' => $ok,
        'response' => (string)($data['response'] ?? ''),
        'error' => $ok ? null : (string)($data['error'] ?? 'falla_sin_detalle'),
        'stats' => [],   // AI Studio web no expone stats granulares por API
        'tools' => null,
        'tokens_input' => $tokIn, 'tokens_output' => $tokOut, 'tokens_thought' => 0,
        'tokens_cached' => 0, 'tokens_total' => $tokTot,
        'session_id' => null,
        'stdout_raw' => (string)($intento['stdout'] ?? ''),
        'stderr_raw' => (string)($intento['stderr'] ?? ''),
        'intentos' => $intentos,
        'exit_code' => $intento['exit_code'] ?? null,
        'duracion_seg' => round(microtime(true) - $t0Total, 2),
        'sandbox_path' => $sandboxPath,
        'cuota_agotada' => $cuotaAgotada,
        'engine' => 'aistudio_web',
    ], $extras);
}
