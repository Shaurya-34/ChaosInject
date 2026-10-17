param([string]$Python = "python", [switch]$FakeFixtures, [ValidateRange(1,65535)][int]$ApiPort = 8000, [ValidateRange(1,65535)][int]$TargetPort = 8090)
$ErrorActionPreference = "Stop"
$taskRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $taskRoot
if (Test-Path -LiteralPath ".venv\Scripts\python.exe") { $Python = Join-Path $taskRoot ".venv\Scripts\python.exe" }
if (-not (Test-Path -LiteralPath "frontend\dist\index.html")) { throw "Run setup and frontend build first (see README)." }
if (-not $env:OPERATOR_TOKEN) {
$bytes = New-Object byte[] 32
$taskRng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
$taskRng.GetBytes($bytes)
$taskRng.Dispose()
$env:OPERATOR_TOKEN = [Convert]::ToBase64String($bytes)
}
$env:DATA_MODE = if ($FakeFixtures) { "fixture" } else { "demo" }
$env:FAKE_RUNTIME_ENABLED = if ($FakeFixtures) { "true" } else { "false" }
$env:LOCAL_API_PORT = [string]$ApiPort
$env:DEMO_TARGET_PORT = [string]$TargetPort
$taskProcesses = @()
try {
    $taskProcesses += Start-Process -FilePath $Python -ArgumentList @("-m","uvicorn","app.demo_target:app","--host","127.0.0.1","--port",([string]$TargetPort),"--no-access-log","--log-level","critical") -WindowStyle Hidden -PassThru
    $taskProcesses += Start-Process -FilePath $Python -ArgumentList @("-m","uvicorn","app.main:create_app","--factory","--host","127.0.0.1","--port",([string]$ApiPort),"--no-access-log","--log-level","critical") -WindowStyle Hidden -PassThru
    Write-Host "Open http://127.0.0.1:$ApiPort and paste this temporary operator token:"
    Write-Host $env:OPERATOR_TOKEN
    Write-Host "LOCAL DEMO: Check your duration. Up to 60 seconds passes validation; longer requests latch STOP. No outage is executed. Ctrl+C stops both processes."
    while ($true) { Start-Sleep -Seconds 1; foreach ($taskProcess in $taskProcesses) { if ($taskProcess.HasExited) { throw "A demo process exited. Check whether ports 8000/8090 are already occupied." } } }
} finally {
    foreach ($taskProcess in $taskProcesses) { if (-not $taskProcess.HasExited) { & taskkill.exe /PID $taskProcess.Id /T /F 2>$null | Out-Null } }
    Remove-Item Env:OPERATOR_TOKEN -ErrorAction SilentlyContinue
}
