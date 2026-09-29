# Registers the four Task Scheduler jobs (plan section 10). Times are defined in UTC and converted to this PC's clock,
# so on an IST machine they land at 13:35, 21:35, 02:35 (+1 day) and Sunday 09:00.
#   powershell -ExecutionPolicy Bypass -File scripts\install_tasks.ps1          # install / update
#   powershell -ExecutionPolicy Bypass -File scripts\install_tasks.ps1 -Remove
param([switch]$Remove)
$Root = Split-Path -Parent $PSScriptRoot
$Runner = Join-Path $Root "scripts\run_cycle.ps1"
$Offset = [TimeZoneInfo]::Local.GetUtcOffset((Get-Date))
$Weekdays = @("Monday", "Tuesday", "Wednesday", "Thursday", "Friday")
$AllDays = @("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")

function LocalSlot([int]$h, [int]$m, [string[]]$utcDays) {
    $t = [datetime]::Today.AddHours($h).AddMinutes($m).Add($Offset)
    $shift = ($t.Date - [datetime]::Today).Days
    $days = $utcDays | ForEach-Object { $AllDays[(($AllDays.IndexOf($_) + $shift) % 7 + 7) % 7] }
    return @{ At = $t.ToString("HH:mm"); Days = $days }
}

$jobs = @(
    @{ Name = "TradingBrain asia";    Args = "-Session asia";    Slot = (LocalSlot 8 5 $Weekdays) },
    @{ Name = "TradingBrain london";  Args = "-Session london";  Slot = (LocalSlot 16 5 $Weekdays) },
    @{ Name = "TradingBrain newyork"; Args = "-Session newyork"; Slot = (LocalSlot 21 5 $Weekdays) },
    @{ Name = "TradingBrain weekly";  Args = "-Weekly";          Slot = (LocalSlot 3 30 @("Sunday")) }
)
foreach ($j in $jobs) {
    Unregister-ScheduledTask -TaskName $j.Name -Confirm:$false -ErrorAction SilentlyContinue
    if ($Remove) { Write-Host "removed $($j.Name)"; continue }
    $action = New-ScheduledTaskAction -Execute "powershell.exe" `
        -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$Runner`" $($j.Args)" -WorkingDirectory $Root
    $trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $j.Slot.Days -At $j.Slot.At
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 2) -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName $j.Name -Action $action -Trigger $trigger -Settings $settings `
        -Description "EURUSD/GBPUSD Pattern Brain" | Out-Null
    Write-Host ("{0,-22} {1} {2}" -f $j.Name, $j.Slot.At, ($j.Slot.Days -join ","))
}
