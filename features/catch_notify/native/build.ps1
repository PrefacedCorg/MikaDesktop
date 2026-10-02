<#
    build.ps1 - compile NotificationBridge.cs using only components that ship with Windows.

    Nothing outside the OS is required: no Visual Studio, no NuGet, no pip.

    Toolchain:
      * csc.exe                       : %WINDIR%\Microsoft.NET\Framework64\v4.0.30319\
      * Windows.winmd                 : Windows SDK UnionMetadata (WinRT metadata)
      * System.Runtime.WindowsRuntime : ships with .NET Framework; WinRT projection + async/await
      * System.Runtime.dll            : .NET Framework facade. Required: without it the compiler
                                        fails with CS0012 ("System.Attribute is not referenced")
      * System.Xml.dll                : pulls <text> values out of the XML strings returned
                                        by ToastNotificationHistory

    NOTE: this script is deliberately ASCII-only. Windows PowerShell 5.1 reads .ps1 files as ANSI
    unless they begin with a UTF-8 BOM, so non-ASCII text in here would break parsing under
    powershell.exe (it is fine under pwsh.exe, but we must support both).

    NOTE: $PSScriptRoot is EMPTY inside a param() default expression under powershell.exe
    (it only becomes available once the script body runs). Using it as a default value makes
    Join-Path blow up with "Cannot bind argument to parameter 'Path' because it is an empty
    string." on Windows PowerShell 5.1, so the default is left empty and resolved below.

    Usage:
      powershell -ExecutionPolicy Bypass -File .\build.ps1
      powershell -ExecutionPolicy Bypass -File .\build.ps1 -OutputDirectory D:\out
      powershell -ExecutionPolicy Bypass -File .\build.ps1 -Clean
#>
[CmdletBinding()]
param(
    [string]$OutputDirectory = '',
    [switch]$Clean
)

$ErrorActionPreference = 'Stop'

# Resolve the default output directory here (see the note above).
if (-not $OutputDirectory) {
    if ($PSScriptRoot) {
        $OutputDirectory = $PSScriptRoot
    } elseif ($MyInvocation.MyCommand.Path) {
        $OutputDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
    } else {
        $OutputDirectory = (Get-Location).Path
    }
}

$exeName = 'NotificationBridge.exe'
$target = Join-Path $OutputDirectory $exeName

if ($Clean) {
    if (Test-Path -LiteralPath $target) {
        Remove-Item -LiteralPath $target -Force
        Write-Host "removed $target"
    }
    return
}

function Find-FirstFile {
    param([string[]]$Patterns)
    foreach ($pattern in $Patterns) {
        $hit = Get-ChildItem -Path $pattern -ErrorAction SilentlyContinue |
               Sort-Object -Property FullName -Descending |
               Select-Object -First 1
        if ($hit) { return $hit.FullName }
    }
    return $null
}

# 1) C# compiler
$csc = Find-FirstFile @(
    (Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'),
    (Join-Path $env:WINDIR 'Microsoft.NET\Framework\v4.0.30319\csc.exe')
)
if (-not $csc) {
    throw 'csc.exe not found (.NET Framework 4.x is required).'
}

# 2) WinRT metadata from the Windows SDK.
#    UnionMetadata holds both "<version>\Windows.winmd" (the real union) and
#    "Facade\Windows.winmd" (a type-forwarding shim with no actual types). The facade must be
#    skipped - picking it up yields CS0234/CS0246 - and the newest version folder wins.
$winmdRoots = @()
if (${env:ProgramFiles(x86)}) {
    $winmdRoots += (Join-Path ${env:ProgramFiles(x86)} 'Windows Kits\10\UnionMetadata')
}
if ($env:ProgramFiles) {
    $winmdRoots += (Join-Path $env:ProgramFiles 'Windows Kits\10\UnionMetadata')
}

$winmdCandidates = @()
foreach ($root in $winmdRoots) {
    $winmdCandidates += @(Get-ChildItem -Path (Join-Path $root '*\Windows.winmd') -ErrorAction SilentlyContinue |
                          Where-Object { $_.Directory.Name -ne 'Facade' })
}

$winmd = $null
if ($winmdCandidates.Count -gt 0) {
    $winmd = ($winmdCandidates |
              Sort-Object -Property @{ Expression = { try { [version]$_.Directory.Name } catch { [version]'0.0.0.0' } } } -Descending |
              Select-Object -First 1).FullName
}
if (-not $winmd) {
    throw 'Windows.winmd not found. Install the Windows SDK (expected: <root>\Windows Kits\10\UnionMetadata\<version>\Windows.winmd).'
}

# 3) Reference assemblies that ship with .NET Framework
$frameworkDir = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319'
if (-not (Test-Path -LiteralPath $frameworkDir)) {
    $frameworkDir = Join-Path $env:WINDIR 'Microsoft.NET\Framework\v4.0.30319'
}

$referenceNames = @('System.Runtime.WindowsRuntime.dll', 'System.Runtime.dll', 'System.Xml.dll')
$references = @()
foreach ($name in $referenceNames) {
    $path = Join-Path $frameworkDir $name
    if (-not (Test-Path -LiteralPath $path)) {
        throw "$name not found (expected under $frameworkDir)."
    }
    $references += $path
}

$source = Join-Path $PSScriptRoot 'NotificationBridge.cs'
if (-not (Test-Path -LiteralPath $source)) {
    throw "source file not found: $source"
}

if (-not (Test-Path -LiteralPath $OutputDirectory)) {
    New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
}

Write-Host "csc      : $csc"
Write-Host "Windows  : $winmd"
foreach ($reference in $references) {
    Write-Host ("ref      : " + $reference)
}
Write-Host "source   : $source"
Write-Host "output   : $target"
Write-Host ''

$cscArgs = @(
    '-nologo'
    '-target:exe'
    '-platform:anycpu'
    '-optimize+'
    '-warn:4'
    ('-out:' + $target)
    ('-r:' + $winmd)
)
foreach ($reference in $references) {
    $cscArgs += ('-r:' + $reference)
}
$cscArgs += $source

& $csc @cscArgs
$code = $LASTEXITCODE
if ($code -ne 0) {
    throw "compile failed, csc exit code $code"
}

Write-Host ''
Write-Host "built: $target"
Write-Host "self-check: & '$target' request"
