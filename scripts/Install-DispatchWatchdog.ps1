$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path $PSScriptRoot -Parent
$python = Join-Path $repoRoot '.venv\Scripts\pythonw.exe'
$script = Join-Path $PSScriptRoot 'dispatch_watchdog.py'
if (-not (Test-Path -LiteralPath $python)) { throw 'Prepare .venv first; see README.' }
Get-Command gh -ErrorAction Stop | Out-Null
& gh auth status *> $null
if ($LASTEXITCODE -ne 0) { throw 'Authenticate gh for this Windows user first.' }
$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$action = New-ScheduledTaskAction -Execute $python -Argument ('"{0}"' -f $script) -WorkingDirectory $repoRoot
$timer = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 10)
$logon = New-ScheduledTaskTrigger -AtLogOn -User $identity
$principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 4) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName 'Novo-servidor GitHub retry watchdog' -Action $action -Trigger @($timer, $logon) -Principal $principal -Settings $settings -Description 'Dispatch missed Oracle retry workflow; respects disabled workflow and retry flags. Requires signed-in Windows user.' -Force | Out-Null
Write-Output 'Installed watchdog: every 10 minutes and at logon, without administrator privileges.'
