# Installs a lan-streamer test version on Windows 10/11 x64.
# Stand-in until the real installer exists (roadmap M5). Safe to run again.
#
#   powershell -ExecutionPolicy Bypass -File install-windows.ps1 [-Ref main] [-InstallDir <path>]
#
# Installs, if missing:
#   - GStreamer (official MSVC installer, checksum-verified; may ask for admin rights)
#   - Python 3.13 (via winget, for the current user)
# and into <InstallDir> (default %LOCALAPPDATA%\lan-streamer):
#   - lan-streamer from GitHub (branch or tag <Ref>)
#   - MediaMTX (checksum-verified)
#   - a Python environment with lan-streamer and its own PyGObject build
# Finally runs the self-check (GStreamer, required elements, encoders).

param(
    [string]$Ref = 'main',
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA 'lan-streamer'),
    # For CI: use this Python / this PyGObject wheel instead of the defaults.
    [string]$PythonExe = '',
    [string]$PyGObjectWheelPath = ''
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$Repo = 'zGLados/lan-streamer'
$GstVersion = '1.28.6'
$GstSha256 = '059251444d1267b486eba390b18d25fed87e10315e72f757ec6c7e912fa746b5'
$PyVersion = '3.13'
$MediaMtxVersion = 'v1.21.2'
$MediaMtxSha256 = 'a84224f5a490855684e62ba6fb71e80ea2a5e16aed61883bb58380290e93e467'
# GStreamer's own Python bindings only work with Python 3.9, so lan-streamer
# builds PyGObject itself (.github/workflows/pygobject-wheel.yml).
$PyGObjectWheel = 'pygobject-3.50.2-cp313-cp313-win_amd64.whl'
$PyGObjectUrl = "https://github.com/$Repo/releases/download/deps-win64-gst$GstVersion/$PyGObjectWheel"

function Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }

function Find-GStreamer {
    $candidates = @(
        [Environment]::GetEnvironmentVariable('GSTREAMER_1_0_ROOT_MSVC_X86_64', 'User'),
        [Environment]::GetEnvironmentVariable('GSTREAMER_1_0_ROOT_MSVC_X86_64', 'Machine'),
        (Join-Path $env:LOCALAPPDATA 'Programs\gstreamer\1.0\msvc_x86_64'),  # winget default (per user)
        'C:\Program Files\gstreamer\1.0\msvc_x86_64',
        'C:\gstreamer\1.0\msvc_x86_64'
    )
    foreach ($c in $candidates) {
        if ($c -and (Test-Path (Join-Path $c 'bin\gst-launch-1.0.exe'))) { return $c.TrimEnd('\') }
    }
    return $null
}

function Winget-Install($id, $version, $extra) {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        throw "winget is missing. Install 'App Installer' from the Microsoft Store, then run this script again."
    }
    $wargs = @('install', '--id', $id, '--exact', '--silent', '--accept-package-agreements', '--accept-source-agreements')
    if ($version) { $wargs += @('--version', $version) }
    winget @wargs @extra
    if ($LASTEXITCODE -ne 0) { throw "winget could not install $id (exit code $LASTEXITCODE)" }
}

function Download($url, $file) {
    Invoke-WebRequest -Uri $url -OutFile $file -UseBasicParsing
}

New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
$Tmp = Join-Path $env:TEMP "lan-streamer-install"
New-Item -ItemType Directory -Force -Path $Tmp | Out-Null

# --- GStreamer -----------------------------------------------------------------
Step "GStreamer $GstVersion"
$Gst = Find-GStreamer
if (-not $Gst) {
    $exe = Join-Path $Tmp 'gstreamer.exe'
    Download "https://gstreamer.freedesktop.org/data/pkg/windows/$GstVersion/msvc/gstreamer-1.0-msvc-x86_64-$GstVersion.exe" $exe
    $hash = (Get-FileHash -Algorithm SHA256 $exe).Hash.ToLower()
    if ($hash -ne $GstSha256) { throw "GStreamer installer checksum mismatch: $hash" }
    Write-Host '    installing (Windows may ask for admin rights) ...'
    Start-Process -Wait -FilePath $exe -ArgumentList '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART'
    $Gst = Find-GStreamer
    if (-not $Gst) { throw 'GStreamer was installed but not found.' }
}
Write-Host "    $Gst"

# --- Python --------------------------------------------------------------------
Step "Python $PyVersion"
$hasPy = [bool]$PythonExe
if (-not $hasPy -and (Get-Command py -ErrorAction SilentlyContinue)) {
    & py "-$PyVersion" -c "import sys" 2>$null
    $hasPy = ($LASTEXITCODE -eq 0)
}
if (-not $hasPy) {
    Winget-Install "Python.Python.$PyVersion" $null @('--scope', 'user')
    $env:Path = [Environment]::GetEnvironmentVariable('Path', 'User') + ';' + [Environment]::GetEnvironmentVariable('Path', 'Machine')
}

# --- lan-streamer source -------------------------------------------------------
Step "lan-streamer ($Ref)"
$AppDir = Join-Path $InstallDir 'app'
$zip = Join-Path $Tmp 'source.zip'
Download "https://github.com/$Repo/archive/$Ref.zip" $zip
if (Test-Path $AppDir) { Remove-Item -Recurse -Force $AppDir }
Expand-Archive -Path $zip -DestinationPath $Tmp -Force
Move-Item -Path (Get-ChildItem -Directory $Tmp | Where-Object Name -like 'lan-streamer-*' | Select-Object -First 1).FullName -Destination $AppDir
Write-Host "    $AppDir"

# --- MediaMTX ------------------------------------------------------------------
Step "MediaMTX $MediaMtxVersion"
$MtxDir = Join-Path $InstallDir 'mediamtx'
$MtxStamp = Join-Path $MtxDir 'VERSION'
if (-not ((Test-Path $MtxStamp) -and ((Get-Content $MtxStamp) -eq $MediaMtxVersion))) {
    New-Item -ItemType Directory -Force -Path $MtxDir | Out-Null
    $zip = Join-Path $Tmp 'mediamtx.zip'
    Download "https://github.com/bluenviron/mediamtx/releases/download/$MediaMtxVersion/mediamtx_${MediaMtxVersion}_windows_amd64.zip" $zip
    $hash = (Get-FileHash -Algorithm SHA256 $zip).Hash.ToLower()
    if ($hash -ne $MediaMtxSha256) { throw "MediaMTX checksum mismatch: $hash" }
    Expand-Archive -Path $zip -DestinationPath $MtxDir -Force
    Set-Content -Path $MtxStamp -Value $MediaMtxVersion
}
Write-Host "    $MtxDir"

# --- Python environment --------------------------------------------------------
Step 'Python environment'
$Venv = Join-Path $InstallDir 'venv'
$VenvPy = Join-Path $Venv 'Scripts\python.exe'
if (-not (Test-Path $VenvPy)) {
    if ($PythonExe) { & $PythonExe -m venv $Venv } else { & py "-$PyVersion" -m venv $Venv }
}
if ($PyGObjectWheelPath) {
    $wheel = $PyGObjectWheelPath
} else {
    $wheel = Join-Path $Tmp $PyGObjectWheel
    Download $PyGObjectUrl $wheel
}
& $VenvPy -m pip install --quiet --upgrade pip
& $VenvPy -m pip install --quiet --force-reinstall $wheel
if ($LASTEXITCODE -ne 0) { throw 'pip install of PyGObject failed' }
& $VenvPy -m pip install --quiet --force-reinstall --no-deps $AppDir
if ($LASTEXITCODE -ne 0) { throw 'pip install of lan-streamer failed' }

Remove-Item -Recurse -Force $Tmp

# --- Self-check ----------------------------------------------------------------
Step 'Self-check'
& $VenvPy -m lanstreamer.doctor
$ok = ($LASTEXITCODE -eq 0)

Write-Host ''
if ($ok) {
    Write-Host "Installed to $InstallDir" -ForegroundColor Green
} else {
    Write-Host 'The self-check found a problem (see above). Please send the whole output.' -ForegroundColor Yellow
}
Write-Host "Run the check again:  & '$VenvPy' -m lanstreamer.doctor [--encoder nvidia|amd|intel|cpu]"
