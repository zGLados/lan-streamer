# Builds the Windows test installer. Runs in GitHub Actions (.github/workflows/windows.yml).
#
# Needs: Python 3.13 (x64) as `python`, the official GStreamer MSVC build at $GstRoot,
# a PyGObject wheel built against it, and Inno Setup 6 (iscc).
#
# Layout of the installed app (what bundled_root() in lanstreamer.media.gst expects):
#   <app>\python\      embeddable CPython + site-packages (lan-streamer, PyGObject)
#   <app>\gstreamer\   runtime part of GStreamer (bin, plugins, typelibs, scanner)
#   <app>\mediamtx\    MediaMTX
#   <app>\*.cmd        start scripts

param(
    [Parameter(Mandatory)] [string]$GstRoot,
    [Parameter(Mandatory)] [string]$PyGObjectWheel,
    [Parameter(Mandatory)] [string]$Version,      # shown to users, e.g. 0.0.1-test.7
    [Parameter(Mandatory)] [string]$NumVersion,   # numeric for the file version, e.g. 0.0.1.7
    [ValidateSet('fast', 'normal', 'max')] [string]$Compression = 'max',
    [string]$MediaMtxVersion = 'v1.21.2',
    [string]$MediaMtxSha256 = 'a84224f5a490855684e62ba6fb71e80ea2a5e16aed61883bb58380290e93e467'
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$Repo = Resolve-Path (Join-Path $PSScriptRoot '..\..')
$Out = Join-Path $Repo 'build\windows'
$Stage = Join-Path $Out 'stage'
if (Test-Path $Out) { Remove-Item -Recurse -Force $Out }
New-Item -ItemType Directory -Force -Path $Stage | Out-Null

function Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }

# --- Python (embeddable, same version as the build Python) --------------------
$PyVer = (python -c "import platform; print(platform.python_version())").Trim()
$PyTag = (python -c "import sys; print(f'{sys.version_info[0]}{sys.version_info[1]}')").Trim()
Step "Python $PyVer (embeddable)"
$PyDir = Join-Path $Stage 'python'
$zip = Join-Path $Out 'python.zip'
Invoke-WebRequest "https://www.python.org/ftp/python/$PyVer/python-$PyVer-embed-amd64.zip" -OutFile $zip
Expand-Archive $zip -DestinationPath $PyDir
Remove-Item $zip
# The ._pth file defines sys.path of the embeddable Python.
Set-Content -Path (Join-Path $PyDir "python$PyTag._pth") -Value @("python$PyTag.zip", '.', 'Lib\site-packages')

$Site = Join-Path $PyDir 'Lib\site-packages'
python -m pip install --quiet --no-deps --target $Site $PyGObjectWheel
if ($LASTEXITCODE -ne 0) { throw 'pip install of PyGObject failed' }
# With its dependencies (pydantic, pystray, Pillow) as wheels for this Python.
python -m pip install --quiet --target $Site $Repo
if ($LASTEXITCODE -ne 0) { throw 'pip install of lan-streamer failed' }
# Build version shown on the page, in the logs and in the self-test
Set-Content -Path (Join-Path $Site 'lanstreamer\BUILD') -Value $Version -NoNewline

# --- GStreamer runtime -----------------------------------------------------------
# Only the plugins in gstreamer-plugins.txt, plus every DLL they need (transitive
# dumpbin closure over GStreamer's bin). Keeps the installer small.
Step "GStreamer runtime from $GstRoot"
$GstDir = Join-Path $Stage 'gstreamer'
$GstBin = Join-Path $GstRoot 'bin'

function Copy-Rel($rel) {
    $target = Join-Path $GstDir $rel
    New-Item -ItemType Directory -Force -Path (Split-Path $target) | Out-Null
    Copy-Item (Join-Path $GstRoot $rel) $target
    return $target
}

function Get-Imports($file) {
    $inDeps = $false
    foreach ($line in (dumpbin /nologo /dependents $file)) {
        if ($line -match 'has the following (delay load )?dependencies') { $inDeps = $true; continue }
        if ($inDeps -and $line -match '^\s+(\S+\.dll)\s*$') { $Matches[1] }
        elseif ($inDeps -and $line -match 'Summary') { break }
    }
}

$plugins = Get-Content (Join-Path $PSScriptRoot 'gstreamer-plugins.txt') |
    ForEach-Object { ($_ -replace '#.*$', '').Trim() } | Where-Object { $_ }
$queue = [System.Collections.Generic.Queue[string]]::new()
foreach ($entry in $plugins) {
    $optional = $entry.StartsWith('?')
    $plugin = $entry.TrimStart('?')
    $rel = "lib\gstreamer-1.0\$plugin"
    if (-not (Test-Path (Join-Path $GstRoot $rel))) {
        if ($optional) { Write-Host "    (optional plugin missing) $plugin"; continue }
        throw "plugin $plugin is not in this GStreamer build"
    }
    $queue.Enqueue((Copy-Rel $rel))
}
# Programs and modules that are not plugins
foreach ($rel in 'bin\gst-launch-1.0.exe', 'bin\gst-inspect-1.0.exe') { $queue.Enqueue((Copy-Rel $rel)) }
Get-ChildItem (Join-Path $GstRoot 'libexec\gstreamer-1.0') -Filter *.exe | ForEach-Object {
    $queue.Enqueue((Copy-Rel "libexec\gstreamer-1.0\$($_.Name)"))
}
Get-ChildItem (Join-Path $GstRoot 'lib\gio\modules') -Filter *.dll -ErrorAction SilentlyContinue | ForEach-Object {
    $queue.Enqueue((Copy-Rel "lib\gio\modules\$($_.Name)"))  # TLS (https), proxy
}
# DLLs that are loaded at runtime (LoadLibrary), so dumpbin cannot see them
$dynamic = @('soup-*.dll', 'libsoup-*.dll', 'dxcompiler*.dll', 'dxil*.dll')
foreach ($pattern in $dynamic) {
    Get-ChildItem $GstBin -Filter $pattern | ForEach-Object { $queue.Enqueue((Copy-Rel "bin\$($_.Name)")) }
}
# libproxy keeps its backend outside bin
Get-ChildItem $GstRoot -Recurse -Filter 'pxbackend-1.0.dll' | Select-Object -First 1 | ForEach-Object {
    Copy-Item $_.FullName (Join-Path $GstDir 'bin'); $queue.Enqueue((Join-Path $GstDir "bin\$($_.Name)"))
}
# PyGObject itself needs GLib, GObject and girepository from GStreamer's bin
Get-ChildItem $Site -Recurse -Filter *.pyd | ForEach-Object { $queue.Enqueue($_.FullName) }

$seen = @{}
while ($queue.Count) {
    foreach ($dep in (Get-Imports $queue.Dequeue())) {
        $key = $dep.ToLower()
        if ($seen[$key]) { continue }
        $seen[$key] = $true
        if (Test-Path (Join-Path $GstBin $dep)) {
            $target = Join-Path $GstDir "bin\$dep"
            if (-not (Test-Path $target)) { $queue.Enqueue((Copy-Rel "bin\$dep")) }
        }
    }
}

# Data files
foreach ($p in @(
    @{ From = 'lib\girepository-1.0'; Filter = '*.typelib' },
    @{ From = 'etc\ssl'; Filter = '*' },
    @{ From = 'share\glib-2.0\schemas'; Filter = '*' }
)) {
    $src = Join-Path $GstRoot $p.From
    if (-not (Test-Path $src)) { continue }
    Get-ChildItem -Path $src -Filter $p.Filter -File -Recurse | ForEach-Object {
        Copy-Rel $_.FullName.Substring($GstRoot.Length).TrimStart('\') | Out-Null
    }
}
$bins = (Get-ChildItem (Join-Path $GstDir 'bin') -Filter *.dll).Count
$mb = [math]::Round((Get-ChildItem $GstDir -Recurse -File | Measure-Object Length -Sum).Sum / 1MB)
Write-Host "    $($plugins.Count) plugins, $bins DLLs in bin, $mb MB"

# Visual C++ runtime, deployed app-locally: many plugins are C++ (d3d11, nvcodec,
# amfcodec, qsv, ...) and a fresh Windows does not have msvcp140.dll.
$crt = Get-ChildItem -Directory (Join-Path $env:VCToolsRedistDir 'x64') -Filter 'Microsoft.VC*.CRT' | Select-Object -First 1
if (-not $crt) { throw "Visual C++ runtime not found under $env:VCToolsRedistDir" }
Copy-Item (Join-Path $crt.FullName '*.dll') (Join-Path $GstDir 'bin')
Write-Host "    + Visual C++ runtime from $($crt.Name)"

$mb = [math]::Round((Get-ChildItem $GstDir -Recurse -File | Measure-Object Length -Sum).Sum / 1MB)
Write-Host "    $mb MB"

# --- MediaMTX ------------------------------------------------------------------
Step "MediaMTX $MediaMtxVersion"
$zip = Join-Path $Out 'mediamtx.zip'
Invoke-WebRequest "https://github.com/bluenviron/mediamtx/releases/download/$MediaMtxVersion/mediamtx_${MediaMtxVersion}_windows_amd64.zip" -OutFile $zip
$hash = (Get-FileHash -Algorithm SHA256 $zip).Hash.ToLower()
if ($hash -ne $MediaMtxSha256) { throw "MediaMTX checksum mismatch: $hash" }
Expand-Archive $zip -DestinationPath (Join-Path $Stage 'mediamtx')
Remove-Item $zip

# --- Dependency check -----------------------------------------------------------
# Every DLL import must be shipped in the stage or be part of Windows itself.
# The build machine has extra runtimes installed, so those never count as Windows.
Step 'Dependency check'
$shipped = @{}
Get-ChildItem $Stage -Recurse -File -Include *.dll, *.pyd, *.exe | ForEach-Object { $shipped[$_.Name.ToLower()] = $true }
$notWindows = '^(msvcp|vcruntime|concrt|vcomp|vccorlib|python)'
$problems = @()
$binaries = @(
    Get-ChildItem (Join-Path $GstDir 'bin') -Filter *.dll
    Get-ChildItem (Join-Path $GstDir 'lib\gstreamer-1.0') -Filter *.dll
    Get-ChildItem (Join-Path $GstDir 'lib\gio\modules') -Filter *.dll -ErrorAction SilentlyContinue
    Get-ChildItem (Join-Path $GstDir 'libexec') -Recurse -Filter *.exe
    Get-ChildItem $Site -Recurse -Filter *.pyd
)
foreach ($bin in $binaries) {
    $inDeps = $false
    foreach ($line in (dumpbin /nologo /dependents $bin.FullName)) {
        if ($line -match 'has the following dependencies') { $inDeps = $true; continue }
        if ($inDeps -and $line -match '^\s*$' -and $seen) { break }
        if ($inDeps -and $line -match '^\s+(\S+\.dll)\s*$') {
            $seen = $true
            $dep = $Matches[1].ToLower()
            if ($shipped[$dep] -or $dep -like 'api-ms-win-*' -or $dep -like 'ext-ms-*') { continue }
            if ($dep -notmatch $notWindows -and (Test-Path (Join-Path $env:SystemRoot "System32\$dep"))) { continue }
            $problems += "$($bin.Name) -> $dep"
        }
    }
    $seen = $false
}
if ($problems) {
    $problems | Sort-Object -Unique | ForEach-Object { Write-Host "    MISSING $_" -ForegroundColor Red }
    throw "$($problems.Count) unresolved DLL dependencies"
}
Write-Host "    $($binaries.Count) files, all dependencies resolved"

# --- Start scripts ---------------------------------------------------------------
python -m pip install --quiet pillow
python (Join-Path $PSScriptRoot 'make_icon.py') (Join-Path $Stage 'lan-streamer.ico')
if ($LASTEXITCODE -ne 0) { throw 'icon generation failed' }
Copy-Item (Join-Path $PSScriptRoot 'self-test.cmd') $Stage
Copy-Item (Join-Path $PSScriptRoot 'start.cmd') $Stage
Copy-Item (Join-Path $Repo 'LICENSE') $Stage

# --- Installer -----------------------------------------------------------------
Step "Installer $Version"
$iscc = (Get-Command iscc -ErrorAction SilentlyContinue).Source
if (-not $iscc) { $iscc = Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6\ISCC.exe' }
& $iscc /Qp "/DAppVersion=$Version" "/DNumVersion=$NumVersion" "/DCompression=$Compression" "/DStageDir=$Stage" "/O$Out" (Join-Path $PSScriptRoot 'lan-streamer.iss')
if ($LASTEXITCODE -ne 0) { throw 'Inno Setup failed' }
Get-ChildItem $Out -Filter '*.exe' | ForEach-Object { Write-Host "    $($_.Name)  $([math]::Round($_.Length / 1MB)) MB" }
