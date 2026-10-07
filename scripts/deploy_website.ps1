<#
.SYNOPSIS
  Deploy website/ to the gh-pages branch (GitHub Pages) from tracked files only.

.DESCRIPTION
  Publishes the website/ tree of the current HEAD commit as the root of the
  gh-pages branch. Only files tracked in git ship: gitignored drops such as
  website/assets/SEAM_* never reach the site, and files that exist only on
  gh-pages (leftovers of earlier manual deploys) are removed.

  Steps:
    1. Refuse when website/ has staged, unstaged or untracked changes (the
       deploy publishes HEAD:website, so uncommitted edits would be silently
       left out). -DryRun only warns.
    2. Fetch <Remote> gh-pages. Refuse when the local gh-pages branch has
       commits the remote lacks.
    3. Check gh-pages out in a temporary worktree (-DryRun: detached, no branch
       is touched), remove every tracked file, write HEAD:website plus an empty
       .nojekyll, and stage everything.
    4. -DryRun: print `git status --short` of that tree and stop. Otherwise
       commit "website: deploy <short sha>" on gh-pages (skipped when nothing
       changed) and push it unless -NoPush.
  The temporary worktree is always removed.

.PARAMETER DryRun
  Stop before the commit and print `git status --short` of the deploy tree.

.PARAMETER NoPush
  Commit to the local gh-pages branch but do not push it.

.PARAMETER Remote
  Remote that hosts gh-pages (default: origin).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\deploy_website.ps1 -DryRun

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\deploy_website.ps1
#>
param(
    [switch]$DryRun,
    [switch]$NoPush,
    [string]$Remote = "origin"
)

# PowerShell 5.1-compatible: no '&&', no ternary. Fail loudly.
$ErrorActionPreference = "Stop"

function Fail($msg) {
    Write-Host ""
    Write-Host "[ERROR] $msg" -ForegroundColor Red
    exit 1
}

function Step($msg) {
    Write-Host ""
    Write-Host "==> $msg" -ForegroundColor Cyan
}

# Returns git's stdout lines. Windows PowerShell turns native stderr (progress,
# hints) into terminating errors under "Stop", so git runs under "Continue" and
# the exit code decides; $script:GitExit keeps it for -AllowFail callers.
function Invoke-Git([string[]]$GitArgs, [switch]$AllowFail) {
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $out = & git @GitArgs
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $prev
    }
    $script:GitExit = $code
    if ($code -ne 0 -and -not $AllowFail) {
        throw "git $($GitArgs -join ' ') failed (exit $code)"
    }
    if ($null -eq $out) { return }
    return $out
}

if (-not (Get-Command git -ErrorAction SilentlyContinue)) { Fail "git not found on PATH." }

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Split-Path -Parent $ScriptDir
Set-Location $RepoRoot

# ------------------------------------------------------------ source commit
Step "Source: HEAD:website"
$Sha = [string](Invoke-Git @("rev-parse", "--verify", "HEAD") | Select-Object -Last 1)
$Short = [string](Invoke-Git @("rev-parse", "--short", "HEAD") | Select-Object -Last 1)
$Branch = [string](Invoke-Git @("rev-parse", "--abbrev-ref", "HEAD") | Select-Object -Last 1)
Invoke-Git @("rev-parse", "--verify", "--quiet", "$($Sha):website") -AllowFail | Out-Null
if ($script:GitExit -ne 0) { Fail "HEAD ($Short) has no website/ tree." }
Write-Host "  commit $Short on $Branch"

$Dirty = @(Invoke-Git @("status", "--porcelain", "--untracked-files=all", "--", "website"))
if ($Dirty.Count -gt 0) {
    $DirtyList = ($Dirty | ForEach-Object { "    $_" }) -join "`n"
    if ($DryRun) {
        Write-Host "  [warn] website/ has uncommitted changes; they are NOT part of this deploy (HEAD only):" -ForegroundColor Yellow
        Write-Host $DirtyList -ForegroundColor Yellow
    } else {
        Fail ("website/ has uncommitted changes; commit them first (the deploy publishes HEAD:website):`n" + $DirtyList)
    }
}

# ------------------------------------------------------------ target branch
Step "Target: $Remote gh-pages"
Invoke-Git @("fetch", "-q", $Remote, "gh-pages") -AllowFail | Out-Null
if ($script:GitExit -ne 0) { Fail "could not fetch gh-pages from '$Remote'." }
$GhSha = [string](Invoke-Git @("rev-parse", "--verify", "FETCH_HEAD") | Select-Object -Last 1)
Write-Host "  $Remote/gh-pages at $($GhSha.Substring(0, 7))"

if (-not $DryRun) {
    Invoke-Git @("show-ref", "--verify", "--quiet", "refs/heads/gh-pages") -AllowFail | Out-Null
    if ($script:GitExit -eq 0) {
        $Ahead = [int](Invoke-Git @("rev-list", "--count", "$GhSha..refs/heads/gh-pages") | Select-Object -Last 1)
        if ($Ahead -gt 0) {
            Fail "local gh-pages has $Ahead commit(s) that $Remote/gh-pages lacks; push or reset it first."
        }
    }
}

$Tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("seam-gh-pages-" + [guid]::NewGuid().ToString("N").Substring(0, 8))
if ($DryRun) {
    Invoke-Git @("worktree", "add", "--quiet", "--detach", $Tmp, $GhSha) | Out-Null
} else {
    Invoke-Git @("worktree", "add", "--quiet", "-B", "gh-pages", $Tmp, $GhSha) | Out-Null
}

try {
    # ------------------------------------------------------------ build tree
    Step "Replacing the gh-pages tree with HEAD:website"
    Invoke-Git @("-C", $Tmp, "rm", "-r", "-q", "--ignore-unmatch", ".") | Out-Null
    Invoke-Git @("-C", $Tmp, "read-tree", "$($Sha):website") | Out-Null
    Invoke-Git @("-C", $Tmp, "checkout-index", "-a", "-f") | Out-Null
    [System.IO.File]::WriteAllText((Join-Path $Tmp ".nojekyll"), "")
    Invoke-Git @("-C", $Tmp, "add", "-A") | Out-Null

    $Status = @(Invoke-Git @("-C", $Tmp, "status", "--short"))
    $Stat = [string](Invoke-Git @("-C", $Tmp, "diff", "--cached", "--shortstat") | Select-Object -Last 1)

    if ($DryRun) {
        Step "Dry run: git status --short of the deploy tree (nothing committed)"
        if ($Status.Count -eq 0) {
            Write-Host "  (no changes: gh-pages already matches $Short)"
        } else {
            $Status | ForEach-Object { Write-Host "  $_" }
            Write-Host " $Stat"
        }
    } elseif ($Status.Count -eq 0) {
        Step "Nothing to deploy: gh-pages already matches $Short"
    } else {
        Step "Committing on gh-pages"
        $Status | ForEach-Object { Write-Host "  $_" }
        Write-Host " $Stat"
        Invoke-Git @("-C", $Tmp, "commit", "-q", "-m", "website: deploy $Short") | Out-Null
        $NewSha = [string](Invoke-Git @("-C", $Tmp, "rev-parse", "--short", "HEAD") | Select-Object -Last 1)
        Write-Host "  gh-pages -> $NewSha (website: deploy $Short)"
        if ($NoPush) {
            Write-Host "  -NoPush: not pushed. Publish with: git push $Remote gh-pages" -ForegroundColor Yellow
        } else {
            Step "Pushing gh-pages to $Remote"
            Invoke-Git @("-C", $Tmp, "push", "-q", $Remote, "gh-pages") | Out-Null
            Write-Host "  pushed; GitHub Pages rebuilds the site in a minute or two."
        }
    }
} finally {
    Invoke-Git @("worktree", "remove", "--force", $Tmp) -AllowFail | Out-Null
    if ($script:GitExit -ne 0) {
        if (Test-Path $Tmp) { Remove-Item -Recurse -Force $Tmp }
        Invoke-Git @("worktree", "prune") -AllowFail | Out-Null
    }
}
