# The update itself, run as SYSTEM by the task "lan-streamer Update" (update-task.ps1).
#
# Started from the admin page (updater.py): lan-streamer ends, this script downloads
# the latest release's installer, checks its SHA-256 against the release and installs
# it silently; the installer waits until lan-streamer has ended (/WAITAPP=1). The
# result goes to <program folder>\updates\result.json, where lan-streamer (started
# again by its own relauncher) shows it. The program folder (C:\Program Files\...) is
# writable only by admins and SYSTEM, so no user can swap the downloaded installer.
#
# PowerShell, not the bundled Python: the installer replaces the program folder.
# The release address can be changed only by an admin (HKLM\Software\lan-streamer,
# value UpdateFeed - for tests), never by the user who starts the task.

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'  # Invoke-WebRequest is slow with a progress bar
$work = Join-Path $PSScriptRoot 'updates'
New-Item -ItemType Directory -Force $work | Out-Null
$result = Join-Path $work 'result.json'
$log = Join-Path $work 'install.log'

function Write-Result([bool]$ok, [string]$message, [string]$version) {
    @{ ok = $ok; message = $message; version = $version; time = (Get-Date).ToUniversalTime().ToString('o') } |
        ConvertTo-Json | Set-Content -Path $result -Encoding UTF8
}

try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $feed = (Get-ItemProperty 'HKLM:\Software\lan-streamer' -Name UpdateFeed -ErrorAction SilentlyContinue).UpdateFeed
    if (-not $feed) { $feed = 'https://api.github.com/repos/zGLados/lan-streamer/releases/latest' }
    $headers = @{ 'User-Agent' = 'lan-streamer-updater'; 'Accept' = 'application/vnd.github+json' }
    $release = Invoke-RestMethod -Uri $feed -Headers $headers -TimeoutSec 30
    $asset = $release.assets | Where-Object { $_.name -match '^lan-streamer-setup-[\w.\-]+\.exe$' } | Select-Object -First 1
    if (-not $asset) { throw 'the release has no Windows installer' }
    if ("$($asset.digest)" -notmatch '^sha256:([0-9a-fA-F]{64})$') { throw 'the release gives no SHA-256 for the installer' }
    $want = $Matches[1].ToLower()

    Get-ChildItem $work -Filter 'lan-streamer-setup-*.exe' | Remove-Item -Force
    $file = Join-Path $work $asset.name
    Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $file -UseBasicParsing -TimeoutSec 900 `
        -Headers @{ 'User-Agent' = 'lan-streamer-updater' }
    if ((Get-FileHash $file -Algorithm SHA256).Hash.ToLower() -ne $want) {
        Remove-Item $file -Force
        throw "the downloaded installer does not match the release's SHA-256"
    }

    $setup = Start-Process -FilePath $file -Wait -PassThru -ArgumentList @(
        '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/CLOSEAPPLICATIONS', '/WAITAPP=1', "/LOG=`"$log`"")
    if ($setup.ExitCode -ne 0) { throw "the installer ended with code $($setup.ExitCode) (log: $log)" }
    Write-Result $true "installed $($release.tag_name)" "$($release.tag_name)"
} catch {
    Write-Result $false "$($_.Exception.Message)" ''
    exit 1
}
