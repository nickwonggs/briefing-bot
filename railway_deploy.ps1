# railway_deploy.ps1 — Run this once in PowerShell to deploy to Railway.
# Reads all secrets from .env — never paste them manually.

Set-Location "C:\Users\wongn\Desktop\briefing-bot"

# Refresh PATH so C:\Users\wongn\AppData\Roaming\npm\railway.cmd is found
$env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")

# Read .env into a hashtable
$envVars = @{}
Get-Content ".env" | ForEach-Object {
    if ($_ -match "^\s*([^#][^=]+)=(.*)$") {
        $key = $matches[1].Trim()
        $val = $matches[2].Trim().Trim("'").Trim('"')
        $envVars[$key] = $val
    }
}

Write-Host "Setting Railway environment variables..."

C:\Users\wongn\AppData\Roaming\npm\railway.cmd variables set TZ=Asia/Singapore
C:\Users\wongn\AppData\Roaming\npm\railway.cmd variables set "TELEGRAM_BOT_TOKEN=$($envVars['TELEGRAM_BOT_TOKEN'])"
C:\Users\wongn\AppData\Roaming\npm\railway.cmd variables set "TELEGRAM_CHAT_ID=$($envVars['TELEGRAM_CHAT_ID'])"
C:\Users\wongn\AppData\Roaming\npm\railway.cmd variables set "ENCRYPTION_KEY=$($envVars['ENCRYPTION_KEY'])"
C:\Users\wongn\AppData\Roaming\npm\railway.cmd variables set "GOOGLE_CREDENTIALS_JSON=$($envVars['GOOGLE_CREDENTIALS_JSON'])"
C:\Users\wongn\AppData\Roaming\npm\railway.cmd variables set "GOOGLE_TOKEN_JSON=$($envVars['GOOGLE_TOKEN_JSON'])"

Write-Host ""
Write-Host "Confirming variables are set..."
C:\Users\wongn\AppData\Roaming\npm\railway.cmd variables

Write-Host ""
Write-Host "Deploying to Railway..."
C:\Users\wongn\AppData\Roaming\npm\railway.cmd up --detach

Write-Host ""
Write-Host "Deploy started. Streaming logs (Ctrl+C to stop watching)..."
Start-Sleep -Seconds 10
C:\Users\wongn\AppData\Roaming\npm\railway.cmd logs
