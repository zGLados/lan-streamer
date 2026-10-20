# Sets up a lan-streamer development environment on Windows 10/11 x64.
# Installs everything the app needs on first run and is safe to run again:
#   - GStreamer (official MSVC build, includes Python bindings and gst-plugins-rs)
#   - MediaMTX (into .deps\mediamtx, checksum-verified)
#   - Python matching GStreamer's bindings, a .venv and lan-streamer in editable mode
# Usage (from the repo root):  powershell -ExecutionPolicy Bypass -File scripts\setup-windows.ps1

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$GstVersion = '1.28.6'
$MediaMtxVersion = 'v1.21.2'
$MediaMtxSha256 = 'a84224f5a490855684e62ba6fb71e80ea2a5e16aed61883bb58380290e93e467'

$Root = Split-Path -Parent $PSScriptRoot
$Deps = Join-Path $Root '.deps'

function Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }

function Find-GStreamer {
    $candidates = @(
        [Environment]::GetEnvironmentVariable('GSTREAMER_1_0_ROOT_MSVC_X86_64', 'Machine'),
        [Environment]::GetEnvironmentVariable('GSTREAMER_1_0_ROOT_MSVC_X86_64', 'User'),
        'C:\Program Files\gstreamer\1.0\msvc_x86_64',
        'C:\gstreamer\1.0\msvc_x86_64'
    )
    foreach ($c in $candidates) {
        if ($c -and (Test-Path (Join-Path $c 'bin\gst-launch-1.0.exe'))) { return $c.TrimEnd('\') }
    }
    return $null
}

# --- GStreamer ---------------------------------------------------------------
Step "GStreamer $GstVersion"
$Gst = Find-GStreamer
if (-not $Gst) {
    winget install --id gstreamerproject.gstreamer --version $GstVersion --exact --silent `
        --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) { throw "winget could not install GStreamer (exit code $LASTEXITCODE)" }
    $Gst = Find-GStreamer
    if (-not $Gst) { throw 'GStreamer was installed but not found. Check the install path.' }
}
Write-Host "    found at $Gst"

# --- MediaMTX ----------------------------------------------------------------
Step "MediaMTX $MediaMtxVersion"
$MtxDir = Join-Path $Deps 'mediamtx'
$MtxExe = Join-Path $MtxDir 'mediamtx.exe'
$MtxStamp = Join-Path $MtxDir 'VERSION'
if (-not ((Test-Path $MtxExe) -and (Test-Path $MtxStamp) -and ((Get-Content $MtxStamp) -eq $MediaMtxVersion))) {
    New-Item -ItemType Directory -Force -Path $MtxDir | Out-Null
    $zip = Join-Path $env:TEMP "mediamtx_$MediaMtxVersion.zip"
    $url = "https://github.com/bluenviron/mediamtx/releases/download/$MediaMtxVersion/mediamtx_${MediaMtxVersion}_windows_amd64.zip"
    Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing
    $hash = (Get-FileHash -Algorithm SHA256 $zip).Hash.ToLower()
    if ($hash -ne $MediaMtxSha256) { throw "MediaMTX checksum mismatch: $hash" }
    Expand-Archive -Path $zip -DestinationPath $MtxDir -Force
    Remove-Item $zip
    Set-Content -Path $MtxStamp -Value $MediaMtxVersion
}
Write-Host "    $MtxExe"

# --- Python matching GStreamer's bindings ------------------------------------
Step 'Python'
$SitePackages = Join-Path $Gst 'lib\site-packages'
$giModule = Get-ChildItem -Path (Join-Path $SitePackages 'gi') -Filter '_gi.cp3*-win_amd64.pyd' -ErrorAction SilentlyContinue |
    Select-Object -First 1
if (-not $giModule) { throw "GStreamer's Python bindings were not found in $SitePackages." }
$tag = [regex]::Match($giModule.Name, 'cp3(\d+)').Groups[1].Value
$PyVersion = "3.$tag"
Write-Host "    GStreamer's bindings are built for Python $PyVersion"

& py "-$PyVersion" -c "import sys" 2>$null
if ($LASTEXITCODE -ne 0) {
    winget install --id "Python.Python.$PyVersion" --exact --silent --scope user `
        --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) { throw "winget could not install Python $PyVersion (exit code $LASTEXITCODE)" }
}

# --- Virtual environment -----------------------------------------------------
Step '.venv'
$Venv = Join-Path $Root '.venv'
$VenvPy = Join-Path $Venv 'Scripts\python.exe'
if (Test-Path $VenvPy) {
    $have = & $VenvPy -c "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}')"
    if ($have -ne $PyVersion) { Remove-Item -Recurse -Force $Venv }
}
if (-not (Test-Path $VenvPy)) { & py "-$PyVersion" -m venv $Venv }

# Make GStreamer's bindings importable inside the venv. lanstreamer.media.gst
# adds GStreamer's bin directory to the DLL search path at import time.
$sitePkgVenv = & $VenvPy -c "import sysconfig; print(sysconfig.get_paths()['purelib'])"
Set-Content -Path (Join-Path $sitePkgVenv 'gstreamer.pth') -Value $SitePackages

& $VenvPy -m pip install --quiet --upgrade pip
& $VenvPy -m pip install --quiet -e "$Root[dev]"
if ($LASTEXITCODE -ne 0) { throw 'pip install failed' }

# --- Check -------------------------------------------------------------------
Step 'Check'
& $VenvPy -m lanstreamer.doctor
if ($LASTEXITCODE -ne 0) { throw 'lanstreamer doctor reported a problem' }

Write-Host ''
Write-Host 'Done. Activate the environment with:  .venv\Scripts\Activate.ps1' -ForegroundColor Green
