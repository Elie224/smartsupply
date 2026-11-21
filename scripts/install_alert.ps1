# Installe l'alerte Windows du cycle automatique SmartSupply (tâche planifiée).
#
#   powershell -ExecutionPolicy Bypass -File .\scripts\install_alert.ps1 -Python $py
#
# La tâche "SmartSupply - alerte" lance `python -m smartsupply.health --notify` :
#   - 5 minutes après l'ouverture de ta session Windows (le temps que Docker démarre) ;
#   - chaque jour à 9 h (ou dès que le PC se réveille, s'il dormait à 9 h).
# Si tout va bien : rien ne s'affiche. Sinon : une fenêtre explique le problème et quoi faire.
#
# Désinstaller : Unregister-ScheduledTask -TaskName "SmartSupply - alerte" -Confirm:$false
# Tester tout de suite : Start-ScheduledTask -TaskName "SmartSupply - alerte"

param(
    [Parameter(Mandatory = $true)]
    [string]$Python            # chemin de python.exe (celui qui a smartsupply installé)
)

$ErrorActionPreference = "Stop"
$TaskName = "SmartSupply - alerte"
$ProjectRoot = Split-Path -Parent $PSScriptRoot

$python = (Resolve-Path $Python).Path
# pythonw.exe = même Python, sans fenêtre noire qui clignote.
$pythonw = Join-Path (Split-Path -Parent $python) "pythonw.exe"
if (-not (Test-Path $pythonw)) { $pythonw = $python }

# Vérifie que ce Python connaît bien le projet avant d'installer quoi que ce soit.
& $python -c "import smartsupply.health" 2>$null
if ($LASTEXITCODE -ne 0) {
    throw "Ce Python ne trouve pas le module smartsupply : $python"
}

$action = New-ScheduledTaskAction -Execute $pythonw `
    -Argument "-m smartsupply.health --notify" -WorkingDirectory $ProjectRoot

$atLogon = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$atLogon.Delay = "PT5M"
$daily = New-ScheduledTaskTrigger -Daily -At 9:00am

$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30)

# Sans -User : la tâche tourne dans TA session (nécessaire pour afficher la fenêtre).
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger @($atLogon, $daily) `
    -Settings $settings -Description "Alerte si le cycle automatique SmartSupply n'a pas tourné" `
    -Force | Out-Null

Write-Host "Tâche planifiée installée : $TaskName"
Write-Host "  Python   : $pythonw"
Write-Host "  Dossier  : $ProjectRoot"
Write-Host "  Quand    : 5 min après l'ouverture de session + chaque jour à 9 h"
