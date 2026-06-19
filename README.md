# ocr-core

Núcleo compartido de los proyectos de OCR-por-LLM **prensadelplata** (A) y
**transcriptor-manuscritos-v2** (B). Contiene motores de proveedor e infra común,
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
└── utils/                  # qa_base, sandbox, cuota_windows, sentinels (Fase 3)
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

// manuscritos-v2  → eventos
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
