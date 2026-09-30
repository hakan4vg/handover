# Drives the resident's own windows through Windows UI Automation for e2e runs.
#   uia.ps1 -List  [-Window "Add Download"]            names of the window's controls
#   uia.ps1 -Button Save [-Window "Add Download"]      invokes a button by its accessible name
# WebView2 fills its accessibility tree lazily, so both retry for -Seconds.
param([string]$Window = "Add Download", [string]$Button = "", [switch]$List, [int]$Seconds = 10)
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
$root = [System.Windows.Automation.AutomationElement]::RootElement
$all = [System.Windows.Automation.Condition]::TrueCondition
$descendants = [System.Windows.Automation.TreeScope]::Descendants
$deadline = (Get-Date).AddSeconds($Seconds)
do {
  $procs = @(Get-Process download-manager -ErrorAction SilentlyContinue | ForEach-Object Id)
  $win = $root.FindAll([System.Windows.Automation.TreeScope]::Children, $all) |
    Where-Object { $procs -contains $_.Current.ProcessId -and $_.Current.Name -eq $Window } |
    Select-Object -First 1
  if ($win) {
    $items = $win.FindAll($descendants, $all)
    if ($List) {
      $names = $items | Where-Object { $_.Current.Name } | ForEach-Object { $_.Current.Name }
      if (@($names).Count -gt 3) { $names; exit 0 }
    } else {
      $target = $items | Where-Object {
        $_.Current.ControlType -eq [System.Windows.Automation.ControlType]::Button -and $_.Current.Name -eq $Button
      } | Select-Object -First 1
      if ($target) {
        $target.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke()
        "invoked $Button"; exit 0
      }
    }
  }
  Start-Sleep -Milliseconds 300
} while ((Get-Date) -lt $deadline)
if (-not $win) { "no window '$Window'" } else { "nothing found in '$Window'" }
exit 1
