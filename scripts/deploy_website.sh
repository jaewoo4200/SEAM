#!/usr/bin/env bash
#
# Deploy website/ to the gh-pages branch (GitHub Pages) from tracked files only.
# Same behaviour as scripts/deploy_website.ps1.
#
# Publishes the website/ tree of the current HEAD commit as the root of the
# gh-pages branch. Only files tracked in git ship: gitignored drops such as
# website/assets/SEAM_* never reach the site, and files that exist only on
# gh-pages (leftovers of earlier manual deploys) are removed.
#
# Steps:
#   1. Refuse when website/ has staged, unstaged or untracked changes (the
#      deploy publishes HEAD:website, so uncommitted edits would be silently
#      left out). --dry-run only warns.
#   2. Fetch <remote> gh-pages. Refuse when the local gh-pages branch has
#      commits the remote lacks.
#   3. Check gh-pages out in a temporary worktree (--dry-run: detached, no
#      branch is touched), remove every tracked file, write HEAD:website plus an
#      empty .nojekyll, and stage everything.
#   4. --dry-run: print `git status --short` of that tree and stop. Otherwise
#      commit "website: deploy <short sha>" on gh-pages (skipped when nothing
#      changed) and push it unless --no-push.
# The temporary worktree is always removed.
#
# Usage:  bash scripts/deploy_website.sh [--dry-run] [--no-push] [--remote NAME]
set -euo pipefail

fail() { printf '\n[ERROR] %s\n' "$1" >&2; exit 1; }
step() { printf '\n==> %s\n' "$1"; }
usage() { sed -n '3,25p' "$0" | sed 's/^# \{0,1\}//'; }

DRY_RUN=0
NO_PUSH=0
REMOTE=origin
while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run|-n) DRY_RUN=1 ;;
        --no-push) NO_PUSH=1 ;;
        --remote) [ $# -ge 2 ] || fail "--remote needs a name"; REMOTE="$2"; shift ;;
        --remote=*) REMOTE="${1#--remote=}" ;;
        -h|--help) usage; exit 0 ;;
        *) fail "unknown argument: $1 (see --help)" ;;
    esac
    shift
done

command -v git >/dev/null 2>&1 || fail "git not found on PATH."
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

# ------------------------------------------------------------ source commit
step "Source: HEAD:website"
SHA="$(git rev-parse --verify HEAD)"
SHORT="$(git rev-parse --short HEAD)"
BRANCH="$(git rev-parse --abbrev-ref HEAD)"
git rev-parse --verify --quiet "$SHA:website" >/dev/null || fail "HEAD ($SHORT) has no website/ tree."
echo "  commit $SHORT on $BRANCH"

DIRTY="$(git status --porcelain --untracked-files=all -- website)"
if [ -n "$DIRTY" ]; then
    if [ "$DRY_RUN" -eq 1 ]; then
        echo "  [warn] website/ has uncommitted changes; they are NOT part of this deploy (HEAD only):"
        printf '%s\n' "$DIRTY" | sed 's/^/    /'
    else
        fail "website/ has uncommitted changes; commit them first (the deploy publishes HEAD:website):
$(printf '%s\n' "$DIRTY" | sed 's/^/    /')"
    fi
fi

# ------------------------------------------------------------ target branch
step "Target: $REMOTE gh-pages"
# No "src:dst" refspec: Git Bash would rewrite an argument containing both '/'
# and ':' as a Windows path list. FETCH_HEAD names the fetched commit instead.
git fetch -q "$REMOTE" gh-pages || fail "could not fetch gh-pages from '$REMOTE'."
GH_SHA="$(git rev-parse --verify FETCH_HEAD)"
echo "  $REMOTE/gh-pages at ${GH_SHA:0:7}"

if [ "$DRY_RUN" -eq 0 ] && git show-ref --verify --quiet refs/heads/gh-pages; then
    AHEAD="$(git rev-list --count "$GH_SHA..refs/heads/gh-pages")"
    [ "$AHEAD" -eq 0 ] || fail "local gh-pages has $AHEAD commit(s) that $REMOTE/gh-pages lacks; push or reset it first."
fi

TMP="$(mktemp -d "${TMPDIR:-/tmp}/seam-gh-pages.XXXXXX")"
WT="$TMP/site"
cleanup() {
    git -C "$REPO_ROOT" worktree remove --force "$WT" >/dev/null 2>&1 || {
        rm -rf "$WT"
        git -C "$REPO_ROOT" worktree prune >/dev/null 2>&1 || true
    }
    rm -rf "$TMP"
}
trap cleanup EXIT
if [ "$DRY_RUN" -eq 1 ]; then
    git worktree add --quiet --detach "$WT" "$GH_SHA"
else
    git worktree add --quiet -B gh-pages "$WT" "$GH_SHA"
fi

# ------------------------------------------------------------ build tree
step "Replacing the gh-pages tree with HEAD:website"
git -C "$WT" rm -r -q --ignore-unmatch .
git -C "$WT" read-tree "$SHA:website"
git -C "$WT" checkout-index -a -f
: > "$WT/.nojekyll"
git -C "$WT" add -A

STATUS="$(git -C "$WT" status --short)"
STAT="$(git -C "$WT" diff --cached --shortstat)"

if [ "$DRY_RUN" -eq 1 ]; then
    step "Dry run: git status --short of the deploy tree (nothing committed)"
    if [ -z "$STATUS" ]; then
        echo "  (no changes: gh-pages already matches $SHORT)"
    else
        printf '%s\n' "$STATUS" | sed 's/^/  /'
        echo "$STAT"
    fi
elif [ -z "$STATUS" ]; then
    step "Nothing to deploy: gh-pages already matches $SHORT"
else
    step "Committing on gh-pages"
    printf '%s\n' "$STATUS" | sed 's/^/  /'
    echo "$STAT"
    git -C "$WT" commit -q -m "website: deploy $SHORT"
    echo "  gh-pages -> $(git -C "$WT" rev-parse --short HEAD) (website: deploy $SHORT)"
    if [ "$NO_PUSH" -eq 1 ]; then
        echo "  --no-push: not pushed. Publish with: git push $REMOTE gh-pages"
    else
        step "Pushing gh-pages to $REMOTE"
        git -C "$WT" push -q "$REMOTE" gh-pages
        echo "  pushed; GitHub Pages rebuilds the site in a minute or two."
    fi
fi
