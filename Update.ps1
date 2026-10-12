[CmdletBinding()]
param([string]$Source, [switch]$NoPause)
$ErrorActionPreference = 'Stop'
$taskExit = 0
try {
    . (Join-Path $PSScriptRoot 'launcher\Bootstrap.ps1')
    $taskPython = Resolve-VianoPython $PSScriptRoot
    $taskArguments = @('-E','-s','-X','utf8',(Join-Path $PSScriptRoot 'launcher\update.py'),'--app-root',$PSScriptRoot)
    if ($Source) { $taskArguments += @('--source',$Source) }
    & $taskPython.Path @taskArguments
    if ($LASTEXITCODE -ne 0) { throw 'Обновление не установлено. Причина указана выше.' }
} catch {
    Write-Host ('Ошибка: ' + $_.Exception.Message) -ForegroundColor Red
    $taskExit = 1
}
if (-not $NoPause) { Read-Host 'Нажмите Enter, чтобы закрыть окно' | Out-Null }
exit $taskExit
