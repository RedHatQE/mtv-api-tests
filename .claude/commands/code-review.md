---
skipConfirmation: true
---

# Code Review Command

**MANDATORY: This command MUST use the `code-reviewer` agent via Task tool. Direct review is FORBIDDEN.**

## Usage

- `/code-review` - Review uncommitted changes (staged + unstaged)
- `/code-review main` - Review changes compared to main branch
- `/code-review feature/branch` - Review changes compared to specified branch

## Instructions

### Step 1: Get the diff

**If argument is provided (`$ARGUMENTS` is not empty):**

Compare current branch against the specified branch:

```bash
git diff $ARGUMENTS
```

**If no argument provided (`$ARGUMENTS` is empty):**

Get all uncommitted changes (staged + unstaged):

```bash
git diff HEAD
```

### Step 2: Route to code-reviewer agent (MANDATORY)

**CRITICAL: You MUST use the Task tool with `code-reviewer` agent. DO NOT review code directly yourself.**

**ABSOLUTE REQUIREMENT:** Call the Task tool with `subagent_type="code-reviewer"` to perform the review.

**Prompt for the agent:**

```text
Review the following code changes from git diff.

Analyze for:
1. Code quality and best practices
2. Potential bugs or logic errors
3. Security vulnerabilities
4. Performance issues
5. Naming conventions and readability
6. Missing error handling
7. Code duplication
8. Suggestions for improvement

Provide actionable feedback with specific file and line references.

Git diff output:
[INSERT GIT DIFF OUTPUT HERE]
```

### Step 3: Present the review

Display the code-reviewer agent's findings to the user in a clear, organized format.

Group findings by:

- Critical issues (must fix)
- Warnings (should fix)
- Suggestions (nice to have)
- Positive observations (good patterns found)
