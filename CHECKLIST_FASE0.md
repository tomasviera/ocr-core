# Checklist Fase 0 — para Tomás

Estado al cierre de la sesión de implementación. Lo que sigue son **tus pasos**
(git/GitHub); el resto ya está hecho y verificado (ver abajo).

## ✅ Ya hecho y verificado (por Claude)

**Core `E:\ocr-core`:**
- Estructura `motores/ infra/ utils/` (vacías, `.gitkeep`) + `VERSION` (=`v0`).
- `.gitignore` (excluye `secrets*`, efímeros, vendor).
- `README.md` con los contratos: motor (firma PHP + `$cfg` + CLI del `.py` + struct
  de ~35 campos) y costura `coreLog` (`function_exists`), + reglas duras del core.
- `bump_core.ps1` y `actualizar_core.ps1` (UTF-8 con BOM, parsean OK).
  El vendoring se **probó end-to-end** en un sandbox descartable: archive→extracción→
  copia, inyección del header `GENERADO — no editar acá`, `core_version.txt`, rotación
  a `core_vendor.prev`, idempotencia. Todo OK.
- `GUIA_GIT.md` (referencia de comandos).
- `git init -b main` ya corrido (el repo existe; falta el primer commit).

**Copia nueva `transcriptor-manuscritos-v2`:**
- Código duplicado (86 MB vs 411 MB del original). Excluidos: `temp/ resultados/
  img-input/ img-rotadas/ backup/ __pycache__/` (pesados/generados) y `.gemini/
  .antigravitycli/ .claude/` (credenciales/estado). Incluido `normalizador-imagenes/`
  completo (con `modelo_orientacion.pkl`, 87 MB), como pediste.
- `web/config/secrets.php` → `transcriptor_manuscritos_v2`. También actualicé los
  *fallbacks* hardcodeados en `database.php`, `scripts/init_db.php` y
  `secrets.example.php` para que el v2 nunca pueda apuntar al original por accidente.
- DB `transcriptor_manuscritos_v2` creada y **clonada con todos los datos** del
  original (pg_dump/pg_restore). Verificado: las 14 tablas coinciden en conteo de filas;
  `database.php` del v2 resuelve a `transcriptor_manuscritos_v2`.

**Entorno Python:** el v2 usa el Python global (no hay venv); ya tiene `pyte` 0.8.2 y
`pywinpty` 3.0.3. **No hace falta `pip install`.**

## ⏳ Tus pasos (git / GitHub)

1. **Identidad git** (si `git config --global user.name` está vacío — lo estaba):
   ```powershell
   git config --global user.name  "Tomás"
   git config --global user.email "adolasi@gmail.com"
   ```
2. **Primer commit del core:**
   ```powershell
   cd E:\ocr-core
   git add -A
   git commit -m "core v0: scaffold (estructura + contratos + scripts)"
   ```
3. **Repo GitHub privado + push** (`gh` no está instalado → por web):
   - Crear repo **privado** vacío en <https://github.com/new> (nombre sugerido
     `ocr-core`; sin README/.gitignore/licencia).
   - ```powershell
     cd E:\ocr-core
     git remote add origin https://github.com/<usuario>/ocr-core.git
     git push -u origin main
     ```
   - (Alternativa con gh: `winget install GitHub.cli`, `gh auth login`,
     `gh repo create ocr-core --private --source . --remote origin --push`.)

## 📌 Notas para Fase 1 (no son de ahora)

- **Auth AGY del v2 — NO hace falta re-autenticar ni copiar nada.** La auth real vive
  fuera del proyecto, en lugares globales compartidos: `C:\Users\Tomás\.gemini\`
  (`oauth_creds.json` etc.), `E:\gemini-profiles\` (perfiles CLI) y `E:\chrome-cdp-profile\`
  (AI Studio). El `config['agy']` no pone `home_dir`, así que agy usa el HOME global → el
  v2 comparte la MISMA auth que el original, automáticamente. No toqué ninguno de esos
  archivos. (El `.gemini/settings.json` del proyecto es sólo un config de tools, lo restauré
  en el v2; no es credencial.)
- **Sandbox trusted de agy (Fase 1):** `config['agy']['sandbox_dir']='temp/agy_sandbox'`
  resuelve a una ruta ABSOLUTA distinta en el v2, que NO está en `trustedWorkspaces` de agy.
  En Fase 1 hay que agregar el sandbox del v2 vía `/permissions` (paso tuyo) o apuntar
  `sandbox_dir` a una ruta absoluta ya confiada.
- **Puerto web:** el v2 hereda `web_port=8082` (igual que el original). No corras los dos
  servidores a la vez; cuando convivan, cambiá uno.
- **Reinicio de workers:** cuando en Fase 1 se cablee el vendoring en `worker.php`,
  los workers necesitan reinicio (lo hacés vos). En Fase 0 **no** se tocó ningún worker.
- **Borrado del original:** dijiste que vas a borrar `transcriptor-manuscritos` cuando
  el v2 lo reemplace. La DB original sigue intacta y separada (`transcriptor_manuscritos`),
  así que sirve de red hasta que valides el v2 end-to-end.
