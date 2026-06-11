$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

python -m py_compile .\fusion360_updater_gui.py
pyinstaller --noconfirm --clean --onefile --windowed --name Fusion360Updater .\fusion360_updater_gui.py

Write-Host "Built: $root\dist\Fusion360Updater.exe"
