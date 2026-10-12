[CmdletBinding()]
param([string]$OutputPath, [switch]$NoPause)
$ErrorActionPreference = 'Stop'
$taskExit = 0
try {
    . (Join-Path $PSScriptRoot 'launcher\Bootstrap.ps1')
    Assert-VianoApplication $PSScriptRoot
    $taskPython = Resolve-VianoPython $PSScriptRoot
    Write-Host 'Создаём ZIP приложения и вашей базы. Каталог можно оставить открытым.' -ForegroundColor Cyan
    $taskArguments = @('-E','-s','-X','utf8',(Join-Path $PSScriptRoot 'launcher\package.py'),'--app-root',$PSScriptRoot)
    if ($OutputPath) { $taskArguments += @('--output',$OutputPath) }
    & $taskPython.Path @taskArguments
    if ($LASTEXITCODE -ne 0) { throw 'Архив не создан. Причина указана выше.' }
} catch {
    Write-Host ('Ошибка: ' + $_.Exception.Message) -ForegroundColor Red
    $taskExit = 1
}
if (-not $NoPause) { Read-Host 'Нажмите Enter, чтобы закрыть окно' | Out-Null }
exit $taskExit
