"""Under stdio transport, stdout is the JSON-RPC channel: every line the
server writes there must be a protocol message.

`import estnltk` used to add a TqdmLoggingHandler to the ROOT logger, and
tqdm.write() prints to sys.stdout. Any INFO record from any library went
into the protocol channel; gensim logs three while loading the fastText
model. Python buffers sys.stdout apart from the stream the MCP SDK writes
to, so a flush could put half a log line in front of a response. The
client skipped the line as non-JSON and waited for a reply that had
already been sent: check_compound_familiarity hung until the client's
30-minute idle timeout.

This test runs the real server as a subprocess over stdio, calls a tool
that imports EstNLTK and one that loads fastText, waits for both answers,
then closes stdin so the server exits and flushes everything it
buffered, and checks every line it wrote to stdout. The server runs
unbuffered so a stray write cannot hide in a buffer.

Run via:

    uv run python tests/test_stdio_channel.py
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

import mcp.types

ROOT = Path(__file__).resolve().parent.parent

failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  PASS {label}")
    else:
        failures.append(f"{label}: {detail}")
        print(f"  FAIL {label} {detail}")


def message(msg_id: int | None, method: str, params: dict) -> str:
    body: dict = {"jsonrpc": "2.0", "method": method, "params": params}
    if msg_id is not None:
        body["id"] = msg_id
    return json.dumps(body, ensure_ascii=False) + "\n"


requests = "".join([
    message(1, "initialize", {
        "protocolVersion": mcp.types.LATEST_PROTOCOL_VERSION,
        "capabilities": {},
        "clientInfo": {"name": "test_stdio_channel", "version": "0"},
    }),
    message(None, "notifications/initialized", {}),
    message(2, "tools/call", {"name": "spell_check", "arguments": {"text": "Tere hommikust"}}),
    message(3, "tools/call", {"name": "check_compound_familiarity",
                              "arguments": {"text": "kõrvalekaldumine tellimuskinnitus"}}),
])

print("stdout carries only JSON-RPC under stdio transport")
proc = subprocess.Popen(
    [sys.executable, str(ROOT / "server.py")],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    text=True, encoding="utf-8", cwd=ROOT,
    # Unbuffered, so a stray write reaches the pipe at once. Buffered, it
    # surfaces only when 8 KB pile up (as in the incident) and is otherwise
    # lost at exit, when the SDK's own wrapper closes the shared buffer.
    env={**os.environ, "PYTHONUNBUFFERED": "1"},
)
# stderr is drained on a thread so a chatty server can't block on a full pipe.
stderr_lines: list[str] = []
threading.Thread(target=lambda: stderr_lines.extend(proc.stderr), daemon=True).start()
lines: queue.Queue[str | None] = queue.Queue()
threading.Thread(target=lambda: ([lines.put(x) for x in proc.stdout], lines.put(None)),
                 daemon=True).start()

proc.stdin.write(requests)
proc.stdin.flush()

# Wait for both answers, then close stdin: the server exits and flushes
# whatever it buffered, which is where a stray log line would surface.
junk: list[str] = []
responses: dict = {}
eof = False


def take(line: str) -> None:
    if not line.strip():
        return
    try:
        msg = json.loads(line)
    except json.JSONDecodeError:
        junk.append(line)
        return
    if isinstance(msg, dict) and "id" in msg:
        responses[msg["id"]] = msg


deadline = time.monotonic() + 120
while not {2, 3} <= responses.keys() and time.monotonic() < deadline:
    try:
        line = lines.get(timeout=max(0.1, deadline - time.monotonic()))
    except queue.Empty:
        break
    if line is None:
        eof = True
        break
    take(line)
proc.stdin.close()
try:
    proc.wait(timeout=30)
except subprocess.TimeoutExpired:
    proc.kill()
while not eof:
    try:
        line = lines.get(timeout=5)
    except queue.Empty:
        break
    if line is None:
        break
    take(line)

check("every stdout line is JSON", not junk,
      f"{len(junk)} non-JSON line(s), first: {junk[0][:120]!r}" if junk else "")
for msg_id, tool in ((2, "spell_check"), (3, "check_compound_familiarity")):
    resp = responses.get(msg_id, {})
    check(f"{tool} answered", "result" in resp and not resp["result"].get("isError"),
          str(resp)[:200] or "no response")

if failures:
    print(f"\n{len(failures)} failure(s):")
    for f in failures:
        print(" -", f)
    print("\nserver stderr (tail):\n" + "".join(stderr_lines[-15:]))
    sys.exit(1)
print("\nall stdio channel tests passed")
