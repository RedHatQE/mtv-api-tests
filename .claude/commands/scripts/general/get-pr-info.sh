#!/bin/bash

# Script to get current branch and PR information
# Usage: get-pr-info.sh
# Returns: REPO_FULL_NAME PR_NUMBER (space separated)

# Get current branch
CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD)

if [ -z "$CURRENT_BRANCH" ]; then
  echo "❌ Error: Could not get current branch" >&2
  exit 1
fi

# Get PR number for current branch
PR_NUMBER=$(gh pr view "$CURRENT_BRANCH" --json number --jq .number)

if [ -z "$PR_NUMBER" ] || [ "$PR_NUMBER" == "null" ]; then
  echo "❌ Error: No PR found for branch '$CURRENT_BRANCH'" >&2
  exit 1
fi

# Get repository full name
REPO_FULL_NAME=$(gh repo view --json owner,name -q '.owner.login + "/" + .name')

if [ -z "$REPO_FULL_NAME" ]; then
  echo "❌ Error: Could not get repository information" >&2
  exit 1
fi

# Output the results (space separated for easy parsing)
echo "$REPO_FULL_NAME $PR_NUMBER"
