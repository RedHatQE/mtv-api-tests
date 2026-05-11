# `/qualify` — AI Qualification Workflow Diagrams

The `/qualify` workflow automates end-to-end qualification of MTV features and bug fixes:
from reading a design doc or bug report, through writing and executing tests on a real OpenShift cluster,
to producing a verified proof report and PR.
This document visualizes the workflow's phases, component relationships, and inter-agent communication.

---

## 1. High-Level Overview

Five phases from setup to proof, showing the bug verify-only shortcut and human checkpoints.

```mermaid
flowchart TD
    classDef human fill:#ff9f43,stroke:#e17055,color:#2d3436,font-weight:bold
    classDef phase fill:#a29bfe,stroke:#6c5ce7,color:#fff,font-weight:bold
    classDef success fill:#55efc4,stroke:#00b894,color:#2d3436,font-weight:bold
    classDef decision fill:#ffeaa7,stroke:#fdcb6e,color:#2d3436,font-weight:bold

    START(["▶ /qualify"]):::success
    P0["⚙️ Phase 0\nSetup"]:::phase
    P1["📋 Phase 1\nTest Plan"]:::phase
    P2["🧪 Phase 2\nWrite & Verify"]:::phase
    P3["🔍 Phase 3\nReview & PR"]:::phase
    P4["📜 Phase 4\nGenerate Proof"]:::phase
    DONE(["🏁 Complete"]):::success

    H1{{"🛑 Human\nPlan Review"}}:::human
    H2{{"🛑 Human\nPR Review"}}:::human
    MODE{{"Bug\nverify-only?"}}:::decision

    START --> P0 --> P1 --> H1 --> P2 --> MODE
    MODE -- "No" --> P3 --> H2 --> P4
    MODE -- "Yes\n(skip PR)" --> P4
    P4 --> DONE
```

---

## 2. Phase 0 — Setup

Parse arguments, validate cluster, collect versions, determine bug mode.

```mermaid
flowchart TD
    classDef agent fill:#74b9ff,stroke:#0984e3,color:#2d3436
    classDef human fill:#ff9f43,stroke:#e17055,color:#2d3436,font-weight:bold
    classDef decision fill:#ffeaa7,stroke:#fdcb6e,color:#2d3436,font-weight:bold
    classDef phase fill:#a29bfe,stroke:#6c5ce7,color:#fff,font-weight:bold

    TITLE["⚙️ PHASE 0: SETUP"]:::phase
    PARSE["Parse CLI args"]:::agent
    VALID{{"Args OK?"}}:::decision
    ASK_ARGS{{"🛑 Ask user\nfor missing args"}}:::human
    CLUSTER["Validate cluster"]:::agent
    C_OK{{"Cluster\nreachable?"}}:::decision
    C_FIX{{"🛑 Ask user\nto fix cluster"}}:::human
    VERSIONS["Collect versions\nOCP · MTV · CNV"]:::agent
    MKDIR["Create output dir"]:::agent
    IS_BUG{{"--type\n== bug?"}}:::decision
    BUG_ASK{{"🛑 Permanent\nor verify-only?"}}:::human
    DONE((" "))

    TITLE ~~~ PARSE
    PARSE --> VALID
    VALID -- "No" --> ASK_ARGS --> PARSE
    VALID -- "Yes" --> CLUSTER --> C_OK
    C_OK -- "No" --> C_FIX --> CLUSTER
    C_OK -- "Yes" --> VERSIONS --> MKDIR --> IS_BUG
    IS_BUG -- "No (feature)" --> DONE
    IS_BUG -- "Yes" --> BUG_ASK --> DONE
```

---

## 3. Phase 1 — Test Plan

Fetch source, delegate to test-planner, human review loop.

```mermaid
flowchart TD
    classDef agent fill:#74b9ff,stroke:#0984e3,color:#2d3436
    classDef human fill:#ff9f43,stroke:#e17055,color:#2d3436,font-weight:bold
    classDef decision fill:#ffeaa7,stroke:#fdcb6e,color:#2d3436,font-weight:bold
    classDef artifact fill:#dfe6e9,stroke:#b2bec3,color:#2d3436,font-style:italic
    classDef phase fill:#a29bfe,stroke:#6c5ce7,color:#fff,font-weight:bold

    TITLE["📋 PHASE 1: TEST PLAN"]:::phase
    FETCH["Fetch source\nmaterial"]:::agent
    DELEGATE["Delegate to\ntest-planner"]:::agent
    PRODUCE["Produce\ntest-plan.md"]:::agent
    SAVE[/"💾 test-plan.md"/]:::artifact
    REVIEW{{"🛑 Human\nReview plan"}}:::human
    OK{{"Approved?"}}:::decision
    FEEDBACK["Incorporate\nfeedback"]:::agent
    DONE((" "))

    TITLE ~~~ FETCH
    FETCH --> DELEGATE --> PRODUCE --> SAVE --> REVIEW --> OK
    OK -- "Feedback" --> FEEDBACK --> PRODUCE
    OK -- "Approved ✅" --> DONE
```

---

## 4. Phase 2 — Write & Verify Tests

Two paths: feature/permanent-test (creates branch) vs. bug verify-only (temp test). Both run on a real cluster with cluster-verifier validation.

```mermaid
flowchart TD
    classDef agent fill:#74b9ff,stroke:#0984e3,color:#2d3436
    classDef human fill:#ff9f43,stroke:#e17055,color:#2d3436,font-weight:bold
    classDef decision fill:#ffeaa7,stroke:#fdcb6e,color:#2d3436,font-weight:bold
    classDef artifact fill:#dfe6e9,stroke:#b2bec3,color:#2d3436,font-style:italic
    classDef phase fill:#a29bfe,stroke:#6c5ce7,color:#fff,font-weight:bold
    classDef success fill:#55efc4,stroke:#00b894,color:#2d3436,font-weight:bold

    TITLE["🧪 PHASE 2: WRITE & VERIFY"]:::phase
    MODE{{"Test mode?"}}:::decision

    TITLE ~~~ MODE

    %% Feature / permanent path
    BRANCH["Create branch\nqualify/‹name›"]:::agent
    WRITE["python-expert\nwrites tests"]:::agent
    RUN["Run pytest\non cluster"]:::agent
    LOG1[/"💾 test-output.log"/]:::artifact
    CV1["cluster-verifier\nchecks state"]:::agent
    PASS1{{"Passed?"}}:::decision
    RETRY1{{"Attempt\n≤ 3?"}}:::decision
    FIX1["Fix tests"]:::agent
    STUCK1{{"🛑 Ask user\nfor guidance"}}:::human
    DONE1(["→ Phase 3"]):::success

    MODE -- "Feature /\npermanent" --> BRANCH --> WRITE --> RUN --> LOG1 --> CV1 --> PASS1
    PASS1 -- "Yes ✅" --> DONE1
    PASS1 -- "No ❌" --> RETRY1
    RETRY1 -- "Yes" --> FIX1 --> RUN
    RETRY1 -- "No" --> STUCK1 --> ESCALATE1["⛔ Escalate / stop autonomous run"]:::human

    %% Verify-only path
    WTEMP["Write temp test\nin /tmp/"]:::agent
    RTEMP["Run temp test\non cluster"]:::agent
    LOG2[/"💾 test-output.log"/]:::artifact
    CV2["cluster-verifier\nchecks state"]:::agent
    PASS2{{"Passed?"}}:::decision
    RETRY2{{"Attempt\n≤ 3?"}}:::decision
    FIX2["Fix temp test"]:::agent
    STUCK2{{"🛑 Ask user\nfor guidance"}}:::human
    DONE2(["→ Phase 4\n(skip PR)"]):::success

    MODE -- "Verify-only" --> WTEMP --> RTEMP --> LOG2 --> CV2 --> PASS2
    PASS2 -- "Yes ✅" --> DONE2
    PASS2 -- "No ❌" --> RETRY2
    RETRY2 -- "Yes" --> FIX2 --> RTEMP
    RETRY2 -- "No" --> STUCK2 --> ESCALATE2["⛔ Escalate / stop autonomous run"]:::human
```

---

## 5. Phase 3 — Code Review & PR

Three parallel reviewers, fix loop, pre-commit, then PR creation.

```mermaid
flowchart TD
    classDef agent fill:#74b9ff,stroke:#0984e3,color:#2d3436
    classDef human fill:#ff9f43,stroke:#e17055,color:#2d3436,font-weight:bold
    classDef decision fill:#ffeaa7,stroke:#fdcb6e,color:#2d3436,font-weight:bold
    classDef artifact fill:#dfe6e9,stroke:#b2bec3,color:#2d3436,font-style:italic
    classDef phase fill:#a29bfe,stroke:#6c5ce7,color:#fff,font-weight:bold

    TITLE["🔍 PHASE 3: REVIEW & PR"]:::phase
    REVIEW["3 parallel reviewers\nquality · guidelines\n· security"]:::agent
    ISSUES{{"Issues\nfound?"}}:::decision
    FIX["Fix findings"]:::agent
    PRECOMMIT["pre-commit\nrun --all-files"]:::agent
    PC_OK{{"Passed?"}}:::decision
    FIX_PC["Fix lint/format"]:::agent
    PR["github-expert\ncreates PR"]:::agent
    PR_ART[/"💾 GitHub PR"/]:::artifact
    HUMAN{{"🛑 Human\nPR review"}}:::human
    DONE((" "))

    TITLE ~~~ REVIEW
    REVIEW --> ISSUES
    ISSUES -- "Yes" --> FIX --> REVIEW
    ISSUES -- "No ✅" --> PRECOMMIT --> PC_OK
    PC_OK -- "No" --> FIX_PC --> PRECOMMIT
    PC_OK -- "Yes ✅" --> PR --> PR_ART --> HUMAN --> DONE
```

---

## 6. Phase 4 — Generate Proof

Assemble proof report, determine verdict, print summary.

```mermaid
flowchart TD
    classDef agent fill:#74b9ff,stroke:#0984e3,color:#2d3436
    classDef decision fill:#ffeaa7,stroke:#fdcb6e,color:#2d3436,font-weight:bold
    classDef artifact fill:#dfe6e9,stroke:#b2bec3,color:#2d3436,font-style:italic
    classDef phase fill:#a29bfe,stroke:#6c5ce7,color:#fff,font-weight:bold
    classDef success fill:#55efc4,stroke:#00b894,color:#2d3436,font-weight:bold
    classDef fail fill:#ff7675,stroke:#d63031,color:#fff,font-weight:bold

    TITLE["📜 PHASE 4: PROOF"]:::phase
    INVOKE["Invoke\nproof-generator"]:::agent
    ASSEMBLE["Assemble\nproof.md"]:::agent
    PROOF[/"💾 proof.md"/]:::artifact
    VERDICT{{"Verdict?"}}:::decision
    QUAL["✅ QUALIFIED"]:::success
    NOTQUAL["❌ NOT QUALIFIED"]:::fail
    FIXED["🐛 BUG FIXED"]:::success
    NOTFIXED["🐛 NOT FIXED"]:::fail
    SUMMARY["Print summary"]:::agent

    TITLE ~~~ INVOKE
    INVOKE --> ASSEMBLE --> PROOF --> VERDICT
    VERDICT -- "Feature pass" --> QUAL --> SUMMARY
    VERDICT -- "Feature fail" --> NOTQUAL --> SUMMARY
    VERDICT -- "Bug pass" --> FIXED --> SUMMARY
    VERDICT -- "Bug fail" --> NOTFIXED --> SUMMARY
```

---

## 7. Component Relationship Diagram

How the orchestrator, agents, skills, templates, and artifacts relate to each other.

```mermaid
flowchart LR
    classDef prompt fill:#a29bfe,stroke:#6c5ce7,color:#fff,font-weight:bold
    classDef agent fill:#74b9ff,stroke:#0984e3,color:#2d3436,font-weight:bold
    classDef skill fill:#55efc4,stroke:#00b894,color:#2d3436,font-weight:bold
    classDef template fill:#ffeaa7,stroke:#fdcb6e,color:#2d3436
    classDef artifact fill:#dfe6e9,stroke:#b2bec3,color:#2d3436,font-style:italic
    classDef external fill:#fab1a0,stroke:#e17055,color:#2d3436

    subgraph ORCH["Orchestrator"]
        Q["qualify.md"]:::prompt
    end

    subgraph AGENTS["Qualify Agents"]
        TP["test-planner"]:::agent
        CV["cluster-verifier"]:::agent
    end

    subgraph EXT["External Agents"]
        PE["python-expert"]:::external
        CR["code-reviewers ×3"]:::external
        GE["github-expert"]:::external
    end

    subgraph SKILL["Qualify Skills"]
        PG["proof-generator"]:::skill
    end

    subgraph TPL["Templates"]
        T1["test-plan-\ntemplate.md"]:::template
        T2["proof-\ntemplate.md"]:::template
    end

    subgraph OUT["Output Artifacts"]
        O1[/"test-plan.md"/]:::artifact
        O2[/"test-output.log"/]:::artifact
        O3[/"proof.md"/]:::artifact
    end

    Q -- "Phase 1" --> TP
    Q -- "Phase 2" --> PE
    Q -- "Phase 2" --> CV
    Q -- "Phase 3" --> CR
    Q -- "Phase 3" --> GE
    Q -- "Phase 4" --> PG

    TP --> T1
    PG --> T2

    TP --> O1
    PE --> O2
    PG --> O3

    O1 --> PE
    O2 --> CV
    O2 --> PG
```

---

## 8. Sequence Diagrams

Happy-path interaction split into two diagrams for readability.

### 8a. Setup, Plan & Write (Phases 0–2)

```mermaid
sequenceDiagram
    actor User
    participant Orch as Orchestrator
    participant TP as test-planner
    participant PE as python-expert
    participant CV as cluster-verifier

    Note over User,CV: Phase 0 — Setup
    User ->> Orch: /qualify --type --source
    Orch ->> Orch: Validate cluster + collect versions

    Note over User,CV: Phase 1 — Test Plan
    Orch ->> TP: Produce test plan
    TP -->> Orch: test-plan.md
    Orch -->> User: 🛑 Review plan
    User -->> Orch: ✅ Approved

    Note over User,CV: Phase 2 — Write & Verify
    Orch ->> PE: Write tests
    PE -->> Orch: Tests ready
    Orch ->> Orch: Run pytest on cluster
    Orch ->> CV: Verify cluster state
    CV -->> Orch: Verification ✅
    Note right of Orch: Retry up to 3× on failure
```

### 8b. Review, PR & Proof (Phases 3–4)

```mermaid
sequenceDiagram
    actor User
    participant Orch as Orchestrator
    participant CR as code-reviewers
    participant GE as github-expert
    participant PG as proof-generator

    Note over User,PG: Phase 3 — Review & PR
    Orch ->> CR: 3 parallel reviews
    CR -->> Orch: Findings
    Note right of Orch: Fix & re-review until clean
    Orch ->> Orch: pre-commit
    Orch ->> GE: Create PR
    GE -->> Orch: PR URL
    Orch -->> User: 🛑 PR ready

    Note over User,PG: Phase 4 — Proof
    Orch ->> PG: Assemble proof
    PG -->> Orch: proof.md + verdict
    Orch -->> User: 🏁 Complete
```

---

## Legend

| Shape | Meaning |
| ------- | --------- |
| 🟪 Purple rounded | Phase header |
| 🟦 Blue rectangle | Agent / automated action |
| 🟧 Orange hexagon | 🛑 Human checkpoint — requires user input |
| 🟨 Yellow diamond | Decision point |
| ⬜ Gray parallelogram | Output artifact (file) |
| 🟩 Green rounded | Start / success outcome |
| 🟥 Red rounded | Failure outcome |
| 🔴 Coral | External agent (not in qualify/) |

## Key Takeaways

1. **Five distinct phases** with clear handoff boundaries.
2. **Human stays in the loop** at test-plan review, bug-mode decision, stuck escalation, and PR review — everything else is autonomous.
3. **Dual verification** — pytest execution alone is never sufficient; the `cluster-verifier` agent independently confirms cluster state.
4. **Bug workflows fork early** (Phase 0) into permanent-test vs. verify-only, rejoining at proof generation (Phase 4).
5. **Three parallel code reviewers** in Phase 3 ensure quality, guideline compliance, and security before any PR is created.
6. **Self-contained proof** — `proof.md` captures test results, cluster evidence, version info, and raw YAML so the qualification can be audited without re-running anything.
