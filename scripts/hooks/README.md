# AGENTS.md pre-commit hooks

Local AST-based hooks that enforce selected AGENTS.md rules. Configured under
`repo: local` in the repository `.pre-commit-config.yaml`.

## Running

```bash
# Full pre-commit suite (all hooks)
pre-commit run --all-files

# One AGENTS hook against the whole tree
pre-commit run check-no-except-exception --all-files

# Same check via the script directly (empty argv = repo walk)
.venv/bin/python scripts/hooks/check_no_except_exception.py
```

## Baselines (ratchet)

Some hooks still fail on legacy code. Those findings are grandfathered in
`scripts/hooks/baselines/<hook_id>.txt` so CI stays green while new violations
still fail.

**`<hook_id>` is the snake_case script stem**, not the kebab-case pre-commit id.
Example: script `check_no_except_exception.py` → baseline
`check_no_except_exception.txt` (not `check-no-except-exception.txt`).

Current baselines:

- `check_exceptions_location.txt`
- `check_no_except_exception.txt`
- `check_no_kubernetes_runtime.txt`
- `check_no_runtimeerror.txt`

- Format: one `relative/posix/path:<16-char-sha256-hex>` per line, where the
  fingerprint is `sha256(span)[:16]`. Each source line is normalized by
  stripping only trailing `\n`/`\r` (leading and internal whitespace are
  preserved). For a single-line finding the span is that line; for a
  multi-line AST node (`end_lineno > lineno`) the span is the normalized
  lines `lines[lineno-1:end_lineno]` joined with `\n`. Optional trailing `#`
  comments are allowed (e.g. `# L10: except Exception as e:`). Blank lines
  and full-line `#` comments are ignored.
- Matching keys on **path + content fingerprint with occurrence counts**, not
  lineno. Each baseline row grants one suppression; duplicate identical
  violations need duplicate baseline rows. Newly added identical violations
  fail when counts are exhausted. Line drift from refactors still suppresses
  the same content; editing any line in the baselined span (including
  whitespace) does not.
- **Shrink** the baseline when you fix a violation (remove that line).
- **Do not expand** baselines casually; only add lines via a deliberate process
  (e.g. issue #610 grandfathering). New path/fingerprint pairs not in the
  baseline must fail the hook.
