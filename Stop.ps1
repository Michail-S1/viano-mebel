$ErrorActionPreference = 'Stop'
$taskRoot = [IO.Path]::GetFullPath($PSScriptRoot)
$taskTarget = Join-Path $taskRoot 'server.py'
Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^python(w)?\.exe$' -and $_.CommandLine -and $_.CommandLine.Contains($taskTarget) } | ForEach-Object { Stop-Process -Id $_.ProcessId }
