#!/usr/bin/env python3
"""reload.py: one pass over the queued requests: /reload-plugins, or `/mcp reconnect all`, typed into each pane
that is ready for it, one pane at a time and GAP seconds apart.

Run by bin/run.sh, from the plugin's action and its event hooks, never by hand. Each request is a JSON file a caller
put in $HERDR_PLUGIN_CONFIG_DIR/requests/<id>.json:

    {"pane": "w1:p2", "session": "<agent session id>", "until": <unix time>, "result": "/path/to/result.json",
     "text": "/reload-plugins"}

`pane` is required; `text`, when given, is one of the two fixed commands in TEXTS, never free text (any other is a
stop: the plugin types nothing else); `session`, when given, is the agent session the pane must still run; `until` is the deadline
(default: ten minutes after the request was written); `result` is the JSON file the outcome is merged into (default:
$HERDR_PLUGIN_CONFIG_DIR/results/<id>.json). Keys already in it are kept; `state`, `why`, `tries`, `until`,
`request` and `at` are set.

A request is tried once per pass, and only types when herdr says, at that moment:

- the pane still runs `session` (else it stops: the pane is another's now);
- its agent is idle or done, and the pane is not focused: a person at it types their own;
- its input line is empty: the prompt character and nothing after it but the client's dim placeholder.

Then it types the text, reads the line again, and sends Enter, as a key of its own, only if the line is
exactly that text and the pane is still idle and unfocused. Never any other text, never a line with anything typed
in it, never a focused pane. A draft that appears on the line meanwhile (someone typed at the same moment) gets no
Enter; when it reads as the draft followed by the text, the text is taken back out (put_back).

`state` in the result: `waiting` (with `why`), `typed`, `stopped: <why>`, or `gave up: <why>` once `until` passed;
`text`, the command. An ended request's file is removed. Two passes never try one request at once (a lock on its file).

Panes are typed GAP seconds apart, machine-wide: a reload, or a reconnect, starts every plugin server of its session,
and a release once reloaded every pane at once, so a server missed the client's 30 s connect limit under the load. The
moment of the last text typed is kept in the config dir (`last-typed`, under a short lock); a request ready sooner
waits, `why` saying so. A pass that typed goes over the queue again once the gap has passed, for at most BUDGET seconds
in all; what is left waits for herdr's next event or the next invocation.

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

TEXTS = ("/reload-plugins", "/mcp reconnect all")  # fixed: this plugin types nothing else; the first is the default
TEXT = TEXTS[0]
GAP = float(os.environ.get("CLAUDE_RELOAD_GAP", "10"))  # seconds between two panes' texts, machine-wide
BUDGET = float(os.environ.get("CLAUDE_RELOAD_BUDGET", "60"))  # seconds one pass keeps going over the queue
PROMPT = "❯"
WAIT = 600  # seconds a request waits by default
SETTLE = float(os.environ.get("CLAUDE_RELOAD_SETTLE", "0.5"))  # Enter apart from the text, and the line read again
SETTLE_READS = 6  # reads, SETTLE apart, for a line someone is typing into to stop changing
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


def settled(pid: str) -> str | None:
    """The input box once two reads SETTLE apart agree (a person typing has paused); None when it keeps changing over
    SETTLE_READS reads, or cannot be read."""
    last = read_box(pid)
    for _ in range(SETTLE_READS):
        time.sleep(SETTLE)
        now = read_box(pid)
        if now == last:
            return now
        last = now
    return None


def put_back(pid: str, text: str) -> str:
    """A draft appeared between the check and the Enter: someone typed while /reload-plugins went in. No Enter, ever.
    When the line reads the draft with the text after it (the cursor right after ours), the text is taken back out with
    as many backspaces, and the line read again; any other mix is left as it stands, since a key that missed would
    delete the person's own characters. Returns why it stopped."""
    line = settled(pid)
    if line is None:
        return f"a draft appeared while {text} was typed and kept changing; left as it stands, no Enter sent"
    draft = line[:-len(text)] if line.endswith(text) else None
    if draft is None or "\n" in line or not draft.strip():
        return (f"a draft appeared while {text} was typed: the line reads {line!r:.80}; left as it stands, "
                "no Enter sent")
    herdr("pane", "send-keys", pid, *["backspace"] * len(text))
    time.sleep(SETTLE)
    after = read_box(pid)
    if after is not None and after.strip() == draft.strip():
        return f"a draft appeared while {text} was typed: {text} taken back out, the draft left as typed, no Enter sent"
    return (f"a draft appeared while {text} was typed; taking {text} back out left the line reading {after!r:.80}, "
            f"not the draft {draft!r:.80}; no Enter sent")


def turn(conf: Path) -> str:
    """The machine's typing turn: '' and the moment taken when the last text was typed GAP or more seconds ago, else
    why it waits. Under a short lock, so two passes never both take it."""
    f = (conf / "last-typed").open("a+")
    try:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.seek(0)
        try:
            last = float(f.read().strip() or 0)
        except ValueError:
            last = 0.0
        ago = time.time() - last
        if 0 <= ago < GAP:
            return f"spacing: another pane was typed into {ago:.0f} s ago, {GAP:.0f} s apart"
        f.seek(0)
        f.truncate()
        f.write(f"{time.time():.3f}")
        f.flush()
        return ""
    finally:
        f.close()


def attempt(pid: str, session: str, text: str, conf: Path) -> tuple[str, str]:
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
    why = turn(conf)
    if why:
        return "waiting", why
    herdr("pane", "send-text", pid, text)
    time.sleep(SETTLE)
    now = read_box(pid)
    if now is None:
        return "stopped", f"after typing, no input box could be read; {text} may be on the line, no Enter sent"
    if typed_part(now) != text:
        return "stopped", put_back(pid, text)
    q = pane(pid)  # once more just before Enter: a person who focused it, or a turn begun since the text went in
    if q.get("focused") or q.get("agent_status") not in ("idle", "done"):
        return "stopped", (f"the pane was {'focused' if q.get('focused') else q.get('agent_status')} after {text} "
                           "was typed; left on the line, no Enter sent")
    herdr("pane", "send-keys", pid, "enter")
    return "typed", ""


def run_one(req: Path, results: Path) -> str:
    """One try at the request REQ: what it came to, '' when this pass did not try it."""
    try:
        fd = os.open(req, os.O_RDWR)
    except FileNotFoundError:
        return ""  # ended by another pass
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return ""  # another pass is trying it now
        if not req.exists() or os.stat(req).st_ino != os.fstat(fd).st_ino:
            return ""
        rid = req.stem
        try:
            r = json.loads(req.read_text())
            if not isinstance(r, dict):
                raise ValueError("not an object")
        except (OSError, ValueError) as e:
            put(results / f"{rid}.json", request=rid, state=f"stopped: the request could not be read ({e})")
            req.unlink(missing_ok=True)
            return "stopped"
        result = Path(str(r.get("result") or results / f"{rid}.json"))
        until = float(r.get("until") or os.fstat(fd).st_mtime + WAIT)
        pid, session = str(r.get("pane") or ""), str(r.get("session") or "")
        tries = int(r.get("tries") or 0)
        text = r.get("text") or TEXT
        if not pid:
            state, why = "stopped", "the request names no pane"
        elif text not in TEXTS:
            state, why = "stopped", f"the text {str(text)[:40]!r} is not one of {', '.join(TEXTS)}: nothing else is typed"
        elif time.time() > until:
            state, why = "gave up", f"the deadline passed: {r.get('why') or 'never tried'}"
        else:
            tries += 1
            try:
                state, why = attempt(pid, session, text, results.parent)
            except Cannot as e:
                state, why = "stopped", str(e)
        if state == "waiting":
            r.update(tries=tries, why=why, until=until)  # the deadline fixed at the first pass, never moved
            tmp = req.with_name(req.name + f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps(r))
            os.replace(tmp, req)  # the lock stays on the old inode: a pass that waited for it sees the change
            put(result, request=rid, state="waiting", why=why, tries=tries, until=int(until), text=str(text))
            return "waiting: " + why
        put(result, request=rid, state="typed" if state == "typed" else f"{state}: {why}", tries=tries,
            until=int(until), text=str(text))
        req.unlink(missing_ok=True)
        return state
    finally:
        os.close(fd)


def main() -> int:
    conf = os.environ.get("HERDR_PLUGIN_CONFIG_DIR")
    if not conf:
        print("reload.py: HERDR_PLUGIN_CONFIG_DIR is not set: run by herdr only", file=sys.stderr)
        return 2
    base = Path(conf)
    start = time.monotonic()
    while True:  # again only after typing, and only while a request waits on the gap alone
        outcomes = [run_one(req, base / "results") for req in sorted((base / "requests").glob("*.json"))]
        if "typed" not in outcomes or not any(o.startswith("waiting: spacing") for o in outcomes):
            return 0
        if time.monotonic() - start + GAP > BUDGET:
            return 0
        time.sleep(GAP)


if __name__ == "__main__":
    sys.exit(main())
