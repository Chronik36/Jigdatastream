# Builds BotTrack-Setup.exe on Windows (x64). Used by .github/workflows/build-mac.yml (the windows job).
# Like the Mac app, everything BotTrack needs goes inside: its own Python, the app code, the Ollama engine and
# cloudflared for the phone link. The installer is made with Inno Setup.
param([string]$Out = "dist")
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"   # Invoke-WebRequest is very slow with the progress bar
$Root = (Resolve-Path "$PSScriptRoot\..\..").Path
New-Item -ItemType Directory -Force $Out | Out-Null
$Out = (Resolve-Path $Out).Path
$Work = Join-Path $env:RUNNER_TEMP "bottrack-win"
if (-not $env:RUNNER_TEMP) { $Work = Join-Path $env:TEMP "bottrack-win" }
Remove-Item -Recurse -Force $Work -ErrorAction SilentlyContinue
$App = Join-Path $Work "BotTrack"
New-Item -ItemType Directory -Force "$App\app" | Out-Null

$PyUrl = if ($env:PY_URL) { $env:PY_URL } else { "https://github.com/astral-sh/python-build-standalone/releases/download/20241016/cpython-3.12.7+20241016-x86_64-pc-windows-msvc-install_only.tar.gz" }
$OllamaUrl = "https://github.com/ollama/ollama/releases/latest/download/ollama-windows-amd64.zip"
$CloudflaredVersion = "2026.10.2"
$CloudflaredUrl = "https://github.com/cloudflare/cloudflared/releases/download/$CloudflaredVersion/cloudflared-windows-amd64.exe"

Write-Host "==> Python"
Invoke-WebRequest $PyUrl -OutFile "$Work\python.tar.gz"
tar -xzf "$Work\python.tar.gz" -C $App   # unpacks to BotTrack\python
& "$App\python\python.exe" -m pip install --quiet --disable-pip-version-check --no-compile `
  "flask>=3.0" "pywebview>=5.1,<6" "cryptography>=42"
if ($LASTEXITCODE) { throw "pip failed" }
Get-ChildItem "$App\python" -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force
foreach ($d in "Lib\test", "Lib\idlelib", "Lib\tkinter", "Lib\ensurepip", "tcl") {
  Remove-Item -Recurse -Force "$App\python\$d" -ErrorAction SilentlyContinue
}

Write-Host "==> App code"
git -C $Root archive --format=zip -o "$Work\code.zip" HEAD jigstream run.py requirements.txt
Expand-Archive "$Work\code.zip" "$App\app"
$build = if ($env:BOTTRACK_BUILD) { $env:BOTTRACK_BUILD } else { git -C $Root rev-parse HEAD }
Set-Content -NoNewline "$App\app\VERSION" "$build`n"
# Where Send logs and shared learning go, from the BOTTRACK_LOGS_TO Actions secret (kept out of the public code).
if ($env:BOTTRACK_DEVELOPER_EMAIL) { Set-Content -NoNewline "$App\app\jigstream\developer.txt" $env:BOTTRACK_DEVELOPER_EMAIL }
Copy-Item "$PSScriptRoot\BotTrack.pyw" $App

Write-Host "==> AI engine"
Invoke-WebRequest $OllamaUrl -OutFile "$Work\ollama.zip"
Expand-Archive "$Work\ollama.zip" "$App\ollama"
if (-not (Test-Path "$App\ollama\ollama.exe")) { throw "ollama.exe missing" }
# NVIDIA cards use the newest CUDA runtime; the older one only adds size.
Get-ChildItem "$App\ollama\lib\ollama" -Directory -Filter "cuda_v11*" -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force

Write-Host "==> Phone link"
Invoke-WebRequest $CloudflaredUrl -OutFile "$App\cloudflared.exe"

Write-Host "==> Icon"
& "$App\python\python.exe" -m pip install --quiet --disable-pip-version-check --target "$Work\pil" pillow
$env:PYTHONPATH = "$Work\pil"
& "$App\python\python.exe" -c "from PIL import Image; Image.open(r'$Root\jigstream\static\icon.png').convert('RGBA').save(r'$App\BotTrack.ico', sizes=[(16,16),(24,24),(32,32),(48,48),(64,64),(128,128),(256,256)])"
Remove-Item Env:\PYTHONPATH
if (-not (Test-Path "$App\BotTrack.ico")) { throw "icon missing" }

Write-Host "==> Smoke test"
$env:PYTHONPATH = "$App\app"
& "$App\python\python.exe" -c "import flask, webview, cryptography, jigstream.macapp, jigstream.app; jigstream.app.create_app(r'$Work\t.db'); print('imports ok')"
if ($LASTEXITCODE) { throw "smoke test failed" }
Remove-Item Env:\PYTHONPATH
& "$App\ollama\ollama.exe" --version

Write-Host "==> Installer"
$iscc = @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) { choco install innosetup -y --no-progress | Out-Null; $iscc = "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe" }
$count = git -C $Root rev-list --count HEAD
& $iscc "/DAppVersion=1.0.$count" "/DSourceDir=$App" "/DOutDir=$Out" "$PSScriptRoot\BotTrack.iss"
if ($LASTEXITCODE) { throw "Inno Setup failed" }
Get-Item "$Out\BotTrack-Setup.exe" | Select-Object Name, Length
