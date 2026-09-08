# Local Futures Trader — PowerShell launcher (+ setup assistant)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

Write-Host "=== Local Futures Trader Launcher ===" -ForegroundColor Cyan

$pyCmd = $null
$pyArgs = @()
$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (Test-Path -LiteralPath $venvPython -PathType Leaf) {
  # Match start.bat: an existing project environment is self-sufficient and
  # must not depend on a global Python still being present in PATH.
  $pyCmd = $venvPython
} elseif (Get-Command py -ErrorAction SilentlyContinue) {
  $pyCmd = "py"
  $pyArgs = @("-3")
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
  $pyCmd = "python"
} else {
  Write-Error "Python not found. Install Python 3.11+."
}

$launch = Join-Path $PSScriptRoot "scripts\launch.py"
if ($args -contains "setup") {
  & $pyCmd @pyArgs $launch --setup @($args | Where-Object { $_ -ne "setup" })
} else {
  & $pyCmd @pyArgs $launch @args
}
exit $LASTEXITCODE
