# Windows PowerShell 5.1 compatible. Called only by Start.ps1.
Set-StrictMode -Version 2.0

function Invoke-VianoCapturedCommand {
    param([string]$Executable, [string]$Arguments, [int]$TimeoutSeconds = 8)
    $taskProcess = New-Object System.Diagnostics.Process
    try {
        $taskProcess.StartInfo.FileName = $Executable
        $taskProcess.StartInfo.Arguments = $Arguments
        $taskProcess.StartInfo.UseShellExecute = $false
        $taskProcess.StartInfo.CreateNoWindow = $true
        $taskProcess.StartInfo.RedirectStandardOutput = $true
        $taskProcess.StartInfo.RedirectStandardError = $true
        if (-not $taskProcess.Start()) { return $null }
        if (-not $taskProcess.WaitForExit($TimeoutSeconds * 1000)) {
            $taskProcess.Kill()
            $taskProcess.WaitForExit()
            return $null
        }
        return [pscustomobject]@{
            ExitCode = $taskProcess.ExitCode
            StdOut = $taskProcess.StandardOutput.ReadToEnd()
            StdErr = $taskProcess.StandardError.ReadToEnd()
        }
    } catch { return $null } finally { $taskProcess.Dispose() }
}

function Get-VianoInstallRoot {
    $taskLocal = [Environment]::GetFolderPath('LocalApplicationData')
    if (-not $taskLocal) { throw 'Не удалось определить папку пользователя Windows.' }
    return Join-Path $taskLocal 'VianoMebel\Python313'
}

function Get-VianoPythonCandidates {
    $taskCandidates = New-Object 'System.Collections.Generic.List[string]'
    $taskCandidates.Add((Join-Path (Get-VianoInstallRoot) 'python.exe'))
    $taskProfile = [Environment]::GetFolderPath('UserProfile')
    if ($taskProfile) {
        $taskCandidates.Add((Join-Path $taskProfile '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'))
    }
    foreach ($taskName in @('python.exe','python3.exe')) {
        foreach ($taskCommand in @(Get-Command $taskName -All -ErrorAction SilentlyContinue)) {
            if ($taskCommand.Source) { $taskCandidates.Add($taskCommand.Source) }
        }
    }
    foreach ($taskRegistryRoot in @(
        'HKCU:\Software\Python\PythonCore',
        'HKLM:\Software\Python\PythonCore',
        'HKLM:\Software\WOW6432Node\Python\PythonCore'
    )) {
        foreach ($taskKey in @(Get-ChildItem -Path $taskRegistryRoot -ErrorAction SilentlyContinue)) {
            $taskInstallKey = Get-Item -LiteralPath ($taskKey.PSPath + '\InstallPath') -ErrorAction SilentlyContinue
            if ($taskInstallKey) {
                $taskExecutable = $taskInstallKey.GetValue('ExecutablePath')
                $taskDirectory = $taskInstallKey.GetValue('')
                if ($taskExecutable) { $taskCandidates.Add([string]$taskExecutable) }
                elseif ($taskDirectory) { $taskCandidates.Add((Join-Path ([string]$taskDirectory) 'python.exe')) }
            }
        }
    }
    $taskLauncher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($taskLauncher -and $taskLauncher.Source -notmatch '\\Microsoft\\WindowsApps\\') {
        $taskListing = Invoke-VianoCapturedCommand $taskLauncher.Source '-0p'
        if ($taskListing -and $taskListing.ExitCode -eq 0) {
            foreach ($taskLine in ($taskListing.StdOut -split '[\r\n]+')) {
                if ($taskLine -match '(?<exe>[A-Za-z]:\\.+\.exe)\s*$') {
                    $taskCandidates.Add($Matches['exe'].Trim())
                }
            }
        }
    }
    $taskSeen = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    foreach ($taskCandidate in $taskCandidates) {
        if ($taskSeen.Add($taskCandidate)) { Write-Output $taskCandidate }
    }
}

function Test-VianoPython {
    param([string]$Path)
    # Do not invoke Windows Store aliases, which can open an installation UI.
    if (-not $Path -or $Path -match '\\Microsoft\\WindowsApps\\' -or -not [IO.File]::Exists($Path)) { return $null }
    $taskProbe = "import sys,json,sqlite3,http.server,decimal; sys.exit(1) if not ((3,12) <= sys.version_info[:2] < (4,0)) else None; print(json.dumps(dict(path=sys.executable,version='.'.join(map(str,sys.version_info[:3])))))"
    $taskResult = Invoke-VianoCapturedCommand $Path ('-I -S -c "' + $taskProbe + '"')
    if (-not $taskResult -or $taskResult.ExitCode -ne 0) { return $null }
    try {
        $taskInfo = $taskResult.StdOut.Trim() | ConvertFrom-Json
        $taskVersion = [version]$taskInfo.version
        if ($taskVersion.Major -ne 3 -or $taskVersion.Minor -lt 12 -or -not [IO.File]::Exists($taskInfo.path)) { return $null }
        return [pscustomobject]@{ Path = [string]$taskInfo.path; Version = [string]$taskInfo.version }
    } catch { return $null }
}

function Find-VianoPython {
    foreach ($taskCandidate in @(Get-VianoPythonCandidates)) {
        $taskPython = Test-VianoPython $taskCandidate
        if ($taskPython) { return $taskPython }
    }
    return $null
}

function Get-VianoArchitecture {
    param([string]$MachineArchitecture = $env:PROCESSOR_ARCHITECTURE, [string]$NativeArchitecture = $env:PROCESSOR_ARCHITEW6432)
    $taskArchitecture = $NativeArchitecture
    if (-not $taskArchitecture) { $taskArchitecture = $MachineArchitecture }
    if (-not $taskArchitecture) { throw 'Не удалось определить архитектуру Windows.' }
    switch ($taskArchitecture.ToUpperInvariant()) {
        'AMD64' { return 'amd64' }
        'ARM64' { return 'arm64' }
        'X86' { return 'x86' }
        default { throw ('Неизвестная архитектура Windows: ' + $taskArchitecture) }
    }
}

function Get-VianoInstaller {
    param([string]$AppRoot, [string]$Architecture = (Get-VianoArchitecture))
    $taskManifest = Get-Content -LiteralPath (Join-Path $AppRoot 'launcher\python-installers.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    $taskEntry = $taskManifest.installers.PSObject.Properties[$Architecture]
    if (-not $taskEntry) { throw 'Для этой архитектуры нет установщика Python.' }
    $taskRecord = $taskEntry.Value
    $taskUri = [Uri]$taskRecord.url
    if ($taskUri.Scheme -ne 'https' -or $taskUri.Host -ne 'www.python.org' -or
        $taskRecord.filename -notmatch '^python-\d+\.\d+\.\d+(?:-(?:amd64|arm64))?\.exe$' -or
        -not $taskUri.AbsolutePath.EndsWith('/' + $taskRecord.filename) -or
        $taskRecord.sha256 -notmatch '^[a-fA-F0-9]{64}$') {
        throw 'Некорректные сведения об официальном установщике Python.'
    }
    return $taskRecord
}

function Test-VianoInstallerChecksum {
    param([string]$Path, [string]$Expected)
    if (-not [IO.File]::Exists($Path)) { return $false }
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash -ieq $Expected
}

function Get-VianoInstallerFile {
    param($Installer, [string]$CacheDirectory)
    $taskCache = [IO.Path]::GetFullPath($CacheDirectory)
    New-Item -ItemType Directory -Path $taskCache -Force | Out-Null
    $taskDestination = [IO.Path]::GetFullPath((Join-Path $taskCache $Installer.filename))
    if (-not $taskDestination.StartsWith($taskCache.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Некорректная папка установщика.'
    }
    if (Test-VianoInstallerChecksum $taskDestination $Installer.sha256) { return $taskDestination }
    $taskDownload = Join-Path $taskCache ('download-' + [Guid]::NewGuid().ToString('N') + '.part')
    $taskPreviousProgress = $ProgressPreference
    try {
        $ProgressPreference = 'SilentlyContinue'
        [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
        for ($taskAttempt = 1; $taskAttempt -le 2; $taskAttempt++) {
            try {
                Invoke-WebRequest -Uri $Installer.url -OutFile $taskDownload -UseBasicParsing -TimeoutSec 90 -ErrorAction Stop
                if (-not (Test-VianoInstallerChecksum $taskDownload $Installer.sha256)) {
                    throw 'Контрольная сумма установщика не совпала.'
                }
                Move-Item -LiteralPath $taskDownload -Destination $taskDestination -Force
                return $taskDestination
            } catch {
                if ([IO.File]::Exists($taskDownload)) { Remove-Item -LiteralPath $taskDownload -Force }
                if ($taskAttempt -eq 2) {
                    throw ('Не удалось скачать проверенный Python с python.org. Проверьте интернет и запустите Start.cmd ещё раз. ' + $_.Exception.Message)
                }
            }
        }
    } finally {
        $ProgressPreference = $taskPreviousProgress
        if ([IO.File]::Exists($taskDownload)) { Remove-Item -LiteralPath $taskDownload -Force }
    }
}

function Install-VianoPython {
    param([string]$AppRoot)
    $taskInstaller = Get-VianoInstaller $AppRoot
    $taskTarget = Get-VianoInstallRoot
    $taskCache = Join-Path ([IO.Path]::GetDirectoryName($taskTarget)) 'Installers'
    Write-Host 'Скачиваем официальный Python. Это нужно только при первом запуске...' -ForegroundColor Cyan
    $taskFile = Get-VianoInstallerFile $taskInstaller $taskCache
    # Recheck immediately before executing a downloaded or cached installer.
    if (-not (Test-VianoInstallerChecksum $taskFile $taskInstaller.sha256)) { throw 'Проверка установщика Python не пройдена.' }
    $taskLog = Join-Path $taskCache ('install-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.log')
    $taskArgs = @(
        '/quiet', '/log', ('"' + $taskLog + '"'),
        'InstallAllUsers=0', ('TargetDir="' + $taskTarget + '"'),
        'Include_launcher=0', 'InstallLauncherAllUsers=0', 'PrependPath=0', 'AppendPath=0',
        'AssociateFiles=0', 'Shortcuts=0', 'Include_doc=0', 'Include_test=0',
        'Include_tcltk=0', 'Include_pip=0', 'Include_exe=1', 'Include_lib=1', 'Include_dev=1'
    )
    Write-Host 'Устанавливаем Python для текущего пользователя...' -ForegroundColor Cyan
    $taskInstallation = Start-Process -FilePath $taskFile -ArgumentList $taskArgs -Wait -PassThru -WindowStyle Hidden
    if ($taskInstallation.ExitCode -notin @(0, 3010)) {
        throw ('Установка Python завершилась с кодом ' + $taskInstallation.ExitCode + '. Журнал: ' + $taskLog)
    }
    $taskPython = Test-VianoPython (Join-Path $taskTarget 'python.exe')
    if (-not $taskPython) {
        if ($taskInstallation.ExitCode -eq 3010) { throw 'Для завершения установки Windows просит перезагрузку. После неё запустите Start.cmd.' }
        throw ('Python установлен, но не запускается. Журнал: ' + $taskLog)
    }
    return $taskPython
}

function Resolve-VianoPython {
    param([string]$AppRoot)
    $taskPython = Find-VianoPython
    if ($taskPython) { return $taskPython }
    $taskIdentity = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $taskMutex = New-Object Threading.Mutex($false, ('Local\VianoMebelPython-' + $taskIdentity))
    $taskAcquired = $false
    try {
        try { $taskAcquired = $taskMutex.WaitOne(1000) } catch [Threading.AbandonedMutexException] { $taskAcquired = $true }
        if (-not $taskAcquired) { throw 'Подготовка Python уже запущена в другом окне. Дождитесь её завершения.' }
        $taskPython = Find-VianoPython
        if ($taskPython) { return $taskPython }
        return Install-VianoPython $AppRoot
    } finally {
        if ($taskAcquired) { $taskMutex.ReleaseMutex() }
        $taskMutex.Dispose()
    }
}

function Assert-VianoApplication {
    param([string]$AppRoot)
    foreach ($taskRelative in @('server.py','backend\importer.py','backend\colors.py','backend\images.py','backend\dimensions.py','launcher\update.py','static\index.html','static\styles.css','static\app.js','vendor\xlrd\__init__.py','launcher\python-installers.json')) {
        if (-not [IO.File]::Exists((Join-Path $AppRoot $taskRelative))) {
            throw ('В папке каталога отсутствует ' + $taskRelative + '. Распакуйте всю папку IBD, затем запустите Start.cmd.')
        }
    }
}

function Test-VianoCatalogReady {
    param([int]$Port = 8765)
    try {
        $taskState = Invoke-RestMethod -Uri ('http://127.0.0.1:' + $Port + '/api/state') -TimeoutSec 1 -ErrorAction Stop
        return ($null -ne $taskState.version -and $null -ne $taskState.stats.total)
    } catch { return $false }
}

function Start-VianoCatalog {
    param($Python, [string]$AppRoot, [switch]$NoBrowser, [ValidateRange(1,65535)][int]$Port = 8765, [switch]$PassThru)
    if (Test-VianoCatalogReady -Port $Port) {
        $taskUrl = 'http://127.0.0.1:' + $Port + '/'
        if (-not $NoBrowser) { Start-Process -FilePath $taskUrl | Out-Null }
        Write-Host ('Каталог уже запущен: ' + $taskUrl) -ForegroundColor Green
        return
    }
    $taskLogs = Join-Path $AppRoot '.runtime\logs'
    New-Item -ItemType Directory -Path $taskLogs -Force | Out-Null
    $taskRun = [Guid]::NewGuid().ToString('N')
    $taskStdout = Join-Path $taskLogs ($taskRun + '.out.log')
    $taskStderr = Join-Path $taskLogs ($taskRun + '.err.log')
    $taskArgs = @('-E','-s','-X','utf8',('"' + (Join-Path $AppRoot 'server.py') + '"'))
    if ($NoBrowser) { $taskArgs += '--no-browser' }
    if ($Port -ne 8765) { $taskArgs += @('--port',[string]$Port) }
    $taskProcess = Start-Process -FilePath $Python.Path -ArgumentList $taskArgs -WorkingDirectory $AppRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput $taskStdout -RedirectStandardError $taskStderr
    for ($taskAttempt = 0; $taskAttempt -lt 400; $taskAttempt++) {
        if (Test-VianoCatalogReady -Port $Port) {
            Write-Host ('Виано Мебель запущена: http://127.0.0.1:' + $Port + '/') -ForegroundColor Green
            if ($PassThru) { return $taskProcess }
            return
        }
        if ($taskProcess.HasExited) { break }
        Start-Sleep -Milliseconds 300
        $taskProcess.Refresh()
    }
    $taskDetails = ''
    if ([IO.File]::Exists($taskStderr)) { $taskDetails = (Get-Content -LiteralPath $taskStderr -Encoding UTF8 -Tail 5) -join [Environment]::NewLine }
    throw ('Не удалось открыть каталог. ' + $taskDetails + [Environment]::NewLine + 'Журнал: ' + $taskStderr)
}
