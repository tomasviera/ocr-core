<?php
/**
 * prompts/lib_prompt_render.php — NÚCLEO COMPARTIDO del render de prompts.
 *
 * Motor de render del sistema de prompts componibles. Es la parte **agnóstica al
 * dominio** del render de prensadelplata: NO sabe de periódicos ni de legajos, NO
 * toca la tabla `prompts`, y sólo lee las 3 tablas reutilizables
 * (`prompt_bases`, `prompt_fragmentos`, `prompt_modelo_addenda`) vía el `$pdo`
 * que le INYECTA el proyecto consumidor. Nunca abre conexión propia.
 *
 * Frontera núcleo / project-side (ver el plan del sistema componible):
 *   - NÚCLEO (este archivo): renderizarPromptAdHoc(), inyectarAddenda(),
 *     cargarBaseActiva/Fragmento/Addenda(), expandirFragmentos(),
 *     aplicarCondicionales(), aplicarVariables(), resolverDot(),
 *     detectarFamiliaModelo(), decodificarExtras() + consts.
 *   - PROJECT-SIDE (queda en cada proyecto, NO acá): renderizarPrompt()
 *     (SELECT … FROM prompts), cargarVariablesPeriodico(),
 *     construirContextoRender*() (arma el diccionario con el vocabulario del
 *     dominio) y propagarBaseAPrompts() (versiona la tabla `prompts`).
 *
 * Sintaxis "Mustache-lite":
 *   {{variable}}                → interpolación. Dot-notation: {{a.b.c}}
 *   {{#if condicion}}...{{/if}} → bloque condicional simple (sin else, anidable).
 *                                 Truthy = no-empty, no-zero, no-"0", no-"false".
 *   {{> slug_fragmento}}        → include de fragmento por slug. Recursivo hasta
 *                                 PROMPT_RENDER_MAX_DEPTH, con detección de ciclos.
 *   {{! comentario }}           → comentario, no aparece en el output.
 *   {{addenda_modelo}}          → punto de inyección de la addenda de familia.
 *
 * NO hay sección invertida `{{^}}`: se modela project-side con un booleano de
 * contexto (p.ej. {{#if no_apuntes}}).
 *
 * Contrato de render (entrada): el proyecto arma project-side un array de prompt
 * YA RESUELTO (`$promptAdHoc`) + un diccionario de contexto (`$contexto`), y el
 * core re-renderiza SIEMPRE desde la base/fragmentos/addenda activos (el snapshot
 * `pro_texto` es sólo fallback si no hay base).
 *
 * GENERADO/PORTADO desde prensadelplata/WEB/includes/lib_prompt_render.php — la
 * fuente canónica de las funciones puras es ESTE archivo del core.
 */

// Profundidad máxima de expansión de fragmentos (anti-recursión infinita).
const PROMPT_RENDER_MAX_DEPTH = 5;

// Cantidad máxima de pases de condicionales/variables (defensa para edge cases).
const PROMPT_RENDER_MAX_PASSES = 8;

// Token opcional que el autor de la base puede colocar DENTRO del texto para
// fijar la posición exacta donde se incrusta la addenda de la familia de modelo,
// en vez del append automático al final. Ver inyectarAddenda().
const PROMPT_ADDENDA_TOKEN = '{{addenda_modelo}}';

/**
 * Incrusta la addenda de familia (texto CRUDO, sin expandir) en el prompt:
 *   - Si el texto contiene `{{addenda_modelo}}`, reemplaza ahí (todas las
 *     ocurrencias) — la posición la decide el autor de la base.
 *   - Si NO hay token, hace el append histórico al final (retrocompatible con
 *     las bases que no usan el placeholder).
 *   - Si la addenda viene vacía (familia sin addenda): igual limpia el token si
 *     está, y no appendea nada.
 *
 * Devuelve el texto con la addenda ya colocada pero SIN expandir: las variables
 * y condicionales de la addenda las resuelve el loop de pases del caller, en el
 * mismo barrido que el resto del prompt. Por eso inyectarAddenda() se llama
 * ANTES de los pases.
 */
function inyectarAddenda(string $texto, string $addendaRaw): string
{
    $addendaRaw = trim($addendaRaw);
    if (strpos($texto, PROMPT_ADDENDA_TOKEN) !== false) {
        return str_replace(PROMPT_ADDENDA_TOKEN, $addendaRaw, $texto);
    }
    if ($addendaRaw === '') return $texto;
    return rtrim($texto) . "\n\n" . $addendaRaw . "\n";
}

/**
 * Renderiza un prompt "ad-hoc": recibe el prompt YA RESUELTO project-side y un
 * diccionario de contexto. Es el ÚNICO entry-point de render del core.
 *
 * $promptAdHoc:
 *   - 'pro_baslinaje' (int|null)        — modo composición. NULL → usa pro_texto.
 *   - 'pro_texto' (string|null)         — fallback (modo "texto crudo"/legacy).
 *   - 'pro_fragmentosextra' (array|string|null) — slugs extra (JSONB o array PHP).
 *   - 'pro_familia' (string, opcional)  — override EXPLÍCITO de familia de modelo
 *                                          (p.ej. v3 pasa 'antigravity'/'aistudio').
 *   - 'pro_modelo' (string, opcional)   — para auto-detectar familia si no hay override.
 *   - 'pro_endpoint' (string, opcional) — idem.
 *
 * La familia (para elegir la addenda) se resuelve con esta precedencia:
 *   1. $contexto['_familia_override'] (no vacío)  ← v3 lo setea desde el motor.
 *   2. $promptAdHoc['pro_familia'] (no vacío).
 *   3. detectarFamiliaModelo($endpoint, $modelo)  ← comportamiento histórico (prensa).
 */
function renderizarPromptAdHoc(PDO $pdo, array $promptAdHoc, array $contexto): string
{
    $basLinaje = $promptAdHoc['pro_baslinaje'] ?? null;
    if ($basLinaje === null || $basLinaje === '' || $basLinaje === 0 || $basLinaje === '0') {
        return (string) ($promptAdHoc['pro_texto'] ?? '');
    }

    $base = cargarBaseActiva($pdo, (int) $basLinaje);
    if ($base === null) return (string) ($promptAdHoc['pro_texto'] ?? '');

    $texto = $base;
    $extras = decodificarExtras($promptAdHoc['pro_fragmentosextra'] ?? null);
    foreach ($extras as $slug) {
        $slug = (string) $slug;
        if ($slug === '') continue;
        $texto .= "\n\n{{> " . $slug . "}}";
    }

    $texto = preg_replace('/\{\{!.*?\}\}/s', '', $texto) ?? $texto;
    $visitados = [];
    $texto = expandirFragmentos($texto, $pdo, $visitados, 0);

    // Addenda ANTES de los pases (para que sus variables/condicionales se
    // expandan en el mismo barrido). Familia por precedencia override → prompt → auto.
    $familiaOverride = (string) ($contexto['_familia_override'] ?? '');
    if ($familiaOverride !== '') {
        $familia = $familiaOverride;
    } else {
        $familiaPrompt = (string) ($promptAdHoc['pro_familia'] ?? '');
        if ($familiaPrompt !== '') {
            $familia = $familiaPrompt;
        } else {
            $modelo   = (string) ($promptAdHoc['pro_modelo']   ?? '');
            $endpoint = (string) ($promptAdHoc['pro_endpoint'] ?? '');
            $familia  = detectarFamiliaModelo($endpoint, $modelo);
        }
    }
    $addendaRaw = $familia !== '' ? (string) (cargarAddendaActiva($pdo, $familia) ?? '') : '';
    $texto = inyectarAddenda($texto, $addendaRaw);

    for ($i = 0; $i < PROMPT_RENDER_MAX_PASSES; $i++) {
        $antes = $texto;
        $texto = aplicarCondicionales($texto, $contexto);
        $texto = aplicarVariables($texto, $contexto);
        if ($texto === $antes) break;
    }

    return $texto;
}

// ─────────────────────────────────────────────────────────────────────────────
// CARGA DE PIEZAS (bases / fragmentos / addenda) — leen las 3 tablas compartidas.
// Caché estática por request: una edición de pieza a mitad de corrida del worker
// no se ve hasta el próximo request (documentado como riesgo conocido).
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Devuelve el texto de la base activa de un linaje, o NULL si no existe.
 */
function cargarBaseActiva(PDO $pdo, int $bas_linaje): ?string
{
    static $cache = [];
    if (isset($cache[$bas_linaje])) return $cache[$bas_linaje];

    $stmt = $pdo->prepare("
        SELECT bas_Texto FROM prompt_bases
        WHERE bas_Linaje = ? AND bas_Activo = TRUE
        ORDER BY bas_Version DESC LIMIT 1
    ");
    $stmt->execute([$bas_linaje]);
    $texto = $stmt->fetchColumn();
    return $cache[$bas_linaje] = ($texto === false ? null : (string) $texto);
}

/**
 * Devuelve el texto del fragmento activo con ese slug, o NULL si no existe.
 */
function cargarFragmentoActivo(PDO $pdo, string $slug): ?string
{
    static $cache = [];
    if (array_key_exists($slug, $cache)) return $cache[$slug];

    $stmt = $pdo->prepare("
        SELECT fra_Texto FROM prompt_fragmentos
        WHERE fra_Slug = ? AND fra_Activo = TRUE
        LIMIT 1
    ");
    $stmt->execute([$slug]);
    $texto = $stmt->fetchColumn();
    return $cache[$slug] = ($texto === false ? null : (string) $texto);
}

/**
 * Devuelve el texto de la addenda activa para una familia, o NULL si no hay.
 */
function cargarAddendaActiva(PDO $pdo, string $familia): ?string
{
    static $cache = [];
    if (array_key_exists($familia, $cache)) return $cache[$familia];

    $stmt = $pdo->prepare("
        SELECT mad_Texto FROM prompt_modelo_addenda
        WHERE mad_Familia = ? AND mad_Activo = TRUE
        LIMIT 1
    ");
    $stmt->execute([$familia]);
    $texto = $stmt->fetchColumn();
    return $cache[$familia] = ($texto === false ? null : (string) $texto);
}

// ─────────────────────────────────────────────────────────────────────────────
// EXPANSIÓN DE FRAGMENTOS
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Reemplaza recursivamente todos los {{> slug}} del texto con el contenido del
 * fragmento activo correspondiente. Detecta ciclos y trunca si la profundidad
 * supera PROMPT_RENDER_MAX_DEPTH (anti-recursión infinita).
 *
 * Si un slug no existe como fragmento activo, lo reemplaza por un placeholder
 * visible: `<!-- frag:slug_x faltante -->`. No tira excepción para no romper
 * el render por un slug no creado todavía.
 */
function expandirFragmentos(string $texto, PDO $pdo, array &$visitados, int $depth): string
{
    if ($depth > PROMPT_RENDER_MAX_DEPTH) {
        return $texto . "\n<!-- max-depth-fragmentos-superada -->";
    }

    return preg_replace_callback(
        '/\{\{>\s*([\w\-]+)\s*\}\}/',
        function ($m) use ($pdo, &$visitados, $depth) {
            $slug = $m[1];
            if (isset($visitados[$slug])) {
                return "<!-- ciclo-fragmento:{$slug} -->";
            }
            $contenido = cargarFragmentoActivo($pdo, $slug);
            if ($contenido === null) {
                return "<!-- frag:{$slug} faltante -->";
            }
            $visitados[$slug] = true;
            $resultado = expandirFragmentos($contenido, $pdo, $visitados, $depth + 1);
            unset($visitados[$slug]);
            return $resultado;
        },
        $texto
    ) ?? $texto;
}

// ─────────────────────────────────────────────────────────────────────────────
// CONDICIONALES Y VARIABLES
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Procesa bloques `{{#if x}}...{{/if}}`. Mantiene el contenido si x es truthy.
 * Soporta anidado arbitrario: procesa el bloque más interno primero (tempered
 * greedy token) e itera hacia afuera hasta estabilizar.
 *
 * Truthy: no-empty, no-zero, no-"0", no-"false". Soporta dot-notation.
 */
function aplicarCondicionales(string $texto, array $contexto): string
{
    // Solo matchea bloques que NO contienen otro {{#if adentro (los más internos).
    // El while procesa capa por capa hasta que no queda ningún bloque sin resolver.
    $patron = '/\{\{#if\s+([\w\.]+)\s*\}\}((?:(?!\{\{#if\b)[\s\S])*?)\{\{\/if\}\}/';
    $anterior = null;
    while ($texto !== $anterior) {
        $anterior = $texto;
        $texto = preg_replace_callback(
            $patron,
            function ($m) use ($contexto) {
                $valor = resolverDot($contexto, $m[1]);
                $truthy = ($valor !== null && $valor !== '' && $valor !== '0'
                           && $valor !== false && strtolower((string)$valor) !== 'false');
                return $truthy ? $m[2] : '';
            },
            $texto
        ) ?? $texto;
    }
    return $texto;
}

/**
 * Reemplaza {{var}} y {{a.b.c}} por su valor en $contexto.
 * Si la variable no existe, se la deja como `(faltante:nombre)` para que sea
 * visible en el output sin romper el render. Las llaves con `#`, `/`, `>` o `!`
 * no son variables; las saltea (el patrón sólo matchea [\w\.]).
 */
function aplicarVariables(string $texto, array $contexto): string
{
    return preg_replace_callback(
        '/\{\{\s*([\w\.]+)\s*\}\}/',
        function ($m) use ($contexto) {
            $clave = $m[1];
            $valor = resolverDot($contexto, $clave);
            if ($valor === null) return "(faltante:{$clave})";
            if (is_bool($valor)) return $valor ? 'true' : 'false';
            if (is_array($valor)) return json_encode($valor, JSON_UNESCAPED_UNICODE);
            return (string) $valor;
        },
        $texto
    ) ?? $texto;
}

/**
 * Resuelve "a.b.c" sobre $ctx. Devuelve null si algún tramo no existe.
 */
function resolverDot(array $ctx, string $path)
{
    $parts = explode('.', $path);
    $cur = $ctx;
    foreach ($parts as $p) {
        if (is_array($cur) && array_key_exists($p, $cur)) {
            $cur = $cur[$p];
        } else {
            return null;
        }
    }
    return $cur;
}

// ─────────────────────────────────────────────────────────────────────────────
// FAMILIA DE MODELO (auto-detección heurística — fallback si no hay override)
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Detecta la familia de modelo a partir del endpoint/modelo del prompt.
 * Es sólo el FALLBACK: cada proyecto puede pasar la familia explícita por
 * contexto (`_familia_override`) o por `$promptAdHoc['pro_familia']`.
 *
 * Devuelve: 'aistudio' | 'antigravity' | 'gemini_cli' | 'google' | 'anthropic'
 *           | 'alibaba' | ''
 *
 * `aistudio` se chequea PRIMERO: el modelo de catálogo de AI Studio web lleva el
 * sufijo `-aistudio` y también contiene `gemini`, así que sin este corte caería
 * en `google` (la API Cloud) y compartiría su addenda.
 */
function detectarFamiliaModelo(string $endpoint, string $modelo): string
{
    $m = strtolower($modelo);
    $e = strtolower($endpoint);

    if (str_contains($m, 'aistudio'))                                      return 'aistudio';
    if (str_contains($m, '-agy'))                                          return 'antigravity';
    if (str_contains($m, 'cli') || str_contains($m, 'gemini-3-auto-cli')) return 'gemini_cli';
    if (str_contains($e, 'anthropic.com') || str_contains($m, 'claude'))  return 'anthropic';
    if (str_contains($e, 'aliyuncs.com') || str_contains($e, 'dashscope')
        || str_contains($m, 'qwen'))                                       return 'alibaba';
    if (str_contains($e, 'googleapis.com') || str_contains($m, 'gemini'))  return 'google';

    return '';
}

// ─────────────────────────────────────────────────────────────────────────────
// HELPERS INTERNOS
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Decodifica el JSONB de pro_FragmentosExtra. Acepta array PHP, string JSON,
 * o NULL. Devuelve siempre array de strings (slugs).
 */
function decodificarExtras($valor): array
{
    if ($valor === null || $valor === '') return [];
    if (is_array($valor)) return array_values(array_filter($valor, 'is_string'));
    if (is_string($valor)) {
        $decoded = json_decode($valor, true);
        if (is_array($decoded)) return array_values(array_filter($decoded, 'is_string'));
    }
    return [];
}
