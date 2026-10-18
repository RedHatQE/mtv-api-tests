---
name: remote-debugging
description: "Debug mtv-api-tests test runs end to end. Triggers: debug, debug the code, debug a test, debugger, pdb, remote pdb, remote-pdb, breakpoint, remote_breakpoint, MTV_REMOTE_PDB, test run blocked, waiting for debugger connection, attach to debugger. Covers placing the breakpoint, watching for the hit, FIFO-piped nc attach, pdb commands, quit/continue semantics."
---

# Remote Debugger Client (mtv-api-tests)

Use this skill when the user asks to debug a test run — from placing the breakpoint to
attaching and driving the session — as well as when a run is already blocked in the remote
debugger ("waiting for a debugger connection", `MTV_REMOTE_PDB`, remote pdb, a breakpoint was
hit, or a test failed with post-mortem debugging enabled).

## Workflow (the agent drives the whole lifecycle)

1. **Place the breakpoint**: add `remote_breakpoint()` (from `utilities/debug.py`) at the line
   of interest in test/utility/conftest code. It is temporary, uncommitted code — remove it
   when the debugging session is done.
2. **Tell the user to run**: `MTV_REMOTE_PDB=1 uv run pytest <args> -s -n 0`.
3. **Watch for the hit**: poll the run's output for the line
   `RemotePdb session open at <host>:<port>, waiting for connection ...` — it means the
   breakpoint was reached and the process is blocked. Its address is the attach address
   (step 1 of "Finding the listener address" below).
4. **Attach and debug** (see "Attaching"), end with `c` (continue the run) or `q` (quit —
   at a breakpoint the current test fails, at a post-mortem the session exits).
5. **Remove the breakpoint** from the code afterwards.

For failure post-mortem there is no code change: with `MTV_REMOTE_PDB=1` set, any test failure
blocks after must-gather collection — go straight to watching for the session-open line.

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

## Finding the listener address

Resolve the listener host and port before attaching, in this order:

1. **The run's own output** (authoritative, requires `-s`): the line
   `RemotePdb session open at <host>:<port>, waiting for connection ...` — grep the run's
   output or ask the user to paste it. `<host>` is the configured `REMOTE_PDB_HOST`
   (default `127.0.0.1`), so do not match the literal `127.0.0.1` when a custom host is set.
2. **The user's configuration**: ask which `REMOTE_PDB_PORT` and `REMOTE_PDB_HOST` were set
   (default `127.0.0.1`), and which pytest-xdist worker hit the breakpoint/failure
   (worker `gwN` listens on `base + N`).
3. **Probe listening sockets**: `ss -ltn | grep -E ':44[0-9]{2}'` shows debugger listeners in
   the default range. With a custom `REMOTE_PDB_PORT`, probe that exact port instead
   (`ss -ltn | grep ":$REMOTE_PDB_PORT"`), including its xdist worker offsets (`base + N`).
   The host is the configured `REMOTE_PDB_HOST` (default `127.0.0.1`) — `ss` output shows
   the bound address.
4. Otherwise assume the defaults: host `127.0.0.1`, port `4444`.

## Attaching (AI agent client pattern)

The debugger speaks plain pdb over a TCP socket. Drive it with `nc` fed through a FIFO so
commands can be sent programmatically:

```bash
# Resolve "$host" and "$port" first (see "Finding the listener address") — the run's
# `RemotePdb session open at <host>:<port>` line is authoritative and carries both.
# Create a private per-session client directory: the atomic mkdir fails closed if the
# path already exists, so another local user cannot pre-create it to read debugger
# output or inject commands through the FIFO.
CLIENT_DIR="/tmp/remote-pdb-${port}-$$"
mkdir -m 700 "$CLIENT_DIR" || exit 1
FIFO="$CLIENT_DIR/in"
OUT="$CLIENT_DIR/out"
mkfifo -m 600 "$FIFO" && touch "$OUT"

# Attach to the listener. Record the wrapper PID — cleanup needs it:
nohup bash -c "exec tail -f $FIFO | exec nc $host $port > $OUT 2>&1" < /dev/null > /dev/null 2>&1 &
CLIENT_PID=$!

# Send a command, wait ~1-2s, read the output:
echo "bt" > "$FIFO"; sleep 1.5; cat "$OUT"
```

For pod runs, forward the port first — pass the Job's generated namespace, or `oc port-forward`
searches only your current namespace and will not find the pod. Run the forward in the
background, wait for the local listener WITHOUT opening a connection, then attach using the
client pattern above with `host=127.0.0.1` (the local forward — not the pod's session-open
host):

```bash
oc port-forward -n "$job_namespace" "pod/$pod_name" "$port:$port" &
FORWARD_PID=$!

# RemotePdb accepts a single client: never probe readiness with a connection (nc -z,
# telnet, ...) — the probe consumes the session (probe exits → EOF → pdb quits → the
# debugged run fails). Verify the listener belongs to THIS forward (pid match) and the
# forward is alive — a bare port match could be another process that already owned the
# port, sending the client to an unrelated process. Bounded:
for _ in $(seq 1 30); do
  ss -ltnp | grep ":$port " | grep -q "pid=$FORWARD_PID" && break
  kill -0 "$FORWARD_PID" 2>/dev/null || { echo "port-forward died (port already in use?)"; exit 1; }
  sleep 1
done
ss -ltnp | grep ":$port " | grep -q "pid=$FORWARD_PID" || { echo "port-forward not ready after 30s"; kill "$FORWARD_PID"; exit 1; }
```

Kill `"$FORWARD_PID"` when the session ends.

## Driving the session

Standard pdb commands work: `p expr`, `pp expr`, `w`/`bt` (stack), `u`/`d` (up/down frames),
`l` (list source), `a` (args), `interact` (drop to a Python REPL).

End the session:

- `c` — continue the run normally.
- `q` (or killing the nc client / dropping the connection) — quit:
  - at a **breakpoint**: the current test fails with `BdbQuit`, the run continues
  - at a **post-mortem**: the whole test session exits

## Cleanup

Terminate the attach pipeline by PID when the session ends — the wrapper and both its children
(`tail`, `nc`): `kill $CLIENT_PID $(pgrep -P $CLIENT_PID)`. Killing only `nc` leaves `tail`
blocked reading the FIFO, and removing the FIFO path does not stop it. Never use `pkill` in any form: broad
`-f` patterns containing the FIFO path or the `nc` command line match your own shell's command line
and kill your own session (exit 143), and even exact-name matching (`pkill -x nc`) signals every
matching process, not just this client. Remove the client directory afterwards:
`rm -rf "$CLIENT_DIR"`.

## Gotchas learned the hard way

- The client directory (FIFO and output file) lives under `/tmp`, outside any repository —
  debugger output can never be committed by accident.

- Killing the shell that spawned `tail | nc` orphans both children — the socket stays open and
  the debugger looks hung. Kill `nc` itself to simulate a real disconnect.
- An abrupt disconnect behaves like `q` (EOF → pdb `set_quit`), it does not hang the run.
- If nothing connects, confirm the run actually has `MTV_REMOTE_PDB=1` — both helpers are
  silent no-ops without it, and the run finishes normally.
