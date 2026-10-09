#requires -Version 5.1
<#
.SYNOPSIS
Installs ContextHub ctx for the current Windows user, without administrator rights.
.DESCRIPTION
Requires Python 3.12 or newer. Downloads ctx.pyz and its SHA-256 checksum from
one HTTPS Hub origin. Redirects are rejected. Neither execution policy nor
machine PATH is changed. Use -ArchivePath and -Sha256 for an offline install.
.EXAMPLE
.\install-ctx.ps1
.EXAMPLE
.\install-ctx.ps1 -ArchivePath .\ctx.pyz -Sha256 <64-character-checksum>
#>
[CmdletBinding(DefaultParameterSetName = 'Download')]
param(
    [string]$HubUrl = 'https://contexthub.reflectstars.dev',
    [string]$InstallDir = '',
    [string]$PythonExecutable = '',
    [Parameter(Mandatory = $true, ParameterSetName = 'Offline')]
    [string]$ArchivePath,
    [Parameter(Mandatory = $true, ParameterSetName = 'Offline')]
    [ValidatePattern('^[a-fA-F0-9]{64}$')]
    [string]$Sha256,
    [switch]$NoPathUpdate
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

function Get-HubOrigin([string]$Value) {
    if ([string]::IsNullOrWhiteSpace($Value) -or $Value -match '[\x00-\x20\x7f\\@]' -or
        $Value -notmatch '^https://[^/?#]+/?$') {
        throw 'HubUrl must be an HTTPS origin, without credentials, path, query or fragment.'
    }
    $uri = $null
    if (![Uri]::TryCreate($Value, [UriKind]::Absolute, [ref]$uri) -or
        $uri.Scheme -ne 'https' -or !$uri.Host -or $uri.UserInfo -or
        $uri.Port -lt 1 -or $uri.Port -gt 65535 -or $uri.Host.Contains('%') -or
        $uri.AbsolutePath -ne '/' -or $uri.Query -or $uri.Fragment) {
        throw 'HubUrl must be an HTTPS origin, without credentials, path, query or fragment.'
    }
    return $uri.GetLeftPart([UriPartial]::Authority)
}

function Assert-SafePath([string]$Path, [switch]$File) {
    # Inspect each existing ancestor; Windows junctions are reparse points too.
    $current = [IO.Path]::GetFullPath($Path)
    while ($current) {
        try {
            $attributes = [IO.File]::GetAttributes($current)
        } catch [IO.FileNotFoundException] {
            $attributes = $null
        } catch [IO.DirectoryNotFoundException] {
            $attributes = $null
        }
        if ($null -ne $attributes) {
            if (($attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "Refusing a symbolic link, junction or other reparse point: $current"
            }
            if ($current -eq $Path -and $File -and
                ($attributes -band [IO.FileAttributes]::Directory) -ne 0) {
                throw "Expected a regular file: $Path"
            }
        }
        $parent = [IO.Path]::GetDirectoryName($current)
        if ($parent -eq $current) { break }
        $current = $parent
    }
}

function Find-Python([string]$Requested) {
    # ASCII output avoids PowerShell 5.1's legacy pipe encoding for user paths.
    # Python single quotes also survive PowerShell 5.1's legacy native argument
    # marshalling, which strips embedded double quotes from a -c argument.
    $probe = 'import base64, sys; assert sys.version_info >= (3, 12), ''Python 3.12 or newer is required''; print(base64.b64encode(sys.executable.encode(''utf-8'')).decode(''ascii''))'
    $candidates = @()
    if ($Requested) {
        $candidates += ,@($Requested)
    } else {
        foreach ($name in @('py.exe', 'python.exe', 'python3.exe')) {
            $command = Get-Command $name -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
            if ($command) {
                if ($name -eq 'py.exe') { $candidates += ,@($command.Source, '-3') }
                else { $candidates += ,@($command.Source) }
            }
        }
    }
    foreach ($candidate in $candidates) {
        try {
            $arguments = @()
            if ($candidate.Count -gt 1) { $arguments += $candidate[1] }
            $arguments += @('-c', $probe)
            $result = & $candidate[0] @arguments 2>$null
            if ($LASTEXITCODE -eq 0 -and $result -and @($result).Count -eq 1) {
                $resolved = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String([string]$result))
                if ([IO.File]::Exists($resolved)) { return [IO.Path]::GetFullPath($resolved) }
            }
        } catch {
            # Try the next installed interpreter; never download Python.
        }
    }
    throw 'Python 3.12 or newer was not found. Install Python, reopen PowerShell, then run this installer again.'
}

function Receive-File([string]$Url, [string]$Target, [long]$Limit) {
    $request = [Net.HttpWebRequest]::Create($Url)
    $request.AllowAutoRedirect = $false
    $request.UserAgent = 'ContextHub/Windows-Installer'
    $request.Timeout = 30000
    $request.ReadWriteTimeout = 30000
    $response = $null
    $inputStream = $null
    $outputStream = $null
    try {
        $response = $request.GetResponse()
        if ([int]$response.StatusCode -ne 200) {
            throw "Download failed with HTTP $([int]$response.StatusCode). Redirects are not allowed."
        }
        if ($response.ContentLength -gt $Limit) { throw 'Download exceeds the expected size limit.' }
        $inputStream = $response.GetResponseStream()
        $outputStream = [IO.File]::Open($Target, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
        $buffer = New-Object byte[] 65536
        [long]$total = 0
        while (($count = $inputStream.Read($buffer, 0, $buffer.Length)) -gt 0) {
            $total += $count
            if ($total -gt $Limit) { throw 'Download exceeds the expected size limit.' }
            $outputStream.Write($buffer, 0, $count)
        }
        $outputStream.Flush($true)
    } finally {
        if ($outputStream) { $outputStream.Dispose() }
        if ($inputStream) { $inputStream.Dispose() }
        if ($response) { $response.Dispose() }
    }
}

function Add-PathEntry([string]$Value, [string]$Entry) {
    foreach ($part in @($Value -split ';')) {
        $expanded = [Environment]::ExpandEnvironmentVariables($part.Trim().Trim('"')).TrimEnd('\')
        if ($expanded -ieq $Entry.TrimEnd('\')) { return $Value }
    }
    if ([string]::IsNullOrEmpty($Value)) { return $Entry }
    if ($Value.EndsWith(';')) { return $Value + $Entry }
    return $Value + ';' + $Entry
}

if ($env:OS -ne 'Windows_NT') { throw 'This installer is for native Windows. Use the Linux installation instructions on other systems.' }
$origin = Get-HubOrigin $HubUrl
$python = Find-Python $PythonExecutable
if (!$InstallDir) {
    $localData = [Environment]::GetFolderPath([Environment+SpecialFolder]::LocalApplicationData)
    if (!$localData) { throw 'The current user has no LocalAppData directory.' }
    $InstallDir = Join-Path $localData 'ContextHub\bin'
}
$InstallDir = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($InstallDir)
if ($InstallDir -match '[\x00-\x1f;]') { throw 'InstallDir cannot contain control characters or a semicolon.' }
Assert-SafePath $InstallDir
if ([IO.File]::Exists($InstallDir)) { throw 'InstallDir must be a directory.' }
[void][IO.Directory]::CreateDirectory($InstallDir)
Assert-SafePath $InstallDir

$lock = $null
$staging = $null
$replaced = New-Object 'System.Collections.Generic.List[object]'
$oldUserPath = [Environment]::GetEnvironmentVariable('Path', 'User')
$oldProcessPath = $env:Path
$pathChanged = $false
$oldTls = [Net.ServicePointManager]::SecurityProtocol
try {
    $lockPath = Join-Path $InstallDir '.ctx-install.lock'
    Assert-SafePath $lockPath -File
    $lock = [IO.File]::Open($lockPath, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
    $staging = Join-Path $InstallDir ('.ctx-install-' + [Guid]::NewGuid().ToString('N'))
    [void][IO.Directory]::CreateDirectory($staging)
    $newArchive = Join-Path $staging 'ctx.pyz'
    if ($PSCmdlet.ParameterSetName -eq 'Offline') {
        $source = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($ArchivePath)
        Assert-SafePath $source -File
        if (![IO.File]::Exists($source)) { throw 'ArchivePath must name an existing ctx.pyz file.' }
        if ((Get-Item -LiteralPath $source).Length -gt 20971520) { throw 'Archive exceeds the 20 MiB size limit.' }
        [IO.File]::Copy($source, $newArchive, $false)
        $expected = $Sha256
    } else {
        [Net.ServicePointManager]::SecurityProtocol = $oldTls -bor [Net.SecurityProtocolType]::Tls12
        $checksumFile = Join-Path $staging 'ctx.pyz.sha256'
        Receive-File ($origin + '/ctx.pyz.sha256') $checksumFile 256
        $checksum = [IO.File]::ReadAllText($checksumFile, [Text.Encoding]::UTF8)
        if ($checksum -notmatch '\A([a-fA-F0-9]{64})[ \t]+ctx\.pyz\r?\n?\z') { throw 'Hub returned an invalid SHA-256 checksum file.' }
        $expected = $Matches[1]
        Receive-File ($origin + '/ctx.pyz') $newArchive 20971520
    }
    $actual = (Get-FileHash -LiteralPath $newArchive -Algorithm SHA256).Hash
    if ($actual -ine $expected) { throw 'SHA-256 checksum mismatch. Existing installation was not changed.' }
    $version = & $python $newArchive --version 2>&1
    if ($LASTEXITCODE -ne 0 -or @($version).Count -ne 1 -or
        [string]$version -notmatch '\AContextHub [0-9]+\.[0-9]+\.[0-9]+(?:[a-zA-Z0-9.+-]*)\z') {
        throw 'The downloaded archive did not pass its version check. Existing installation was not changed.'
    }

    # The verified archive builds a native launcher from this Python installation's
    # pip/ensurepip resources. No batch shell reinterprets conversation arguments.
    $newLauncher = Join-Path $staging 'ctx.exe'
    $build = 'import sys; sys.path.insert(0,sys.argv[1]); from contexthub.windows_launcher import build_launcher; build_launcher(sys.argv[2])'
    & $python -c $build $newArchive $newLauncher
    if ($LASTEXITCODE -ne 0 -or ![IO.File]::Exists($newLauncher)) {
        throw 'Could not build ctx.exe. Use a full Python 3.12+ installation with pip/ensurepip. The portable ctx.pyz can also be run directly with Python.'
    }
    $launcherVersion = & $newLauncher --version 2>&1
    if ($LASTEXITCODE -ne 0 -or @($launcherVersion).Count -ne 1 -or [string]$launcherVersion -cne [string]$version) {
        throw 'The native launcher did not pass its version check. Existing installation was not changed.'
    }

    foreach ($name in @('ctx.pyz', 'ctx.exe')) {
        $target = Join-Path $InstallDir $name
        $incoming = Join-Path $staging $name
        $backup = Join-Path $staging ($name + '.previous')
        Assert-SafePath $target -File
        $existed = [IO.File]::Exists($target)
        if ($existed) { [IO.File]::Replace($incoming, $target, $backup) }
        else { [IO.File]::Move($incoming, $target) }
        $replaced.Add(@{ Target = $target; Backup = $backup; Existed = $existed })
    }
    if (!$NoPathUpdate) {
        $newUserPath = Add-PathEntry $oldUserPath $InstallDir
        if ($newUserPath -cne $oldUserPath) {
            $pathChanged = $true
            [Environment]::SetEnvironmentVariable('Path', $newUserPath, 'User')
        }
        $env:Path = Add-PathEntry $oldProcessPath $InstallDir
    }
    Write-Host "Installed $version in $InstallDir"
    if (!$NoPathUpdate) { Write-Host 'ctx is available in this PowerShell session and newly opened terminals.' }
    else { Write-Host "Run: & `"$(Join-Path $InstallDir 'ctx.exe')`" --version" }
} catch {
    $originalError = $_
    $rollbackErrors = @()
    for ($index = $replaced.Count - 1; $index -ge 0; $index--) {
        $item = $replaced[$index]
        try {
            # PowerShell 5.1 marshals $null to an empty string for this .NET
            # overload, which is an invalid backup path. Keep the failed new
            # file inside staging while atomically restoring the old one.
            if ($item.Existed) { [IO.File]::Replace($item.Backup, $item.Target, ($item.Backup + '.failed')) }
            else { [IO.File]::Delete($item.Target) }
        } catch { $rollbackErrors += $_.Exception.Message }
    }
    if ($pathChanged) {
        try { [Environment]::SetEnvironmentVariable('Path', $oldUserPath, 'User') }
        catch { $rollbackErrors += $_.Exception.Message }
    }
    $env:Path = $oldProcessPath
    if ($rollbackErrors.Count) {
        # Preserve old files if another program prevented restoration.
        Write-Warning "Rollback was interrupted. Previous files are retained in $staging"
        $staging = $null
    }
    throw $originalError
} finally {
    [Net.ServicePointManager]::SecurityProtocol = $oldTls
    if ($staging -and [IO.Directory]::Exists($staging)) {
        Remove-Item -LiteralPath $staging -Recurse -Force -ErrorAction SilentlyContinue
    }
    if ($lock) { $lock.Dispose() }
}
