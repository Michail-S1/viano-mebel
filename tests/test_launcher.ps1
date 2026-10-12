# Run with Windows PowerShell 5.1: powershell -NoProfile -File tests\test_launcher.ps1
$ErrorActionPreference = 'Stop'
$taskProject = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
. (Join-Path $taskProject 'launcher\Bootstrap.ps1')
$script:LauncherChecks = 0
function Assert-Launcher {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) { throw ('FAIL: ' + $Message) }
    $script:LauncherChecks++
}
function Assert-LauncherThrows {
    param([scriptblock]$Action, [string]$Pattern)
    $taskCaught = $null
    try { & $Action | Out-Null } catch { $taskCaught = $_ }
    Assert-Launcher ($null -ne $taskCaught) 'Expected an error'
    if ($Pattern) { Assert-Launcher ($taskCaught.Exception.Message -match $Pattern) ('Error text: ' + $taskCaught.Exception.Message) }
}
$taskQaRoot = [IO.Path]::GetFullPath((Join-Path $taskProject '.qa'))
$script:LauncherFixture = [IO.Path]::GetFullPath((Join-Path $taskQaRoot ('launcher-' + [Guid]::NewGuid().ToString('N'))))
New-Item -ItemType Directory -Path $script:LauncherFixture -Force | Out-Null
try {
    Assert-VianoApplication $taskProject
    $taskActual = Find-VianoPython
    Assert-Launcher ($null -ne $taskActual) 'Actual compatible interpreter found'
    Assert-Launcher (([version]$taskActual.Version) -ge [version]'3.12') 'Actual Python version'
    Assert-Launcher ($null -eq (Test-VianoPython (Join-Path $script:LauncherFixture 'missing.exe'))) 'Missing executable rejected'

    & {
        $taskFake = Join-Path $script:LauncherFixture 'probe.exe'
        [IO.File]::WriteAllText($taskFake,'test fixture, never executed')
        $script:ProbeVersion = '3.11.9'
        function Invoke-VianoCapturedCommand {
            param($Executable,$Arguments,$TimeoutSeconds)
            return [pscustomobject]@{ExitCode=0;StdOut=(@{path=$Executable;version=$script:ProbeVersion}|ConvertTo-Json -Compress);StdErr=''}
        }
        Assert-Launcher ($null -eq (Test-VianoPython $taskFake)) 'Old Python 3.11 rejected'
        $script:ProbeVersion='3.13.16'
        Assert-Launcher ((Test-VianoPython $taskFake).Version -eq '3.13.16') 'Compatible Python accepted'
        $script:ProbeVersion='4.0.0'
        Assert-Launcher ($null -eq (Test-VianoPython $taskFake)) 'Incompatible next major rejected'
    }
    & {
        function Invoke-VianoCapturedCommand { throw 'Store alias must not execute' }
        Assert-Launcher ($null -eq (Test-VianoPython 'C:\Users\Test\AppData\Local\Microsoft\WindowsApps\python.exe')) 'Store alias skipped'
    }
    & {
        function Get-Command {
            [CmdletBinding()]
            param($Name,[switch]$All)
            if ($Name -eq 'py.exe') {
                return [pscustomobject]@{Source='C:\Users\Test\AppData\Local\Microsoft\WindowsApps\py.exe'}
            }
        }
        function Get-ChildItem {
            [CmdletBinding()]
            param($Path)
        }
        function Invoke-VianoCapturedCommand { throw 'Store launcher alias must not execute' }
        Assert-Launcher (@(Get-VianoPythonCandidates).Count -ge 1) 'Store launcher alias skipped during discovery'
    }
    & {
        function Test-VianoCatalogReady { param($Port) return $true }
        function Start-Process { throw 'A running catalog must not launch another Python process' }
        Start-VianoCatalog -Python @{Path='never.exe'} -AppRoot $taskProject -NoBrowser
        Assert-Launcher $true 'Existing catalog reuses its process'
    }
    Assert-Launcher ((Get-VianoArchitecture -MachineArchitecture AMD64 -NativeArchitecture '') -eq 'amd64') 'x64 architecture'
    Assert-Launcher ((Get-VianoArchitecture -MachineArchitecture x86 -NativeArchitecture AMD64) -eq 'amd64') '32-bit launcher on x64 OS'
    Assert-Launcher ((Get-VianoArchitecture -MachineArchitecture ARM64 -NativeArchitecture '') -eq 'arm64') 'ARM64 architecture'
    Assert-Launcher ((Get-VianoArchitecture -MachineArchitecture x86 -NativeArchitecture '') -eq 'x86') 'x86 architecture'
    Assert-LauncherThrows { Get-VianoArchitecture -MachineArchitecture Mystery -NativeArchitecture '' } 'архитектур'
    foreach ($taskArch in @('amd64','x86','arm64')) {
        $taskEntry=Get-VianoInstaller $taskProject $taskArch
        Assert-Launcher ($taskEntry.url.StartsWith('https://www.python.org/ftp/python/')) 'Official HTTPS installer'
        Assert-Launcher ($taskEntry.sha256 -match '^[a-f0-9]{64}$') 'Pinned SHA256'
    }
    Assert-LauncherThrows { Get-VianoInstaller $taskProject 'unknown' } 'архитектур'

    $script:DownloadBytes=[Text.Encoding]::UTF8.GetBytes('deterministic installer fixture; never execute')
    $taskHashSource=Join-Path $script:LauncherFixture 'hash-source.bin'
    [IO.File]::WriteAllBytes($taskHashSource,$script:DownloadBytes)
    $script:MockInstaller=[pscustomobject]@{
        filename='python-3.13.16-amd64.exe'
        url='https://www.python.org/ftp/python/3.13.16/python-3.13.16-amd64.exe'
        sha256=(Get-FileHash -LiteralPath $taskHashSource -Algorithm SHA256).Hash
    }
    & {
        $script:DownloadCalls=0
        function Invoke-WebRequest {
            [CmdletBinding()]param($Uri,$OutFile,[switch]$UseBasicParsing,[int]$TimeoutSec)
            $script:DownloadCalls++
            [IO.File]::WriteAllBytes($OutFile,$script:DownloadBytes)
        }
        $taskCache=Join-Path $script:LauncherFixture 'Cache with spaces Цвет'
        $taskFile=Get-VianoInstallerFile $script:MockInstaller $taskCache
        Assert-Launcher (Test-VianoInstallerChecksum $taskFile $script:MockInstaller.sha256) 'Download validated'
        Assert-Launcher ($script:DownloadCalls -eq 1) 'Download exactly once'
        [IO.File]::WriteAllText($taskFile,'corrupt old cache')
        $taskFile=Get-VianoInstallerFile $script:MockInstaller $taskCache
        Assert-Launcher ($script:DownloadCalls -eq 2) 'Corrupt cached installer redownloaded'
        $taskAgain=Get-VianoInstallerFile $script:MockInstaller $taskCache
        Assert-Launcher ($script:DownloadCalls -eq 2 -and $taskAgain -eq $taskFile) 'Verified cached installer reused offline'
    }
    & {
        $script:DownloadCalls=0
        function Invoke-WebRequest {
            [CmdletBinding()]param($Uri,$OutFile,[switch]$UseBasicParsing,[int]$TimeoutSec)
            $script:DownloadCalls++
            [IO.File]::WriteAllText($OutFile,'broken download')
        }
        $taskCache=Join-Path $script:LauncherFixture 'Corrupt download'
        Assert-LauncherThrows { Get-VianoInstallerFile $script:MockInstaller $taskCache } 'Контрольная сумма'
        Assert-Launcher ($script:DownloadCalls -eq 2) 'Corrupt download retried once'
        Assert-Launcher (@(Get-ChildItem -LiteralPath $taskCache -File).Count -eq 0) 'Corrupt files removed, never cached'
    }
    & {
        $script:DownloadCalls=0
        function Invoke-WebRequest {
            [CmdletBinding()]param($Uri,$OutFile,[switch]$UseBasicParsing,[int]$TimeoutSec)
            $script:DownloadCalls++;throw 'offline test'
        }
        Assert-LauncherThrows { Get-VianoInstallerFile $script:MockInstaller (Join-Path $script:LauncherFixture 'Offline') } 'Проверьте интернет'
        Assert-Launcher ($script:DownloadCalls -eq 2) 'Offline download bounded retry'
    }
    & {
        $taskBad=[pscustomobject]@{filename='..\outside.exe';url=$script:MockInstaller.url;sha256=$script:MockInstaller.sha256}
        Assert-LauncherThrows { Get-VianoInstallerFile $taskBad (Join-Path $script:LauncherFixture 'Traversal') } 'папка'
    }

    & {
        function Find-VianoPython { return [pscustomobject]@{Path='C:\Existing\python.exe';Version='3.12.10'} }
        function Install-VianoPython { throw 'No unnecessary installation' }
        Assert-Launcher ((Resolve-VianoPython $taskProject).Path -eq 'C:\Existing\python.exe') 'Existing Python bypasses download/install'
    }
    & {
        $script:FindCalls=0;$script:InstallCalls=0
        function Find-VianoPython { $script:FindCalls++;return $null }
        function Install-VianoPython {
            param($AppRoot)
            $script:InstallCalls++;return [pscustomobject]@{Path='C:\New\python.exe';Version='3.13.16'}
        }
        Assert-Launcher ((Resolve-VianoPython $taskProject).Path -eq 'C:\New\python.exe') 'Missing Python installed automatically'
        Assert-Launcher ($script:InstallCalls -eq 1 -and $script:FindCalls -eq 2) 'Rechecks after installation mutex'
    }
    & {
        $script:InstalledCode=0;$script:UsableInstalled=$true;$script:InstallProcessCalls=0
        function Get-VianoInstaller { return $script:MockInstaller }
        function Get-VianoInstallRoot { return Join-Path $script:LauncherFixture 'User install with spaces\Python313' }
        function Get-VianoInstallerFile { return 'C:\Verified\python-3.13.16-amd64.exe' }
        function Test-VianoInstallerChecksum { return $true }
        function Start-Process {
            param($FilePath,$ArgumentList,[switch]$Wait,[switch]$PassThru,$WindowStyle)
            $script:InstallProcessCalls++
            $script:LastInstallerArgs=$ArgumentList;$script:LastInstallerWindow=$WindowStyle
            return [pscustomobject]@{ExitCode=$script:InstalledCode}
        }
        function Test-VianoPython {
            param($Path)
            if ($script:UsableInstalled) { return [pscustomobject]@{Path=$Path;Version='3.13.16'} }
            return $null
        }
        $taskInstalled=Install-VianoPython $taskProject
        Assert-Launcher ($taskInstalled.Version -eq '3.13.16') 'Installed interpreter revalidated'
        Assert-Launcher ($script:LastInstallerWindow -eq 'Hidden') 'Installer hidden'
        foreach ($taskOption in @('/quiet','InstallAllUsers=0','PrependPath=0','Include_launcher=0','Include_pip=0')) {
            Assert-Launcher ($script:LastInstallerArgs -contains $taskOption) ('Installer option: ' + $taskOption)
        }
        Assert-Launcher ($script:LastInstallerArgs -contains ('TargetDir="' + (Get-VianoInstallRoot) + '"')) 'Quoted custom user directory'
        $script:InstalledCode=1603
        Assert-LauncherThrows { Install-VianoPython $taskProject } '1603'
        $script:InstalledCode=3010
        Assert-Launcher ((Install-VianoPython $taskProject).Version -eq '3.13.16') 'Reboot code allowed when runtime usable'
        $script:UsableInstalled=$false
        Assert-LauncherThrows { Install-VianoPython $taskProject } 'перезагруз'
        $script:InstalledCode=0
        Assert-LauncherThrows { Install-VianoPython $taskProject } 'не запускается'
        function Test-VianoInstallerChecksum { return $false }
        $taskBefore=$script:InstallProcessCalls
        Assert-LauncherThrows { Install-VianoPython $taskProject } 'Проверка установщика'
        Assert-Launcher ($script:InstallProcessCalls -eq $taskBefore) 'Bad checksum never executed'
    }
    & {
        function Invoke-RestMethod { return [pscustomobject]@{other='unrelated listener'} }
        Assert-Launcher (-not (Test-VianoCatalogReady)) 'Unrelated port listener rejected'
    }
    Assert-LauncherThrows { Assert-VianoApplication (Join-Path $script:LauncherFixture 'Incomplete unpack') } 'Распакуйте'
    # A real probe and entry-point execution from a copied folder with Cyrillic/spaces.
    $taskPortable=Join-Path $script:LauncherFixture 'Распаковка с пробелами'
    New-Item -ItemType Directory -Path $taskPortable -Force | Out-Null
    foreach ($taskName in @('Start.ps1','launcher','backend','static','vendor','server.py')) {
        Copy-Item -LiteralPath (Join-Path $taskProject $taskName) -Destination $taskPortable -Recurse
    }
    $taskShell=Join-Path $env:WINDIR 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $taskResult=Invoke-VianoCapturedCommand $taskShell ('-NoProfile -ExecutionPolicy Bypass -File "' + (Join-Path $taskPortable 'Start.ps1') + '" -CheckOnly') 20
    Assert-Launcher ($taskResult -and $taskResult.ExitCode -eq 0) 'Copied launcher runs from Cyrillic/spaces folder'
    Assert-Launcher (-not (Test-Path -LiteralPath (Join-Path $taskPortable 'data'))) 'CheckOnly never creates or changes catalog data'
    $taskListener = New-Object Net.Sockets.TcpListener([Net.IPAddress]::Loopback,0)
    $taskListener.Start(); $taskPort=$taskListener.LocalEndpoint.Port; $taskListener.Stop()
    $taskFreshProcess=$null
    try {
        $taskFreshProcess=Start-VianoCatalog -Python $taskActual -AppRoot $taskPortable -NoBrowser -Port $taskPort -PassThru
        $taskState=Invoke-RestMethod -Uri ('http://127.0.0.1:' + $taskPort + '/api/state')
        Assert-Launcher ($taskState.stats.total -eq 0) 'Fresh copied app starts with its own empty database'
        Assert-Launcher ([IO.File]::Exists((Join-Path $taskPortable 'data\catalog.sqlite3'))) 'Fresh app database belongs to copied folder'
    } finally {
        if ($taskFreshProcess) {
            $taskFreshProcess.Refresh()
            if (-not $taskFreshProcess.HasExited) {
                $taskOwned=Get-CimInstance Win32_Process -Filter ('ProcessId=' + $taskFreshProcess.Id)
                if ($taskOwned.CommandLine.Contains((Join-Path $taskPortable 'server.py'))) {
                    Stop-Process -Id $taskFreshProcess.Id
                    $taskFreshProcess.WaitForExit(5000) | Out-Null
                } else { throw 'Unexpected test process' }
            }
        }
    }
    Write-Host ('PASS: ' + $script:LauncherChecks + ' launcher checks') -ForegroundColor Green
} finally {
    $taskResolved=[IO.Path]::GetFullPath($script:LauncherFixture)
    if ($taskResolved.StartsWith($taskQaRoot.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) {
        Remove-Item -LiteralPath $taskResolved -Recurse -Force
    } else { throw 'Unsafe test cleanup path' }
}
