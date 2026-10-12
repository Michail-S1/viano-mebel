[CmdletBinding()]
param([string]$OutputPath, [string]$PackageUrl, [switch]$NoPause)
$ErrorActionPreference = 'Stop'
$taskExit = 0
try {
    . (Join-Path $PSScriptRoot 'launcher\Bootstrap.ps1')
    $taskPython = Resolve-VianoPython $PSScriptRoot
    $taskArguments = @('-E','-s','-X','utf8',(Join-Path $PSScriptRoot 'launcher\release.py'),'--app-root',$PSScriptRoot)
    if ($OutputPath) { $taskArguments += @('--output',$OutputPath) }
    if ($PackageUrl) { $taskArguments += @('--url',$PackageUrl) }
    & $taskPython.Path @taskArguments
    if ($LASTEXITCODE -ne 0) { throw 'Пакет обновления не создан.' }
} catch {
    Write-Host ('Ошибка: ' + $_.Exception.Message) -ForegroundColor Red
    $taskExit = 1
}
if (-not $NoPause) { Read-Host 'Нажмите Enter, чтобы закрыть окно' | Out-Null }
exit $taskExit
