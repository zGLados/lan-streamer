# Registers (or removes) the Windows tasks "lan-streamer Update" and "lan-streamer
# Update (beta)" - run by the installer with admin rights, for an install for all users.
#
# The tasks run update-helper.ps1 as SYSTEM: it downloads the latest release's (or the
# beta's) installer, checks its SHA-256 and installs it silently. Every signed-in user
# may start them (schtasks /run), so "Install update" on the admin page needs no admin
# prompt. They install only what the official releases offer - nothing a user passes:
# the channel is fixed in each task.
param([switch]$Unregister)

$ErrorActionPreference = 'Stop'
$tasks = [ordered]@{
    'lan-streamer Update'        = @{ Channel = 'stable'; What = 'the latest lan-streamer release' }
    'lan-streamer Update (beta)' = @{ Channel = 'beta'; What = 'the lan-streamer beta (pre-release)' }
}

if ($Unregister) {
    foreach ($name in $tasks.Keys) { schtasks.exe /Delete /TN $name /F 2>$null | Out-Null }
    exit 0
}

$helper = Join-Path $PSScriptRoot 'update-helper.ps1'
$code = 0
foreach ($name in $tasks.Keys) {
    $task = $tasks[$name]
    # SYSTEM and Administrators: full control; Authenticated Users: read and run.
    $xml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Installs $($task.What) (started from the admin page).</Description>
    <SecurityDescriptor>D:(A;;FA;;;SY)(A;;FA;;;BA)(A;;GRGX;;;AU)</SecurityDescriptor>
  </RegistrationInfo>
  <Principals>
    <Principal id="System">
      <UserId>S-1-5-18</UserId>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <ExecutionTimeLimit>PT30M</ExecutionTimeLimit>
  </Settings>
  <Actions Context="System">
    <Exec>
      <Command>$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe</Command>
      <Arguments>-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "$helper" -Channel $($task.Channel)</Arguments>
    </Exec>
  </Actions>
</Task>
"@
    $file = Join-Path $env:TEMP 'lan-streamer-update-task.xml'
    [IO.File]::WriteAllText($file, $xml, [Text.Encoding]::Unicode)
    schtasks.exe /Create /TN $name /XML $file /F | Out-Null
    if ($LASTEXITCODE -ne 0) { $code = $LASTEXITCODE }
    Remove-Item $file -ErrorAction SilentlyContinue
}
exit $code
