# Run locally; credentials are read privately and only set in this process environment.
$ErrorActionPreference = 'Stop'
$senderAddress = Read-Host 'Gmail sender address'
$senderAppPassword = Read-Host 'Google app password (not your normal password)' -AsSecureString
$senderCredential = New-Object System.Management.Automation.PSCredential($senderAddress, $senderAppPassword)
$env:SMTP_HOST = 'smtp.gmail.com'
$env:SMTP_PORT = '587'
$env:SMTP_USERNAME = $senderAddress
$env:SMTP_FROM = $senderAddress
$env:SMTP_PASSWORD = $senderCredential.GetNetworkCredential().Password.Replace(' ', '')
$env:SMTP_USE_TLS = 'true'
$env:SMTP_USE_SSL = 'false'
if (-not $env:APP_BASE_URL) { $env:APP_BASE_URL = 'http://127.0.0.1:8000' }
try {
    Push-Location -LiteralPath $PSScriptRoot
    python main.py
} finally {
    Pop-Location
    Remove-Item Env:SMTP_PASSWORD -ErrorAction SilentlyContinue
}
