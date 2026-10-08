<#
Registers JobPilot's two Windows Task Scheduler tasks for the current user.
Times come from the dashboard Settings (schedule + weekly review); run this again after changing them.

  JobPilot Search-Score-Docs      daily from <start>, every <every_hours> h for the window  -> python -m jobpilot.scheduled
  JobPilot Weekly-Answer-Review   weekly on <day> at <time>                                   -> python -m jobpilot.weekly

Runs only while you are logged on (the search uses your signed-in browser profile), starts a missed
run as soon as the PC is available again, never runs two passes at once, and needs no admin rights.

  powershell -ExecutionPolicy Bypass -File scripts\register_tasks.ps1             # register / update
  powershell -ExecutionPolicy Bypass -File scripts\register_tasks.ps1 -WhatIf     # show what it would do
  powershell -ExecutionPolicy Bypass -File scripts\register_tasks.ps1 -Unregister # remove both tasks
#>
param([switch]$Unregister, [switch]$WhatIf)
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$pythonw = Join-Path $root ".venv\Scripts\pythonw.exe"
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $pythonw)) { throw "Missing $pythonw. Create the venv first (see README)." }

$cfg = & $python -m jobpilot.scheduled --print-config | ConvertFrom-Json
$names = @($cfg.tasks.pipeline, $cfg.tasks.weekly)

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

# Pipeline: daily trigger at <start>, repeated every <every_hours> h for the window.
$pipelineTrigger = New-ScheduledTaskTrigger -Daily -At $cfg.start
$repeat = New-ScheduledTaskTrigger -Once -At $cfg.start -RepetitionInterval (New-TimeSpan -Hours $cfg.every_hours) `
    -RepetitionDuration (New-TimeSpan -Minutes $cfg.window_minutes)
$pipelineTrigger.Repetition = $repeat.Repetition
$pipelineAction = New-ScheduledTaskAction -Execute $pythonw -Argument "-m jobpilot.scheduled --log" -WorkingDirectory $root

$weeklyTrigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $days[$cfg.weekly_day] -At $cfg.weekly_time
$weeklyAction = New-ScheduledTaskAction -Execute $pythonw -Argument "-m jobpilot.weekly" -WorkingDirectory $root

$plan = @(
    @{ Name = $cfg.tasks.pipeline; Trigger = $pipelineTrigger; Action = $pipelineAction
       Text = "daily from $($cfg.start), every $($cfg.every_hours) h for $($cfg.window_minutes / 60) h: search -> score -> docs" },
    @{ Name = $cfg.tasks.weekly; Trigger = $weeklyTrigger; Action = $weeklyAction
       Text = "every $($days[$cfg.weekly_day]) at $($cfg.weekly_time): weekly answer-review reminder" }
)
foreach ($task in $plan) {
    if ($WhatIf) { "Would register: $($task.Name) ($($task.Text))"; continue }
    Register-ScheduledTask -TaskName $task.Name -Trigger $task.Trigger -Action $task.Action -Principal $principal `
        -Settings $settings -Description "JobPilot ($root): $($task.Text)" -Force | Out-Null
    $info = Get-ScheduledTaskInfo -TaskName $task.Name
    "Registered: $($task.Name) ($($task.Text)); next run $($info.NextRunTime)"
}
