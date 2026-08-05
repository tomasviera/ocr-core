<#
    agy_clipboard.ps1 — manipulador del portapapeles para el `cmd_mode = "paste"`
    de transcribir_agy.py (bump v34).

    WORKAROUND TEMPORAL — bug upstream agy #735 (el language server 1.1.10 manda
    `inline_data` de longitud 0 cuando el modelo abre una imagen con `view_file`
    → INVALID_ARGUMENT 400). https://github.com/google-antigravity/antigravity-cli/issues/735
    El paste del TUI adjunta la imagen como *media del mensaje* y evita el
    converter roto. Si el bug se arregla upstream, este script queda huérfano
    junto con el modo `paste`.

    Adaptado (sin cambios de mecánica) de `set_clipboard.ps1` del test
    `temp/tests/2026-08-03_170000_agy_tui_paste_media/`, que validó la
    combinación ganadora (`-Mode file` + Ctrl+V) contra agy real.

    Modos:
      -Mode get                       imprime el TEXTO actual del portapapeles (vacío si no hay texto)
      -Mode text     -TextFile <path> restaura texto desde un archivo UTF-8 (evita quoting)
      -Mode file     -Path <img>      pone la LISTA DE ARCHIVOS (CF_HDROP) con <img>  ← el que usa producción
      -Mode image    -Path <img>      pone el BITMAP (CF_DIB) cargando <img>          ← sólo diagnóstico
      -Mode clear                     vacía el portapapeles

    OJO: el portapapeles es un recurso GLOBAL de la máquina. El .py lo pisa por
    la ventana más corta posible (poner file-drop → Ctrl+V → verificar chip →
    restaurar) y restaura SIEMPRE, también en los caminos de error. No hay lock:
    hoy hay un solo slot agy por host (decisión de diseño, ver notas/motor_agy.md).

    Nota: se invoca SIEMPRE con -STA (las APIs de portapapeles de WinForms lo
    exigen). Todos los SetDataObject van con copy=$true para que el contenido
    sobreviva a la muerte de este proceso (OleFlushClipboard).

    Códigos de salida: 0 OK · 1 error (mensaje por stderr).
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)]
    [ValidateSet('get','text','file','image','clear')]
    [string]$Mode,
    [string]$Path,
    [string]$TextFile
)

$ErrorActionPreference = 'Stop'

function Fail([string]$msg) {
    [Console]::Error.WriteLine("agy_clipboard: $msg")
    exit 1
}

try {
    Add-Type -AssemblyName System.Windows.Forms
    Add-Type -AssemblyName System.Drawing
} catch {
    Fail "no se pudieron cargar System.Windows.Forms/System.Drawing: $($_.Exception.Message)"
}

switch ($Mode) {

    'get' {
        $txt = ''
        try {
            if ([System.Windows.Forms.Clipboard]::ContainsText()) {
                $txt = [System.Windows.Forms.Clipboard]::GetText()
            }
        } catch { $txt = '' }
        # Se emite crudo por stdout; el .py lo guarda tal cual.
        [Console]::Out.Write($txt)
        exit 0
    }

    'clear' {
        try { [System.Windows.Forms.Clipboard]::Clear() } catch { Fail $_.Exception.Message }
        exit 0
    }

    'text' {
        if (-not $TextFile) { Fail "-Mode text requiere -TextFile" }
        if (-not (Test-Path -LiteralPath $TextFile)) { Fail "no existe TextFile: $TextFile" }
        $txt = [System.IO.File]::ReadAllText($TextFile, [System.Text.Encoding]::UTF8)
        if ([string]::IsNullOrEmpty($txt)) {
            # Nada que restaurar: dejamos el portapapeles vacío en vez de fallar.
            try { [System.Windows.Forms.Clipboard]::Clear() } catch {}
            exit 0
        }
        try {
            [System.Windows.Forms.Clipboard]::SetDataObject($txt, $true)
        } catch { Fail "SetDataObject(text) falló: $($_.Exception.Message)" }
        exit 0
    }

    'file' {
        if (-not $Path) { Fail "-Mode file requiere -Path" }
        if (-not (Test-Path -LiteralPath $Path)) { Fail "no existe Path: $Path" }
        $abs = (Resolve-Path -LiteralPath $Path).ProviderPath
        try {
            $col = New-Object System.Collections.Specialized.StringCollection
            [void]$col.Add($abs)
            $dobj = New-Object System.Windows.Forms.DataObject
            $dobj.SetFileDropList($col)
            [System.Windows.Forms.Clipboard]::SetDataObject($dobj, $true)
        } catch {
            # Fallback: el cmdlet nativo (PS 5.0+) también escribe CF_HDROP.
            try { Set-Clipboard -LiteralPath $abs }
            catch { Fail "no se pudo poner el file drop list: $($_.Exception.Message)" }
        }
        exit 0
    }

    'image' {
        if (-not $Path) { Fail "-Mode image requiere -Path" }
        if (-not (Test-Path -LiteralPath $Path)) { Fail "no existe Path: $Path" }
        $abs = (Resolve-Path -LiteralPath $Path).ProviderPath
        $img = $null
        try {
            # FromFile deja el archivo tomado hasta el Dispose; copiamos a un
            # Bitmap en memoria para poder liberarlo enseguida (el sandbox se
            # limpia entre corridas).
            $src = [System.Drawing.Image]::FromFile($abs)
            $img = New-Object System.Drawing.Bitmap $src
            $src.Dispose()
            [System.Windows.Forms.Clipboard]::SetDataObject($img, $true)
        } catch {
            Fail "SetDataObject(image) falló: $($_.Exception.Message)"
        } finally {
            if ($img) { $img.Dispose() }
        }
        exit 0
    }
}
