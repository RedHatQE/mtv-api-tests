---
skipConfirmation: true
---

# GitHub Review Handler

**Description:** Finds and processes human reviewer comments from the current branch's GitHub PR.

SCRIPT_PATHS = .claude/commands/scripts/github-review-handler/get-human-reviews.sh
.claude/commands/scripts/general/get-pr-info.sh

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

- **Instead of**: "Use Task tool to run all tests"
  - **Do**: Run tests directly using run_terminal_cmd (e.g., `uv run pytest`)

- **Instead of**: "Use Task tool to commit changes"
  - **Do**: Use git-expert agent if available, otherwise handle git operations directly (but prefer git-expert)

- **Instead of**: "Use Task tool to push changes"
  - **Do**: Use git-expert agent if available, otherwise handle git operations directly (but prefer git-expert)

**The workflow phases remain the same, but execution method adapts based on available tools.**

---

## Instructions

### Step 1: Get human review comments using the extraction script

```bash
# Call the human review extraction script with PR info script path
{{SCRIPT_PATHS}}
```

### Step 2: Process the JSON output

The script returns structured JSON containing:

- `summary`: Total count of human review comments
- `comments`: Array of review comments from human reviewers
  - Each has: reviewer, file, line, body

### Step 3: PHASE 1 - Collect User Decisions (COLLECTION ONLY - NO PROCESSING)

**🚨 CRITICAL: This is the COLLECTION phase. Do NOT execute, implement, or process ANY comments yet. Only ask
questions and create tasks.**

Go through ALL comments sequentially, collecting user decisions:

**IMPORTANT: Present each comment individually, WAIT for user response, but NEVER execute, implement, or
process anything during this phase.**

For each comment, present:

```text
👤 Human Review - Comment X of Y
👨‍💻 Reviewer: [reviewer name]
📁 File: [file path]
📍 Line: [line]
💬 Comment: [body]

Do you want to address this comment? (yes/no/skip/all)
```

**For each "yes" response:**

- **If Task tool available**: Create a TodoWrite task with appropriate agent assignment
- **If Task tool NOT available**: Track the task mentally/in a list for direct implementation later
- Show confirmation: "✅ Task created: [brief description]" (or "✅ Task tracked: [brief description]" if no
  Task tool)
- **DO NOT execute the task - Continue to next comment immediately**

**For "all" response:**

- **If Task tool available**: Create TodoWrite tasks for the current comment AND **ALL remaining comments**
  automatically
- **If Task tool NOT available**: Track all remaining tasks mentally/in a list for direct implementation
- **CRITICAL**: "all" means process EVERY remaining comment - do NOT skip any comments
- Show summary: "✅ Created/tracked tasks for current comment + X remaining comments"
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
   - **🚨 CRITICAL**: Process ALL tasks created/tracked during Phase 1
   - **NEVER skip tasks** - if a task was created/tracked in Phase 1, it MUST be executed in Phase 2
   - **If Task tool/agents available**: Route to appropriate specialists using Task tool based on comment
     content
   - **If Task tool/agents NOT available**: Implement changes directly using read_file, search_replace, write
     tools based on comment content
   - Process multiple tasks in parallel when possible
   - Mark each task as completed after finishing

1. **Post-execution workflow (PHASES 3 & 4 - MANDATORY CHECKPOINTS):**

   **PHASE 3: Testing & Commit**
   - **STEP 1** (REQUIRED):
     - **If Task tool/agents available**: Use Task tool to run all tests
     - **If Task tool/agents NOT available**: Run tests directly using run_terminal_cmd (e.g., `uv run pytest`)
   - **STEP 2** (REQUIRED): If tests pass, MUST ask: "All tests pass. Do you want to commit the changes?
     (yes/no)"
     - If user says "yes":
       - **If git-expert agent available**: Use git-expert agent to commit changes with descriptive message
       - **If git-expert agent NOT available**: Handle git commit directly (but prefer git-expert if available)
     - If user says "no": Acknowledge and proceed to Phase 4 checkpoint (ask about push anyway)
   - **STEP 3** (REQUIRED): If tests fail:
     - **If Task tool/agents available**: Use Task tool to analyze and fix failures
     - **If Task tool/agents NOT available**: Analyze and fix failures directly
     - Re-run tests until they pass
   - **CHECKPOINT**: Must reach this point before Phase 4 - commit confirmation MUST be asked

   **PHASE 4: Push to Remote**
   - **STEP 1** (REQUIRED): After successful commit (or commit decline), MUST ask: "Changes committed
     successfully. Do you want to push the changes to remote? (yes/no)"
     - If no commit was made, ask: "Do you want to push any existing commits to remote? (yes/no)"
   - **STEP 2** (REQUIRED): If user says "yes":
     - **If git-expert agent available**: Use git-expert agent to push changes to remote
     - **If git-expert agent NOT available**: Handle git push directly (but prefer git-expert if available)
   - **CHECKPOINT**: Push confirmation MUST be asked - this is the final step of the workflow

**🚨 CRITICAL WORKFLOW - STRICT PHASE SEQUENCE:**

This workflow has **4 MANDATORY PHASES** that MUST be executed in order. Each phase has **REQUIRED CHECKPOINTS**
that CANNOT be skipped:

### PHASE 1: Collection Phase

- ONLY collect decisions (yes/no/skip/all) and create tasks - NO execution
- **CHECKPOINT**: ALL comments have been presented and user decisions collected

### PHASE 2: Execution Phase

- ONLY execute tasks after ALL comments reviewed - NO more questions
- Process ALL approved tasks
- **CHECKPOINT**: ALL approved tasks have been completed

### PHASE 3: Testing & Commit Phase

- **MANDATORY STEP 1**:
  - **If Task tool/agents available**: Run tests via Task tool
  - **If Task tool/agents NOT available**: Run tests directly using run_terminal_cmd (e.g., `uv run pytest`)
- **MANDATORY STEP 2**: If tests pass, MUST ask user: "All tests pass. Do you want to commit the changes?
  (yes/no)"
- **MANDATORY STEP 3**: If user says yes:
  - **If git-expert agent available**: Use git-expert agent to commit changes
  - **If git-expert agent NOT available**: Handle git commit directly (but prefer git-expert if available)
- **CHECKPOINT**: Tests completed AND commit confirmation asked (even if user declined)

### PHASE 4: Push Phase

- **MANDATORY STEP 1**: After successful commit, MUST ask user: "Changes committed successfully. Do you want to
  push the changes to remote? (yes/no)"
- **MANDATORY STEP 2**: If user says yes:
  - **If git-expert agent available**: Use git-expert agent to push changes
  - **If git-expert agent NOT available**: Handle git push directly (but prefer git-expert if available)
- **CHECKPOINT**: Push confirmation asked (even if user declined)

**🚨 ENFORCEMENT RULES:**

- **NEVER skip phases** - all 4 phases are mandatory
- **NEVER skip checkpoints** - each phase must reach its checkpoint before proceeding
- **NEVER skip confirmations** - commit and push confirmations are REQUIRED even if previously discussed
- **NEVER assume** - always ask for confirmation, never assume user wants to commit/push
- **COMPLETE each phase fully** before starting the next phase

**If tests fail**:

- **If Task tool/agents available**: Use Task tool to analyze and fix failures
- **If Task tool/agents NOT available**: Analyze and fix failures directly
- Re-run tests until they pass before proceeding to Phase 3's commit confirmation.

Note: Human review comments are treated equally (no priority system like CodeRabbit).
