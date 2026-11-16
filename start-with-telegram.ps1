# Token is read privately and stored only in the child server environment.
$ErrorActionPreference = 'Stop'
$botToken = Read-Host 'Telegram bot token from @BotFather' -AsSecureString
$botCredential = New-Object System.Management.Automation.PSCredential('telegram', $botToken)
$env:TELEGRAM_BOT_TOKEN = $botCredential.GetNetworkCredential().Password.Trim()
# Ask on each launch so stale SMTP_HOST cannot silently skip Gmail credentials.
$senderAddress = (Read-Host 'Gmail sender address (press Enter to skip email)').Trim()
if ($senderAddress) {
    $senderPassword = Read-Host 'Gmail app password (not your Google password)' -AsSecureString
    $senderCredential = New-Object System.Management.Automation.PSCredential($senderAddress, $senderPassword)
    $env:SMTP_HOST = 'smtp.gmail.com'
    $env:SMTP_PORT = '587'
    $env:SMTP_USERNAME = $senderAddress
    $env:SMTP_FROM = $senderAddress
    $env:SMTP_PASSWORD = $senderCredential.GetNetworkCredential().Password.Replace(' ', '')
    $env:SMTP_USE_TLS = 'true'
    $env:SMTP_USE_SSL = 'false'
} elseif ($env:SMTP_HOST -and -not $env:SMTP_PASSWORD) {
    # Clear incomplete settings inherited from a prior run; .env may still configure SMTP.
    Remove-Item Env:SMTP_HOST, Env:SMTP_USERNAME, Env:SMTP_FROM -ErrorAction SilentlyContinue
}

try {
    Push-Location -LiteralPath $PSScriptRoot
    $port = if ($env:APP_PORT) { $env:APP_PORT } else { '8001' }
    if (-not $env:APP_BASE_URL) { $env:APP_BASE_URL = "http://127.0.0.1:$port" }
    Write-Host "Open http://127.0.0.1:$port"
    python -m uvicorn main:app --host 127.0.0.1 --port $port --reload
} finally {
    Pop-Location
    Remove-Item Env:TELEGRAM_BOT_TOKEN -ErrorAction SilentlyContinue
    Remove-Item Env:SMTP_PASSWORD -ErrorAction SilentlyContinue
}
