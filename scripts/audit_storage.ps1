[CmdletBinding()]
param(
    [Parameter()]
    [string]$CodexHome = 'D:\CodexHome',

    [Parameter()]
    [ValidateRange(1, 200)]
    [int]$TopFiles = 30
)

$ErrorActionPreference = 'Stop'

function Get-TreeStat {
    param(
        [Parameter(Mandatory)]
        [string]$Path
    )

    if (-not (Test-Path -LiteralPath $Path)) {
        return [pscustomobject]@{
            Path = $Path
            Exists = $false
            Bytes = [int64]0
            Files = 0
            Directories = 0
            ReparsePoints = 0
            Errors = @()
        }
    }

    $resolved = (Resolve-Path -LiteralPath $Path).Path
    $rootItem = Get-Item -LiteralPath $resolved -Force
    if (-not $rootItem.PSIsContainer) {
        return [pscustomobject]@{
            Path = $resolved
            Exists = $true
            Bytes = [int64]$rootItem.Length
            Files = 1
            Directories = 0
            ReparsePoints = 0
            Errors = @()
        }
    }

    if (($rootItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        return [pscustomobject]@{
            Path = $resolved
            Exists = $true
            Bytes = [int64]0
            Files = 0
            Directories = 0
            ReparsePoints = 1
            Errors = @()
        }
    }

    $bytes = [int64]0
    $files = 0
    $directories = 0
    $reparsePoints = 0
    $errors = [System.Collections.Generic.List[string]]::new()
    $stack = [System.Collections.Generic.Stack[string]]::new()
    $stack.Push($resolved)

    while ($stack.Count -gt 0) {
        $directory = $stack.Pop()
        $directories++
        try {
            foreach ($child in (Get-ChildItem -LiteralPath $directory -Force -ErrorAction Stop)) {
                if ($child.PSIsContainer) {
                    if (($child.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                        $reparsePoints++
                    }
                    else {
                        $stack.Push($child.FullName)
                    }
                }
                else {
                    $bytes += [int64]$child.Length
                    $files++
                }
            }
        }
        catch {
            $errors.Add("$directory :: $($_.Exception.Message)")
        }
    }

    return [pscustomobject]@{
        Path = $resolved
        Exists = $true
        Bytes = $bytes
        Files = $files
        Directories = $directories
        ReparsePoints = $reparsePoints
        Errors = @($errors)
    }
}

function Convert-StatForOutput {
    param([Parameter(Mandatory)]$Stat)
    return [pscustomobject]@{
        Path = $Stat.Path
        Exists = $Stat.Exists
        Bytes = [int64]$Stat.Bytes
        GiB = [math]::Round($Stat.Bytes / 1GB, 4)
        Files = $Stat.Files
        Directories = $Stat.Directories
        ReparsePoints = $Stat.ReparsePoints
        Errors = @($Stat.Errors)
    }
}

$root = (Resolve-Path -LiteralPath $CodexHome).Path.TrimEnd('\')
$groups = [System.Collections.Generic.List[object]]::new()
$largest = [System.Collections.Generic.List[object]]::new()
$skippedReparse = [System.Collections.Generic.List[string]]::new()
$scanErrors = [System.Collections.Generic.List[string]]::new()

foreach ($entry in (Get-ChildItem -LiteralPath $root -Force -ErrorAction Stop)) {
    $bytes = [int64]0
    $files = 0
    $directories = 0
    $reparsePoints = 0

    if (-not $entry.PSIsContainer) {
        $bytes = [int64]$entry.Length
        $files = 1
        $largest.Add([pscustomobject]@{
            Path = $entry.FullName
            Bytes = [int64]$entry.Length
            LastWrite = $entry.LastWriteTime.ToString('yyyy-MM-dd HH:mm:ss')
        })
    }
    elseif (($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        $reparsePoints = 1
        $skippedReparse.Add($entry.FullName)
    }
    else {
        $stack = [System.Collections.Generic.Stack[string]]::new()
        $stack.Push($entry.FullName)
        while ($stack.Count -gt 0) {
            $directory = $stack.Pop()
            $directories++
            try {
                foreach ($child in (Get-ChildItem -LiteralPath $directory -Force -ErrorAction Stop)) {
                    if ($child.PSIsContainer) {
                        if (($child.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                            $reparsePoints++
                            $skippedReparse.Add($child.FullName)
                        }
                        else {
                            $stack.Push($child.FullName)
                        }
                    }
                    else {
                        $length = [int64]$child.Length
                        $bytes += $length
                        $files++
                        $largest.Add([pscustomobject]@{
                            Path = $child.FullName
                            Bytes = $length
                            LastWrite = $child.LastWriteTime.ToString('yyyy-MM-dd HH:mm:ss')
                        })
                    }
                }
            }
            catch {
                $scanErrors.Add("$directory :: $($_.Exception.Message)")
            }
        }
    }

    $groups.Add([pscustomobject]@{
        Name = $entry.Name
        Path = $entry.FullName
        Bytes = $bytes
        GiB = [math]::Round($bytes / 1GB, 4)
        Files = $files
        Directories = $directories
        ReparsePoints = $reparsePoints
    })
}

$candidateRows = [System.Collections.Generic.List[object]]::new()
$toolsPath = Join-Path $root 'tools'
if (Test-Path -LiteralPath $toolsPath) {
    foreach ($backup in (Get-ChildItem -LiteralPath $toolsPath -Directory -Force -ErrorAction Stop | Where-Object { $_.Name -like 'LibreOffice-*-broken-backup-*' })) {
        $candidateRows.Add([pscustomobject]@{
            Class = 'online-review'
            Reason = 'LibreOffice broken backup; require a separate working installation'
            Stat = (Convert-StatForOutput (Get-TreeStat $backup.FullName))
        })
    }

    $downloads = Join-Path $toolsPath 'downloads'
    if (Test-Path -LiteralPath $downloads) {
        $candidateRows.Add([pscustomobject]@{
            Class = 'online-review'
            Reason = 'Downloaded installers; cleanup script only selects installed LibreOffice versions'
            Stat = (Convert-StatForOutput (Get-TreeStat $downloads))
        })
    }
}

foreach ($candidate in @(
    @{ Path = (Join-Path $root '.tmp\marketplaces\.staging'); Reason = 'Incomplete marketplace staging'; Class = 'offline-safe' },
    @{ Path = (Join-Path $root 'cache'); Reason = 'Rebuildable Codex cache'; Class = 'offline-safe' }
)) {
    if (Test-Path -LiteralPath $candidate.Path) {
        $candidateRows.Add([pscustomobject]@{
            Class = $candidate.Class
            Reason = $candidate.Reason
            Stat = (Convert-StatForOutput (Get-TreeStat $candidate.Path))
        })
    }
}

$todayLogName = 'sandbox.{0}.log' -f (Get-Date -Format 'yyyy-MM-dd')
$oldSandboxLogs = @()
$sandboxPath = Join-Path $root '.sandbox'
if (Test-Path -LiteralPath $sandboxPath) {
    $oldSandboxLogs = @(Get-ChildItem -LiteralPath $sandboxPath -File -Force -Filter 'sandbox.*.log' -ErrorAction Stop |
        Where-Object { $_.Name -ne $todayLogName -and $_.LastWriteTime.Date -lt (Get-Date).Date })
}

$oldSandboxBytes = [int64](($oldSandboxLogs | Measure-Object -Property Length -Sum).Sum)
if ($oldSandboxLogs.Count -gt 0) {
    $candidateRows.Add([pscustomobject]@{
        Class = 'offline-safe'
        Reason = 'Old sandbox logs; preserve today and ACL/setup state files'
        Stat = [pscustomobject]@{
            Path = $sandboxPath
            Exists = $true
            Bytes = $oldSandboxBytes
            GiB = [math]::Round($oldSandboxBytes / 1GB, 4)
            Files = $oldSandboxLogs.Count
            Directories = 0
            ReparsePoints = 0
            Errors = @()
        }
    })
}

$processes = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -ieq 'ChatGPT.exe' -or $_.Name -ieq 'codex.exe' } |
    Select-Object ProcessId, Name, ExecutablePath)

$sessions = Convert-StatForOutput (Get-TreeStat (Join-Path $root 'sessions'))
$archived = Convert-StatForOutput (Get-TreeStat (Join-Path $root 'archived_sessions'))
$artifactStats = @(
    Convert-StatForOutput (Get-TreeStat (Join-Path $root 'generated_images'))
    Convert-StatForOutput (Get-TreeStat (Join-Path $root 'visualizations'))
    Convert-StatForOutput (Get-TreeStat (Join-Path $root 'attachments'))
)

$totalBytes = [int64](($groups | Measure-Object -Property Bytes -Sum).Sum)
$output = [pscustomobject]@{
    SchemaVersion = 1
    ScannedAt = (Get-Date).ToString('o')
    CodexHome = $root
    TotalBytes = $totalBytes
    TotalGiB = [math]::Round($totalBytes / 1GB, 4)
    OfflineSafeNow = ($processes.Count -eq 0)
    ActiveCodexProcesses = $processes
    ConversationStorage = [pscustomobject]@{
        Sessions = $sessions
        ArchivedSessions = $archived
        TotalBytes = [int64]($sessions.Bytes + $archived.Bytes)
        TotalGiB = [math]::Round(($sessions.Bytes + $archived.Bytes) / 1GB, 4)
    }
    UserArtifacts = $artifactStats
    CleanupCandidates = @($candidateRows)
    TopLevel = @($groups | Sort-Object Bytes -Descending)
    LargestFiles = @($largest | Sort-Object Bytes -Descending | Select-Object -First $TopFiles | ForEach-Object {
        [pscustomobject]@{
            Path = $_.Path
            Bytes = $_.Bytes
            MiB = [math]::Round($_.Bytes / 1MB, 2)
            LastWrite = $_.LastWrite
        }
    })
    SkippedReparsePoints = @($skippedReparse)
    Errors = @($scanErrors)
}

$output | ConvertTo-Json -Depth 8
