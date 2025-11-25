---
skipConfirmation: true
---

# GitHub CodeRabbit AI Review Handler

**Description:** Finds and processes CodeRabbit AI comments from the current branch's GitHub PR with
priority-based handling.

---

## 🚨 CRITICAL: SESSION ISOLATION & FLOW ENFORCEMENT

**THIS PROMPT DEFINES A STRICT, SELF-CONTAINED WORKFLOW THAT MUST BE FOLLOWED EXACTLY:**

1. **IGNORE ALL PREVIOUS CONTEXT**: Previous conversations, tasks, or commands in this session are IRRELEVANT
2. **START FRESH**: This prompt creates a NEW workflow that starts from Step 1 and follows the exact sequence
   below
3. **NO ASSUMPTIONS**: Do NOT assume any steps have been completed - follow the workflow from the beginning
4. **MANDATORY CHECKPOINTS**: Each phase MUST complete fully before proceeding to the next phase
5. **REQUIRED CONFIRMATIONS**: All user confirmations (commit, push) MUST be asked - NEVER skip them

**If this prompt is called multiple times in a session, treat EACH invocation as a completely independent
workflow.**

---

## 🔧 CRITICAL: Task Tool & Agent Availability Check

**BEFORE proceeding with the workflow, check if Task tool and agents are available:**

1. **Check Task Tool Availability**: Determine if you have access to the `Task` tool (or `todo_write` tool)
2. **Check Agent Availability**: Determine if you have access to specialized agents (python-expert, git-expert,
   test-automator, etc.)

**If Task tool AND agents are available:**

- ✅ Use the standard workflow with Task tool and agent routing as described below
- ✅ Create tasks using `todo_write` or `Task` tool
- ✅ Route work to appropriate agents (python-expert, git-expert, test-automator, etc.)

**If Task tool OR agents are NOT available:**

- ⚠️ **FALLBACK MODE**: Handle all tasks directly yourself
- ⚠️ Do NOT attempt to use Task tool or route to agents
- ⚠️ Implement code changes directly using available tools (read_file, search_replace, write, etc.)
- ⚠️ Run tests directly using available tools (run_terminal_cmd, etc.)
- ⚠️ Handle git operations directly if git-expert agent is not available (but prefer git-expert if available)

**Fallback Instructions for Direct Implementation:**

When Task tool/agents are unavailable, replace agent routing with direct implementation:

- **Instead of**: "Create TodoWrite task with appropriate agent assignment"
  - **Do**: Track the task mentally or in a simple list, then implement directly

- **Instead of**: "Route to appropriate specialists using Task tool"
  - **Do**: Implement the changes directly using read_file, search_replace, write tools

- **Instead of**: "Use Task tool to run all tests WITH coverage"
  - **Do**: Run tests directly using run_terminal_cmd (e.g., `uv run pytest --cov`)

- **Instead of**: "Use Task tool to commit changes"
  - **Do**: Use git-expert agent if available, otherwise handle git operations directly (but prefer git-expert)

- **Instead of**: "Use Task tool to push changes"
  - **Do**: Use git-expert agent if available, otherwise handle git operations directly (but prefer git-expert)

**The workflow phases remain the same, but execution method adapts based on available tools.**

---

## Instructions

### Step 1: Get CodeRabbit comments using the extraction script

SCRIPT_PATHS = .claude/commands/scripts/github-coderabbitai-review-handler/get-coderabbit-comments.sh .claude/commands/scripts/general/get-pr-info.sh

### 🎯 CRITICAL: Simple Command - DO NOT OVERCOMPLICATE

**ALWAYS use this exact command format:**

```bash
{{SCRIPT_PATHS}} <USER_INPUT_IF_PROVIDED>
```

**That's it. Nothing more. No script extraction. No variable assignments. Just one simple command.**

---

**If user provides input (review ID, URL, or commit SHA):**

```bash
# User provided: 3379917343
{{SCRIPT_PATHS}} 3379917343

# User provided: https://github.com/owner/repo/pull/123#pullrequestreview-3379917343
{{SCRIPT_PATHS}} "https://github.com/owner/repo/pull/123#pullrequestreview-3379917343"

# User provided: 6c544434d69b2ef76441949cfe839167b7de775a
{{SCRIPT_PATHS}} 6c544434d69b2ef76441949cfe839167b7de775a
```

**If user provides NO input:**

```bash
# Gets latest commit comments automatically
{{SCRIPT_PATHS}}
```

**THAT'S ALL. DO NOT extract scripts, get PR info, or do ANY bash manipulation. The scripts handle
EVERYTHING.**

### Step 2: Process the JSON output

The script returns structured JSON containing:

- `summary`: Counts of actionable, nitpicks, duplicates, outside_diff_range (if any), and total
- `actionable_comments`: Array of HIGH priority issues with AI instructions (body contains direct AI prompts)
  - Each has: priority, title, file, body (body = AI instruction to execute)
- `nitpick_comments`: Array of LOW priority style/maintainability issues with clean descriptions
  - Each has: priority, title, file, line, body
- `duplicate_comments`: Array of MEDIUM priority duplicates (only present if any exist)
  - Each has: priority, title, file, line, body
- `outside_diff_range_comments`: Array of LOW priority comments on code outside the diff (only present if any exist)
  - Each has: priority, title, file, line, body

### Step 2.5: Filter Positive Comments from Duplicates

🎯 **CRITICAL: Before presenting MEDIUM priority comments to user, classify each duplicate comment to filter
out positive feedback.**

For each comment in `duplicate_comments` (if any exist), analyze the title and body to determine:

**POSITIVE (Filter Out) - Comments that are:**

- Praise/acknowledgment: Contains words like "good", "great", "nice", "excellent", "perfect", "well done",
  "correct"
- Positive feedback on fixes: "good fix", "nice improvement", "better approach", "correct implementation"
- Acknowledgment without suggestions: No action words like "should", "consider", "recommend", "suggest", "try"

**ACTIONABLE (Keep) - Comments that:**

- Contain suggestions: "should", "consider", "recommend", "suggest", "could", "might want to"
- Point out issues: "issue", "problem", "concern", "potential", "risk"
- Request changes: "change", "update", "modify", "improve", "refactor"

**Examples:**

- ✅ POSITIVE (filter out): "Windows-safe resource import guard: good portability fix"
- ✅ POSITIVE (filter out): "Nice error handling improvement"
- ❌ ACTIONABLE (keep): "Consider adding error handling here"
- ❌ ACTIONABLE (keep): "This could cause performance issues"

After classification, remove all POSITIVE comments from the `duplicate_comments` array before proceeding to
Step 3.

### Step 3: PHASE 1 - Collect User Decisions (COLLECTION ONLY - NO PROCESSING)

**🚨 CRITICAL: This is the COLLECTION phase. Do NOT execute, implement, or process ANY comments yet. Only ask
questions and create tasks.**

Go through ALL comments in priority order, collecting user decisions:

1. **HIGH Priority (Actionable)** first
2. **MEDIUM Priority (Duplicates)** - if any exist
3. **LOW Priority (Nitpicks and Outside Diff)** last

**IMPORTANT: Present each comment individually, WAIT for user response, but NEVER execute, implement, or
process anything during this phase.**

For ALL comment types, use this unified format:

```text
🔴 [PRIORITY] Priority - Comment X of Y
📁 File: [file path]
📍 Line: [line] (if available)
📋 Title: [title]
💬 Description: [body]

Do you want to address this comment? (yes/no/skip/all)
```

**For each "yes" response:**

- **If Task tool available**: Create a TodoWrite task with appropriate agent assignment
- **If Task tool NOT available**: Track the task mentally/in a list for direct implementation later
- Show confirmation: "✅ Task created: [brief description]" (or "✅ Task tracked: [brief description]" if no
  Task tool)
- **DO NOT execute the task - Continue to next comment immediately**

**For "all" response:**

- **If Task tool available**: Create TodoWrite tasks for the current comment AND **ALL remaining comments
  across ALL priority levels** automatically
- **If Task tool NOT available**: Track all remaining tasks mentally/in a list for direct implementation
- **CRITICAL**: "all" means process EVERY remaining comment (HIGH, MEDIUM, and LOW priority) - do NOT skip any
  priority level
- Show summary: "✅ Created/tracked tasks for current comment + X remaining comments (Y HIGH, Z MEDIUM, W LOW)"
- **Skip to Phase 2 immediately**

**For "no" or "skip" responses:**

- Show: "⏭️ Skipped"
- Continue to next comment immediately

**🚨 REMINDER: Do NOT execute, implement, fix, or process anything during this phase. Only collect decisions
and create tasks.**

### Step 4: PHASE 2 - Process All Approved Tasks (EXECUTION PHASE)

**🚨 IMPORTANT: Only start this phase AFTER all comments have been presented and decisions collected.**

After ALL comments have been reviewed in Phase 1:

1. **Show approved tasks and proceed directly:**

```text
📋 Processing X approved tasks:
1. [Task description]
2. [Task description]
...
```

Proceed directly to execution (no confirmation needed since user already approved each task in Phase 1)

1. **Process all approved tasks:**
   - **🚨 CRITICAL**: Process ALL tasks created/tracked during Phase 1, regardless of priority level
   - **NEVER skip LOW priority tasks** - if a task was created/tracked in Phase 1, it MUST be executed in
     Phase 2
   - **HIGH Priority (Actionable)**: Execute AI instructions directly using body as prompt
   - **MEDIUM Priority (Duplicates)**:
     - **If Task tool/agents available**: Route to appropriate specialists using Task tool
     - **If Task tool/agents NOT available**: Implement changes directly using read_file, search_replace,
       write tools
   - **LOW Priority (Nitpicks/Outside Diff)**:
     - **If Task tool/agents available**: Route to appropriate specialists using Task tool
     - **If Task tool/agents NOT available**: Implement changes directly using read_file, search_replace,
       write tools
   - Process multiple tasks in parallel when possible
   - Mark each task as completed after finishing
   - **Track unimplemented changes**: If AI decides NOT to make changes for an approved task, track the reason

1. **Review unimplemented changes (MANDATORY CHECKPOINT):**

   **🚨 CRITICAL: Before proceeding to testing, MUST review any approved comments where AI decided not to make
   changes.**

   If AI decided NOT to implement changes for ANY approved tasks (tasks where user said "yes" but AI determined
   no changes needed):
   - **Show summary of unimplemented changes:**

     ```text
     📋 Unimplemented Changes Review (X approved comments not changed):

     1. [PRIORITY] Priority - File: [file] - Line: [line]
        Title: [title]
        Reason AI did not implement: [Explain why no changes were made - e.g., "Current code already
        implements the suggestion", "Comment is not applicable", "Suggested change would break existing
        functionality"]

     2. [PRIORITY] Priority - File: [file] - Line: [line]
        Title: [title]
        Reason AI did not implement: [Explain why no changes were made]
     ...
     ```

   - **MANDATORY**: Ask user for confirmation:

     ```text
     Do you approve proceeding without these changes? (yes/no)
     - yes: Proceed to Phase 4 (testing and commit)
     - no: Reconsider and implement the changes
     ```

   - **If user says "no"**: Re-implement the changes as requested
   - **If user says "yes"**: Proceed to Phase 4 (testing and commit)

   **If ALL approved tasks were implemented**: Proceed directly to Phase 4

   **CHECKPOINT**: User has reviewed and approved all unimplemented changes OR all approved tasks were implemented

1. **Post-execution workflow (PHASES 4 & 5 - MANDATORY CHECKPOINTS):**

   **PHASE 4: Testing & Commit**
   - **STEP 1** (REQUIRED):
     - **If Task tool/agents available**: Use Task tool to run all tests WITH coverage
     - **If Task tool/agents NOT available**: Run tests directly using run_terminal_cmd (e.g., `uv run pytest --cov`)
   - **STEP 2** (REQUIRED): Check BOTH test results AND coverage results:
     - **If tests pass AND coverage passes**: MUST ask: "All tests and coverage pass. Do you want to commit
       the changes? (yes/no)"
       - If user says "yes":
         - **If git-expert agent available**: Use git-expert agent to commit changes
         - **If git-expert agent NOT available**: Handle git commit directly (but prefer git-expert if
           available)
       - If user says "no": Acknowledge and proceed to Phase 5 checkpoint (ask about push anyway)
     - **If tests pass BUT coverage fails**: This is a FAILURE - do NOT ask about commit yet
       - **If Task tool/agents available**: Use Task tool to analyze coverage gaps and add missing tests
       - **If Task tool/agents NOT available**: Analyze coverage gaps and add missing tests directly
       - Re-run tests with coverage until BOTH pass
     - **If tests fail**:
       - **If Task tool/agents available**: Use Task tool to analyze and fix test failures
       - **If Task tool/agents NOT available**: Analyze and fix test failures directly
       - Re-run until tests pass
   - **CHECKPOINT**: Tests AND coverage BOTH pass, AND commit confirmation asked (even if user declined)

   **PHASE 5: Push to Remote**
   - **STEP 1** (REQUIRED): After successful commit (or commit decline), MUST ask: "Changes committed
     successfully. Do you want to push the changes to remote? (yes/no)"
     - If no commit was made, ask: "Do you want to push any existing commits to remote? (yes/no)"
   - **STEP 2** (REQUIRED): If user says "yes":
     - **If git-expert agent available**: Use git-expert agent to push changes to remote
     - **If git-expert agent NOT available**: Handle git push directly (but prefer git-expert if available)
   - **CHECKPOINT**: Push confirmation MUST be asked - this is the final step of the workflow

**🚨 CRITICAL WORKFLOW - STRICT PHASE SEQUENCE:**

This workflow has **5 MANDATORY PHASES** that MUST be executed in order. Each phase has **REQUIRED CHECKPOINTS**
that CANNOT be skipped:

### PHASE 1: Collection Phase

- ONLY collect decisions (yes/no/skip/all) and create tasks - NO execution
- **CHECKPOINT**: ALL comments have been presented and user decisions collected

### PHASE 2: Execution Phase

- ONLY execute tasks after ALL comments reviewed - NO more questions
- Process ALL approved tasks (HIGH, MEDIUM, LOW priority)
- Track any tasks where AI decides not to make changes (with reasoning)
- **CHECKPOINT**: ALL approved tasks have been processed (implemented or reasoned skip)

### PHASE 3: Unimplemented Changes Review

- **MANDATORY STEP 1**: Show summary of any approved tasks where AI decided not to make changes
- **MANDATORY STEP 2**: Explain WHY changes were not made for each
- **MANDATORY STEP 3**: Ask user: "Do you approve proceeding without these changes? (yes/no)"
- **MANDATORY STEP 4**: If user says no, re-implement the changes
- **CHECKPOINT**: User has approved all unimplemented changes OR all tasks were implemented

### PHASE 4: Testing & Commit Phase

- **MANDATORY STEP 1**:
  - **If Task tool/agents available**: Run tests WITH coverage via Task tool
  - **If Task tool/agents NOT available**: Run tests directly using run_terminal_cmd (e.g., `uv run pytest --cov`)
- **MANDATORY STEP 2**: Check BOTH tests AND coverage - only proceed if BOTH pass
  - If tests pass BUT coverage fails - FIX coverage gaps (this is a FAILURE)
  - If tests fail - FIX test failures
- **MANDATORY STEP 3**: Once BOTH pass, MUST ask user: "All tests and coverage pass. Do you want to commit the
  changes? (yes/no)"
- **MANDATORY STEP 4**: If user says yes:
  - **If git-expert agent available**: Use git-expert agent to commit changes
  - **If git-expert agent NOT available**: Handle git commit directly (but prefer git-expert if available)
- **CHECKPOINT**: Tests AND coverage BOTH pass, AND commit confirmation asked (even if user declined)

### PHASE 5: Push Phase

- **MANDATORY STEP 1**: After successful commit, MUST ask user: "Changes committed successfully. Do you want to
  push the changes to remote? (yes/no)"
- **MANDATORY STEP 2**: If user says yes:
  - **If git-expert agent available**: Use git-expert agent to push changes
  - **If git-expert agent NOT available**: Handle git push directly (but prefer git-expert if available)
- **CHECKPOINT**: Push confirmation asked (even if user declined)

**🚨 ENFORCEMENT RULES:**

- **NEVER skip phases** - all 5 phases are mandatory
- **NEVER skip checkpoints** - each phase must reach its checkpoint before proceeding
- **NEVER skip confirmations** - commit and push confirmations are REQUIRED even if previously discussed
- **NEVER assume** - always ask for confirmation, never assume user wants to commit/push
- **COMPLETE each phase fully** before starting the next phase

**If tests OR coverage fail**:

- **If Task tool/agents available**: Use Task tool to analyze and fix failures (add tests for coverage gaps)
- **If Task tool/agents NOT available**: Analyze and fix failures directly (add tests for coverage gaps)
- Re-run tests with coverage until BOTH pass before proceeding to Phase 3's commit confirmation.
