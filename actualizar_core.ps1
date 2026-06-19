#requires -Version 5.1
<#
.SYNOPSIS
  Vendoriza (copia fijada) una versión del core dentro de un proyecto consumidor.

.DESCRIPTION
  Este script es la PLANTILLA que se copia a la raíz de cada proyecto consumidor
  (prensadelplata\WEB, transcriptor-manuscritos-v2). Corrido desde la raíz de un
  proyecto, extrae el árbol del core en el tag <Version> y lo copia a
  <proyecto>\core_vendor\, inyecta el header "GENERADO — no editar acá" en cada
  .php/.py vendorizado, y escribe core_version.txt.

  - El core_vendor anterior se preserva como core_vendor.prev (red de seguridad;
    el rollback real es por tag: correr este script con un vN anterior).
  - Rollback: .\actualizar_core.ps1 -Version v<anterior>

.PARAMETER Version
  Tag del core a vendorizar, formato vN (ej: v1).

.PARAMETER CorePath
  Raíz del repo core. Default: E:\ocr-core.

.PARAMETER ProjectPath
  Raíz del proyecto consumidor. Default: la carpeta donde vive este script.

.EXAMPLE
  .\actualizar_core.ps1 -Version v1
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidatePattern('^v\d+$')]
    [string]$Version,

    [string]$CorePath = "E:\ocr-core",

    [string]$ProjectPath = $PSScriptRoot
)

$ErrorActionPreference = 'Stop'

function Fail($msg) { Write-Host "ERROR: $msg" -ForegroundColor Red; exit 1 }
function WriteTextNoBom($path, $text) {
    [System.IO.File]::WriteAllText($path, $text, (New-Object System.Text.UTF8Encoding($false)))
}

$MARKER = "GENERADO — no editar acá, editar E:\ocr-core"

# Inyecta el header GENERADO en un archivo .php/.py, idempotente.
function Inject-Header($path, $ver) {
    $raw = [System.IO.File]::ReadAllText($path)
    if ($raw.Contains($MARKER)) { return }   # ya tiene header
    $ext = [System.IO.Path]::GetExtension($path).ToLower()

    if ($ext -eq '.php') {
        $comment = "// $MARKER (vendorizado $ver)"
        $idx = $raw.IndexOf('<?php')
        if ($idx -ge 0) {
            $cut = $idx + 5
            $new = $raw.Substring(0, $cut) + "`r`n" + $comment + $raw.Substring($cut)
        } else {
            $new = "<?php`r`n$comment`r`n?>`r`n" + $raw
        }
    }
    elseif ($ext -eq '.py') {
        $comment = "# $MARKER (vendorizado $ver)"
        $lines = $raw -split "`n"
        # Insertar tras shebang y/o línea de coding, si las hay.
        $insertAt = 0
        if ($lines.Count -gt 0 -and $lines[0].TrimEnd("`r").StartsWith('#!')) { $insertAt = 1 }
        if ($lines.Count -gt $insertAt -and ($lines[$insertAt].TrimEnd("`r") -match 'coding[:=]\s*[-\w.]+')) { $insertAt++ }
        $head = @(); if ($insertAt -gt 0) { $head = $lines[0..($insertAt - 1)] }
        $tail = $lines[$insertAt..($lines.Count - 1)]
        $new = (($head + $comment + $tail) -join "`n")
    }
    else { return }

    WriteTextNoBom $path $new
}

# --- 1. Validaciones ---
if (-not (Test-Path (Join-Path $CorePath '.git'))) {
    Fail "el core no es un repo git: $CorePath"
}
if (-not (Test-Path $ProjectPath)) { Fail "el proyecto no existe: $ProjectPath" }

$coreFull = (Resolve-Path $CorePath).Path.TrimEnd('\')
$projFull = (Resolve-Path $ProjectPath).Path.TrimEnd('\')
if ($coreFull -eq $projFull) {
    Fail "ProjectPath == CorePath. Copiá este script a la raíz del proyecto consumidor y corrélo desde ahí (o pasá -ProjectPath)."
}

Push-Location $CorePath
$tagExists = (git tag -l $Version)
Pop-Location
if ($tagExists -ne $Version) {
    Fail "el tag '$Version' no existe en el core. Listá con:  git -C `"$CorePath`" tag"
}

# --- 2. Extraer el árbol del tag a un temp ---
$tmp = Join-Path $env:TEMP ("ocr_core_vendor_" + [System.Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $tmp -Force | Out-Null

try {
    $archive = Join-Path $tmp "core.tar"
    git -C $CorePath archive --format=tar -o $archive $Version
    if ($LASTEXITCODE -ne 0) { Fail "git archive falló para el tag $Version." }
    tar -x -f $archive -C $tmp
    if ($LASTEXITCODE -ne 0) { Fail "tar -x falló (¿tar disponible?)." }
    Remove-Item $archive -Force

    # --- 3. Rotar core_vendor anterior a core_vendor.prev ---
    $vendor     = Join-Path $ProjectPath 'core_vendor'
    $vendorPrev = Join-Path $ProjectPath 'core_vendor.prev'
    if (Test-Path $vendor) {
        if (Test-Path $vendorPrev) { Remove-Item $vendorPrev -Recurse -Force }
        Rename-Item $vendor $vendorPrev
    }
    New-Item -ItemType Directory -Path $vendor -Force | Out-Null

    # --- 4. Copiar motores/infra/utils + VERSION + README ---
    foreach ($d in @('motores', 'infra', 'utils')) {
        $src = Join-Path $tmp $d
        if (Test-Path $src) { Copy-Item $src -Destination $vendor -Recurse -Force }
    }
    foreach ($f in @('VERSION', 'README.md')) {
        $src = Join-Path $tmp $f
        if (Test-Path $src) { Copy-Item $src -Destination $vendor -Force }
    }

    # --- 5. Inyectar header GENERADO en cada .php/.py ---
    $files = @(Get-ChildItem -Path $vendor -Recurse -Include *.php, *.py -File)
    foreach ($file in $files) { Inject-Header $file.FullName $Version }

    # --- 6. core_version.txt (UTF-8 sin BOM) ---
    WriteTextNoBom (Join-Path $ProjectPath 'core_version.txt') "$Version`n"

    Write-Host "OK: core $Version vendorizado en $vendor" -ForegroundColor Green
    Write-Host "    $($files.Count) archivo(s) .php/.py con header GENERADO."
    if (Test-Path $vendorPrev) {
        Write-Host "    Backup del vendor anterior en core_vendor.prev (red de seguridad)."
    }
}
finally {
    if (Test-Path $tmp) { Remove-Item $tmp -Recurse -Force }
}
