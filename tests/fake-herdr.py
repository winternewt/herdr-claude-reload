#!/usr/bin/env python3
"""fake-herdr.py: herdr's command line as bin/reload.py calls it, over files in $FAKE_HERDR.

pane.json is the pane's record (`pane get` answers it inside the CLI's envelope). input.json, {"text", "cursor"}, is
the client's input line; `pane read` draws a screen around it: a rule, the prompt row (the client's dim placeholder
when the line is empty), a rule, a footer; a file `screen` there is answered instead, as it stands.

`pane send-text` inserts its text at the cursor, after $FAKE_HERDR_RACE_BEFORE and before $FAKE_HERDR_RACE_AFTER when
set (a person typing at the same moment), then merges $FAKE_HERDR_AFTER, a JSON object, into pane.json (a person who
focused the pane, a turn begun); after it, each `pane read` first inserts $FAKE_HERDR_TYPING at the cursor when set
(a person still typing). `pane send-keys` plays its keys on the line: enter submits it (submitted.log),
backspace, left and right edit it; with $FAKE_HERDR_DEAF set, the keys are logged and change nothing. $FAKE_HERDR_MENU
is drawn after a line that starts with a slash (the client's slash menu). Every call is appended to calls.log; with a
file `fail` present, every call exits 1."""
import json
import os
import sys
from pathlib import Path

d = Path(os.environ["FAKE_HERDR"])
with (d / "calls.log").open("a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\n")
if (d / "fail").exists():
    print('{"error":{"code":"server_not_running","message":"the server is not running"}}', file=sys.stderr)
    sys.exit(1)
cmd, args = sys.argv[1:3], sys.argv[3:]
line = d / "input.json"
inp = json.loads(line.read_text()) if line.exists() else {"text": "", "cursor": 0}


def save():
    line.write_text(json.dumps(inp))


if cmd == ["pane", "get"]:
    print(json.dumps({"id": "cli:pane:get", "result": {"pane": json.loads((d / "pane.json").read_text()),
                                                       "type": "pane_info"}}))
elif cmd == ["pane", "read"]:
    if os.environ.get("FAKE_HERDR_TYPING") and (d / "typed").exists():  # a person still typing: more at each read
        c = inp["cursor"]
        inp.update(text=inp["text"][:c] + os.environ["FAKE_HERDR_TYPING"] + inp["text"][c:],
                   cursor=c + len(os.environ["FAKE_HERDR_TYPING"]))
        save()
    if (d / "screen").exists():
        sys.stdout.write((d / "screen").read_text())
    else:
        t = inp["text"]
        row = ("\x1b[0m\x1b[38;2;153;153;153m❯\xa0\x1b[0m"
               + (f"{t}{os.environ.get('FAKE_HERDR_MENU', '') if t.startswith('/') else ''}\r" if t
                  else "\x1b[2mTry \"fix lint errors\"\x1b[0m\r"))
        sys.stdout.write(f"some output\n{'─' * 40}\n{row}\n{'─' * 40}\n  ⏵⏵ auto mode on\n")
elif cmd == ["pane", "send-text"]:
    ins = os.environ.get("FAKE_HERDR_RACE_BEFORE", "") + args[1] + os.environ.get("FAKE_HERDR_RACE_AFTER", "")
    c = inp["cursor"]
    inp.update(text=inp["text"][:c] + ins + inp["text"][c:], cursor=c + len(ins))
    save()
    (d / "typed").touch()
    if os.environ.get("FAKE_HERDR_AFTER"):
        (d / "pane.json").write_text(json.dumps({**json.loads((d / "pane.json").read_text()),
                                                 **json.loads(os.environ["FAKE_HERDR_AFTER"])}))
elif cmd == ["pane", "send-keys"]:
    if not os.environ.get("FAKE_HERDR_DEAF"):
        for k in args[1:]:
            t, c = inp["text"], inp["cursor"]
            if k == "enter":
                with (d / "submitted.log").open("a") as f:
                    f.write(t + "\n")
                inp.update(text="", cursor=0)
            elif k == "backspace" and c:
                inp.update(text=t[:c - 1] + t[c:], cursor=c - 1)
            elif k == "left":
                inp["cursor"] = max(0, c - 1)
            elif k == "right":
                inp["cursor"] = min(len(t), c + 1)
        save()
else:
    print(f"fake-herdr: {' '.join(sys.argv[1:])} is not faked", file=sys.stderr)
    sys.exit(2)
