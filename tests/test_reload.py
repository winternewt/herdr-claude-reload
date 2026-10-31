"""bin/run.sh and bin/reload.py, run as herdr runs them (the plugin's folder as the working directory, its config dir
in the environment), against tests/fake-herdr.py: /reload-plugins typed only into an idle, unfocused pane whose input
line is empty, Enter only once the line reads exactly that."""
import fcntl
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FAKE = ROOT / "tests" / "fake-herdr.py"
TEXT = "/reload-plugins"


@pytest.fixture
def H(tmp_path):
    fake, conf = tmp_path / "herdr", tmp_path / "config"
    fake.mkdir()
    (conf / "requests").mkdir(parents=True)

    class H_:
        dir = fake

        def state(self, status="idle", focused=False, session="sess-1", text=""):
            (fake / "pane.json").write_text(json.dumps({"pane_id": "w1:p2", "agent_status": status,
                                                        "focused": focused, "agent_session": {"value": session}}))
            (fake / "input.json").write_text(json.dumps({"text": text, "cursor": len(text)}))

        def request(self, rid="r1", **kw):
            body = {"pane": "w1:p2", "session": "sess-1", **kw}
            (conf / "requests" / f"{rid}.json").write_text(json.dumps(body))
            return conf / "requests" / f"{rid}.json"

        def run(self, event="", **env):
            e = {k: v for k, v in os.environ.items() if not k.startswith(("HERDR_", "FAKE_HERDR"))}
            e.update({"FAKE_HERDR": str(fake), "HERDR_BIN_PATH": str(FAKE), "HERDR_PLUGIN_CONFIG_DIR": str(conf),
                      "CLAUDE_RELOAD_PYTHON": sys.executable, "CLAUDE_RELOAD_SETTLE": "0", **env})
            if event:
                e.update(HERDR_PLUGIN_EVENT=event,
                         HERDR_PLUGIN_EVENT_JSON=json.dumps({"event": event, "data": {"pane_id": "w1:p2"}}))
            return subprocess.run(["sh", "bin/run.sh"], cwd=ROOT, env=e, capture_output=True, text=True, timeout=60)

        def result(self, rid="r1"):
            return json.loads((conf / "results" / f"{rid}.json").read_text())

        def calls(self):
            log = fake / "calls.log"
            return [json.loads(ln) for ln in log.read_text().splitlines()] if log.exists() else []

        def typed(self):
            return [c[3:] for c in self.calls() if c[1] in ("send-text", "send-keys")]

        def line(self):
            return json.loads((fake / "input.json").read_text())["text"]

        def submitted(self):
            s = fake / "submitted.log"
            return s.read_text().splitlines() if s.exists() else []

        def queued(self):
            return sorted(p.name for p in (conf / "requests").glob("*.json"))

    return H_()


def test_types_into_an_idle_unfocused_empty_pane(H):
    """the dim placeholder is no text: the line is empty, /reload-plugins typed, then Enter as a key of its own"""
    H.state()
    H.request()
    r = H.run()
    assert r.returncode == 0, r
    assert H.result()["state"] == "typed", H.result()
    assert H.typed() == [[TEXT], ["enter"]], H.calls()
    assert H.submitted() == [TEXT] and H.queued() == []


@pytest.mark.parametrize("kw,why", [({"status": "working"}, "the agent is working"),
                                    ({"status": "blocked"}, "the agent is blocked"),
                                    ({"focused": True}, "the pane is focused"),
                                    ({"text": "half a sentence"}, "the input line holds a draft")],
                         ids=["busy", "blocked", "focused", "draft"])
def test_never_types_into_a_busy_focused_or_typed_in_pane(H, kw, why):
    """each guard alone holds it: the request waits, nothing typed, the person's draft untouched"""
    H.state(**kw)
    H.request()
    H.run()
    assert H.result()["state"] == "waiting" and H.result()["why"] == why, H.result()
    assert H.typed() == [] and H.queued() == ["r1.json"], H.calls()
    assert H.line() == kw.get("text", "")


def test_no_input_box_on_the_screen_is_never_read_as_empty(H):
    """a dialog's option list carries the prompt character but no box closed by a rule: it waits"""
    H.state()
    (H.dir / "screen").write_text("Do you want to proceed?\n\x1b[36m❯ 1. Yes\x1b[0m\r\n  2. No\nEsc to cancel\n")
    H.request()
    H.run()
    assert H.result()["state"] == "waiting" and "no input box" in H.result()["why"], H.result()
    assert H.typed() == []


def test_a_waiting_request_is_typed_at_the_event_that_frees_the_pane(H):
    """busy when queued; the agent's status event once it fell idle runs the pass again, and it types"""
    H.state(status="working")
    H.request()
    H.run()
    assert H.result()["state"] == "waiting" and H.typed() == []
    H.state(status="idle")
    H.run(event="pane.agent_status_changed")
    assert H.result()["state"] == "typed" and H.result()["tries"] == 2, H.result()
    assert H.submitted() == [TEXT]


def test_another_session_in_the_pane_stops_it(H):
    H.state(session="sess-9")
    H.request()
    H.run()
    assert H.result()["state"].startswith("stopped: the pane runs another session"), H.result()
    assert H.typed() == [] and H.queued() == []


def test_a_draft_appearing_while_typing_gets_no_enter(H):
    """a person typing at the same moment: the line is not exactly /reload-plugins, so nothing is submitted"""
    H.state()
    H.request()
    H.run(FAKE_HERDR_RACE_AFTER="hel", FAKE_HERDR_DEAF="1")
    assert H.result()["state"].startswith("stopped:") and "no Enter" in H.result()["state"], H.result()
    assert ["enter"] not in H.typed() and H.submitted() == [], H.calls()


@pytest.mark.parametrize("after", ['{"focused": true}', '{"agent_status": "working"}'], ids=["focused", "busy"])
def test_a_pane_focused_or_busy_after_typing_gets_no_enter(H, after):
    """the pane is read again just before Enter: a person who focused it, or a turn begun, stops it, the text left on
    the line for them to see"""
    H.state()
    H.request()
    H.run(FAKE_HERDR_AFTER=after)
    assert "no Enter sent" in H.result()["state"], H.result()
    assert H.typed() == [[TEXT]] and H.line() == TEXT


def test_the_slash_menu_on_the_input_line_still_gets_its_enter(H):
    """the client's slash menu prints the command's description past a column gap: what was typed is still exactly
    /reload-plugins"""
    H.state()
    H.request()
    H.run(FAKE_HERDR_MENU="                                     Activate pending plugin chan")
    assert H.result()["state"] == "typed" and H.submitted() == [TEXT], H.result()


def test_a_passed_deadline_gives_up_without_typing(H):
    H.state()
    H.request(until=1)
    H.run()
    assert H.result()["state"].startswith("gave up: the deadline passed"), H.result()
    assert H.typed() == [] and H.queued() == []


def test_the_default_deadline_is_fixed_at_the_first_pass(H):
    """a request without `until` waits ten minutes from the first pass; rewriting it while it waits never moves that"""
    H.state(status="working")
    req = H.request()
    H.run()
    first = H.result()["until"]
    os.utime(req, (first + 100, first + 100))  # rewritten later: its file's time is no deadline
    H.run()
    assert H.result()["until"] == first and first > 0, H.result()


def test_the_result_keeps_what_the_caller_keeps_in_it(H, tmp_path):
    """the outcome is merged into the caller's own file, its keys kept"""
    mine = tmp_path / "mine.json"
    mine.write_text(json.dumps({"target": "1.2.3", "spawns": 2}))
    H.state()
    H.request(result=str(mine))
    H.run()
    got = json.loads(mine.read_text())
    assert got["state"] == "typed" and got["target"] == "1.2.3" and got["spawns"] == 2, got


def test_a_request_another_pass_holds_is_left_to_it(H):
    """two passes never try one request at once: the action and an event hook started together type once"""
    H.state()
    req = H.request()
    with open(req) as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        H.run()
    assert H.typed() == [] and H.queued() == ["r1.json"]


def test_herdr_that_cannot_be_asked_stops_the_request(H):
    H.state()
    H.request()
    (H.dir / "fail").touch()
    H.run()
    assert H.result()["state"].startswith("stopped: herdr pane get exited 1"), H.result()
    assert H.typed() == []


def test_with_nothing_queued_an_event_starts_nothing(H):
    """herdr runs the hooks for every pane on the machine: with no request, run.sh exits before any Python starts"""
    H.state()
    r = H.run(event="pane.focused", CLAUDE_RELOAD_PYTHON="/nonexistent/python3")
    assert r.returncode == 0 and H.calls() == [], r


def test_the_manifest_declares_the_action_and_both_events():
    import tomllib
    m = tomllib.loads((ROOT / "herdr-plugin.toml").read_text())
    assert m["id"] == "claude-reload" and m["min_herdr_version"]
    assert [a["id"] for a in m["actions"]] == ["reload"]
    assert sorted(e["on"] for e in m["events"]) == ["pane.agent_status_changed", "pane.focused"]
    for entry in m["actions"] + m["events"]:
        assert entry["command"] == ["sh", "bin/run.sh"]
