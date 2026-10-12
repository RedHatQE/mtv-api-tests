# Skills

Agent-harness skills vendored with this repository. Each subdirectory is a skill (a
`SKILL.md` with YAML frontmatter: `name`, `description`) that automates a workflow for
AI agents working on this repo.

## Available skills

| Skill | Purpose |
| --- | --- |
| `remote-debugging` | Attach an AI agent to a blocked remote debugger session (`MTV_REMOTE_PDB`) |

## Installation (manual — each user)

Skills are not auto-discovered from this folder. Symlink the skill you want into the project-local
skills directory (`.pi/` is gitignored, so the symlink is local-only):

```bash
# pi (project-local — recommended):
ln -s ../../skills/remote-debugging .pi/skills/remote-debugging

# pi (user-level, if you want the skill in every project):
ln -s /path/to/mtv-api-tests/skills/remote-debugging ~/.pi/agent/skills/remote-debugging
```

Other harnesses (Claude Code, Cursor, ...) have equivalent skill directories — symlink into whichever
you use, project-local preferred.

**Note:** most harnesses discover skills at session start, so a freshly created symlink
only takes effect after restarting the session.
