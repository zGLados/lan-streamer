# Installs the official GStreamer MSVC build into $env:GST_ROOT (build machine only;
# the installer ships what build.ps1 picks from it). Used by windows.yml and caches.yml.
$ErrorActionPreference = 'Stop'
$exe = "$env:RUNNER_TEMP\gstreamer.exe"
$url = "https://gstreamer.freedesktop.org/data/pkg/windows/$env:GST_VERSION/msvc/gstreamer-1.0-msvc-x86_64-$env:GST_VERSION.exe"
# The server is slow (~0.6 MB/s for 530 MB) and cuts long downloads off
# (2026-11-11, twice "response ended prematurely"): resume until complete.
for ($try = 1; $try -le 10; $try++) {
  curl.exe -fsSL --connect-timeout 30 -C - -o $exe $url
  if ($LASTEXITCODE -eq 0) { break }
  Write-Host "download interrupted (curl $LASTEXITCODE), resuming ($try/10)"
  Start-Sleep 5
}
if ($LASTEXITCODE -ne 0) { throw "GStreamer download failed" }
$want = ((curl.exe -fsSL "$url.sha256sum") -split '\s+')[0]
if ((Get-FileHash $exe -Algorithm SHA256).Hash -ne $want) { throw "GStreamer download does not match its SHA-256" }
Start-Process -Wait -FilePath $exe -ArgumentList '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', "/DIR=$env:GST_ROOT"
