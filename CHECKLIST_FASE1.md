# Checklist Fase 1 — motor AGY en el core

Piloto vertical: el motor `agy` sale de prensadelplata y pasa al core; prensa lo
consume idéntico y manuscritos-v2 reemplaza su agy experimental por el del core.
Valida mecanismo + contrato + vendoring + rollback.

> Las reglas git (commit/tag/push) y los pasos costosos (transcribir 1 imagen)
> son **tuyos**, igual que en Fase 0. Lo inerte ya está hecho y verificado abajo.

---

## ✅ Ya hecho y verificado (por Claude)

**Core `E:\ocr-core`:**
- `motores/lib_agy.php` — semilla de prensa, **generalizada**:
  - `agyLog('AGY', …)` → `coreLog('agy', …)` vía la costura `coreLogSink`
    (`function_exists`, guardado contra redeclare de Fase 2+).
  - Quitado `require_once db_logger.php` (era archivo de proyecto; el core no
    requiere nada del consumidor).
  - Eliminado `$proyectoRoot = dirname(__DIR__)`: el `.py` se resuelve como
    **sibling** (`__DIR__/transcribir_agy.py`) y la base del workdir efímero
    entra por argumento (`workdir_base` → si falta, `$resultadosDir` → si falta,
    `sys_get_temp_dir()`; siempre cuelga `agy_subprocess/`).
  - Firma de `ejecutarAgy()` **intacta** (contrato) y struct de retorno idéntico.
  - `php -l` OK.
- `motores/transcribir_agy.py` — **copia verbatim** del `.py` de prensa
  (sha1 idéntico). Ya estaba limpio: todos los paths entran por `--imagen
  --prompt --salida-json --sandbox-dir --debug-dir`; **no** hay `PROJECT_ROOT`
  hardcodeado. `ast.parse` OK.
- `README.md`: agregada la clave `workdir_base` a la tabla del contrato `$agyConfig`.

**Prensa `…\prensadelplata\WEB`:**
- `core_bootstrap.php` — define `coreLogSink → logDebug(strtoupper($engine), …)`
  (preserva el canal `AGY`) y `require_once core_vendor/motores/lib_agy.php`.
  `php -l` OK.
- `actualizar_core.ps1` — plantilla de vendoring copiada a la raíz de la app.

**Manuscritos-v2 `…\transcriptor-manuscritos-v2\web`:**
- `core_bootstrap.php` — define `coreLogSink → logEvento($engine, …)` (tabla
  `eventos`) y `require_once core_vendor/motores/lib_agy.php`. `php -l` OK.
- `actualizar_core.ps1` — plantilla copiada a `web\`.

> **Ubicación elegida** (no la raíz que dibujaba el plan): `core_vendor/`,
> `core_bootstrap.php` y `actualizar_core.ps1` viven **junto al código de la app**
> — `WEB\` en prensa y `web\` en v2 — porque ahí están worker/orquestador/config
> y el `require` queda de un solo nivel. Ver **Decisión D3**.

**Nada de esto está activo todavía:** los bootstraps no los requiere nadie hasta
el rewire (pasos 4 y 6); `core_vendor/` no existe hasta vendorizar (pasos 3 y 5);
los workers no se reiniciaron.

---

## ⏳ Tus pasos

### 1. Publicar el core v1 (git)
El working tree del core ya tiene staged-listo `motores/lib_agy.php`,
`motores/transcribir_agy.py`, el `README.md` y este checklist. `bump_core.ps1`
hace `VERSION=v1` + commit + `tag v1` + push:

```powershell
cd E:\ocr-core
.\bump_core.ps1 -Version v1 -Message "motor agy"
```
(Verificá después: `git -C E:\ocr-core tag` debe listar `v1`; el push va a
`origin` = github.com/tomasviera/ocr-core.)

### 2. Vendorizar v1 en prensa
```powershell
cd E:\OneDrive\Programacion\OCR-Gemini\prensadelplata\WEB
.\actualizar_core.ps1 -Version v1
```
Crea `WEB\core_vendor\{motores,…}` + `core_version.txt=v1` con el header
`GENERADO` inyectado. (No habrá `core_vendor.prev` porque es la primera vez.)

### 3. Rewire de prensa (aprobación + reinicio de workers)
**Toca `worker.php` → requiere tu OK explícito y reinicio de workers.** Es UNA línea:

`worker.php:47`
```diff
- require_once __DIR__ . '/includes/lib_agy.php';
+ require_once __DIR__ . '/core_bootstrap.php';
```
El resto de `worker.php` no se toca: `$agyConfig` y la llamada a `ejecutarAgy`
quedan idénticas. El core deriva el workdir de `$resultadosDir` (= `WEB/temp`),
así que la ruta efímera es la misma de siempre (`WEB/temp/agy_subprocess/`).
- `includes\lib_agy.php` viejo queda en disco **dormido** (ya no lo requiere
  nadie) como referencia/rollback. No lo borres.
- **Reiniciá los workers** (lo hacés vos; puede haber jobs CLI en curso).

### 4. Validar prensa con 1 PÁGINA (regla #9)
Transcribir **1 página** vía agy y verificar, sin regresión:
- fila nueva en `api_calls` (tokens, `api_estado`, perfil);
- `entradas` con el backup automático (`ent_estado='Backup'` de la versión previa)
  y los estados QA esperados;
- en `transcripcion_debug_log`, los eventos del canal `AGY` siguen apareciendo
  (confirma que la costura `coreLog→logDebug` quedó bien cableada).

### 5. Vendorizar v1 en manuscritos-v2
```powershell
cd E:\OneDrive\Programacion\transcriptor-manuscritos-v2\web
.\actualizar_core.ps1 -Version v1
```

### 6. Rewire de v2 (reemplaza el agy experimental)
a. **Backup de los viejos** (NO borrar):
```powershell
cd E:\OneDrive\Programacion\transcriptor-manuscritos-v2
Rename-Item web\includes\lib_agy.php  lib_agy.php.orig
Rename-Item scripts\transcribir_agy.py transcribir_agy.py.orig
```
b. **Swap del require en los DOS callers** (ambos cargan el agy viejo):

`web\includes\lib_orquestador.php:27`
```diff
- require_once __DIR__ . '/lib_agy.php';      // ejecutarAgy() — motor Antigravity CLI (Etapa K)
+ require_once dirname(__DIR__) . '/core_bootstrap.php';  // ejecutarAgy() — motor agy del core
```
`web\includes\lib_postprocesador.php:25`
```diff
- require_once __DIR__ . '/lib_agy.php';           // ejecutarAgy()
+ require_once dirname(__DIR__) . '/core_bootstrap.php';  // ejecutarAgy() — motor agy del core
```
c. **`web\config\settings.php` → `config['agy']`** (ver **D1/D2**). El core NO
resuelve rutas relativas: `sandbox_dir` DEBE ser absoluto y estar en
`trustedWorkspaces`. Quedaría así:
```php
    'agy' => [
        'enabled'      => true,
        // Absoluto y en trustedWorkspaces de agy (paso 'd' de abajo). SIN home_dir
        // → usa el HOME global compartido (misma auth AGY que el original).
        'sandbox_dir'  => 'E:\\OneDrive\\Programacion\\transcriptor-manuscritos-v2\\temp\\agy_sandbox',
        'modelo_agy'   => '<MODELO_AGY>',   // D2: modelo exacto para settings.json["model"]
        'workdir_base' => 'E:\\OneDrive\\Programacion\\transcriptor-manuscritos-v2\\temp',
    ],
```
d. **Crear + confiar el sandbox** (v2 no tiene `temp/` — Fase 0 lo excluyó):
```powershell
New-Item -ItemType Directory -Force E:\OneDrive\Programacion\transcriptor-manuscritos-v2\temp\agy_sandbox | Out-Null
```
   Después, en una sesión interactiva de agy, agregar ese path absoluto a
   `trustedWorkspaces` vía `/permissions` (igual que hiciste con el sandbox de
   prensa). El `temp\agy_subprocess\` lo crea solo el core.

### 7. Validar v2 con 1 IMAGEN, luego 1 LEGAJO chico (regla #9)
- 1 imagen vía agy → verificar fila en `llamadas` (tokens, `engine='agy'`,
  veredicto) + `transcripciones_raw` (texto válido) + eventos en `eventos`
  (canal `agy`, confirma `coreLog→logEvento`).
- Recién si sale OK: 1 legajo chico, y confirmar que postproceso + export `.docx`
  siguen funcionando (el postprocesador también usa el motor agy del core).

> **No corras prensa y v2 a la vez:** ambos heredan `web_port=8082` (Fase 0). Si
> conviven, cambiá uno.

### 8. Test de rollback (cuando exista un 2º tag)
El rollback por tag (`actualizar_core.ps1 -Version v<anterior>`) necesita ≥2 tags;
`v0` no tiene motores, así que el test real de tag-rollback queda para Fase 2.
Para Fase 1, el rollback es reversible a mano:
- prensa: volver `worker.php:47` al `require includes/lib_agy.php` + reiniciar workers;
- v2: volver los dos `require` y `Rename-Item` los `.orig` a su nombre.
Confirmá que revertir uno **no afecta** al otro (cada proyecto tiene su `core_version.txt`).

---

## 📌 Decisiones a confirmar (defaults entre paréntesis)

- **D1 — sandbox absoluto de v2** *(default: `…\transcriptor-manuscritos-v2\temp\agy_sandbox`,
  en OneDrive, paralelo a prensa)*. Si preferís un sandbox fuera de OneDrive (evita
  sync del `.agy_last_status.json` por corrida), decímelo y ajusto `sandbox_dir`
  + `workdir_base`.
- **D2 — `modelo_agy` de v2** *(sin default)*: el string exacto del modelo agy para
  manuscritos (ej. `"Gemini 3.5 Flash (Low)"`). En prensa lo mapea `agyMapearModelo`;
  v2 no tiene ese mapeo, así que va literal en `config['agy']['modelo_agy']`.
- **D3 — ubicación de `core_vendor/`** *(default: junto a la app — `web\` en v2,
  `WEB\` en prensa)*. El plan lo dibujaba en la raíz del proyecto v2; elegí `web\`
  porque ahí viven worker/orquestador/config y el `require` queda de un nivel. Si
  lo querés en la raíz, muevo la plantilla y ajusto el `require` del bootstrap.

## 📎 Auth AGY (recordatorio de Fase 0 — NO tocar)
La auth real vive fuera del proyecto (`C:\Users\Tomás\.gemini`, `E:\gemini-profiles`,
`E:\chrome-cdp-profile`). `config['agy']` **sin** `home_dir` → v2 usa el HOME global
y comparte la misma auth que el original. No reautenticar ni copiar/mover nada.
