# Registers (or removes) the Windows task "lan-streamer Update" - run by the installer
# with admin rights, for an install for all users.
#
# The task runs update-helper.ps1 as SYSTEM: it downloads the latest release's
# installer, checks its SHA-256 and installs it silently. Every signed-in user may
# start it (schtasks /run), so "Install update" on the admin page needs no admin
# prompt. It installs only what the official release offers - nothing a user passes.
param([switch]$Unregister)

$ErrorActionPreference = 'Stop'
$name = 'lan-streamer Update'

if ($Unregister) {
    schtasks.exe /Delete /TN $name /F 2>$null | Out-Null
    exit 0
}

$helper = Join-Path $PSScriptRoot 'update-helper.ps1'
# SYSTEM and Administrators: full control; Authenticated Users: read and run.
$xml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Installs the latest lan-streamer release (started from the admin page).</Description>
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
      <Arguments>-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "$helper"</Arguments>
    </Exec>
  </Actions>
</Task>
"@
$file = Join-Path $env:TEMP 'lan-streamer-update-task.xml'
[IO.File]::WriteAllText($file, $xml, [Text.Encoding]::Unicode)
schtasks.exe /Create /TN $name /XML $file /F | Out-Null
$code = $LASTEXITCODE
Remove-Item $file -ErrorAction SilentlyContinue
exit $code
