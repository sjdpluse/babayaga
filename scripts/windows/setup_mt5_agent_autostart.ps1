$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\.." )).Path
$launcher = Join-Path $repoRoot "scripts\windows\run_mt5_agent.ps1"
$secretDir = Join-Path $env:LOCALAPPDATA "Babayaga"
$configPath = Join-Path $secretDir "mt5-agent-config.json"
$passwordPath = Join-Path $secretDir "mt5-password.dpapi"
$tokenPath = Join-Path $secretDir "agent-token.dpapi"
$taskName = "Babayaga MT5 Agent"

New-Item -ItemType Directory -Force -Path $secretDir | Out-Null

$login = Read-Host "MT5 demo login"
if ($login -notmatch '^\d+$') { throw "MT5 login must be numeric" }
$server = Read-Host "MT5 server (example: JustMarkets-Demo3)"
if ([string]::IsNullOrWhiteSpace($server)) { throw "MT5 server is required" }

$defaultTerminal = "C:\Program Files\MetaTrader 5\terminal64.exe"
$terminal = Read-Host "MT5 terminal path [$defaultTerminal]"
if ([string]::IsNullOrWhiteSpace($terminal)) { $terminal = $defaultTerminal }
if (-not (Test-Path $terminal)) { throw "MT5 terminal not found: $terminal" }

$stateDir = Join-Path $repoRoot ("data\mt5-demo-" + $login)

$password = Read-Host "MT5 password" -AsSecureString
$token = Read-Host "MT5 Agent Token (same value as Railway)" -AsSecureString

$password | ConvertFrom-SecureString | Set-Content -Encoding ASCII $passwordPath
$token | ConvertFrom-SecureString | Set-Content -Encoding ASCII $tokenPath

[ordered]@{
    login = [int64]$login
    server = $server
    terminal_path = $terminal
    state_dir = $stateDir
} | ConvertTo-Json | Set-Content -Encoding UTF8 $configPath

$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument ('-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + $launcher + '"') `
    -WorkingDirectory $repoRoot

$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -RestartCount 5 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0)

$principal = New-ScheduledTaskPrincipal `
    -UserId ("$env:USERDOMAIN\$env:USERNAME") `
    -LogonType Interactive `
    -RunLevel Highest

$task = New-ScheduledTask -Action $action -Trigger $trigger -Settings $settings -Principal $principal
Register-ScheduledTask -TaskName $taskName -InputObject $task -Force | Out-Null

Write-Host "Configured encrypted secrets in: $secretDir"
Write-Host "Registered task: $taskName"
Write-Host "Starting task now..."
Start-ScheduledTask -TaskName $taskName
Start-Sleep -Seconds 3
Get-ScheduledTask -TaskName $taskName | Select-Object TaskName, State
