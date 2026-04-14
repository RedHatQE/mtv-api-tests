#!/bin/bash
set -euo pipefail

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[0;33m'
NC='\033[0m'

info() { echo -e "${YELLOW}[INFO]${NC} $*"; }
success() { echo -e "${GREEN}[OK]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }

cleanup() {
    if [[ -n "${ORIGINAL_BRANCH:-}" ]]; then
        git checkout "$ORIGINAL_BRANCH" --quiet 2>/dev/null || true
    fi
}
trap cleanup EXIT

if ! command -v gh &>/dev/null; then
    error "gh CLI is not installed"
    exit 1
fi

if ! git rev-parse --is-inside-work-tree &>/dev/null; then
    error "Not inside a git repository"
    exit 1
fi

ORIGINAL_BRANCH="$(git symbolic-ref --short HEAD 2>/dev/null || git rev-parse --short HEAD)"
info "Current branch: $ORIGINAL_BRANCH"

info "Fetching all remotes..."
git fetch --all --prune

info "Fetching protected branches from GitHub..."
PROTECTED_BRANCHES="$(gh api 'repos/{owner}/{repo}/branches' --jq '.[] | select(.protected==true) | .name' --paginate)"

if [[ -z "$PROTECTED_BRANCHES" ]]; then
    info "No protected branches found"
    exit 0
fi

info "Protected branches: $(echo "$PROTECTED_BRANCHES" | tr '\n' ' ')"

while IFS= read -r branch; do
    if git show-ref --verify --quiet "refs/heads/$branch"; then
        info "Updating existing branch: $branch"
        if git checkout "$branch" --quiet && git pull --quiet; then
            success "$branch updated"
        else
            error "Failed to update $branch"
        fi
    else
        info "Creating local branch: $branch"
        if git checkout -b "$branch" "origin/$branch" --quiet; then
            success "$branch created from origin/$branch"
        else
            error "Failed to create $branch"
        fi
    fi
done <<< "$PROTECTED_BRANCHES"

info "Switching back to $ORIGINAL_BRANCH"
