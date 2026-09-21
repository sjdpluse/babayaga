$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\.." )).Path
$secretDir = Join-Path $env:LOCALAPPDATA "Babayaga"
$configPath = Join-Path $secretDir "mt5-agent-config.json"
$passwordPath = Join-Path $secretDir "mt5-password.dpapi"
$tokenPath = Join-Path $secretDir "agent-token.dpapi"

if (-not (Test-Path $configPath)) { throw "Missing Babayaga config: $configPath" }
if (-not (Test-Path $passwordPath)) { throw "Missing encrypted MT5 password" }
if (-not (Test-Path $tokenPath)) { throw "Missing encrypted agent token" }

$config = Get-Content $configPath -Raw | ConvertFrom-Json

$passwordCipher = (Get-Content $passwordPath -Raw).Trim()
$tokenCipher = (Get-Content $tokenPath -Raw).Trim()
if ([string]::IsNullOrWhiteSpace($passwordCipher)) { throw "Encrypted MT5 password is empty" }
if ([string]::IsNullOrWhiteSpace($tokenCipher)) { throw "Encrypted agent token is empty" }

$mt5Secure = ConvertTo-SecureString -String $passwordCipher
$tokenSecure = ConvertTo-SecureString -String $tokenCipher
$env:MT5_PASSWORD = [System.Net.NetworkCredential]::new("", $mt5Secure).Password
$env:MT5_AGENT_TOKEN = [System.Net.NetworkCredential]::new("", $tokenSecure).Password

$env:MT5_LOGIN = [string]$config.login
$env:MT5_SERVER = [string]$config.server
$env:MT5_TERMINAL_PATH = [string]$config.terminal_path
$env:MT5_MODE = "demo"
$env:ALLOW_LIVE_TRADING = "false"
$env:MT5_ALLOWED_SYMBOLS = "XAUUSD"
$env:MT5_SYMBOL_MAP = '{"XAUUSD":"XAUUSD.m"}'
$env:MT5_AGENT_HOST = "127.0.0.1"
$env:MT5_AGENT_PORT = "8787"
$env:MT5_AGENT_URL = "http://127.0.0.1:8787"
$env:MT5_SERVER_UTC_OFFSET_SECONDS = "10800"
$env:MT5_STATE_DIR = [string]$config.state_dir
$env:MT5_COMMISSION_PER_LOT = "0"
$env:MT5_MAX_SPREAD_POINTS = "50"
$env:MT5_RESEARCH_SWAP_LONG_PER_LOT_DAY = "71.04"
$env:MT5_RESEARCH_SWAP_SHORT_PER_LOT_DAY = "84.12"
$env:MT5_RESEARCH_ROLLOVER_UTC_HOUR = "19"
$env:MT5_RESEARCH_TRIPLE_WEEKDAY = "2"

$tailscale = Get-Service -Name "Tailscale" -ErrorAction SilentlyContinue
if ($tailscale -and $tailscale.Status -ne "Running") {
    Start-Service -Name "Tailscale"
    Start-Sleep -Seconds 3
}

if (-not (Get-Process -Name "terminal64" -ErrorAction SilentlyContinue)) {
    Start-Process -FilePath $env:MT5_TERMINAL_PATH
    Start-Sleep -Seconds 15
}

Set-Location $repoRoot
$python = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { throw "Python venv not found: $python" }

& $python -m truetrade.execution.agent
exit $LASTEXITCODE
