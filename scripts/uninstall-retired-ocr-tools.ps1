#requires -Version 5.1
<##
.SYNOPSIS
    Finds and optionally uninstalls software left over from Organize's retired
    OCR subtitle pipeline.

.DESCRIPTION
    Organize 6.0.0 no longer uses pgsrip, sup2srt, PgsToSrt, Tesseract or
    Subtitle Edit as OCR backends. The old subtitle-sync helper ffsubsync was
    removed as well, but it is only removed when -RemoveLegacySync is supplied.

    This script is deliberately an inventory first. With no -Apply switch it
    makes no changes. It checks Windows uninstall records, winget, PATH, Python
    packages, and the old OCR environment variables. Portable Subtitle Edit
    copies are searched for separately; they are reported by default and are
    removed only with the explicit -RemovePortable switch.

    It intentionally does NOT remove FFmpeg/ffprobe, MKVToolNix, Python,
    organizekit, OpenSubtitles credentials, or your media files. v6 still uses
    ffprobe, mkvmerge and mkvextract, and v6 still supports OpenSubtitles for
    image-only subtitle tracks.

.EXAMPLE
    .\uninstall-retired-ocr-tools.ps1
    Inventory only. Safe to run first.

.EXAMPLE
    .\uninstall-retired-ocr-tools.ps1 -Apply -RemovePythonPackages -RemoveLegacySync -RemovePortable
    Remove detected OCR applications, old OCR Python packages, ffsubsync, and
    portable Subtitle Edit folders with the exact expected folder name.
    Add -RemoveMono only if Mono is not used by anything else on this PC.

.EXAMPLE
    .\uninstall-retired-ocr-tools.ps1 -Apply -RemovePythonPackages -WhatIf
    Show the actions that would be taken without changing anything.

.NOTES
    Run from an elevated PowerShell window if you want to remove machine-wide
    environment variables or an application installed for all users.
#>

[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'High')]
param(
    [switch]$Apply,
    [switch]$RemovePythonPackages,
    [switch]$RemoveLegacySync,
    [switch]$RemoveMono,
    [switch]$RemovePortable,
    [switch]$RemoveMachineEnvironment
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Write-Section {
    param([Parameter(Mandatory)][string]$Title)
    Write-Host "`n=== $Title ===" -ForegroundColor Cyan
}

function Get-TargetSpecs {
    $specs = @(
        [pscustomobject]@{
            Name       = 'Subtitle Edit'
            Pattern    = '(?i)^Subtitle Edit(?:$|[\s(])'
            WingetIds  = @('Nikse.SubtitleEdit')
        },
        [pscustomobject]@{
            Name       = 'Tesseract OCR'
            Pattern    = '(?i)^(?:Tesseract(?: OCR)?|tesseract-ocr)(?:$|[\s-])'
            WingetIds  = @('UB-Mannheim.TesseractOCR', 'tesseract-ocr.tesseract')
        },
        [pscustomobject]@{
            Name       = 'PgsToSrt'
            Pattern    = '(?i)^(?:PgsToSrt|PGS.?to.?SRT|PGS2SRT)(?:$|[\s-])'
            WingetIds  = @()
        },
        [pscustomobject]@{
            Name       = 'pgsrip'
            Pattern    = '(?i)^pgsrip(?:$|[\s-])'
            WingetIds  = @()
        },
        [pscustomobject]@{
            Name       = 'sup2srt'
            Pattern    = '(?i)^sup2srt(?:$|[\s-])'
            WingetIds  = @()
        }
    )

    if ($RemoveMono) {
        $specs += [pscustomobject]@{
            Name      = 'Mono'
            Pattern   = '(?i)^Mono(?: Runtime| Framework)?(?:$|[\s-])'
            WingetIds = @('Mono.Mono')
        }
    }

    if ($RemoveLegacySync) {
        $specs += [pscustomobject]@{
            Name      = 'ffsubsync'
            Pattern   = '(?i)^ffsubsync(?:$|[\s-])'
            WingetIds = @()
        }
    }

    return $specs
}

function Get-TextProperty {
    param(
        [Parameter(Mandatory)][object]$Object,
        [Parameter(Mandatory)][string]$Name
    )

    $property = $Object.PSObject.Properties[$Name]
    if ($null -eq $property -or $null -eq $property.Value) {
        return ''
    }
    return [string]$property.Value
}

function Get-RegistryApplications {
    $roots = @(
        'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall',
        'HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall',
        'HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall'
    )

    foreach ($root in $roots) {
        if (-not (Test-Path -LiteralPath $root)) {
            continue
        }

        foreach ($key in (Get-ChildItem -LiteralPath $root -ErrorAction SilentlyContinue)) {
            try {
                $app = Get-ItemProperty -LiteralPath $key.PSPath -ErrorAction Stop
            }
            catch {
                continue
            }

            $displayName = Get-TextProperty -Object $app -Name 'DisplayName'
            if (-not [string]::IsNullOrWhiteSpace($displayName)) {
                [pscustomobject]@{
                    DisplayName          = $displayName
                    DisplayVersion       = Get-TextProperty -Object $app -Name 'DisplayVersion'
                    Publisher            = Get-TextProperty -Object $app -Name 'Publisher'
                    UninstallString      = Get-TextProperty -Object $app -Name 'UninstallString'
                    QuietUninstallString = Get-TextProperty -Object $app -Name 'QuietUninstallString'
                    RegistryPath         = [string]$key.PSPath
                }
            }
        }
    }
}

function Get-RegistryMatches {
    param([Parameter(Mandatory)][object[]]$Specs)

    $seen = @{}
    foreach ($app in (Get-RegistryApplications)) {
        $spec = $Specs | Where-Object { $app.DisplayName -match $_.Pattern } | Select-Object -First 1
        if ($null -eq $spec) {
            continue
        }

        $identity = "$($app.DisplayName)|$($app.UninstallString)|$($app.RegistryPath)"
        if ($seen.ContainsKey($identity)) {
            continue
        }
        $seen[$identity] = $true

        [pscustomobject]@{
            Target               = $spec.Name
            DisplayName          = $app.DisplayName
            Version              = $app.DisplayVersion
            Publisher            = $app.Publisher
            Source               = 'Windows uninstall registry'
            UninstallString      = $app.UninstallString
            QuietUninstallString = $app.QuietUninstallString
            RegistryPath         = $app.RegistryPath
        }
    }
}

function Get-WingetMatches {
    param([Parameter(Mandatory)][object[]]$Specs)

    $winget = Get-Command winget.exe -ErrorAction SilentlyContinue
    if ($null -eq $winget) {
        return @()
    }

    $matches = @()
    $seenIds = @{}
    foreach ($spec in $Specs) {
        foreach ($id in $spec.WingetIds) {
            if ($seenIds.ContainsKey($id)) {
                continue
            }
            $seenIds[$id] = $true

            $output = (& $winget.Source list --id $id --exact `
                --accept-source-agreements --disable-interactivity 2>$null | Out-String)
            if ($output -match [regex]::Escape($id)) {
                $matches += [pscustomobject]@{
                    Target  = $spec.Name
                    Id      = $id
                    Source  = 'winget'
                    Command = $winget.Source
                }
            }
        }
    }
    return $matches
}

function Get-PathMatches {
    param([Parameter(Mandatory)][object[]]$Specs)

    $names = @(
        'SubtitleEdit.exe', 'tesseract.exe', 'pgsrip.exe', 'sup2srt.exe',
        'PgsToSrt.exe', 'pgstosrt.exe', 'mono.exe', 'ffsubsync.exe'
    )
    $matches = @()
    $seen = @{}

    foreach ($name in $names) {
        foreach ($command in @(Get-Command $name -CommandType Application -ErrorAction SilentlyContinue)) {
            $path = [string]$command.Source
            if ([string]::IsNullOrWhiteSpace($path) -or $seen.ContainsKey($path)) {
                continue
            }
            $seen[$path] = $true

            $targetByExecutable = @{
                'SubtitleEdit.exe' = 'Subtitle Edit'
                'tesseract.exe'    = 'Tesseract OCR'
                'pgsrip.exe'       = 'pgsrip'
                'sup2srt.exe'      = 'sup2srt'
                'PgsToSrt.exe'     = 'PgsToSrt'
                'mono.exe'         = 'Mono'
                'ffsubsync.exe'    = 'ffsubsync'
            }
            $targetName = $targetByExecutable[$name]
            $spec = $Specs | Where-Object { $_.Name -eq $targetName } | Select-Object -First 1
            if ($null -ne $spec) {
                $matches += [pscustomobject]@{
                    Target = $spec.Name
                    Path   = $path
                    Source = 'PATH'
                }
            }
        }
    }
    return $matches
}

function Get-PortableSubtitleEditMatches {
    $roots = @(
        (Join-Path $env:USERPROFILE 'Downloads'),
        (Join-Path $env:USERPROFILE 'Desktop'),
        (Join-Path $env:USERPROFILE 'Documents'),
        (Join-Path $env:USERPROFILE 'Subtitle Edit'),
        (Join-Path $env:USERPROFILE 'SubtitleEdit'),
        (Join-Path $env:LOCALAPPDATA 'Subtitle Edit'),
        (Join-Path $env:LOCALAPPDATA 'SubtitleEdit'),
        $env:ProgramFiles,
        ${env:ProgramFiles(x86)},
        $env:SystemDrive
    ) | Where-Object { -not [string]::IsNullOrWhiteSpace($_) -and (Test-Path -LiteralPath $_) } | Select-Object -Unique

    $seen = @{}
    foreach ($root in $roots) {
        foreach ($file in @(Get-ChildItem -LiteralPath $root -Filter 'SubtitleEdit.exe' -File -Recurse -Force -ErrorAction SilentlyContinue)) {
            $path = [string]$file.FullName
            if ($seen.ContainsKey($path)) {
                continue
            }
            $seen[$path] = $true
            [pscustomobject]@{
                Path   = $path
                Folder = [string]$file.DirectoryName
                Source = 'portable/file search'
            }
        }
    }
}

function Get-PythonCommands {
    $seen = @{}
    foreach ($name in @('py.exe', 'python.exe', 'python3.exe')) {
        foreach ($command in @(Get-Command $name -CommandType Application -ErrorAction SilentlyContinue)) {
            $path = [string]$command.Source
            if (-not [string]::IsNullOrWhiteSpace($path) -and -not $seen.ContainsKey($path)) {
                $seen[$path] = $true
                [pscustomobject]@{ Name = $name; Path = $path }
            }
        }
    }
}

function Get-PythonPackageMatches {
    param([Parameter(Mandatory)][string[]]$PackageNames)

    $matches = @()
    foreach ($python in (Get-PythonCommands)) {
        foreach ($package in $PackageNames) {
            $output = @(& $python.Path -m pip show $package 2>$null)
            if ($LASTEXITCODE -ne 0) {
                continue
            }

            $versionLine = $output | Where-Object { $_ -match '^Version:\s*(.+)$' } | Select-Object -First 1
            $version = 'unknown'
            if ($null -ne $versionLine -and $versionLine -match '^Version:\s*(.+)$') {
                $version = $Matches[1].Trim()
            }
            $matches += [pscustomobject]@{
                Package     = $package
                Version     = $version
                Interpreter = $python.Path
                Source      = 'Python package'
            }
        }
    }
    return $matches
}

function Get-ObsoleteEnvironmentVariables {
    $names = @('PGSTOSRT_DLL')
    $matches = @()
    $scopes = @('User')
    if ($RemoveMachineEnvironment) {
        $scopes += 'Machine'
    }

    foreach ($scope in $scopes) {
        $values = [Environment]::GetEnvironmentVariables($scope)
        foreach ($key in $values.Keys) {
            $name = [string]$key
            if ($names -contains $name -or $name -like 'OCR_BACKEND_*') {
                $matches += [pscustomobject]@{
                    Name  = $name
                    Scope = $scope
                    Value = [string]$values[$key]
                }
            }
        }
    }
    return $matches
}

function Invoke-WingetRemoval {
    [CmdletBinding(SupportsShouldProcess = $true)]
    param([Parameter(Mandatory)][object]$Item)

    if ($PSCmdlet.ShouldProcess("winget package $($Item.Id)", 'Uninstall')) {
        $wingetOutput = @(& $Item.Command uninstall --id $Item.Id --exact --source winget --silent `
            --accept-source-agreements --disable-interactivity 2>&1)
        $exitCode = $LASTEXITCODE
        if ($exitCode -ne 0) {
            Write-Warning "winget returned exit code $exitCode for $($Item.Id): $($wingetOutput -join ' ')"
            return $false
        }
        return $true
    }
    return $false
}

function Invoke-RegistryRemoval {
    [CmdletBinding(SupportsShouldProcess = $true)]
    param([Parameter(Mandatory)][object]$Item)

    $command = if (-not [string]::IsNullOrWhiteSpace($Item.QuietUninstallString)) {
        $Item.QuietUninstallString
    }
    else {
        $Item.UninstallString
    }

    if ([string]::IsNullOrWhiteSpace($command)) {
        Write-Warning "No uninstaller was recorded for $($Item.DisplayName). Remove it from Windows Settings manually."
        return
    }

    if ($PSCmdlet.ShouldProcess($Item.DisplayName, "Run uninstall command")) {
        try {
            # Registry uninstall strings are already quoted by their installers.
            # Running through cmd preserves those quotes and supports both MSI
            # and ordinary .exe uninstallers.
            $process = Start-Process -FilePath $env:ComSpec `
                -ArgumentList @('/d', '/s', '/c', $command) -Wait -PassThru
            if ($process.ExitCode -ne 0) {
                Write-Warning "$($Item.DisplayName) returned exit code $($process.ExitCode)."
            }
        }
        catch {
            Write-Warning "Could not run the uninstaller for $($Item.DisplayName): $($_.Exception.Message)"
        }
    }
}

function Invoke-PythonRemoval {
    [CmdletBinding(SupportsShouldProcess = $true)]
    param([Parameter(Mandatory)][object]$Item)

    if ($PSCmdlet.ShouldProcess("$($Item.Package) from $($Item.Interpreter)", 'Uninstall Python package')) {
        & $Item.Interpreter -m pip uninstall --yes $Item.Package
        if ($LASTEXITCODE -ne 0) {
            Write-Warning "pip returned exit code $LASTEXITCODE for $($Item.Package) from $($Item.Interpreter)."
        }
    }
}

function Remove-ObsoleteEnvironmentVariable {
    [CmdletBinding(SupportsShouldProcess = $true)]
    param([Parameter(Mandatory)][object]$Item)

    if ($Item.Scope -eq 'Machine' -and -not ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
            [Security.Principal.WindowsBuiltInRole]::Administrator)) {
        Write-Warning "Administrator rights are required to remove machine variable $($Item.Name)."
        return
    }

    if ($PSCmdlet.ShouldProcess("$($Item.Scope) environment variable $($Item.Name)", 'Remove')) {
        [Environment]::SetEnvironmentVariable($Item.Name, $null, $Item.Scope)
        Remove-Item -LiteralPath "Env:$($Item.Name)" -ErrorAction SilentlyContinue
    }
}

function Remove-PortableSubtitleEdit {
    [CmdletBinding(SupportsShouldProcess = $true)]
    param([Parameter(Mandatory)][object]$Item)

    $folderName = Split-Path -Leaf $Item.Folder
    if ($folderName -notmatch '(?i)^Subtitle\s*Edit(?:[ ._-]|$)') {
        Write-Warning "Found SubtitleEdit.exe in '$($Item.Folder)', but will not delete a folder with an unrelated name. Remove that portable copy manually if it is yours."
        return
    }

    if ($PSCmdlet.ShouldProcess($Item.Folder, 'Delete portable Subtitle Edit folder')) {
        Get-Process -Name 'SubtitleEdit' -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $Item.Folder -Recurse -Force
    }
}

$specs = @(Get-TargetSpecs)
$pythonPackages = @('pgsrip', 'sup2srt')
if ($RemoveLegacySync) {
    $pythonPackages += 'ffsubsync'
}

Write-Host 'Organize retired OCR cleanup' -ForegroundColor Green
if (-not $Apply) {
    Write-Warning 'Inventory mode: nothing will be uninstalled. Use -Apply after reviewing the list.'
}
if ($RemoveMono) {
    Write-Warning 'Mono was requested. Remove it only if no other application on this PC needs it.'
}

Write-Section 'Windows applications'
$registryMatches = @(Get-RegistryMatches -Specs $specs)
$wingetMatches = @(Get-WingetMatches -Specs $specs)

if ($registryMatches.Count -eq 0 -and $wingetMatches.Count -eq 0) {
    Write-Host 'No matching registered OCR applications were found.'
}
else {
    foreach ($item in $registryMatches) {
        Write-Host ("{0} {1} {2}" -f $item.Target, $item.DisplayName, $item.Version)
    }
    foreach ($item in $wingetMatches) {
        Write-Host ("{0} {1} ({2})" -f $item.Target, $item.Id, $item.Source)
    }
}

Write-Section 'Python packages'
$pythonMatches = @(Get-PythonPackageMatches -PackageNames $pythonPackages)
if ($pythonMatches.Count -eq 0) {
    Write-Host 'No matching Python packages were found in the Python interpreters on PATH.'
}
else {
    foreach ($item in $pythonMatches) {
        Write-Host ("{0} {1} via {2}" -f $item.Package, $item.Version, $item.Interpreter)
    }
    if (-not $RemovePythonPackages) {
        Write-Warning 'Python packages are inventory-only. Add -RemovePythonPackages to uninstall them.'
    }
}

Write-Section 'PATH and portable programs'
$pathMatches = @(Get-PathMatches -Specs $specs)
if ($pathMatches.Count -eq 0) {
    Write-Host 'No retired OCR executables were found on PATH.'
}
else {
    foreach ($item in $pathMatches) {
        Write-Host ("{0}: {1}" -f $item.Target, $item.Path)
    }
    Write-Warning 'PATH executables are not deleted automatically.'
}

$portableMatches = @(Get-PortableSubtitleEditMatches)
if ($portableMatches.Count -eq 0) {
    Write-Host 'No SubtitleEdit.exe portable copies were found in the common folders.'
}
else {
    foreach ($item in $portableMatches) {
        Write-Host ("Subtitle Edit portable: {0}" -f $item.Path)
    }
    if (-not $RemovePortable) {
        Write-Warning 'Portable copies are inventory-only. Add -RemovePortable to delete folders named Subtitle Edit or SubtitleEdit.'
    }
}

Write-Section 'Obsolete environment variables'
$environmentMatches = @(Get-ObsoleteEnvironmentVariables)
if ($environmentMatches.Count -eq 0) {
    Write-Host 'No PGSTOSRT_DLL or OCR_BACKEND_* variables were found.'
}
else {
    foreach ($item in $environmentMatches) {
        Write-Host ("{0} ({1}) = [value hidden]" -f $item.Name, $item.Scope)
    }
}

if (-not $Apply) {
    Write-Section 'No changes made'
    Write-Host 'Review the inventory above, then rerun with -Apply.'
    Write-Host 'Recommended full cleanup:'
    Write-Host '.\uninstall-retired-ocr-tools.ps1 -Apply -RemovePythonPackages -RemoveLegacySync -RemovePortable -RemoveMachineEnvironment'
    Write-Host 'Add -RemoveMono only when you are certain Mono is not used elsewhere.'
    exit 0
}

Write-Section 'Applying cleanup'

$wingetSucceeded = @{}
foreach ($item in $wingetMatches) {
    if (Invoke-WingetRemoval -Item $item) {
        $wingetSucceeded[$item.Target] = $true
    }
}

# If winget removed a known application, do not immediately run its registry
# uninstaller a second time. If winget failed, the registry uninstaller is the
# fallback path.
foreach ($item in $registryMatches) {
    if ($wingetSucceeded.ContainsKey($item.Target)) {
        continue
    }
    Invoke-RegistryRemoval -Item $item
}

if ($RemovePortable) {
    foreach ($item in $portableMatches) {
        Remove-PortableSubtitleEdit -Item $item
    }
}

if ($RemovePythonPackages) {
    foreach ($item in $pythonMatches) {
        Invoke-PythonRemoval -Item $item
    }
}

foreach ($item in $environmentMatches) {
    Remove-ObsoleteEnvironmentVariable -Item $item
}

Write-Section 'Finished'
Write-Host 'Restart PowerShell or sign out and back in for environment-variable changes to reach new processes.'
Write-Host 'FFmpeg/ffprobe and MKVToolNix were intentionally left installed because Organize 6.0.0 still uses them.'
Write-Host 'OpenSubtitles credentials and media files were intentionally left untouched.'
