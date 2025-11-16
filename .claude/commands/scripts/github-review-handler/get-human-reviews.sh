#!/bin/bash

# Script to extract human reviewer comments for processing
# Usage: get-human-reviews.sh <pr-info-script-path>
#   OR:  get-human-reviews.sh <owner/repo> <pr_number>

if [ $# -eq 1 ]; then
  # Single argument: path to pr-info script
  PR_INFO_SCRIPT="$1"

  if [ ! -f "$PR_INFO_SCRIPT" ]; then
    echo "❌ Error: PR info script not found: $PR_INFO_SCRIPT"
    exit 1
  fi

  # Call the pr-info script and parse output
  PR_INFO=$("$PR_INFO_SCRIPT")
  if [ $? -ne 0 ]; then
    echo "❌ Error: Failed to get PR information"
    exit 1
  fi

  # Parse the output (space-separated: REPO_FULL_NAME PR_NUMBER)
  REPO_FULL_NAME=$(echo "$PR_INFO" | cut -d' ' -f1)
  PR_NUMBER=$(echo "$PR_INFO" | cut -d' ' -f2)

elif [ $# -eq 2 ]; then
  # Two arguments: direct repo and PR number (backwards compatibility)
  REPO_FULL_NAME="$1"
  PR_NUMBER="$2"

else
  echo "Usage: $0 <pr-info-script-path>"
  echo "   OR: $0 <owner/repo> <pr_number>"
  exit 1
fi

OWNER=$(echo "$REPO_FULL_NAME" | cut -d'/' -f1)
REPO=$(echo "$REPO_FULL_NAME" | cut -d'/' -f2)

# Step 1: Get the latest commit SHA and timestamp
LATEST_COMMIT_SHA=$(gh api "/repos/$OWNER/$REPO/pulls/$PR_NUMBER" --jq '.head.sha')

if [ -z "$LATEST_COMMIT_SHA" ]; then
  echo "❌ Error: Could not retrieve latest commit SHA"
  exit 1
fi

# Get the latest commit timestamp
LATEST_COMMIT_DATE=$(gh api "/repos/$OWNER/$REPO/commits/$LATEST_COMMIT_SHA" --jq '.commit.committer.date')

if [ -z "$LATEST_COMMIT_DATE" ]; then
  echo "❌ Error: Could not retrieve latest commit date"
  exit 1
fi

# Step 2: Get all reviews submitted after the latest commit, excluding CodeRabbit
HUMAN_REVIEWS=$(gh api "/repos/$OWNER/$REPO/pulls/$PR_NUMBER/reviews" |
  jq --arg bot_user "coderabbitai[bot]" --arg latest_date "$LATEST_COMMIT_DATE" \
    '[.[] | select(.user.login != $bot_user and (.body | length) > 10 and .submitted_at > $latest_date)] | sort_by(.submitted_at)')

# Step 3: Get all review comments from human reviewers
ALL_COMMENTS='[]'

for review_id in $(echo "$HUMAN_REVIEWS" | jq -r '.[].id'); do
  REVIEWER=$(echo "$HUMAN_REVIEWS" | jq -r --arg id "$review_id" '.[] | select(.id == ($id | tonumber)) | .user.login')

  # Get inline comments for this review
  REVIEW_COMMENTS=$(gh api "/repos/$OWNER/$REPO/pulls/$PR_NUMBER/reviews/$review_id/comments" |
    jq --arg reviewer "$REVIEWER" '[.[] |
      {
        reviewer: $reviewer,
        file: .path,
        line: (.line // .original_line // ""),
        body: .body
      }
    ]')

  # Merge with all comments
  ALL_COMMENTS=$(echo "$ALL_COMMENTS $REVIEW_COMMENTS" | jq -s 'add')
done

# Step 4: Get PR review comments (not inline review comments) created after the latest commit
PR_COMMENTS=$(gh api "/repos/$OWNER/$REPO/pulls/$PR_NUMBER/comments" |
  jq --arg bot_user "coderabbitai[bot]" --arg latest_date "$LATEST_COMMIT_DATE" '[.[] |
    select(.user.login != $bot_user and (.body | length) > 10 and .created_at > $latest_date) |
    {
      reviewer: .user.login,
      file: .path,
      line: (.line // .original_line // ""),
      body: .body
    }
  ]')

# Merge PR review comments with review-specific comments
ALL_COMMENTS=$(echo "$ALL_COMMENTS $PR_COMMENTS" | jq -s 'add')

# Note: We only include review comments (with file/line info) submitted after the latest commit, not general PR conversation comments
# This ensures we only get human feedback that came after the latest changes

# Count comments
TOTAL_COUNT=$(echo "$ALL_COMMENTS" | jq '. | length')

# Build final JSON
JSON_OUTPUT="{"
JSON_OUTPUT="$JSON_OUTPUT\"summary\": {"
JSON_OUTPUT="$JSON_OUTPUT\"total\": $TOTAL_COUNT"
JSON_OUTPUT="$JSON_OUTPUT},"
JSON_OUTPUT="$JSON_OUTPUT\"comments\": $ALL_COMMENTS"
JSON_OUTPUT="$JSON_OUTPUT}"

echo "$JSON_OUTPUT" | jq '.'
