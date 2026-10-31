# herdr-claude-reload

A [herdr](https://herdr.dev) plugin that types `/reload-plugins` into one named Claude Code pane, and presses Enter,
only at a moment when herdr says nobody is using that pane: its agent is idle, the pane is not focused, and its input
line is empty.

Claude Code picks up a changed plugin (new hooks, commands, skills) only after `/reload-plugins`, which no hook, tool
or subcommand can run from inside the session. A script outside the session can type it, but a pane is somebody's
workspace: typing into it while a person is there, or while they have half a sentence in the input, breaks their
work. This plugin is the careful version of that script.

It is a building block: something else decides *that* a pane should reload (a version check, a release watcher, your
own script) and queues a request; the plugin decides *when*.

## What it checks

At each try, read live from herdr:

1. **The pane still runs the expected session.** When the request names a session id, and the pane's
   `agent_session` is another one, the request stops: the pane belongs to someone else now.
2. **The agent is idle or done.** `working`, `blocked` (a dialog or permission prompt), `unknown`: it waits.
3. **The pane is not focused.** A person looking at it types their own commands; it waits until the focus moves.
4. **The input line is empty.** The input box is read from the screen with its colours: the prompt character `❯`,
   then nothing but the client's dim placeholder. Any typed text, and any screen with no input box closed by a rule
   (a dialog, a screen still drawing), means wait. An unreadable box is never taken for an empty one.

Then it types `/reload-plugins`, waits half a second, and reads the line again. Enter is sent, as a key of its own,
only if the line is exactly `/reload-plugins` (the client's slash menu, printed past a column gap, is allowed) **and**
herdr still says the pane is idle and unfocused. Otherwise it stops with no Enter. A line holding only
`/reload-plugins` in a pane that just got focus or started a turn is left there for the person to see.

### A draft that appears while it types

If someone typed into the pane in the half second between the check and the Enter, the line holds their text and
ours. Nothing is ever submitted then. The plugin first waits for the line to stop changing (up to six reads, half a
second apart; a line still changing is left alone). Then:

- **Their text, then `/reload-plugins`** (they typed just before ours went in, so the cursor sits right after our
  text): it sends exactly 15 backspaces, reads the line again, and reports the draft put back only if the line now
  reads their text.
- **`/reload-plugins`, then their text, or the two mixed:** it sends nothing and leaves the line as it stands. The
  cursor sits somewhere in their text, and the screen does not show where (nor trailing spaces), so any key it sent
  could delete their characters instead of ours. The result says what the line reads.

It never types any other text: `/reload-plugins` is fixed in the code, not taken from the request, so the plugin
cannot be used to type arbitrary input into a pane.

## When it tries

herdr has no event for the text of an input line, so the plugin cannot wait on that directly. It tries:

- when the action is invoked (right after a request is queued);
- at every `pane.agent_status_changed` event (an agent fell idle);
- at every `pane.focused` event (the focus moved away from a pane).

Each run is one pass over the queue, one try per request, and then it exits: nothing keeps running between events.
With no request queued, the event hooks exit at once without starting Python. A request has a deadline; the first
run after it gives the request up. Two runs that start together (the action and an event) never try one request
twice: each request file is locked while it is tried.

## Install

Requirements: herdr 0.9.5 or later, POSIX `sh`, Python 3.11 or later as `python3` on the herdr server's `PATH`, and
herdr's Claude Code integration (`herdr integration install claude`), without which herdr does not know which session
runs in which pane or whether it is idle.

From a local checkout:

```sh
herdr plugin link /path/to/herdr-claude-reload
herdr plugin list --plugin claude-reload        # enabled: yes
herdr plugin action list --plugin claude-reload  # claude-reload.reload
```

Once published, from GitHub: `herdr plugin install OWNER/herdr-claude-reload`.

Linux only so far (`platforms = ["linux"]`); nothing in it is Linux-specific, but it has not been run elsewhere.

## Interface

A caller queues a request, then invokes the action.

1. Find the queue: `herdr plugin config-dir claude-reload` prints the plugin's config directory (it creates it if
   needed). Requests go in its `requests/` folder.
2. Write the request as `requests/<id>.json`, where `<id>` is any name of yours (letters, digits, `-`, `_`). Write it
   to a temporary name in the same folder and rename it, so a run never reads half a file:

   ```json
   {"pane": "w1:p2", "session": "6f1c…", "until": 1793450000, "result": "/path/to/result.json"}
   ```

   | Key | Required | Meaning |
   |---|---|---|
   | `pane` | yes | The herdr pane id (`herdr pane list`, or `$HERDR_PANE_ID` inside the pane). |
   | `session` | no | The agent session id the pane must still run (`agent_session.value` in `herdr pane get`). |
   | `until` | no | Deadline, Unix seconds. Default: ten minutes after the request was queued. |
   | `result` | no | A JSON file the outcome is merged into. Default: `results/<id>.json` beside `requests/`. |

3. Invoke the action: `herdr plugin action invoke claude-reload.reload`. It returns at once; the run happens in the
   herdr server.

`herdr plugin action invoke` carries no arguments of its own, which is why the request is a file.

### The result

The result file is a JSON object. Keys already in it are kept, so a caller can point `result` at its own record.
The plugin sets:

| Key | Meaning |
|---|---|
| `state` | `waiting`, `typed`, `stopped: <why>` (a draft put back, or left, is a stop), or `gave up: <why>` |
| `why` | while `waiting`: what it waits for (`the agent is working`, `the pane is focused`, `the input line holds a draft`, …) |
| `tries` | how many tries were made |
| `until` | the deadline in force |
| `request` | the request's id |
| `at` | when the result was last written, Unix seconds |

`typed`, `stopped` and `gave up` end the request: its file is removed. A request still `waiting` once `until` has
passed has seen no event since; treat it as given up (the next run will say so).

## Tests

```sh
uv sync --group dev
uv run pytest -q
```

The tests run `bin/run.sh` as herdr does, against a fake `herdr` (`tests/fake-herdr.py`) that keeps a pane's record
and its input line in files; nothing is typed into a real pane.
