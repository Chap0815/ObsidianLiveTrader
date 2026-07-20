<#
  create-shortcut.ps1 — legt einen doppelklickbaren Launcher "Obsidian Live
  Trader" an, der start.bat startet und app\obsidian.ico als Icon nutzt
  (Taskbar + Explorer). Erzeugt die Verknuepfung im Projektordner UND auf dem
  Desktop. Jederzeit gefahrlos erneut ausfuehrbar (ueberschreibt die .lnk).

  Aufruf (im Projektordner):  powershell -ExecutionPolicy Bypass -File scripts\create-shortcut.ps1
#>
$ErrorActionPreference = 'Stop'

$root   = Split-Path -Parent $PSScriptRoot          # Projekt-Root (= scripts\..)
$target = Join-Path $root 'start.bat'
$icon   = Join-Path $root 'app\obsidian.ico'
$name   = 'Obsidian Live Trader.lnk'

if (-not (Test-Path $target)) { throw "start.bat nicht gefunden: $target" }
if (-not (Test-Path $icon))   { throw "Icon nicht gefunden: $icon" }

$targets = @($root, [Environment]::GetFolderPath('Desktop'))
$ws = New-Object -ComObject WScript.Shell
foreach ($dir in $targets) {
    $path = Join-Path $dir $name
    $lnk = $ws.CreateShortcut($path)
    $lnk.TargetPath       = $target
    $lnk.WorkingDirectory = $root
    $lnk.IconLocation     = "$icon,0"
    $lnk.WindowStyle      = 1   # normales Fenster — Server-Konsole bleibt sichtbar
    $lnk.Description       = 'Obsidian Live Trader — Local Futures Cockpit'
    $lnk.Save()
    Write-Host "Verknuepfung erstellt: $path"
}
Write-Host ""
Write-Host "Fertig. Doppelklick auf 'Obsidian Live Trader' startet den Server."
