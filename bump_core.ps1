#requires -Version 5.1
<#
.SYNOPSIS
  Publica una versión nueva del core: escribe VERSION, commitea, taggea y pushea.

.DESCRIPTION
  Se corre DESDE E:\ocr-core. Idempotencia: falla si el tag ya existe (usá el
  siguiente número). No reescribe historia. Si no hay remoto 'origin', commitea y
  taggea local y te dice cómo pushear.

.PARAMETER Version
  Etiqueta de versión, formato vN (ej: v1, v2).

.PARAMETER Message
  Mensaje corto opcional para el commit ("motor agy", "split http", etc).

.PARAMETER CorePath
  Raíz del repo core. Default: la carpeta donde vive este script.

.EXAMPLE
  .\bump_core.ps1 -Version v1 -Message "motor agy"
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidatePattern('^v\d+$')]
    [string]$Version,

    [string]$Message = "",

    [string]$CorePath = $PSScriptRoot
)

$ErrorActionPreference = 'Stop'

function Fail($msg) { Write-Host "ERROR: $msg" -ForegroundColor Red; exit 1 }
function WriteTextNoBom($path, $text) {
    [System.IO.File]::WriteAllText($path, $text, (New-Object System.Text.UTF8Encoding($false)))
}

# --- 1. Validaciones ---
if (-not (Test-Path (Join-Path $CorePath '.git'))) {
    Fail "no parece un repo git: $CorePath. Corré 'git init' primero (ver GUIA_GIT.md)."
}
Set-Location $CorePath

$gitName  = (git config user.name)
$gitEmail = (git config user.email)
if ([string]::IsNullOrWhiteSpace($gitName) -or [string]::IsNullOrWhiteSpace($gitEmail)) {
    Fail "git user.name/email sin setear. Ver GUIA_GIT.md (paso 'Identidad')."
}

$existing = git tag -l $Version
if ($existing -eq $Version) {
    Fail "el tag '$Version' ya existe. Usá el siguiente número."
}

# --- 2. Escribir VERSION (UTF-8 sin BOM) ---
WriteTextNoBom (Join-Path $CorePath 'VERSION') "$Version`n"

# --- 3. Commit + tag ---
git add -A
$commitMsg = if ($Message -ne "") { "core ${Version}: $Message" } else { "core $Version" }
git commit -m $commitMsg
if ($LASTEXITCODE -ne 0) { Fail "git commit falló (¿no había cambios para commitear? ¿identidad?)." }
git tag $Version
if ($LASTEXITCODE -ne 0) { Fail "git tag falló." }

# --- 4. Push (si hay remoto 'origin') ---
$remotes = @(git remote)
if ($remotes -contains 'origin') {
    git push origin HEAD
    if ($LASTEXITCODE -ne 0) { Fail "git push (rama) falló." }
    git push origin $Version
    if ($LASTEXITCODE -ne 0) { Fail "git push (tag) falló." }
    Write-Host "OK: $Version commiteado, taggeado y pusheado a origin." -ForegroundColor Green
} else {
    Write-Host "OK local: $Version commiteado y taggeado, SIN remoto 'origin'." -ForegroundColor Yellow
    Write-Host "Para pushear:" -ForegroundColor Yellow
    Write-Host "  git remote add origin <URL-del-repo-privado>"
    Write-Host "  git push -u origin HEAD"
    Write-Host "  git push origin $Version"
}
