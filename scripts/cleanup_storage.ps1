[CmdletBinding()]
param(
    [Parameter()]
    [ValidateSet('OnlineSafe', 'OfflineSafe')]
    [string]$Phase = 'OnlineSafe',

    [Parameter()]
    [string]$CodexHome = 'D:\CodexHome',

    [Parameter()]
    [switch]$Execute,

    [Parameter()]
    [string]$ConfirmToken = ''
)

$ErrorActionPreference = 'Stop'
$requiredToken = 'CLEAN_CODEX_STORAGE'

function Get-TreeMeasure {
    param([Parameter(Mandatory)][string]$Path)

    $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    if (-not $item.PSIsContainer) {
        return [pscustomobject]@{ Bytes = [int64]$item.Length; Files = 1 }
    }

    $bytes = [int64]0
    $files = 0
    $stack = [System.Collections.Generic.Stack[string]]::new()
    $stack.Push($item.FullName)
    while ($stack.Count -gt 0) {
        $directory = $stack.Pop()
        foreach ($child in (Get-ChildItem -LiteralPath $directory -Force -ErrorAction Stop)) {
            if (($child.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "Refusing target containing a reparse point: $($child.FullName)"
            }
            if ($child.PSIsContainer) {
                $stack.Push($child.FullName)
            }
            else {
                $bytes += [int64]$child.Length
                $files++
            }
        }
    }
    return [pscustomobject]@{ Bytes = $bytes; Files = $files }
}

function Resolve-SafeTarget {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string]$AllowedRoot
    )

    $resolved = (Resolve-Path -LiteralPath $Path -ErrorAction Stop).Path
    $allowed = (Resolve-Path -LiteralPath $AllowedRoot -ErrorAction Stop).Path.TrimEnd('\')
    if (-not $resolved.StartsWith($allowed + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw "Target escaped allowed root: $resolved"
    }
    $item = Get-Item -LiteralPath $resolved -Force -ErrorAction Stop
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "Refusing reparse-point target: $resolved"
    }
    return $resolved
}

function Test-LibreOfficeInstall {
    param([Parameter(Mandatory)][string]$InstallPath)

    $soffice = Join-Path $InstallPath 'program\soffice.com'
    if (-not (Test-Path -LiteralPath $soffice -PathType Leaf)) {
        return $false
    }
    $null = & $soffice --headless --version 2>$null
    return ($LASTEXITCODE -eq 0)
}

$root = (Resolve-Path -LiteralPath $CodexHome -ErrorAction Stop).Path.TrimEnd('\')
$toolsRoot = Join-Path $root 'tools'
$candidates = [System.Collections.Generic.List[object]]::new()
$skipped = [System.Collections.Generic.List[object]]::new()

$codexProcesses = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -ieq 'ChatGPT.exe' -or $_.Name -ieq 'codex.exe' } |
    Select-Object ProcessId, Name, ExecutablePath)
$libreOfficeProcesses = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object {
        $_.Name -match '^soffice(\.bin|\.exe)?$' -or
        ($_.ExecutablePath -and $_.ExecutablePath.StartsWith((Join-Path $toolsRoot 'LibreOffice-'), [StringComparison]::OrdinalIgnoreCase))
    } |
    Select-Object ProcessId, Name, ExecutablePath)

if ($Phase -eq 'OnlineSafe') {
    if (Test-Path -LiteralPath $toolsRoot) {
        foreach ($backup in (Get-ChildItem -LiteralPath $toolsRoot -Directory -Force -ErrorAction Stop |
            Where-Object { $_.Name -like 'LibreOffice-*-broken-backup-*' })) {
            $baseName = $backup.Name -replace '-broken-backup-.*$', ''
            $currentPath = Join-Path $toolsRoot $baseName
            if (-not (Test-Path -LiteralPath $currentPath -PathType Container)) {
                $skipped.Add([pscustomobject]@{ Path = $backup.FullName; Reason = 'No separate current installation' })
                continue
            }
            if (-not (Test-LibreOfficeInstall $currentPath)) {
                $skipped.Add([pscustomobject]@{ Path = $backup.FullName; Reason = 'Current installation failed version check' })
                continue
            }
            $resolved = Resolve-SafeTarget -Path $backup.FullName -AllowedRoot $toolsRoot
            $measure = Get-TreeMeasure $resolved
            $candidates.Add([pscustomobject]@{
                Kind = 'Directory'
                Path = $resolved
                Bytes = $measure.Bytes
                Files = $measure.Files
                Reason = "Broken backup; working installation verified at $currentPath"
            })
        }

        $downloadsRoot = Join-Path $toolsRoot 'downloads'
        if (Test-Path -LiteralPath $downloadsRoot -PathType Container) {
            foreach ($download in (Get-ChildItem -LiteralPath $downloadsRoot -Directory -Force -ErrorAction Stop)) {
                if ($download.Name -notmatch '^(?i)libreoffice-(?<version>[0-9][0-9A-Za-z._-]*)$') {
                    $skipped.Add([pscustomobject]@{ Path = $download.FullName; Reason = 'Unknown installer family' })
                    continue
                }
                $currentPath = Join-Path $toolsRoot ("LibreOffice-{0}" -f $Matches.version)
                if (-not (Test-Path -LiteralPath $currentPath -PathType Container) -or -not (Test-LibreOfficeInstall $currentPath)) {
                    $skipped.Add([pscustomobject]@{ Path = $download.FullName; Reason = 'Matching working installation not verified' })
                    continue
                }
                $resolved = Resolve-SafeTarget -Path $download.FullName -AllowedRoot $downloadsRoot
                $measure = Get-TreeMeasure $resolved
                $candidates.Add([pscustomobject]@{
                    Kind = 'Directory'
                    Path = $resolved
                    Bytes = $measure.Bytes
                    Files = $measure.Files
                    Reason = "Installer download; installed version verified at $currentPath"
                })
            }
        }
    }
}
else {
    foreach ($directoryCandidate in @(
        @{ Path = (Join-Path $root '.tmp\marketplaces\.staging'); Reason = 'Incomplete marketplace staging' },
        @{ Path = (Join-Path $root 'cache'); Reason = 'Rebuildable Codex cache' }
    )) {
        if (Test-Path -LiteralPath $directoryCandidate.Path -PathType Container) {
            $resolved = Resolve-SafeTarget -Path $directoryCandidate.Path -AllowedRoot $root
            $measure = Get-TreeMeasure $resolved
            $candidates.Add([pscustomobject]@{
                Kind = 'Directory'
                Path = $resolved
                Bytes = $measure.Bytes
                Files = $measure.Files
                Reason = $directoryCandidate.Reason
            })
        }
    }

    $sandboxPath = Join-Path $root '.sandbox'
    $todayLogName = 'sandbox.{0}.log' -f (Get-Date -Format 'yyyy-MM-dd')
    if (Test-Path -LiteralPath $sandboxPath -PathType Container) {
        foreach ($log in (Get-ChildItem -LiteralPath $sandboxPath -File -Force -Filter 'sandbox.*.log' -ErrorAction Stop |
            Where-Object { $_.Name -ne $todayLogName -and $_.LastWriteTime.Date -lt (Get-Date).Date })) {
            $resolved = Resolve-SafeTarget -Path $log.FullName -AllowedRoot $sandboxPath
            $candidates.Add([pscustomobject]@{
                Kind = 'File'
                Path = $resolved
                Bytes = [int64]$log.Length
                Files = 1
                Reason = 'Old sandbox log; current-day log preserved'
            })
        }
    }
}

$totalBytes = [int64](($candidates | Measure-Object -Property Bytes -Sum).Sum)
$canExecute = $true
$blockReasons = [System.Collections.Generic.List[string]]::new()

if ($Phase -eq 'OnlineSafe' -and $libreOfficeProcesses.Count -gt 0) {
    $canExecute = $false
    $blockReasons.Add('LibreOffice is running')
}
if ($Phase -eq 'OfflineSafe' -and $codexProcesses.Count -gt 0) {
    $canExecute = $false
    $blockReasons.Add('Codex or ChatGPT processes are running')
}

if ($Execute) {
    if ($ConfirmToken -cne $requiredToken) {
        throw "Execution requires -ConfirmToken $requiredToken"
    }
    if (-not $canExecute) {
        throw ('Execution blocked: ' + ($blockReasons -join '; '))
    }

    foreach ($candidate in $candidates) {
        $allowedRoot = if ($candidate.Path.StartsWith($toolsRoot + '\', [StringComparison]::OrdinalIgnoreCase)) { $toolsRoot } else { $root }
        $resolved = Resolve-SafeTarget -Path $candidate.Path -AllowedRoot $allowedRoot
        if ($candidate.Kind -eq 'Directory') {
            $null = Get-TreeMeasure $resolved
            [IO.Directory]::Delete($resolved, $true)
        }
        else {
            [IO.File]::Delete($resolved)
        }
        if (Test-Path -LiteralPath $resolved) {
            throw "Deletion failed: $resolved"
        }
    }

    $downloadsRoot = Join-Path $toolsRoot 'downloads'
    if (Test-Path -LiteralPath $downloadsRoot -PathType Container) {
        $remaining = @(Get-ChildItem -LiteralPath $downloadsRoot -Force -ErrorAction Stop)
        if ($remaining.Count -eq 0) {
            $resolvedDownloads = Resolve-SafeTarget -Path $downloadsRoot -AllowedRoot $toolsRoot
            [IO.Directory]::Delete($resolvedDownloads, $false)
        }
    }
}

[pscustomobject]@{
    SchemaVersion = 1
    Phase = $Phase
    ExecuteRequested = [bool]$Execute
    Executed = [bool]$Execute
    CodexHome = $root
    CandidateCount = $candidates.Count
    CandidateBytes = $totalBytes
    CandidateGiB = [math]::Round($totalBytes / 1GB, 4)
    CanExecute = $canExecute
    BlockReasons = @($blockReasons)
    ActiveCodexProcesses = $codexProcesses
    ActiveLibreOfficeProcesses = $libreOfficeProcesses
    Candidates = @($candidates)
    Skipped = @($skipped)
} | ConvertTo-Json -Depth 7
