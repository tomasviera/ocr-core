# ocr-core

Núcleo compartido de los proyectos de OCR-por-LLM **prensadelplata** (A) y
**transcriptor-manuscritos-v3** (B). Contiene motores de proveedor e infra común,
**agnósticos al dominio**. Cada proyecto **vendoriza** una copia fijada de una
versión (`core_vendor/` + `core_version.txt`); este repo (`E:\ocr-core`) es el
**único lugar donde se edita** el código compartido.

> **Ubicación:** `E:\ocr-core`, **fuera de `E:\OneDrive\`** para que OneDrive no
> sincronice el `.git`. Backup en la nube = repo **GitHub privado** (ver `GUIA_GIT.md`).

---

## Estructura

```
ocr-core/
├── VERSION                 # versión actual del core (empieza en "v0"; bump → v1, v2…)
├── README.md               # este archivo: contratos + reglas duras
├── GUIA_GIT.md             # cómo publicar versiones y volver atrás
├── .gitignore              # excluye cualquier secrets*, efímeros, vendor
├── bump_core.ps1           # publicar versión nueva del core (commit + tag + push)
├── motores/                # ejecutarAgy()/ejecutarAistudio()/… → struct. (Fase 1+)
├── infra/                  # database.php, lib_logger.php (Fase 4)
├── utils/                  # qa_base, sandbox, cuota_windows, sentinels (Fase 3)
└── prompts/                # render de prompts componibles (puro) + schema compartido
    ├── lib_prompt_render.php   # render Mustache-lite agnóstico al dominio
    └── schema_compartido.sql   # DDL de prompt_bases/fragmentos/modelo_addenda
```

En **v0** (Fase 0) `motores/ infra/ utils/` están vacíos (solo `.gitkeep`).
Los motores entran faseado: `lib_agy.php` + `transcribir_agy.py` en **Fase 1**.

---

## Reglas duras del core (no negociables)

1. **`motores/` no accede a BD de dominio.** Cero SQL. Reciben `(prompt, imagen, config)`
   y devuelven un **struct** (ver contrato abajo). Slots, QA, sentinels y persistencia
   viven **en el worker de cada proyecto**.
2. **El core nunca conoce tabla/columnas.** Loguea vía la costura `coreLog`
   (`function_exists`, ver abajo); cada proyecto provee el adaptador.
3. **Paths efímeros entran por argumento** (sandbox/debug/home). Nunca hardcodeados.
   El `PROJECT_ROOT` del `.py` queda **solo como fallback**; en producción el worker
   pasa rutas absolutas.
4. **Ningún secreto en el repo.** `.gitignore` excluye `secrets*`. Credenciales y
   passwords viven en el `secrets.php` de cada proyecto, jamás acá.
5. **Una sola dirección.** Se edita en `E:\ocr-core`; los proyectos solo **consumen**
   copias vendorizadas (que llevan header `GENERADO — no editar acá`).

---

## Artefacto #1 — Contrato de motor (struct de A, *verbatim*)

Semilla: `prensadelplata/WEB/includes/lib_agy.php` + `scripts/transcribir_agy.py`.
El struct de A es **superset** del de B → se adopta como contrato. B ya lee con
`?? default` (`registrarLlamada`), así que el superset encaja sin tocar su persistencia.

### Entrada PHP (firma ya alineada en A y B)

```php
ejecutarAgy(
    string $promptCompleto,   // prompt completo (con addenda del proveedor)
    string $imagenPath,       // ruta absoluta a la imagen
    int    $jobId,
    string $imagenStem,       // nombre base de la imagen (para nombrar workdir)
    string $resultadosDir,
    array  $agyConfig = [],    // ver claves abajo
    int    $timeout = 300,
    int    $maxIntentos = 1
): array
```

`$agyConfig` (lo que hoy parametriza A):

| clave | tipo | obligatoria | nota |
|---|---|---|---|
| `sandbox_dir` | string | **sí** | sandbox PRE-TRUSTED del slot (cwd de agy) |
| `home_dir` | string | no | override de HOME (lee/escribe `~/.gemini` de acá) |
| `modelo_agy` | string | no | modelo exacto para `settings.json["model"]` |
| `debug_dir` | string | no | si está, vuelca bundle forense (gateado por flag) |
| `workdir_base` | string | no | base del workdir efímero del wrapper; si falta cae a `$resultadosDir` y luego a `sys_get_temp_dir()` (se cuelga `agy_subprocess/`) |
| `timeout_respuesta_seg` | int | no | default = `$timeout` |
| `cols` | int | no | columnas del pseudo-terminal (default 2000) |
| `rows` | int | no | filas (default 100) |
| `grace` | float | no | segundos de estabilidad tras candidato (default 5.0) |
| `agy_bin` | string | no | ejecutable de agy (default `agy` en PATH) |

### Entrada CLI del `.py` (`transcribir_agy.py`)

```
--imagen        (req)  ruta absoluta a la imagen (.jpg/.png/.b64)
--prompt        (req)  ruta absoluta al prompt.md completo
--salida-json   (req)  ruta donde escribir el JSON con el struct
--sandbox-dir   (req)  sandbox PRE-TRUSTED (cwd de agy)
--home-dir      (opt)  override de HOME para agy
--modelo-agy    (opt)  modelo para settings.json["model"]
--timeout       (opt, def 300)   timeout total de captura (s)
--cols          (opt, def 2000)  columnas del pseudo-terminal
--rows          (opt, def 100)   filas
--grace         (opt, def 5.0)   segundos de estabilidad tras candidato
--debug-dir     (opt)  vuelca bundle .txt forense ahí
--launch-mode   (opt, def conpty)  solo 'conpty' por ahora
--agy-bin       (opt, def agy)   ejecutable de agy
```

El `.py` escribe el struct como **JSON al archivo `--salida-json`** (no a stdout).
Requiere `pyte` + `pywinpty` en el Python que lo ejecute (`agyPython()` resuelve
`where python` en Windows).

### Salida (struct) — `_agyShapeRespuesta()`

**Núcleo obligatorio** (presente siempre, todo motor lo respeta):

```
ok            bool        éxito de la captura
response      string      texto transcripto
error         string|null null si ok; mensaje si no
engine        string      'agy' (id del motor)
duracion_seg  float
intentos      int
exit_code     int|null
stdout_raw    string
stderr_raw    string
sandbox_path  string|null ruta al workdir (null si se barrió)
cuota_agotada bool
stats         array       ([] en agy: no expone granulares)
tools         null
session_id    null
tokens_input    int
tokens_output   int
tokens_thought  int
tokens_cached   int
tokens_total    int
```

**Extras agy** (condicionales, solo si el motor los reporta):

```
errores_intentos, veredicto, fin_presente (bool), websearch_detectado (bool),
websearch_patrones, websearch_fuente, tools_used, longitud_sospechosa (bool),
stdout_largo_sospechoso (bool), estado_captura, bytes_leidos (int),
zombis_barridos (int), modelo_pedido, statusline_disponible (bool),
context_window_size (int), used_percentage (float), plan_tier
```

> **El motor REPORTA señales, no decide estado/QA.** La interpretación
> (`OK`/`Revisar`/`OKDudosa`, QA bits, backup, sentinels) vive en el worker.

---

## Motor AI Studio (Fase 2) — `lib_aistudio.php` + 2 scripts

Segundo motor, **mismo contrato** (firma + struct) que agy; `engine='aistudio_web'`.
Transcribe vía la web UI de Google AI Studio (Playwright + Chrome por CDP). Scripts
sibling: `transcribir_aistudio.py` (transcripción) y `aistudio_check_login.py`
(probe de login para `aistudioCheckLoginCDP`).

```php
ejecutarAiStudio(
    string $promptTexto, string $imagenPath, int $jobId, string $imagenStem,
    string $resultadosDir, array $aistudioConfig = [], int $timeout = 300,
    int $maxIntentos = 1
): array
```

`$aistudioConfig` (claves que lee el motor):

| clave | tipo | nota |
|---|---|---|
| `cdp_url` | string | Chrome con CDP (default `http://localhost:9222`) |
| `modelo` | string | modelo en el selector de AI Studio |
| `timeout_respuesta_seg` | int | default = `$timeout` |
| `media_resolution` | string | `High`/… (resolución de imagen) |
| `thinking_level` | string | `''` = no tocar el dropdown |
| `screenshot_error_dir` | string | rel → root del proyecto; abs se usa tal cual |
| `no_cerrar_tab_error` | bool | debug: no cerrar el tab de Chrome al fallar |

- Los `.py` son **siblings** del lib (`__DIR__`), no `scripts/`.
- El **root del proyecto** se reconstruye de `$resultadosDir` (`<root>/temp`) para
  ubicar sandbox/errores efímeros — NO se deriva de `__DIR__` (que en `core_vendor/`
  apuntaría mal). Sirve a prensa (root=`WEB`) y v2 (root=`web/`) sin tocar el caller.
- Struct: núcleo idéntico + extras `veredicto, fuente_dom, heuristicas,
  media_resolution, thinking_level, modo_prompt, incompleto, longitud_sospechosa`.
  `tokens_thought=0` (AI Studio no separa thoughts del output; van en `tokens_output`).
- `aistudioLog()` (helper de logging usado por worker/supervisor/cuentas del
  proyecto) **NO** está en el core: vive en el shim `includes/lib_aistudio.php` de
  cada proyecto. El motor del core loguea por `coreLog('aistudio_web', …)`.

---

## Modo sin imagen — `$sinImagen` (param compartido, Fase 2)

Ambos motores (`ejecutarAgy` / `ejecutarAiStudio`) aceptan un **9º parámetro
opcional** `bool $sinImagen = false`. Sirve al **postproceso de v2** (llamados al
motor con prompt de texto puro, sin imagen). Default `false` → prensa (sólo
transcripción) no se entera; sus llamados de 8 args quedan idénticos.

- `ejecutarAgy(..., $sinImagen: true)`: el motor materializa una **dummy 1×1** en
  el workdir (agy exige un `image.jpg` copiable). El `.py` de agy **no cambia**.
- `ejecutarAiStudio(..., $sinImagen: true)`: saltea la copia y pasa `--sin-imagen`
  al `.py`, que **no adjunta** nada (AI Studio web no puede mandar una dummy sin
  que se vea). `--imagen` pasa a opcional en el `.py`.

El "cómo" del sin-imagen difiere por motor (inherente); el **contrato y la lógica
del caller son idénticos** (un flag). Un proyecto que llame motores para
transcripción *y* postproceso usa la misma función con `$sinImagen` distinto —
en v2 vía el dispatcher único `ejecutarMotor()`.

---

## Artefacto #3 — Render de prompts componibles (`prompts/`)

Parte **agnóstica al dominio** del sistema de prompts componibles de prensa.
Mismo patrón que los motores: **el core es lógica pura; cómo cada proyecto
discrimina/invoca es project-side.**

**Qué lee el core:** SÓLO las 3 tablas reutilizables `prompt_bases`,
`prompt_fragmentos`, `prompt_modelo_addenda`, vía el `$pdo` que **inyecta el
proyecto**. Nunca abre conexión propia.

**Qué NO sabe / NO toca el core:** la tabla `prompts` (project-specific), los
periódicos, los legajos, ni cualquier discriminador de dominio. El proyecto arma
**project-side** el array de prompt YA RESUELTO + el diccionario de contexto, y
el core re-renderiza.

```php
// Entry-point único del core:
renderizarPromptAdHoc(PDO $pdo, array $promptAdHoc, array $contexto): string
```

`$promptAdHoc`: `pro_baslinaje` (int|null; NULL → devuelve `pro_texto` literal),
`pro_texto` (fallback), `pro_fragmentosextra` (array|JSON de slugs),
`pro_familia` (override opcional de familia), `pro_modelo`/`pro_endpoint`
(auto-detección de familia si no hay override).

**Familia de la addenda** (precedencia): `$contexto['_familia_override']` →
`$promptAdHoc['pro_familia']` → `detectarFamiliaModelo($endpoint,$modelo)`.

**Sintaxis Mustache-lite:** `{{var}}`, dot `{{a.b}}`, `{{#if x}}…{{/if}}`
(anidable; sin `{{^}}` → modelar con un booleano de contexto), `{{> slug}}`
(recursivo, anti-ciclo), `{{! comentario }}`, token `{{addenda_modelo}}`.

**Schema:** `prompts/schema_compartido.sql` (DDL idéntico entre proyectos →
anti-drift). Lo aplica el `init_db.php` de cada proyecto; el core no lo ejecuta.
`*_proposito` queda como TEXT libre (cada proyecto usa su vocabulario).

**Caché estática por request** de los cargadores: una edición de base/fragmento
a mitad de corrida del worker no se ve hasta el próximo request.

**Funciones project-side (NO en el core):** `renderizarPrompt()` (SELECT FROM
`prompts`), `cargarVariablesPeriodico()`, `construirContextoRender*()`,
`propagarBaseAPrompts()` (versiona la tabla `prompts`).

---

## Artefacto #2 — Costura logger (convención `function_exists`)

El motor del core loguea llamando a una función de **nombre fijo**, protegida por
`function_exists`. El core **no conoce** la tabla ni las columnas; cada proyecto define
el adaptador.

```php
// En el core (motores/lib_agy.php), wrapper defensivo:
function coreLog(string $engine, string $nivel, string $mensaje, array $detalle = []): void
{
    try {
        if (function_exists('coreLogSink')) {
            coreLogSink($engine, $nivel, $mensaje, $detalle);
        }
    } catch (Throwable $e) { /* logging best-effort: nunca propagar */ }
}
```

Cada proyecto define `coreLogSink(...)` ruteando a SU log de eventos:

```php
// prensadelplata  → transcripcion_debug_log
function coreLogSink($engine, $nivel, $msg, $detalle) { logDebug($engine, $nivel, $msg, $detalle); }

// manuscritos-v3  → eventos
function coreLogSink($engine, $nivel, $msg, $detalle) { logEvento($engine, $nivel, $msg, $detalle); }
```

> **Estado hoy (a generalizar en Fase 1):** A usa `agyLog($nivel, $msg, $detalle)` →
> `logDebug('AGY', …)`. Fase 1 renombra el entry-point a `coreLog` y agrega el
> parámetro `$engine` (deja de hardcodear `'AGY'`). El nombre del *sink* del proyecto
> (acá `coreLogSink`) se fija al cablear Fase 1.

**Lo que NO pasa por `coreLog`:** la *persistencia de dominio*
(`registrarLlamada` / `guardarTranscripcionRaw` / `parseAndInsertEntradas`) la hace
el worker. El *debug de modo-prueba a filesystem* (`--debug-dir`) lo escribe el `.py`;
lo importante siempre va a DB en el worker.

---

## Vendoring (cómo lo consumen los proyectos)

```
☁️ GitHub privado ←→ E:\ocr-core (FUERA de OneDrive, único lugar de edición + .git)
                          │ actualizar_core.ps1 vN  (copia el árbol del tag vN)
                          ▼
   <proyecto>\core_vendor\      (copia fijada vN, sin .git, en OneDrive, offline-ready)
   <proyecto>\core_version.txt  (qué versión tiene vendorizada)
```

- **Publicar versión del core:** desde `E:\ocr-core` → `.\bump_core.ps1 -Version vN`.
- **Vendorizar en un proyecto:** desde la raíz del proyecto → `.\actualizar_core.ps1 -Version vN`.
- **Rollback de un proyecto:** `.\actualizar_core.ps1 -Version v<anterior>` (no afecta al otro).
- Cada proyecto hace `require_once` de `core_vendor/…` vía un `core_bootstrap.php`
  propio (se cablea en Fase 1, junto con el rewire de `worker.php`).

Ver `GUIA_GIT.md` para el detalle de comandos git.

---

## Versionado

Un `VERSION` único del core. Tags `v1, v2, …` (uno por release). `v0` = scaffold de
Fase 0 (sin motores). El primer motor (agy) sale en `v1` (Fase 1, vía `bump_core.ps1 v1`).
