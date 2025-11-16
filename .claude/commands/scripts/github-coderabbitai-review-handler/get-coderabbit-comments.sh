#!/bin/bash

# Script to extract CodeRabbit comments for AI processing
# Usage: get-coderabbit-comments.sh <pr-info-script-path> [commit_sha|review_id|review_url]
#   OR:  get-coderabbit-comments.sh <owner/repo> <pr_number> [commit_sha|review_id|review_url]

if [ $# -eq 1 ] || [ $# -eq 2 ]; then
  # One or two arguments: check if first arg is a file (pr-info script)
  if [ -f "$1" ]; then
    # First argument is pr-info script path
    PR_INFO_SCRIPT="$1"
    TARGET_PARAM="${2:-}"  # Optional second parameter (commit_sha, review_id, or review_url)

    # Call the pr-info script and parse output
    PR_INFO=$("$PR_INFO_SCRIPT")
    if [ $? -ne 0 ]; then
      echo "❌ Error: Failed to get PR information" >&2
      exit 1
    fi

    # Parse the output (space-separated: REPO_FULL_NAME PR_NUMBER)
    REPO_FULL_NAME=$(echo "$PR_INFO" | cut -d' ' -f1)
    PR_NUMBER=$(echo "$PR_INFO" | cut -d' ' -f2)
  else
    # First argument is owner/repo, second is PR number
    REPO_FULL_NAME="$1"
    PR_NUMBER="$2"
    TARGET_PARAM=""
  fi

elif [ $# -eq 3 ]; then
  # Three arguments: owner/repo, PR number, and target
  REPO_FULL_NAME="$1"
  PR_NUMBER="$2"
  TARGET_PARAM="$3"

else
  echo "Usage: $0 <pr-info-script-path> [commit_sha|review_id|review_url]" >&2
  echo "   OR: $0 <owner/repo> <pr_number> [commit_sha|review_id|review_url]" >&2
  echo "" >&2
  echo "Examples:" >&2
  echo "  $0 /path/to/get-pr-info.sh                      # Latest commit" >&2
  echo "  $0 /path/to/get-pr-info.sh 3379917343           # Specific review ID" >&2
  echo "  $0 owner/repo 123 abc1234...                    # Use specific commit SHA" >&2
  echo "  $0 owner/repo 123 3379917343                    # Use review ID" >&2
  echo "  $0 owner/repo 123 https://github.com/.../pull/123#pullrequestreview-3379917343" >&2
  exit 1
fi

OWNER=$(echo "$REPO_FULL_NAME" | cut -d'/' -f1)
REPO=$(echo "$REPO_FULL_NAME" | cut -d'/' -f2)

# Step 1: Determine target commit SHA from parameter
if [ -n "$TARGET_PARAM" ]; then
  # Check if it's a review URL
  if [[ "$TARGET_PARAM" =~ pullrequestreview-([0-9]+) ]]; then
    REVIEW_ID="${BASH_REMATCH[1]}"
    echo "📝 Extracting commit from review URL (review ID: $REVIEW_ID)..." >&2
    LATEST_COMMIT_SHA=$(gh api "/repos/$OWNER/$REPO/pulls/$PR_NUMBER/reviews/$REVIEW_ID" --jq '.commit_id')
    if [ -z "$LATEST_COMMIT_SHA" ] || [ "$LATEST_COMMIT_SHA" == "null" ]; then
      echo "❌ Error: Could not retrieve commit SHA from review $REVIEW_ID" >&2
      exit 1
    fi
    echo "✅ Using commit from review: $LATEST_COMMIT_SHA" >&2
  # Check if it's a numeric review ID
  elif [[ "$TARGET_PARAM" =~ ^[0-9]+$ ]]; then
    REVIEW_ID="$TARGET_PARAM"
    echo "📝 Extracting commit from review ID: $REVIEW_ID..." >&2
    LATEST_COMMIT_SHA=$(gh api "/repos/$OWNER/$REPO/pulls/$PR_NUMBER/reviews/$REVIEW_ID" --jq '.commit_id')
    if [ -z "$LATEST_COMMIT_SHA" ] || [ "$LATEST_COMMIT_SHA" == "null" ]; then
      echo "❌ Error: Could not retrieve commit SHA from review $REVIEW_ID" >&2
      exit 1
    fi
    echo "✅ Using commit from review: $LATEST_COMMIT_SHA" >&2
  # Otherwise treat as commit SHA
  else
    LATEST_COMMIT_SHA="$TARGET_PARAM"
    echo "📝 Using provided commit SHA: $LATEST_COMMIT_SHA" >&2
  fi
else
  # Get the latest commit SHA from PR
  LATEST_COMMIT_SHA=$(gh api "/repos/$OWNER/$REPO/pulls/$PR_NUMBER" --jq '.head.sha')
  echo "📝 Using latest commit from PR: $LATEST_COMMIT_SHA" >&2
fi

if [ -z "$LATEST_COMMIT_SHA" ]; then
  echo "❌ Error: Could not retrieve commit SHA" >&2
  exit 1
fi

# Step 2: Get CodeRabbit reviews for the target commit
# Note: GitHub API defaults to 30 items per page, so we request more to avoid missing recent reviews
REVIEW_DATA=$(gh api "/repos/$OWNER/$REPO/pulls/$PR_NUMBER/reviews?per_page=100" |
  jq --arg bot_user "coderabbitai[bot]" --arg latest_sha "$LATEST_COMMIT_SHA" \
    '[.[] | select(.user.login == $bot_user and (.body | length) > 100 and .commit_id == $latest_sha)] | sort_by(.submitted_at) | .[-1]')

REVIEW_ID=$(echo "$REVIEW_DATA" | jq -r '.id')

if [ -z "$REVIEW_ID" ] || [ "$REVIEW_ID" == "null" ]; then
  echo "❌ No CodeRabbit reviews found"
  exit 1
fi

# Step 3: Get inline comments (actionable)
INLINE_COMMENTS=$(gh api "/repos/$OWNER/$REPO/pulls/$PR_NUMBER/reviews/$REVIEW_ID/comments" --jq '.')

# Step 4: Get review body (contains nitpicks)
REVIEW_BODY=$(gh api "/repos/$OWNER/$REPO/pulls/$PR_NUMBER/reviews/$REVIEW_ID" --jq '.body')

# Extract actionable comments with AI prompts
ACTIONABLE_COMMENTS=$(echo "$INLINE_COMMENTS" | jq '[.[] |
  {
    priority: "HIGH",
    title: (.body | split("\n")[2] | ltrimstr("**") | rtrimstr("**")),
    file: .path,
    body: (if (.body | contains("🤖 Prompt for AI Agents")) then
            (.body | split("🤖 Prompt for AI Agents")[1] | split("```")[1] | split("```")[0] | gsub("^\\n+|\\n+$"; ""))
          else
            .body
          end)
  }
]')

# Function to parse comment blocks by pattern
parse_comment_blocks() {
  local pattern="$1"
  local priority="$2"

  echo "$REVIEW_BODY" | awk -v pattern="$pattern" -v priority="$priority" '
    BEGIN {
        in_block = 0
        content = ""
        printf "["
        first = 1
        current_file = ""
    }

    # Extract file path from collapsible summary lines
    /<summary>.*\([0-9]+\)<\/summary>/ {
        match($0, /<summary>([^(]+) \([0-9]+\)<\/summary>/, arr)
        # Only update if it looks like a file path (contains slash or dot)
        if (arr[1] != "" && (arr[1] ~ /\// || arr[1] ~ /\./)) {
            current_file = arr[1]
        }
        next
    }

    # Stop processing at Review details section
    /^<details>$/ {
        getline
        if (/^<summary>📜 Review details<\/summary>$/) {
            # Output current block if we have one
            if (in_block && content != "") {
                if (first != 1) printf ","
                output_block()
            }
            exit
        }
    }

    # Start of a comment block with backtick pattern
    /^`[0-9]+-?[0-9]*`: \*\*.*\*\*/ {
        # Output previous block if we have one
        if (content != "" && in_block) {
            if (first != 1) printf ","
            output_block()
            first = 0
        }

        # Extract line and title
        match($0, /`([^`]+)`: \*\*(.+)\*\*/, arr)
        line_num = arr[1]
        title = arr[2]
        content = $0
        in_block = 1
        next
    }

    # Continuation of current block - lines that are not separators or new blocks
    in_block && $0 !~ /^`[0-9]+-?[0-9]*`: \*\*.*\*\*/ && $0 !~ /^----$/ && $0 !~ /^<details>$/ {
        # Add non-empty lines to content
        if ($0 != "") {
            content = content "\n" $0
        }
        next
    }

    # End of block (separator line or start of new block)
    in_block && ($0 ~ /^----$/ || $0 ~ /^`[0-9]+-?[0-9]*`: \*\*.*\*\*/) {
        if (content != "") {
            if (first != 1) printf ","
            output_block()
            first = 0
        }
        in_block = 0
        content = ""

        # Check if this line starts a new block
        if ($0 ~ /^`[0-9]+-?[0-9]*`: \*\*.*\*\*/) {
            match($0, /`([^`]+)`: \*\*(.+)\*\*/, arr)
            line_num = arr[1]
            title = arr[2]
            content = $0
            in_block = 1
        }
    }

    function output_block() {
        # Clean up trailing separators and unwanted content
        gsub(/\n---$/, "", content)
        gsub(/\n----$/, "", content)
        gsub(/<\/blockquote><\/details>.*$/, "", content)
        gsub(/\n+$/, "", content)

        # For nitpicks, remove only HTML/markdown formatting, keep all text
        if (priority == "LOW") {
            # Remove code blocks (```...```)
            gsub(/```[^`]*```/, "", content)
            # Remove HTML tags
            gsub(/<[^>]*>/, "", content)
            # Clean up extra whitespace but keep all text content
            gsub(/\n\n+/, "\n", content)
            gsub(/\n+$/, "", content)
        }

        gsub(/"/, "\\\"", title)
        gsub(/"/, "\\\"", content)
        gsub(/\n/, "\\n", content)

        # Always include file field for consistency
        gsub(/"/, "\\\"", current_file)
        printf "{\n  \"priority\": \"%s\",\n  \"title\": \"%s\",\n  \"file\": \"%s\",\n  \"line\": \"%s\",\n  \"body\": \"%s\"\n}", priority, title, current_file, line_num, content
    }

    END {
        if (in_block && content != "") {
            if (first != 1) printf ","
            output_block()
        }
        printf "]\n"
    }' | jq '.'
}

# Extract actionable comments (check if any exist)
ACTIONABLE_COMMENTS=$(echo "$INLINE_COMMENTS" | jq '. | length' | grep -q '^[1-9]' && echo "$ACTIONABLE_COMMENTS" || echo '[]')

# Extract nitpick comments (check if any exist) with multiple file support
if echo "$REVIEW_BODY" | grep -q "Nitpick comments"; then
    NITPICK_COMMENTS=$(echo "$REVIEW_BODY" | awk '
    BEGIN {
        in_nitpick_section = 0
        in_file_section = 0
        in_block = 0
        content = ""
        printf "["
        first = 1
        current_file = ""
    }

    # Detect nitpick section start
    /🧹 Nitpick comments/ { in_nitpick_section = 1; next }

    # Stop at review details
    /📜 Review details/ { exit }

    # Extract file path from file-specific summary
    in_nitpick_section && /<summary>.*\([0-9]+\)<\/summary>/ {
        # Output any pending block before changing files
        if (in_block && content != "") {
            if (first != 1) printf ","
            output_block()
            first = 0
            in_block = 0
            content = ""
        }

        match($0, /<summary>([^(]+) \([0-9]+\)<\/summary>/, arr)
        if (arr[1] != "" && (arr[1] ~ /\// || arr[1] ~ /\./)) {
            current_file = arr[1]
            in_file_section = 1
        }
        next
    }

    # Start of nitpick block
    in_file_section && /^`[0-9]+-?[0-9]*`: \*\*.*\*\*/ {
        # Output previous block if exists
        if (in_block && content != "") {
            if (first != 1) printf ","
            output_block()
            first = 0
        }

        # Extract line and title
        match($0, /`([^`]+)`: \*\*(.+)\*\*/, arr)
        line_num = arr[1]
        title = arr[2]
        content = $0
        in_block = 1
        next
    }

    # Continue nitpick content
    in_block && $0 !~ /^`[0-9]+-?[0-9]*`: \*\*.*\*\*/ && $0 !~ /^----$/ && $0 !~ /<\/summary>/ {
        if ($0 != "") {
            content = content "\n" $0
        }
        next
    }

    # End of block
    in_block && ($0 ~ /^----$/ || $0 ~ /^`[0-9]+-?[0-9]*`: \*\*.*\*\*/ || $0 ~ /<\/blockquote>/) {
        if (content != "") {
            if (first != 1) printf ","
            output_block()
            first = 0
        }
        in_block = 0
        content = ""

        # Check if new block starts
        if ($0 ~ /^`[0-9]+-?[0-9]*`: \*\*.*\*\*/) {
            match($0, /`([^`]+)`: \*\*(.+)\*\*/, arr)
            line_num = arr[1]
            title = arr[2]
            content = $0
            in_block = 1
        }
    }

    function output_block() {
        # Clean up content
        gsub(/\n---$/, "", content)
        gsub(/\n----$/, "", content)
        gsub(/```[^`]*```/, "", content)
        gsub(/<[^>]*>/, "", content)
        gsub(/\n\n+/, "\n", content)
        gsub(/\n+$/, "", content)

        # Escape backslashes first, then quotes, then newlines
        gsub(/\\/, "\\\\", title)
        gsub(/\\/, "\\\\", content)
        gsub(/\\/, "\\\\", current_file)
        gsub(/"/, "\\\"", title)
        gsub(/"/, "\\\"", content)
        gsub(/"/, "\\\"", current_file)
        gsub(/\n/, "\\n", content)

        printf "{\n  \"priority\": \"LOW\",\n  \"title\": \"%s\",\n  \"file\": \"%s\",\n  \"line\": \"%s\",\n  \"body\": \"%s\"\n}", title, current_file, line_num, content
    }

    END {
        if (in_block && content != "") {
            if (first != 1) printf ","
            output_block()
        }
        printf "]\n"
    }' | jq '.')
else
    NITPICK_COMMENTS='[]'
fi

# Extract duplicate comments (check if any exist) with multiple file support
if echo "$REVIEW_BODY" | grep -q "Duplicate comments"; then
    DUPLICATE_COMMENTS=$(echo "$REVIEW_BODY" | awk '
    BEGIN {
        in_duplicate_section = 0
        in_file_section = 0
        in_block = 0
        content = ""
        printf "["
        first = 1
        current_file = ""
    }

    # Detect duplicate section start
    /♻️ Duplicate comments/ { in_duplicate_section = 1; next }

    # Stop at next major section or review details
    /🧹 Nitpick comments/ || /📜 Review details/ { exit }

    # Extract file path from file-specific summary
    in_duplicate_section && /<summary>.*\([0-9]+\)<\/summary>/ {
        # Output any pending block before changing files
        if (in_block && content != "") {
            if (first != 1) printf ","
            output_block()
            first = 0
            in_block = 0
            content = ""
        }

        match($0, /<summary>([^(]+) \([0-9]+\)<\/summary>/, arr)
        if (arr[1] != "" && (arr[1] ~ /\// || arr[1] ~ /\./)) {
            current_file = arr[1]
            in_file_section = 1
        }
        next
    }

    # Start of duplicate block
    in_file_section && /^`[0-9]+-?[0-9]*`: \*\*.*\*\*/ {
        # Output previous block if exists
        if (in_block && content != "") {
            if (first != 1) printf ","
            output_block()
            first = 0
        }

        # Extract line and title
        match($0, /`([^`]+)`: \*\*(.+)\*\*/, arr)
        line_num = arr[1]
        title = arr[2]
        content = $0
        in_block = 1
        next
    }

    # Continue duplicate content
    in_block && $0 !~ /^`[0-9]+-?[0-9]*`: \*\*.*\*\*/ && $0 !~ /^----$/ && $0 !~ /<\/summary>/ {
        if ($0 != "") {
            content = content "\n" $0
        }
        next
    }

    # End of block
    in_block && ($0 ~ /^----$/ || $0 ~ /^`[0-9]+-?[0-9]*`: \*\*.*\*\*/ || $0 ~ /<\/blockquote>/) {
        if (content != "") {
            if (first != 1) printf ","
            output_block()
            first = 0
        }
        in_block = 0
        content = ""

        # Check if new block starts
        if ($0 ~ /^`[0-9]+-?[0-9]*`: \*\*.*\*\*/) {
            match($0, /`([^`]+)`: \*\*(.+)\*\*/, arr)
            line_num = arr[1]
            title = arr[2]
            content = $0
            in_block = 1
        }
    }

    function output_block() {
        # Clean up content
        gsub(/\n---$/, "", content)
        gsub(/\n----$/, "", content)
        gsub(/```[^`]*```/, "", content)
        gsub(/<[^>]*>/, "", content)
        gsub(/\n\n+/, "\n", content)
        gsub(/\n+$/, "", content)

        # Escape backslashes first, then quotes, then newlines
        gsub(/\\/, "\\\\", title)
        gsub(/\\/, "\\\\", content)
        gsub(/\\/, "\\\\", current_file)
        gsub(/"/, "\\\"", title)
        gsub(/"/, "\\\"", content)
        gsub(/"/, "\\\"", current_file)
        gsub(/\n/, "\\n", content)

        printf "{\n  \"priority\": \"MEDIUM\",\n  \"title\": \"%s\",\n  \"file\": \"%s\",\n  \"line\": \"%s\",\n  \"body\": \"%s\"\n}", title, current_file, line_num, content
    }

    END {
        if (in_block && content != "") {
            if (first != 1) printf ","
            output_block()
        }
        printf "]\n"
    }' | jq 'group_by(.title) | map(.[0])')
else
    DUPLICATE_COMMENTS='[]'
fi

# Extract outside diff range comments (check if any exist) with multiple file support
if echo "$REVIEW_BODY" | grep -q "Outside diff range"; then
    OUTSIDE_DIFF_COMMENTS=$(echo "$REVIEW_BODY" | awk '
    BEGIN {
        in_outside_diff_section = 0
        in_file_section = 0
        in_block = 0
        content = ""
        printf "["
        first = 1
        current_file = ""
    }

    # Detect outside diff range section start
    /⚠️ Outside diff range/ || /Outside diff range/ { in_outside_diff_section = 1; next }

    # Stop at nitpick section (since outside diff comes before nitpicks) or other major sections
    (in_outside_diff_section && /🧹 Nitpick comments/) || /♻️ Duplicate comments/ || /📜 Review details/ { exit }

    # Extract file path from file-specific summary
    in_outside_diff_section && /<summary>.*\([0-9]+\)<\/summary>/ {
        # Output any pending block before changing files
        if (in_block && content != "") {
            if (first != 1) printf ","
            output_block()
            first = 0
            in_block = 0
            content = ""
        }

        match($0, /<summary>([^(]+) \([0-9]+\)<\/summary>/, arr)
        if (arr[1] != "" && (arr[1] ~ /\// || arr[1] ~ /\./)) {
            current_file = arr[1]
            in_file_section = 1
        }
        next
    }

    # Start of outside diff range block (handle > prefix from JSON)
    in_file_section && (/^`[0-9]+-?[0-9]*`: \*\*.*\*\*/ || /^> `[0-9]+-?[0-9]*`: \*\*.*\*\*/) {
        # Output previous block if exists
        if (in_block && content != "") {
            if (first != 1) printf ","
            output_block()
            first = 0
        }

        # Extract line and title
        match($0, /`([^`]+)`: \*\*(.+)\*\*/, arr)
        line_num = arr[1]
        title = arr[2]
        content = $0
        in_block = 1
        next
    }

    # Continue outside diff range content
    in_block && $0 !~ /^`[0-9]+-?[0-9]*`: \*\*.*\*\*/ && $0 !~ /^> `[0-9]+-?[0-9]*`: \*\*.*\*\*/ && $0 !~ /^----$/ && $0 !~ /<\/summary>/ {
        if ($0 != "") {
            content = content "\n" $0
        }
        next
    }

    # End of block
    in_block && ($0 ~ /^----$/ || $0 ~ /^`[0-9]+-?[0-9]*`: \*\*.*\*\*/ || $0 ~ /^> `[0-9]+-?[0-9]*`: \*\*.*\*\*/ || $0 ~ /<\/blockquote>/) {
        if (content != "") {
            if (first != 1) printf ","
            output_block()
            first = 0
        }
        in_block = 0
        content = ""

        # Check if new block starts
        if ($0 ~ /^`[0-9]+-?[0-9]*`: \*\*.*\*\*/ || $0 ~ /^> `[0-9]+-?[0-9]*`: \*\*.*\*\*/) {
            match($0, /`([^`]+)`: \*\*(.+)\*\*/, arr)
            line_num = arr[1]
            title = arr[2]
            content = $0
            in_block = 1
        }
    }

    function output_block() {
        # Clean up content (similar to nitpicks - remove formatting but keep text)
        gsub(/\n---$/, "", content)
        gsub(/\n----$/, "", content)
        gsub(/```[^`]*```/, "", content)
        gsub(/<[^>]*>/, "", content)
        gsub(/\n\n+/, "\n", content)
        gsub(/\n+$/, "", content)

        # Escape backslashes first, then quotes, then newlines
        gsub(/\\/, "\\\\", title)
        gsub(/\\/, "\\\\", content)
        gsub(/\\/, "\\\\", current_file)
        gsub(/"/, "\\\"", title)
        gsub(/"/, "\\\"", content)
        gsub(/"/, "\\\"", current_file)
        gsub(/\n/, "\\n", content)

        printf "{\n  \"priority\": \"VERY LOW\",\n  \"title\": \"%s\",\n  \"file\": \"%s\",\n  \"line\": \"%s\",\n  \"body\": \"%s\"\n}", title, current_file, line_num, content
    }

    END {
        if (in_block && content != "") {
            if (first != 1) printf ","
            output_block()
        }
        printf "]\n"
    }' | jq 'group_by(.title) | map(.[0])')
else
    OUTSIDE_DIFF_COMMENTS='[]'
fi

# Count actual items
ACTIONABLE_COUNT=$(echo "$ACTIONABLE_COMMENTS" | jq '. | length')
NITPICK_COUNT=$(echo "$NITPICK_COMMENTS" | jq '. | length')
DUPLICATE_COUNT=$(echo "$DUPLICATE_COMMENTS" | jq '. | length')
OUTSIDE_DIFF_COUNT=$(echo "$OUTSIDE_DIFF_COMMENTS" | jq '. | length')

# Calculate total
TOTAL_COUNT=$((ACTIONABLE_COUNT + NITPICK_COUNT + DUPLICATE_COUNT + OUTSIDE_DIFF_COUNT))

# Build JSON dynamically
JSON_OUTPUT="{"
JSON_OUTPUT="$JSON_OUTPUT\"summary\": {"
if [ "$ACTIONABLE_COUNT" -gt 0 ]; then
  JSON_OUTPUT="$JSON_OUTPUT\"actionable\": $ACTIONABLE_COUNT,"
fi
if [ "$NITPICK_COUNT" -gt 0 ]; then
  JSON_OUTPUT="$JSON_OUTPUT\"nitpicks\": $NITPICK_COUNT,"
fi
if [ "$DUPLICATE_COUNT" -gt 0 ]; then
  JSON_OUTPUT="$JSON_OUTPUT\"duplicates\": $DUPLICATE_COUNT,"
fi
if [ "$OUTSIDE_DIFF_COUNT" -gt 0 ]; then
  JSON_OUTPUT="$JSON_OUTPUT\"outside_diff_range\": $OUTSIDE_DIFF_COUNT,"
fi
JSON_OUTPUT="$JSON_OUTPUT\"total\": $TOTAL_COUNT"
JSON_OUTPUT="$JSON_OUTPUT}"

# Add comment arrays conditionally
COMMENT_SECTIONS=""
if [ "$ACTIONABLE_COUNT" -gt 0 ]; then
  COMMENT_SECTIONS="$COMMENT_SECTIONS,\"actionable_comments\": $ACTIONABLE_COMMENTS"
fi
if [ "$NITPICK_COUNT" -gt 0 ]; then
  COMMENT_SECTIONS="$COMMENT_SECTIONS,\"nitpick_comments\": $NITPICK_COMMENTS"
fi
if [ "$DUPLICATE_COUNT" -gt 0 ]; then
  COMMENT_SECTIONS="$COMMENT_SECTIONS,\"duplicate_comments\": $DUPLICATE_COMMENTS"
fi
if [ "$OUTSIDE_DIFF_COUNT" -gt 0 ]; then
  COMMENT_SECTIONS="$COMMENT_SECTIONS,\"outside_diff_range_comments\": $OUTSIDE_DIFF_COMMENTS"
fi

JSON_OUTPUT="$JSON_OUTPUT$COMMENT_SECTIONS}"

# Filter out LGTM and positive feedback comments from duplicates only
echo "$JSON_OUTPUT" | jq '
  if has("duplicate_comments") then
    .duplicate_comments |= map(select((.title + " " + .body) | test("LGTM|looks good|good fix|nice improvement|great work|excellent|perfect|well done|correct implementation|good approach|nice work|good portability|better approach"; "i") | not)) |
    .summary.duplicates = (.duplicate_comments | length) |
    .summary.total = ((.summary.actionable // 0) + (.summary.nitpicks // 0) + (.summary.duplicates // 0) + (.summary.outside_diff_range // 0))
  else . end
'
