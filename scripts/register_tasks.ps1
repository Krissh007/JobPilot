<#
Registers JobPilot's three Windows Task Scheduler tasks for the current user.
Times come from the dashboard Settings (schedule + weekly review); run this again after changing them.

  JobPilot Search-Score-Docs      on the chosen days: from <start> every <every_hours> h for the window,
                                  or at each explicit time if Settings lists run times       -> python -m jobpilot.scheduled
                                  (the `pipeline` setting picks the agents: legacy or redesign)
  JobPilot Weekly-Answer-Review   weekly on <day> at <time>                                   -> python -m jobpilot.weekly
  JobPilot Catch-up               on sign-in, unlock and wake from sleep (2 min delay)         -> python -m jobpilot.scheduled --catch-up
                                  runs a pass only when the last search is overdue (Windows doesn't
                                  reliably start missed runs after sleep or modern standby)

Runs only while you are logged on (the search uses your signed-in browser profile), starts a missed
run as soon as the PC is available again, never runs two passes at once, and needs no admin rights.

  powershell -ExecutionPolicy Bypass -File scripts\register_tasks.ps1             # register / update
  powershell -ExecutionPolicy Bypass -File scripts\register_tasks.ps1 -WhatIf     # show what it would do
  powershell -ExecutionPolicy Bypass -File scripts\register_tasks.ps1 -Unregister # remove all three tasks
#>
param([switch]$Unregister, [switch]$WhatIf)
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$pythonw = Join-Path $root ".venv\Scripts\pythonw.exe"
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $pythonw)) { throw "Missing $pythonw. Create the venv first (see README)." }

$cfg = & $python -m jobpilot.scheduled --print-config | ConvertFrom-Json
$names = @($cfg.tasks.pipeline, $cfg.tasks.weekly, $cfg.tasks.catchup)

if ($Unregister) {
    foreach ($name in $names) {
        if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
            if ($WhatIf) { "Would remove: $name" } else { Unregister-ScheduledTask -TaskName $name -Confirm:$false; "Removed: $name" }
        } else { "Not registered: $name" }
    }
    return
}

$days = @{ mon = "Monday"; tue = "Tuesday"; wed = "Wednesday"; thu = "Thursday"; fri = "Friday"; sat = "Saturday"; sun = "Sunday" }
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 2)

# Pipeline: on the chosen days. Interval mode = one trigger at <start> repeated every <every_hours> h for the window;
# times mode = one trigger per explicit time.
$dayNames = @($cfg.days | ForEach-Object { $days[$_] })
$everyDay = $dayNames.Count -ge 7
function New-PipelineTrigger($at) {
    if ($everyDay) { New-ScheduledTaskTrigger -Daily -At $at } else { New-ScheduledTaskTrigger -Weekly -DaysOfWeek $dayNames -At $at }
}
$runTimes = @($cfg.times)
if ($runTimes.Count -gt 0) {
    $pipelineTrigger = @($runTimes | ForEach-Object { New-PipelineTrigger $_ })
    $when = "at $($runTimes -join ', ')"
} else {
    $pipelineTrigger = New-PipelineTrigger $cfg.start
    $repeat = New-ScheduledTaskTrigger -Once -At $cfg.start -RepetitionInterval (New-TimeSpan -Hours $cfg.every_hours) `
        -RepetitionDuration (New-TimeSpan -Minutes $cfg.window_minutes)
    $pipelineTrigger.Repetition = $repeat.Repetition
    $when = "from $($cfg.start), every $($cfg.every_hours) h for $($cfg.window_minutes / 60) h"
}
$dayText = if ($everyDay) { "daily" } else { ($dayNames -join "/") }
$steps = if ($cfg.pipeline -eq "redesign") { "discover -> shortlist -> documents" } else { "search -> score -> docs" }
$pipelineAction = New-ScheduledTaskAction -Execute $pythonw -Argument "-m jobpilot.scheduled --log" -WorkingDirectory $root

$weeklyTrigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $days[$cfg.weekly_day] -At $cfg.weekly_time
$weeklyAction = New-ScheduledTaskAction -Execute $pythonw -Argument "-m jobpilot.weekly" -WorkingDirectory $root

# Catch-up: sign-in, unlock, and wake from sleep (Power-Troubleshooter 1) or modern standby (Kernel-Power 507).
$user = "$env:USERDOMAIN\$env:USERNAME"
$taskNs = "Root/Microsoft/Windows/TaskScheduler"
$logonTrigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$logonTrigger.Delay = "PT2M"
$unlockTrigger = New-CimInstance -CimClass (Get-CimClass -Namespace $taskNs -ClassName MSFT_TaskSessionStateChangeTrigger) -ClientOnly
$unlockTrigger.StateChange = 8  # session unlock
$unlockTrigger.UserId = $user
$unlockTrigger.Delay = "PT2M"
$unlockTrigger.Enabled = $true
$wakeTrigger = New-CimInstance -CimClass (Get-CimClass -Namespace $taskNs -ClassName MSFT_TaskEventTrigger) -ClientOnly
$wakeTrigger.Subscription = '<QueryList><Query Id="0" Path="System"><Select Path="System">' +
    "*[System[Provider[@Name='Microsoft-Windows-Power-Troubleshooter'] and EventID=1]] or " +
    "*[System[Provider[@Name='Microsoft-Windows-Kernel-Power'] and EventID=507]]" +
    '</Select></Query></QueryList>'
$wakeTrigger.Delay = "PT2M"
$wakeTrigger.Enabled = $true
$catchupAction = New-ScheduledTaskAction -Execute $pythonw -Argument "-m jobpilot.scheduled --catch-up" -WorkingDirectory $root

$plan = @(
    @{ Name = $cfg.tasks.pipeline; Trigger = $pipelineTrigger; Action = $pipelineAction
       Text = "$dayText $when`: $steps" },
    @{ Name = $cfg.tasks.weekly; Trigger = $weeklyTrigger; Action = $weeklyAction
       Text = "every $($days[$cfg.weekly_day]) at $($cfg.weekly_time): weekly answer-review reminder" },
    @{ Name = $cfg.tasks.catchup; Trigger = @($logonTrigger, $unlockTrigger, $wakeTrigger); Action = $catchupAction
       Text = "on sign-in, unlock and wake: $steps only if the last search is overdue" }
)
foreach ($task in $plan) {
    if ($WhatIf) { "Would register: $($task.Name) ($($task.Text))"; continue }
    Register-ScheduledTask -TaskName $task.Name -Trigger $task.Trigger -Action $task.Action -Principal $principal `
        -Settings $settings -Description "JobPilot ($root): $($task.Text)" -Force | Out-Null
    $info = Get-ScheduledTaskInfo -TaskName $task.Name
    "Registered: $($task.Name) ($($task.Text)); next run $($info.NextRunTime)"
}
