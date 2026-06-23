<?php
/**
 * ocr-core/motores/lib_http_caller.php   (CORE — semilla: prensadelplata/WEB)
 *
 * Transporte HTTP puro de los proveedores API (Google Gemini vía REST, Alibaba
 * Qwen/DashScope, Anthropic Claude): construcción de payload/URL/headers,
 * ejecución cURL, parseo de respuesta, cálculo de costo, y las rutas Batch
 * (Async Batch API de Gemini + Batch API de Qwen).
 *
 * Motor agnóstico al dominio (regla dura del core): cero SQL, no toca BD, no
 * lee config/secrets del proyecto. Sólo stdlib (curl_*, CURLFile, json_*,
 * tempnam). Se puede require standalone (no necesita coreLog ni lib_bloqueo_*).
 *
 * El consumidor (prensa) orquesta estas puras y se queda con TODO el dominio:
 * routing (detectarProveedor), interpretación de estado (lib_api_estados),
 * persistencia (api_calls / entradas+backup / trabajos_api) y la memoria de
 * bloqueo Gemini. El core sólo reporta señales; no decide estado ni persiste.
 *
 * COSTURA provider-neutral: parsearRespuestaOCR sólo MARCA en el struct
 * 'bloqueo_determinista' + 'finish_reason' (hoy lo setea sólo la rama Gemini
 * ante RECITATION/SAFETY/etc. con HTTP 200 sin texto usable). NO llama a la
 * memoria de bloqueo — marcar/limpiar es dominio del consumidor
 * (bloqueoGeminiMarcar/Limpiar en prensa). Sumar un proveedor bloqueante =
 * extender SU rama de parse acá; el worker ya keya sobre la señal neutral.
 *
 * Contrato bit-a-bit: estas funciones se movieron VERBATIM desde
 * prensadelplata/WEB : includes/lib_api_caller.php (split http, core v7). Los
 * switch ($provider) operation-oriented quedan tal cual — NO se reorganizó a
 * provider-oriented en este paso. Ver planificacion/PLAN_SPLIT_HTTP.md (1a/2a).
 *
 * Funciones expuestas:
 *   Sync   : obtenerApiKey, construirPayloadOCR, construirUrlOCR,
 *            construirHeadersOCR, ejecutarCurl, parsearRespuestaOCR,
 *            extraerMensajeError, calcularCostoOCR
 *   Batch G: construirPayloadBatchGemini, enviarBatchGemini, pollBatchGemini,
 *            listarBatchesGemini
 *   Batch Q: construirJSONLBatchQwen, uploadFileQwen, uploadImageQwen,
 *            enviarBatchQwen, pollBatchQwen, descargarResultadosQwen,
 *            listarBatchesQwen
 *
 * NO declara strict_types (igual que el archivo de origen): preserva el modo
 * coercitivo de los call sites históricos.
 */

/**
 * Obtiene la API key del proveedor desde el array de secrets.
 */
function obtenerApiKey(array $secrets, string $provider): string
{
    $map = [
        'gemini'      => $secrets['gemini_api_key']      ?? '',
        'gemini_test' => $secrets['gemini_api_key_test'] ?? '',
        'alibaba'     => $secrets['alibaba_api_key']     ?? '',
        'claude'      => $secrets['claude_api_key']      ?? '',
    ];
    return $map[$provider] ?? $map['gemini'];
}

// ============================================================
// CONSTRUCCIÓN DE PAYLOAD (OCR / Transcripción)
// ============================================================

/**
 * Construye el payload JSON para la llamada a la API de OCR/transcripción.
 * Cada proveedor tiene una estructura diferente.
 *
 * @param string $provider   'gemini' | 'alibaba' | 'claude' | 'gemini_test'
 * @param string $modelo     Nombre del modelo (ej: 'gemini-2.5-pro', 'qwen-vl-max')
 * @param string $image_b64  Imagen en base64 (sin prefijo data:)
 * @param string $mimeType   'image/jpeg' | 'image/png'
 * @param string $prompt     Texto del prompt
 */
function construirPayloadOCR(string $provider, string $modelo, string $image_b64, string $mimeType, string $prompt, ?string $thinkingLevel = null): array
{
    if ($provider === 'alibaba') {
        $payload = [
            "model"         => $modelo,
            "enable_search" => false,
            "messages"      => [[
                "role"    => "user",
                "content" => [
                    [
                        "type"      => "image_url",
                        "image_url" => [
                            "url"    => "data:{$mimeType};base64,{$image_b64}"
                        ]
                    ],
                    ["type" => "text", "text" => $prompt]
                ]
            ]]
        ];
        if (!empty($thinkingLevel)) {
            if ($thinkingLevel === 'enabled' || $thinkingLevel === 'true') {
                $payload['enable_thinking'] = true;
            } elseif ($thinkingLevel === 'disabled' || $thinkingLevel === 'false') {
                $payload['enable_thinking'] = false;
            } elseif (is_numeric($thinkingLevel)) {
                // Presupuesto numérico opcional (algunos Qwen lo toman como reasoning_budget o max_tokens, 
                // pero la forma estándar en qwen-max es con enable_thinking). Enviamos true y un max_tokens equivalente provisorio (no daña) 
                $payload['enable_thinking'] = true;
            }
        }
        return $payload;
    }

    if ($provider === 'claude') {
        return [
            "model"      => $modelo,
            "max_tokens" => 8192,
            "messages"   => [[
                "role"    => "user",
                "content" => [
                    [
                        "type"   => "image",
                        "source" => [
                            "type"       => "base64",
                            "media_type" => $mimeType,
                            "data"       => $image_b64
                        ]
                    ],
                    ["type" => "text", "text" => $prompt]
                ]
            ]]
        ];
    }

    // Gemini (default)
    $isGemini3      = (stripos($modelo, 'gemini-3') !== false);
    $imageBase64Part = ["inlineData" => ["mimeType" => $mimeType, "data" => $image_b64]];
    if ($isGemini3) {
        $imageBase64Part["mediaResolution"] = ["level" => "MEDIA_RESOLUTION_ULTRA_HIGH"];
    }

    if ($isGemini3) {
        $imageBase64Part["mediaResolution"] = ["level" => "MEDIA_RESOLUTION_ULTRA_HIGH"];
    }
    
    $payload = [
        "contents" => [[
            "parts" => [
                ["text" => $prompt],
                $imageBase64Part
            ]
        ]]
    ];

    $genConfig = [];
    if (!$isGemini3) {
        $genConfig["mediaResolution"] = "MEDIA_RESOLUTION_HIGH";
    }

    if (!empty($thinkingLevel)) {
        if ($isGemini3) {
            $genConfig["thinkingConfig"] = ["thinkingLevel" => $thinkingLevel];
        } else {
            if ($thinkingLevel === 'disabled' || $thinkingLevel === 'false' || $thinkingLevel === '0') {
                $genConfig["thinkingConfig"] = ["thinkingBudget" => 0];
            } elseif (is_numeric($thinkingLevel) && $thinkingLevel > 0) {
                $genConfig["thinkingConfig"] = ["thinkingBudget" => (int)$thinkingLevel];
            }
        }
    }

    if (!empty($genConfig)) {
        $payload["generationConfig"] = $genConfig;
    }

    return $payload;
}

/**
 * Construye la URL del endpoint para la llamada de OCR.
 */
function construirUrlOCR(string $provider, string $modelo, string $endpoint, string $apiKey): string
{
    if ($provider === 'alibaba') {
        return $endpoint ?: "https://dashscope-intl.aliyuncs.com/compatible-mode/v1/chat/completions";
    }
    if ($provider === 'claude') {
        return $endpoint ?: "https://api.anthropic.com/v1/messages";
    }
    // Gemini
    $url = $endpoint ?: "https://generativelanguage.googleapis.com/v1beta/models/{$modelo}:generateContent";
    // Interpolar placeholder {modelo} si vino del catálogo del selector
    // (lib_modelos_catalogo.php / js/modelo_selector.js) — el endpoint
    // canónico se guarda con `{modelo}` literal para reutilizarlo entre
    // modelos del mismo proveedor. Si no hay placeholder no cambia nada.
    $url = str_replace('{modelo}', $modelo, $url);
    $url .= (parse_url($url, PHP_URL_QUERY) ? '&' : '?') . "key={$apiKey}";
    return $url;
}

/**
 * Construye los headers HTTP para la llamada de OCR.
 */
function construirHeadersOCR(string $provider, string $apiKey): array
{
    if ($provider === 'alibaba') {
        return [
            'Content-Type: application/json',
            'Authorization: Bearer ' . $apiKey
        ];
    }
    if ($provider === 'claude') {
        return [
            'Content-Type: application/json',
            'x-api-key: ' . $apiKey,
            'anthropic-version: 2023-06-01'
        ];
    }
    // Gemini: la key va en la URL, no en headers
    return ['Content-Type: application/json'];
}

// ============================================================
// EJECUCIÓN CURL
// ============================================================

/**
 * Ejecuta una petición POST con cURL.
 *
 * @return array ['http_code', 'response', 'curl_error', 'curl_errno', 'duration']
 */
function ejecutarCurl(string $url, array $payload, array $headers, int $timeout = 300): array
{
    $payloadJson = json_encode($payload);
    $startTime   = microtime(true);

    $ch = curl_init($url);
    curl_setopt_array($ch, [
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_POST           => true,
        CURLOPT_POSTFIELDS     => $payloadJson,
        CURLOPT_HTTPHEADER     => $headers,
        CURLOPT_TIMEOUT        => $timeout,
        CURLOPT_SSL_VERIFYPEER => false,
        CURLOPT_SSL_VERIFYHOST => false,
    ]);

    $response  = curl_exec($ch);
    $httpCode  = curl_getinfo($ch, CURLINFO_HTTP_CODE);
    $curlError = curl_error($ch);
    $curlErrNo = curl_errno($ch);
    curl_close($ch);

    return [
        'http_code'  => $httpCode,
        'response'   => $response ?: '',
        'curl_error' => $curlError,
        'curl_errno' => $curlErrNo,
        'duration'   => round(microtime(true) - $startTime, 3),
    ];
}

// ============================================================
// PARSEO DE RESPUESTA (OCR)
// ============================================================

/**
 * Parsea la respuesta JSON de la API de OCR y extrae el texto + metadata de uso.
 *
 * @return array [
 *   'texto'          => string|null,
 *   'tokens_input'   => int,
 *   'tokens_output'  => int,
 *   'tokens_thought' => int,
 *   'tokens_total'   => int,
 *   'raw_response'   => string,
 *   'error_msg'      => string|null,   // null si OK
 * ]
 */
function parsearRespuestaOCR(string $provider, string $modelo, string $response): array
{
    $data = json_decode($response, true);

    $result = [
        'texto'          => null,
        'tokens_input'   => 0,
        'tokens_output'  => 0,
        'tokens_thought' => 0,
        'tokens_total'   => 0,
        'raw_response'   => $response,
        'error_msg'      => null,
    ];

    if ($provider === 'alibaba') {
        $rawContent = $data['choices'][0]['message']['content']
            ?? $data['output']['choices'][0]['message']['content']
            ?? $data['output']['text']
            ?? null;

        if (is_array($rawContent)) {
            $concat = '';
            foreach ($rawContent as $part) {
                if (is_array($part) && isset($part['text'])) $concat .= $part['text'];
            }
            $result['texto'] = $concat ?: null;
        } else {
            $result['texto'] = $rawContent;
        }

        $usage = $data['usage'] ?? [];
        $result['tokens_input']   = $usage['prompt_tokens']    ?? $usage['input_tokens']  ?? 0;
        $result['tokens_thought'] = $usage['completion_tokens_details']['reasoning_tokens'] ?? $usage['reasoning_tokens'] ?? 0;
        $result['tokens_output']  = $usage['completion_tokens'] ?? $usage['output_tokens'] ?? 0;
        $result['tokens_total']   = $usage['total_tokens'] ?? 0;

    } elseif ($provider === 'claude') {
        $result['texto']          = $data['content'][0]['text'] ?? null;
        $usage = $data['usage'] ?? [];
        $result['tokens_input']   = $usage['input_tokens']  ?? 0;
        $result['tokens_output']  = $usage['output_tokens'] ?? 0;
        $result['tokens_total']   = $result['tokens_input'] + $result['tokens_output'];

    } else {
        // Gemini
        $cand = $data['candidates'][0] ?? [];
        $result['texto']          = $cand['content']['parts'][0]['text'] ?? null;
        $usage = $data['usageMetadata'] ?? [];
        $result['tokens_input']   = $usage['promptTokenCount']      ?? 0;
        $result['tokens_thought'] = $usage['thoughtTokenCount']     ?? $usage['thoughtsTokenCount'] ?? 0;
        $result['tokens_output']  = $usage['candidatesTokenCount']  ?? 0;
        $result['tokens_total']   = $usage['totalTokenCount']       ?? 0;

        // finishReason / promptFeedback: detectar bloqueos del modelo. La API
        // los devuelve como HTTP 200 + finishReason (no como error HTTP), así
        // que sin esto el job sólo veía "texto vacío" genérico. STOP y
        // MAX_TOKENS son terminaciones normales (MAX_TOKENS puede traer texto
        // truncado y se acepta como hasta ahora). El resto bloquea la salida:
        // el candidate viene sin texto usable. El más común con prensa antigua
        // es RECITATION (el clasificador cree que el modelo recita material de
        // entrenamiento). Se descarta cualquier parcial (fidelidad) y se
        // devuelve un error claro y distinto para el log y el estado del job.
        $finishReason = $cand['finishReason'] ?? null;
        $blockReason  = $data['promptFeedback']['blockReason'] ?? null;
        $razonesBloqueo = ['RECITATION', 'SAFETY', 'PROHIBITED_CONTENT', 'BLOCKLIST', 'SPII', 'OTHER'];
        if ($blockReason) {
            $result['texto']     = null;
            $result['error_msg'] = "Gemini bloqueó el prompt de entrada (promptFeedback.blockReason={$blockReason}).";
            // Marca para la memoria de bloqueo Gemini: determinístico, compartido
            // por toda la familia (worker.php no re-despacha la página a Gemini).
            $result['bloqueo_determinista'] = true;
            $result['finish_reason']        = $blockReason;
        } elseif ($finishReason !== null && in_array($finishReason, $razonesBloqueo, true)) {
            $msg = "Gemini bloqueó la respuesta (finishReason={$finishReason}; sin texto usable).";
            if ($finishReason === 'RECITATION') {
                $msg .= " Clasificador de recitación: frecuente y casi determinístico con dominio público.";
            }
            $result['texto']     = null;
            $result['error_msg'] = $msg;
            $result['bloqueo_determinista'] = true;
            $result['finish_reason']        = $finishReason;
        }
    }

    return $result;
}

/**
 * Extrae el mensaje de error estructurado de una respuesta de API fallida.
 */
function extraerMensajeError(string $provider, string $response, int $httpCode, string $curlError): string
{
    $data = json_decode($response, true);
    if ($data) {
        if ($provider === 'alibaba' && isset($data['code'])) {
            return "Error Alibaba [{$data['code']}]: " . ($data['message'] ?? 'Sin detalle');
        }
        if (in_array($provider, ['gemini', 'gemini_test']) && isset($data['error'])) {
            $err = $data['error'];
            return "Error Gemini [{$err['status']}]: " . ($err['message'] ?? 'Sin detalle');
        }
        if ($provider === 'claude' && isset($data['error'])) {
            $err = $data['error'];
            return "Error Claude [{$err['type']}]: " . ($err['message'] ?? 'Sin detalle');
        }
    }
    return "HTTP {$httpCode}" . ($curlError ? " / CurlErr: {$curlError}" : '');
}

// ============================================================
// CÁLCULO DE COSTOS
// ============================================================

/**
 * Calcula el costo estimado en USD de una llamada de OCR.
 * Extensible: para añadir un nuevo modelo, agregar el caso correspondiente.
 */
function calcularCostoOCR(string $provider, string $modelo, int $tokensIn, int $tokensOut, int $tokensThought = 0, bool $isBatch = false): float
{
    if ($provider === 'alibaba') {
        if (stripos($modelo, 'qwen-vl') !== false || stripos($modelo, 'qwen3-vl') !== false) {
            $price = ['in' => 0.00016, 'out' => 0.00064]; // per 1k tokens
        } elseif (stripos($modelo, 'qwen2.5-max') !== false || stripos($modelo, 'plus') !== false) {
            $price = ['in' => 0.0004, 'out' => 0.0024];
        } else {
            $price = ['in' => 0.0004, 'out' => 0.0016]; // fallback qwen3-vl-235b
        }
        return (($tokensIn / 1000) * $price['in']) + (($tokensOut / 1000) * $price['out']);
    }

    if ($provider === 'claude') {
        $price = (stripos($modelo, 'opus') !== false)
            ? ['in' => 0.015, 'out' => 0.075]
            : ['in' => 0.003, 'out' => 0.015];
        return (($tokensIn / 1000) * $price['in']) + (($tokensOut / 1000) * $price['out']);
    }

    // Gemini (default)
    if (stripos($modelo, 'gemini-1.5-flash') !== false) {
        $price = ['in' => 0.000075, 'out' => 0.00030];
        if ($tokensIn > 128000) $price['in'] = 0.00015;
    } elseif (stripos($modelo, 'gemini-1.5-pro') !== false || stripos($modelo, 'gemini-2.5-pro') !== false) {
        $price = ['in' => 0.00125, 'out' => 0.005];
        if ($tokensIn > 128000) $price['in'] = 0.0025;
    } elseif (stripos($modelo, 'gemini-2.5-flash') !== false) {
        $price = ['in' => 0.0003, 'out' => 0.0025];           // $0.30/$2.50 por 1M
    } elseif (stripos($modelo, 'gemini-3.1-pro') !== false || stripos($modelo, 'gemini-3-pro') !== false) {
        $price = ['in' => 0.002, 'out' => 0.012];
        if ($tokensIn > 200000) $price['in'] = 0.004;
    } elseif (stripos($modelo, 'gemini-3.5-flash') !== false) {
        $price = ['in' => 0.0015, 'out' => 0.009];            // $1.50/$9.00 por 1M
    } elseif (stripos($modelo, 'gemini-3-flash') !== false || stripos($modelo, 'gemini-3.1-flash') !== false) {
        $price = ['in' => 0.0005, 'out' => 0.003];            // $0.50/$3.00 por 1M
    } else {
        $price = ['in' => 0.00125, 'out' => 0.005]; // fallback
    }

    $costo = (($tokensIn / 1000) * $price['in']) + ((($tokensOut + $tokensThought) / 1000) * $price['out']);
    
    // Todos los proveedores principales tienen la Batch API a mitad de precio actual
    if ($isBatch) {
        $costo *= 0.5;
    }
    
    return $costo;
}

// ============================================================
// BATCH API — GOOGLE GEMINI
// ============================================================

/**
 * Construye el payload para un batch de Gemini con múltiples páginas de una edición.
 *
 * @param array $paginas  Array de ['prompt'=>string, 'image_b64'=>string, 'mime_type'=>string, 'modelo'=>string]
 * @param array $genConfig Configuración extra de generación (opcional)
 * @return array Array de InlinedRequest para batchGenerateContent
 */
function construirPayloadBatchGemini(array $paginas, array $genConfig = [], ?string $thinkingLevel = null): array
{
    $requests = [];
    foreach ($paginas as $pag) {
        $modeloPag = $pag['modelo'] ?? 'gemini-2.5-flash';
        $isGemini3 = (stripos($modeloPag, 'gemini-3') !== false);
        $imagePart = ['inlineData' => ['mimeType' => $pag['mime_type'], 'data' => $pag['image_b64']]];
        if ($isGemini3) {
            $imagePart['mediaResolution'] = ['level' => 'MEDIA_RESOLUTION_ULTRA_HIGH'];
        }
        $request = [
            'model'    => "models/{$modeloPag}",   // Requerido por inlinedRequest en batchGenerateContent
            'contents' => [[
                'parts' => [
                    ['text' => $pag['prompt']],
                    $imagePart,
                ]
            ]]
        ];
        $baseConfig = $isGemini3 ? [] : ['mediaResolution' => 'MEDIA_RESOLUTION_HIGH'];
        $mergedConfig = array_merge($baseConfig, $genConfig);
        
        if (!empty($thinkingLevel)) {
            if ($isGemini3) {
                $mergedConfig["thinkingConfig"] = ["thinkingLevel" => $thinkingLevel];
            } else {
                if ($thinkingLevel === 'disabled' || $thinkingLevel === 'false' || $thinkingLevel === '0') {
                    $mergedConfig["thinkingConfig"] = ["thinkingBudget" => 0];
                } elseif (is_numeric($thinkingLevel) && $thinkingLevel > 0) {
                    $mergedConfig["thinkingConfig"] = ["thinkingBudget" => (int)$thinkingLevel];
                }
            }
        }

        if (!empty($mergedConfig)) {
            $request['generationConfig'] = $mergedConfig;
        }

        // Formato InlinedRequest de batchGenerateContent: { request: GenerateContentRequest }
        $requests[] = ['request' => $request];
    }
    return $requests;
}

/**
 * Envía un batch a la Gemini Async Batch API.
 *
 * Endpoint correcto: POST /v1beta/batches (NO /models/{model}:batchGenerateContent)
 * Referencia: https://ai.google.dev/api/generate-content#method:-batches.create
 *
 * @param string $apiKey
 * @param string $modelo      Nombre del modelo (sin "models/")
 * @param array  $requests    Resultado de construirPayloadBatchGemini()
 * @param string $displayName Nombre identificador del batch (ej: "edi_123_scraper")
 * @return array ['success'=>bool, 'batch_name'=>string|null, 'error'=>string|null, 'http_code'=>int]
 */
function enviarBatchGemini(string $apiKey, string $modelo, array $requests, string $displayName = ''): array
{
    // batchGenerateContent ES el endpoint de Async Batch API para generativelanguage.googleapis.com
    // (distinto del /v1beta/batches de Vertex AI que requiere otra autenticación)
    $url = "https://generativelanguage.googleapis.com/v1beta/models/{$modelo}:batchGenerateContent"
         . "?key={$apiKey}";

    // Body: wrapper 'batch' con snake_case (NO camelCase). El campo 'requests' contiene
    // el objeto InlinedRequests con su array de requests.
    $body = [
        'batch' => [
            'display_name' => $displayName,
            'input_config' => [
                'requests' => [
                    'requests' => $requests   // array de InlinedRequest: [{ request: GenerateContentRequest }]
                ]
            ]
        ]
    ];

    $json = json_encode($body, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);

    $ch = curl_init($url);
    curl_setopt_array($ch, [
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_POST           => true,
        CURLOPT_POSTFIELDS     => $json,
        CURLOPT_HTTPHEADER     => ['Content-Type: application/json'],
        CURLOPT_TIMEOUT        => 60,
        CURLOPT_SSL_VERIFYPEER => false,
    ]);
    $response = curl_exec($ch);
    $httpCode = curl_getinfo($ch, CURLINFO_HTTP_CODE);
    $curlErr  = curl_error($ch);
    curl_close($ch);

    if ($curlErr) {
        return ['success' => false, 'batch_name' => null, 'error' => "cURL: {$curlErr}", 'http_code' => 0];
    }

    $data = json_decode($response, true);

    if ($httpCode !== 200 || empty($data['name'])) {
        // Incluir el body completo para debug (errores 4xx de Google siempre traen JSON con detalle)
        $errDetail = $data['error']['message'] ?? null;
        $errMsg = $errDetail
            ? "HTTP {$httpCode}: {$errDetail}"
            : "HTTP {$httpCode}: " . substr($response, 0, 500);
        return ['success' => false, 'batch_name' => null, 'error' => $errMsg, 'http_code' => $httpCode, 'raw_response' => $response];
    }

    return [
        'success'    => true,
        'batch_name' => $data['name'],  // ej: "batches/abc123"
        'state'      => $data['state'] ?? '',
        'expire_time'=> $data['expireTime'] ?? '',
        'http_code'  => $httpCode,
        'error'      => null,
    ];
}

/**
 * Consulta el estado de un batch de Gemini.
 *
 * @return array ['done'=>bool, 'state'=>string, 'responses'=>array|null, 'error'=>string|null]
 */
function pollBatchGemini(string $apiKey, string $batchName): array
{
    $url = "https://generativelanguage.googleapis.com/v1beta/{$batchName}?key={$apiKey}";

    $ch = curl_init($url);
    curl_setopt_array($ch, [
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_TIMEOUT        => 30,
        CURLOPT_SSL_VERIFYPEER => false,
    ]);
    $response = curl_exec($ch);
    $httpCode = curl_getinfo($ch, CURLINFO_HTTP_CODE);
    $curlErr  = curl_error($ch);
    curl_close($ch);

    if ($curlErr) {
        return ['done' => false, 'state' => 'ERROR', 'responses' => null, 'error' => "cURL: {$curlErr}", 'raw_response' => null];
    }
    if ($httpCode !== 200) {
        return ['done' => false, 'state' => 'ERROR', 'responses' => null, 'error' => "HTTP {$httpCode}", 'raw_response' => $response];
    }

    $data  = json_decode($response, true);
    $state = $data['state'] ?? 'UNKNOWN';
    $done  = in_array($state, ['JOB_STATE_SUCCEEDED', 'SUCCEEDED', 'COMPLETE']);
    $failed = in_array($state, ['JOB_STATE_FAILED', 'FAILED', 'JOB_STATE_CANCELLED']);

    if ($failed) {
        $errorMsg = "Batch falló con estado: {$state}";
        if (!empty($data['error']['message'])) {
            $errorMsg .= " - Detalle: " . $data['error']['message'];
        }
        return ['done' => false, 'state' => $state, 'responses' => null, 'error' => $errorMsg, 'is_fatal' => true, 'raw_response' => $response];
    }

    // Extraer responses si están disponibles
    // Formato de respuesta de la Async Batch API: response.inlinedResponses.responses[]
    $responses = null;
    if ($done) {
        $responses = $data['response']['inlinedResponses']['responses']
                  ?? $data['inlinedResponses']['responses']
                  ?? [];
    }

    $meta = $data['metadata'] ?? [];
    return [
        'done'      => $done,
        'state'     => $state,
        'responses' => $responses,
        'completed_count' => $meta['completedRequestCount'] ?? 0,
        'total_count'     => $meta['totalRequestCount']     ?? 0,
        'error'     => null,
        'raw'       => $data,
    ];
}

/**
 * Lista los batches activos en la cuenta Gemini.
 * Usado para el mecanismo de rescate anti-duplicación.
 *
 * @return array Lista de batches con 'name', 'state', 'display_name', 'create_time'
 */
function listarBatchesGemini(string $apiKey): array
{
    $url = "https://generativelanguage.googleapis.com/v1beta/batches?key={$apiKey}";

    $ch = curl_init($url);
    curl_setopt_array($ch, [
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_TIMEOUT        => 30,
        CURLOPT_SSL_VERIFYPEER => false,
    ]);
    $response = curl_exec($ch);
    $httpCode = curl_getinfo($ch, CURLINFO_HTTP_CODE);
    curl_close($ch);

    if ($httpCode !== 200) return [];
    $data = json_decode($response, true);
    return $data['batches'] ?? [];
}

// ============================================================
// BATCH API — ALIBABA QWEN
// ============================================================

/**
 * Construye el contenido JSONL para un batch de Qwen.
 * Cada página se convierte en una línea JSON con custom_id.
 *
 * @param array  $paginas Array de ['custom_id'=>string, 'modelo'=>string, 'prompt'=>string, 'image_b64'=>string, 'mime_type'=>string]
 * @return string  JSONL (una línea por página)
 */
function construirJSONLBatchQwen(array $paginas, ?string $thinkingLevel = null): string
{
    $lines = [];
    foreach ($paginas as $pag) {
        $imageUrl = !empty($pag['image_url']) 
            ? $pag['image_url'] 
            : "data:{$pag['mime_type']};base64,{$pag['image_b64']}";

        $line = [
            'custom_id' => $pag['custom_id'],
            'method'    => 'POST',
            'url'       => '/v1/chat/completions',
            'body'      => [
                'model'    => $pag['modelo'],
                'messages' => [[
                    'role'    => 'user',
                    'content' => [
                        [
                            'type'      => 'image_url',
                            'image_url' => [
                                'url'    => $imageUrl
                            ]
                        ],
                        ['type' => 'text', 'text' => $pag['prompt']],
                    ]
                ]],
            ]
        ];
        if (!empty($thinkingLevel)) {
            if ($thinkingLevel === 'enabled' || $thinkingLevel === 'true' || is_numeric($thinkingLevel)) {
                $line['body']['enable_thinking'] = true;
            } elseif ($thinkingLevel === 'disabled' || $thinkingLevel === 'false') {
                $line['body']['enable_thinking'] = false;
            }
        }
        $lines[] = json_encode($line, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
    }
    return implode("\n", $lines);
}

/**
 * Sube el JSONL como archivo a la API de Qwen Files.
 *
 * @return array ['success'=>bool, 'file_id'=>string|null, 'error'=>string|null]
 */
function uploadFileQwen(string $apiKey, string $jsonlContent, string $filename = 'batch.jsonl'): array
{
    $url = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1/files";

    // Crear un archivo temporal para el multipart upload
    $tmpFile = tempnam(sys_get_temp_dir(), 'qwen_batch_') . '.jsonl';
    file_put_contents($tmpFile, $jsonlContent);

    $cfile = new CURLFile($tmpFile, 'application/jsonl', $filename);

    $ch = curl_init($url);
    curl_setopt_array($ch, [
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_POST           => true,
        CURLOPT_POSTFIELDS     => ['file' => $cfile, 'purpose' => 'batch'],
        CURLOPT_HTTPHEADER     => ["Authorization: Bearer {$apiKey}"],
        CURLOPT_TIMEOUT        => 120,
        CURLOPT_SSL_VERIFYPEER => false,
    ]);
    $response = curl_exec($ch);
    $httpCode = curl_getinfo($ch, CURLINFO_HTTP_CODE);
    $curlErr  = curl_error($ch);
    curl_close($ch);
    @unlink($tmpFile);

    if ($curlErr) {
        return ['success' => false, 'file_id' => null, 'error' => "cURL upload: {$curlErr}", 'raw_response' => null];
    }

    $data = json_decode($response, true);
    if ($httpCode !== 200 || empty($data['id'])) {
        $errMsg = $data['error']['message'] ?? "HTTP {$httpCode}: {$response}";
        return ['success' => false, 'file_id' => null, 'error' => $errMsg, 'raw_response' => $response];
    }

    return ['success' => true, 'file_id' => $data['id'], 'error' => null];
}

/**
 * Sube una imagen (física) a la API de Qwen Files para ser usada en Batch API.
 *
 * @param string $apiKey     API Key de Alibaba DashScope
 * @param string $imagePath  Ruta física absoluta a la imagen .jpg o .png en servidor
 * @param string $mimeType   image/jpeg o image/png
 * @return array ['success'=>bool, 'file_id'=>string|null, 'error'=>string|null]
 */
function uploadImageQwen(string $apiKey, string $imagePath, string $mimeType): array
{
    $url = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1/files";
    
    if (!file_exists($imagePath)) {
        return ['success' => false, 'file_id' => null, 'error' => 'La imagen local no existe.'];
    }

    $filename = basename($imagePath);
    $cfile = new CURLFile($imagePath, $mimeType, $filename);

    $ch = curl_init($url);
    curl_setopt_array($ch, [
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_POST           => true,
        CURLOPT_POSTFIELDS     => ['file' => $cfile, 'purpose' => 'batch'],
        CURLOPT_HTTPHEADER     => ["Authorization: Bearer {$apiKey}"],
        CURLOPT_TIMEOUT        => 120,
        CURLOPT_SSL_VERIFYPEER => false,
    ]);
    $response = curl_exec($ch);
    $httpCode = curl_getinfo($ch, CURLINFO_HTTP_CODE);
    $curlErr  = curl_error($ch);
    curl_close($ch);

    if ($curlErr) {
        return ['success' => false, 'file_id' => null, 'error' => "cURL upload: {$curlErr}"];
    }

    $data = json_decode($response, true);
    if ($httpCode !== 200 || empty($data['id'])) {
        $errMsg = $data['error']['message'] ?? "HTTP {$httpCode}: {$response}";
        return ['success' => false, 'file_id' => null, 'error' => $errMsg];
    }

    return ['success' => true, 'file_id' => $data['id'], 'error' => null];
}

/**
 * Crea el batch job en Qwen después de haber subido el archivo.
 *
 * @return array ['success'=>bool, 'batch_id'=>string|null, 'error'=>string|null]
 */
function enviarBatchQwen(string $apiKey, string $fileId, int $completionWindowDays = 1): array
{
    $url = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1/batches";

    $body = [
        'input_file_id'      => $fileId,
        'endpoint'           => '/v1/chat/completions',
        'completion_window'  => "{$completionWindowDays}d",
    ];

    $ch = curl_init($url);
    curl_setopt_array($ch, [
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_POST           => true,
        CURLOPT_POSTFIELDS     => json_encode($body),
        CURLOPT_HTTPHEADER     => [
            'Content-Type: application/json',
            "Authorization: Bearer {$apiKey}",
        ],
        CURLOPT_TIMEOUT        => 60,
        CURLOPT_SSL_VERIFYPEER => false,
    ]);
    $response = curl_exec($ch);
    $httpCode = curl_getinfo($ch, CURLINFO_HTTP_CODE);
    $curlErr  = curl_error($ch);
    curl_close($ch);

    if ($curlErr) {
        return ['success' => false, 'batch_id' => null, 'error' => "cURL: {$curlErr}", 'raw_response' => null];
    }

    $data = json_decode($response, true);
    if ($httpCode !== 200 || empty($data['id'])) {
        $errMsg = $data['error']['message'] ?? "HTTP {$httpCode}: {$response}";
        return ['success' => false, 'batch_id' => null, 'error' => $errMsg, 'raw_response' => $response];
    }

    return [
        'success'  => true,
        'batch_id' => $data['id'],
        'status'   => $data['status'] ?? '',
        'error'    => null,
    ];
}

/**
 * Consulta el estado de un batch de Qwen.
 *
 * @return array ['done'=>bool, 'status'=>string, 'output_file_id'=>string|null, 'error'=>string|null]
 */
function pollBatchQwen(string $apiKey, string $batchId): array
{
    $url = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1/batches/{$batchId}";

    $ch = curl_init($url);
    curl_setopt_array($ch, [
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_HTTPHEADER     => ["Authorization: Bearer {$apiKey}"],
        CURLOPT_TIMEOUT        => 30,
        CURLOPT_SSL_VERIFYPEER => false,
    ]);
    $response = curl_exec($ch);
    $httpCode = curl_getinfo($ch, CURLINFO_HTTP_CODE);
    $curlErr  = curl_error($ch);
    curl_close($ch);

    if ($curlErr) {
        return ['done' => false, 'status' => 'error', 'output_file_id' => null, 'error' => "cURL: {$curlErr}"];
    }
    if ($httpCode !== 200) {
        return ['done' => false, 'status' => 'error', 'output_file_id' => null, 'error' => "HTTP {$httpCode}"];
    }

    $data   = json_decode($response, true);
    $status = $data['status'] ?? 'unknown';
    $done   = ($status === 'completed');
    $failed = in_array($status, ['failed', 'expired', 'cancelled']);

    if ($failed) {
        $errorMsg = "Batch Qwen falló: {$status}";
        if (!empty($data['errors']) && is_array($data['errors']) && isset($data['errors']['data'][0]['message'])) {
            $errorMsg .= " - Detalle: " . $data['errors']['data'][0]['message'];
        } elseif (!empty($data['error']['message'])) {
            $errorMsg .= " - Detalle: " . $data['error']['message'];
        }
        return ['done' => false, 'status' => $status, 'output_file_id' => null, 'error' => $errorMsg, 'is_fatal' => true];
    }

    $completedCount = $data['request_counts']['completed'] ?? 0;
    $totalCount     = $data['request_counts']['total']     ?? 0;

    return [
        'done'           => $done,
        'status'         => $status,
        'output_file_id' => $data['output_file_id'] ?? null,
        'error_file_id'  => $data['error_file_id']  ?? null,
        'completed_count'=> $completedCount,
        'total_count'    => $totalCount,
        'error'          => null,
        'raw'            => $data,
    ];
}

/**
 * Descarga y parsea el archivo de resultados de un batch Qwen.
 * Retorna un array indexado por custom_id.
 *
 * @return array ['custom_id' => ['texto'=>string, 'tokens_input'=>int, ...], ...]
 */
function descargarResultadosQwen(string $apiKey, string $outputFileId): array
{
    $url = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1/files/{$outputFileId}/content";

    $ch = curl_init($url);
    curl_setopt_array($ch, [
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_HTTPHEADER     => ["Authorization: Bearer {$apiKey}"],
        CURLOPT_TIMEOUT        => 120,
        CURLOPT_SSL_VERIFYPEER => false,
    ]);
    $content  = curl_exec($ch);
    $httpCode = curl_getinfo($ch, CURLINFO_HTTP_CODE);
    curl_close($ch);

    if ($httpCode !== 200 || empty($content)) {
        return [];
    }

    $results = [];
    foreach (explode("\n", trim($content)) as $line) {
        $line = trim($line);
        if (empty($line)) continue;
        $obj = json_decode($line, true);
        if (!$obj || empty($obj['custom_id'])) continue;

        $customId = $obj['custom_id'];
        $body     = $obj['response']['body'] ?? [];
        $texto    = $body['choices'][0]['message']['content'] ?? null;
        
        // content puede ser array de parts (qwen-vl) o string
        if (is_array($texto)) {
            $concat = '';
            foreach ($texto as $part) {
                if (is_array($part) && isset($part['text'])) $concat .= $part['text'];
            }
            $texto = $concat ?: null;
        }

        $usage = $body['usage'] ?? [];
        $results[$customId] = [
            'texto'          => $texto,
            'tokens_input'   => $usage['prompt_tokens']     ?? 0,
            'tokens_output'  => $usage['completion_tokens'] ?? 0,
            'tokens_thought' => $usage['completion_tokens_details']['reasoning_tokens'] ?? 0,
            'tokens_total'   => $usage['total_tokens']      ?? 0,
            'error'          => $obj['error'] ?? null,
            'raw_response'   => $line,
        ];
    }

    return $results;
}

/**
 * Lista batches activos en Qwen.
 * Usado para el mecanismo de rescate anti-duplicación.
 */
function listarBatchesQwen(string $apiKey): array
{
    $url = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1/batches";

    $ch = curl_init($url);
    curl_setopt_array($ch, [
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_HTTPHEADER     => ["Authorization: Bearer {$apiKey}"],
        CURLOPT_TIMEOUT        => 30,
        CURLOPT_SSL_VERIFYPEER => false,
    ]);
    $response = curl_exec($ch);
    $httpCode = curl_getinfo($ch, CURLINFO_HTTP_CODE);
    curl_close($ch);

    if ($httpCode !== 200) return [];
    $data = json_decode($response, true);
    return $data['data'] ?? [];
}
