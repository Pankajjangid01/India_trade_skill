# Starts the backend and the UI in the background and opens an ngrok tunnel to the UI (Windows PowerShell).
#   .\tunnel.ps1          start (or restart) all three; prints the public URL
#   .\tunnel.ps1 -Stop    stop all three
# The UI proxies /api and /ws to the backend, so the tunnel reaches the backend too: the backend then always
# runs with APP_TOKEN (generated into backend/.env the first time), and CORS_ORIGINS gets the tunnel URL.
# Needs once: ngrok config add-authtoken <your token>   (https://dashboard.ngrok.com/get-started/your-authtoken)
# Logs: trader/state/tunnel-*.log
param([switch]$Stop)
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$backend = Join-Path $root "backend"
$frontend = Join-Path $root "frontend"
$state = Join-Path $root "trader\state"
$envFile = Join-Path $backend ".env"
New-Item -ItemType Directory -Force $state | Out-Null

function Stop-All {
    foreach ($port in 8000, 5173) {
        Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue |
            ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }
    }
    Get-Process ngrok -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
}

function Set-EnvLine($key, $value) {
    # replaces or appends one KEY=value line; the other lines (the Angel keys) are left as they are
    $lines = @(if (Test-Path $envFile) { Get-Content $envFile } else { @() })
    $lines = @($lines | Where-Object { $_ -notmatch "^\s*$key\s*=" }) + "$key=$value"
    Set-Content -Path $envFile -Value $lines -Encoding ascii
}

function Start-Hidden($file, $arguments, $dir, $name) {
    # a process outside this shell's job, so it keeps running after the shell (or the terminal) closes
    $out = Join-Path $state "tunnel-$name.log"
    $cmd = "cmd.exe /c cd /d `"$dir`" && `"$file`" $arguments > `"$out`" 2>&1"
    $hidden = New-CimInstance -ClassName Win32_ProcessStartup -Property @{ ShowWindow = [uint16]0 } -ClientOnly
    $r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = $cmd;
        ProcessStartupInformation = $hidden }
    if ($r.ReturnValue -ne 0) { throw "could not start $name (Win32_Process.Create returned $($r.ReturnValue))" }
}

function Wait-Url($url, $seconds) {
    for ($i = 0; $i -lt $seconds; $i++) {
        try { return Invoke-RestMethod -Uri $url -TimeoutSec 2 } catch { Start-Sleep -Seconds 1 }
    }
    return $null
}

Stop-All
if ($Stop) { "Stopped the backend, the UI and ngrok."; return }

$ngrok = (Get-Command ngrok -ErrorAction SilentlyContinue).Source
if (-not $ngrok) { throw "ngrok is not installed: winget install --id Ngrok.Ngrok -e" }
$conf = Join-Path $env:LOCALAPPDATA "ngrok\ngrok.yml"
if (-not ((Test-Path $conf) -and (Select-String -Path $conf -Pattern '^\s*authtoken:' -Quiet))) {
    throw "ngrok has no authtoken yet: run  ngrok config add-authtoken <your token>  (https://dashboard.ngrok.com/get-started/your-authtoken)"
}

# 1. the access token: every /api call and the live socket need it once the UI is public
$hasToken = (Test-Path $envFile) -and (Select-String -Path $envFile -Pattern '^\s*APP_TOKEN\s*=\s*\S{24,}' -Quiet)
if (-not $hasToken) {
    $bytes = New-Object byte[] 32
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    Set-EnvLine "APP_TOKEN" (([Convert]::ToBase64String($bytes)) -replace '\+', '-' -replace '/', '_' -replace '=', '')
    "Generated a new APP_TOKEN in backend\.env"
}

# 2. the tunnel first: its URL goes into CORS_ORIGINS before the backend starts
Start-Hidden $ngrok "http 5173 --log stdout" $root "ngrok"
$tunnels = $null
for ($i = 0; $i -lt 30 -and -not $tunnels; $i++) {
    $t = Wait-Url "http://127.0.0.1:4040/api/tunnels" 1
    if ($t -and $t.tunnels) { $tunnels = $t.tunnels } else { Start-Sleep -Seconds 1 }   # API up, tunnel not yet
}
$public = ($tunnels | Where-Object { $_.public_url -like "https://*" } | Select-Object -First 1).public_url
if (-not $public) {
    Stop-All
    throw "ngrok did not open a tunnel; its log: $(Get-Content (Join-Path $state 'tunnel-ngrok.log') -Tail 5 -ErrorAction SilentlyContinue)"
}
Set-EnvLine "CORS_ORIGINS" "$public,http://localhost:5173"

# 3. the backend (engine included) and the UI
Start-Hidden (Join-Path $backend "venv\Scripts\python.exe") "-m uvicorn app.main:app --host 127.0.0.1 --port 8000" $backend "backend"
if (-not (Wait-Url "http://127.0.0.1:8000/health" 40)) { throw "the backend did not answer /health; see trader\state\tunnel-backend.log" }
$node = (Get-Command node).Source
Start-Hidden $node "node_modules\vite\bin\vite.js --port 5173 --strictPort" $frontend "frontend"
for ($i = 0; $i -lt 40; $i++) {
    try { Invoke-WebRequest -Uri "http://127.0.0.1:5173/" -UseBasicParsing -TimeoutSec 2 | Out-Null; break } catch { Start-Sleep -Seconds 1 }
}

""
"UI (public):  $public"
"UI (local):   http://localhost:5173"
"Access token: the APP_TOKEN line in backend\.env (the UI asks for it once per browser)"
"Stop:         .\tunnel.ps1 -Stop"
