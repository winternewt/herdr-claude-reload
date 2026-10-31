#!/usr/bin/env python3
"""reload.py: one pass over the queued requests: /reload-plugins typed into each pane that is ready for it.

Run by bin/run.sh, from the plugin's action and its event hooks, never by hand. Each request is a JSON file a caller
put in $HERDR_PLUGIN_CONFIG_DIR/requests/<id>.json:

    {"pane": "w1:p2", "session": "<agent session id>", "until": <unix time>, "result": "/path/to/result.json"}

`pane` is required; `session`, when given, is the agent session the pane must still run; `until` is the deadline
(default: ten minutes after the request was written); `result` is the JSON file the outcome is merged into (default:
$HERDR_PLUGIN_CONFIG_DIR/results/<id>.json). Keys already in it are kept; `state`, `why`, `tries`, `until`,
`request` and `at` are set.

A request is tried once per pass, and only types when herdr says, at that moment:

- the pane still runs `session` (else it stops: the pane is another's now);
- its agent is idle or done, and the pane is not focused: a person at it types their own;
- its input line is empty: the prompt character and nothing after it but the client's dim placeholder.

Then it types `/reload-plugins`, reads the line again, and sends Enter, as a key of its own, only if the line is
exactly that text and the pane is still idle and unfocused. Never any other text, never a line with anything typed
in it, never a focused pane.

`state` in the result: `waiting` (with `why`), `typed`, `stopped: <why>`, or `gave up: <why>` once `until` passed.
An ended request's file is removed. Two passes never try one request at once (a lock on its file).

EXIT STATUS  0 the pass ran · 2 not run by herdr (no config dir)
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

TEXT = "/reload-plugins"  # fixed: this plugin types nothing else
PROMPT = "❯"
WAIT = 600  # seconds a request waits by default
SETTLE = float(os.environ.get("CLAUDE_RELOAD_SETTLE", "0.5"))  # Enter apart from the text, and the line read again
SGR = re.compile(r"\x1b\[([0-9;]*)m")
OTHER = re.compile(r"\x1b\[[0-9;?]*[A-Za-ln-z]|\r")  # every CSI but SGR (m), which typed() reads
RULE = "─"


class Cannot(Exception):
    """herdr could not be asked."""


def herdr(*args: str) -> str:
    try:
        r = subprocess.run([os.environ.get("HERDR_BIN_PATH") or "herdr", *args], capture_output=True, text=True,
                           timeout=15)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise Cannot(f"herdr {args[0]} {args[1]}: {e}") from None
    if r.returncode != 0:
        raise Cannot(f"herdr {args[0]} {args[1]} exited {r.returncode}: {(r.stderr or r.stdout).strip()[:200]}")
    return r.stdout


def pane(pid: str) -> dict:
    try:
        return json.loads(herdr("pane", "get", pid))["result"]["pane"]
    except (ValueError, KeyError, TypeError) as e:
        raise Cannot(f"herdr pane get: an answer it could not read ({e})") from None


def sgr_dim(codes: list[str], dim: bool) -> bool:
    """Whether text after an SGR with CODES is dim: 2 sets it, 0 and 22 clear it; a colour's own parameters
    (38, 48 or 58, then 5;n or 2;r;g;b) are skipped, so a 2 among them is never read as dim."""
    k = 0
    while k < len(codes):
        c = codes[k]
        if c in ("38", "48", "58"):
            k += 3 if codes[k + 1:k + 2] == ["5"] else 5
            continue
        if c == "2":
            dim = True
        elif c in ("0", "", "22"):
            dim = False
        k += 1
    return dim


def visible(row: str) -> str:
    """A row's text with its dim runs (the client's placeholder) left out."""
    out, dim = [], False
    for i, part in enumerate(SGR.split(OTHER.sub("", row))):
        if i % 2:  # an SGR's parameters
            dim = sgr_dim(part.split(";") if part else ["0"], dim)
        elif not dim:
            out.append(part)
    return "".join(out).replace("\xa0", " ")


def plain(row: str) -> str:
    return SGR.sub("", OTHER.sub("", row))


def is_rule(text: str) -> bool:
    t = text.strip()
    return t.startswith(RULE) and t.endswith(RULE) and t.count(RULE) >= 8


def input_box(screen: str) -> str | None:
    """What is typed in the input box: the rows from the last one holding the prompt character down to the rule that
    closes the box, dim text left out, rows joined by newlines. None when no such box is on the screen (a dialog, a
    screen still drawing): never read as empty."""
    rows = screen.split("\n")
    at = [i for i, r in enumerate(rows) if PROMPT in plain(r)]
    if not at:
        return None
    top = at[-1]
    end = next((j for j in range(top + 1, len(rows)) if is_rule(plain(rows[j]))), None)
    if end is None:
        return None
    lines = [visible(rows[top]).split(PROMPT, 1)[1].strip()] + [visible(r).strip() for r in rows[top + 1:end]]
    while len(lines) > 1 and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def read_box(pid: str) -> str | None:
    return input_box(herdr("pane", "read", pid, "--source", "visible", "--format", "ansi"))


def typed_part(box: str) -> str:
    """The client's slash menu prints the command's description on the input row, past a column gap: what precedes
    that gap is what was typed."""
    return re.split(r"\s{2,}", box, maxsplit=1)[0]


def put(result: Path, **kw) -> None:
    """Merges KW into RESULT, keeping what the caller keeps there."""
    try:
        got = json.loads(result.read_text())
        got = got if isinstance(got, dict) else {}
    except (OSError, ValueError):
        got = {}
    result.parent.mkdir(parents=True, exist_ok=True)
    tmp = result.with_name(result.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps({**got, **kw, "at": int(time.time())}))
    tmp.replace(result)


def attempt(pid: str, session: str) -> tuple[str, str]:
    """One try at PID: ('typed', ''), ('waiting', why) or ('stopped', why)."""
    p = pane(pid)
    if session and str((p.get("agent_session") or {}).get("value") or "") != session:
        return "stopped", f"the pane runs another session now, not {session}"
    status = p.get("agent_status")
    if status not in ("idle", "done"):
        return "waiting", f"the agent is {status}"
    if p.get("focused"):
        return "waiting", "the pane is focused"
    box = read_box(pid)
    if box is None:
        return "waiting", "no input box on the pane's screen (a dialog open?)"
    if box:
        return "waiting", "the input line holds a draft"
    herdr("pane", "send-text", pid, TEXT)
    time.sleep(SETTLE)
    now = read_box(pid)
    if now is None or typed_part(now) != TEXT:
        return "stopped", (f"after typing, the input line read {now!r:.80}, not {TEXT}; left as it stands, "
                           "no Enter sent")
    q = pane(pid)  # once more just before Enter: a person who focused it, or a turn begun since the text went in
    if q.get("focused") or q.get("agent_status") not in ("idle", "done"):
        return "stopped", (f"the pane was {'focused' if q.get('focused') else q.get('agent_status')} after {TEXT} "
                           "was typed; left on the line, no Enter sent")
    herdr("pane", "send-keys", pid, "enter")
    return "typed", ""


def run_one(req: Path, results: Path) -> None:
    try:
        fd = os.open(req, os.O_RDWR)
    except FileNotFoundError:
        return  # ended by another pass
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return  # another pass is trying it now
        if not req.exists() or os.stat(req).st_ino != os.fstat(fd).st_ino:
            return
        rid = req.stem
        try:
            r = json.loads(req.read_text())
            if not isinstance(r, dict):
                raise ValueError("not an object")
        except (OSError, ValueError) as e:
            put(results / f"{rid}.json", request=rid, state=f"stopped: the request could not be read ({e})")
            req.unlink(missing_ok=True)
            return
        result = Path(str(r.get("result") or results / f"{rid}.json"))
        until = float(r.get("until") or os.fstat(fd).st_mtime + WAIT)
        pid, session = str(r.get("pane") or ""), str(r.get("session") or "")
        tries = int(r.get("tries") or 0)
        if not pid:
            state, why = "stopped", "the request names no pane"
        elif time.time() > until:
            state, why = "gave up", f"the deadline passed: {r.get('why') or 'never tried'}"
        else:
            tries += 1
            try:
                state, why = attempt(pid, session)
            except Cannot as e:
                state, why = "stopped", str(e)
        if state == "waiting":
            r.update(tries=tries, why=why, until=until)  # the deadline fixed at the first pass, never moved
            tmp = req.with_name(req.name + f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps(r))
            os.replace(tmp, req)  # the lock stays on the old inode: a pass that waited for it sees the change
            put(result, request=rid, state="waiting", why=why, tries=tries, until=int(until))
            return
        put(result, request=rid, state="typed" if state == "typed" else f"{state}: {why}", tries=tries,
            until=int(until))
        req.unlink(missing_ok=True)
    finally:
        os.close(fd)


def main() -> int:
    conf = os.environ.get("HERDR_PLUGIN_CONFIG_DIR")
    if not conf:
        print("reload.py: HERDR_PLUGIN_CONFIG_DIR is not set: run by herdr only", file=sys.stderr)
        return 2
    base = Path(conf)
    for req in sorted((base / "requests").glob("*.json")):
        run_one(req, base / "results")
    return 0


if __name__ == "__main__":
    sys.exit(main())
