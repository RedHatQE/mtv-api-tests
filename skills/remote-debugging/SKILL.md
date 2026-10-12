---
name: remote-debugging
description: "Attach to the mtv-api-tests remote debugger (remote-pdb) when a test run is blocked waiting for a debugger connection. Covers the FIFO-piped nc client pattern for AI agents, pdb session commands, and quit/continue semantics."
---

# Remote Debugger Client (mtv-api-tests)

Use this skill when a test run is blocked in the remote debugger — the user says a run is
"waiting for a debugger connection", mentions `MTV_REMOTE_PDB`, remote pdb, a breakpoint was
hit, or a test failed with post-mortem debugging enabled.

## How the debugger works

- Enabled per run with `MTV_REMOTE_PDB=1` (plus `-s` so the listening message is not captured,
  `-n 0` for a single worker). Without the env var everything is a no-op.
- Listener binds `127.0.0.1:4444` by default. Overrides: `REMOTE_PDB_PORT`; `REMOTE_PDB_HOST`
  must resolve to loopback (non-loopback is rejected). With pytest-xdist, worker `gwN` listens on
  `base + N` (gw1 → 4445).
- Two triggers, same attach mechanism:
  1. **Code breakpoint** — `remote_breakpoint()` (from `utilities/debug.py`) placed temporarily in
     test/utility code. Run blocks at the call site.
  2. **Failure post-mortem** — any test failure opens a session on the failing traceback after
     must-gather collection. No code changes needed.
- The test process is frozen while attached; the cluster keeps moving server-side. Inspect, do
  not idle inside migration polling loops.

## Attaching (AI agent client pattern)

The debugger speaks plain pdb over a TCP socket. Drive it with `nc` fed through a FIFO so
commands can be sent programmatically:

```bash
FIFO=.pi/tmp/remote-pdb-in
OUT=.pi/tmp/remote-pdb-out
rm -f "$FIFO" "$OUT" && mkfifo "$FIFO"

# Attach to the listener (host/port from the run; default 127.0.0.1 4444):
nohup bash -c "exec tail -f $FIFO | exec nc 127.0.0.1 4444 > $OUT 2>&1" < /dev/null > /dev/null 2>&1 &

# Send a command, wait ~1-2s, read the output:
echo "bt" > "$FIFO"; sleep 1.5; cat "$OUT"
```

For pod runs, forward the port first: `oc port-forward pod/<pod-name> 4444:4444`, then attach
to `localhost 4444`.

## Driving the session

Standard pdb commands work: `p expr`, `pp expr`, `w`/`bt` (stack), `u`/`d` (up/down frames),
`l` (list source), `a` (args), `interact` (drop to a Python REPL).

End the session:

- `c` — continue the run normally.
- `q` (or killing the nc client / dropping the connection) — quit:
  - at a **breakpoint**: the current test fails with `BdbQuit`, the run continues
  - at a **post-mortem**: the whole test session exits

## Cleanup

Track PIDs and kill the nc/tail processes by PID when done. Do NOT use broad `pkill -f`
patterns containing the FIFO path or `nc 127.0.0.1 4444` — they match your own shell's command
line and kill your own session (exit 143). `pkill -x nc` is safe. Remove the FIFO afterwards.

## Gotchas learned the hard way

- Killing the shell that spawned `tail | nc` orphans both children — the socket stays open and
  the debugger looks hung. Kill `nc` itself to simulate a real disconnect.
- An abrupt disconnect behaves like `q` (EOF → pdb `set_quit`), it does not hang the run.
- If nothing connects, confirm the run actually has `MTV_REMOTE_PDB=1` — both helpers are
  silent no-ops without it, and the run finishes normally.
