[CmdletBinding()]
param([switch]$CheckOnly, [switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
$taskRoot = $PSScriptRoot
try {
    . (Join-Path $taskRoot 'launcher\Bootstrap.ps1')
    Assert-VianoApplication $taskRoot
    if ($CheckOnly) {
        $taskPython = Find-VianoPython
        if (-not $taskPython) { throw 'Подходящий Python не найден. Обычный запуск Start.cmd скачает и установит его автоматически.' }
    } else {
        $taskPython = Resolve-VianoPython $taskRoot
    }
    Write-Host ('Python ' + $taskPython.Version + ': ' + $taskPython.Path)
    if (-not $CheckOnly) { Start-VianoCatalog -Python $taskPython -AppRoot $taskRoot -NoBrowser:$NoBrowser }
    exit 0
} catch {
    Write-Host ('Ошибка запуска: ' + $_.Exception.Message) -ForegroundColor Red
    exit 1
}
