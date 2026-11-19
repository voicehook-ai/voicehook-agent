"""SS2kwgDK (Oliver 10.11.): a permission dialog of a helper session goes on the call's board, and only
the owner's CLICK allows it (never by voice, never automatically, never a guest). A no denies; anything
else (timeout, free text, missing fields) leaves the dialog in the session as before. Fixtures only.

Review Ines #196 (19.11.): allow only (1) on a LOCAL relay of a join whose signed token carried
vh.owner_agent=1, (2) on a click the server signed (answered_owner_signed), (3) for a slot created fresh
for exactly this request (sha256 of session, tool, tool_input), (4) for a one-line Bash command shown
whole and verbatim on the card, (5) with nothing blacked out; (6) off unless installed locally.
Every allow test has a counter-test that must stay None."""
# ruff: noqa: F811  (pytest fixtures imported from test_asks)
from __future__ import annotations

import json

import pytest
from test_asks import (  # noqa: F401  (fixtures)
    TOKEN,
    URL,
    Clock,
    FakeBoard,
    _env,
    relay,
)

from voicehook_agent import activity, asks, notify
from voicehook_agent import multisession as ms

T = 1000.0
ALLOW = {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {"behavior": "allow"}}}


def _local(tmp_path, owner_agent=True):
    d = tmp_path / "join"
    d.mkdir(exist_ok=True)
    ms.write_local_relay(d, "room-a", owner_session=ms.session_hash("JOINER"), owner_agent=owner_agent)
    return ms.load_relay()


@pytest.fixture
def local(tmp_path, monkeypatch):
    """Local relay of the joining session JOINER (WebRTC join with signed vh.owner_agent=1)."""
    monkeypatch.setattr(ms, "_spawn_post", lambda *a: None)
    monkeypatch.setattr(notify, "perm_wait_installed", lambda path=None: True)
    return _local(tmp_path)


def _perm(sid="other-1", cmd="git push origin main", tool="Bash", inp=None):
    return json.dumps({"session_id": sid, "hook_event_name": "PermissionRequest", "cwd": "/w/p",
                       "transcript_path": "", "tool_name": tool,
                       "tool_input": inp if inp is not None else {"command": cmd}})


def _answer(verdict, via="click", role="owner", text="", signed=True):
    ev = {"type": "decision", "id": "d00000000", "verdict": verdict, "by": "Oliver", "number": 3, "question": "q"}
    if via:
        ev["answered_via"] = via
    if role:
        ev["answered_role"] = role
    if signed is not None:
        ev["answered_owner_signed"] = signed
    if text:
        ev["text"] = text
    return ev


def _run(raw, answers, wait_raw=None, wall=T):
    """The parallel permission-request hook (notify.handle) creates the card for `raw`; permission-wait
    then runs for `wait_raw` (default: the same request)."""
    fb, clock = FakeBoard(), Clock()
    jobs: list = []
    notify.handle(raw, now=T, post=jobs.append)
    assert jobs, "permission-request legte keine Entscheidung an"
    fb.create(jobs[0])
    fb.queue = list(answers)
    out = asks.permission_wait(wait_raw or raw, next_events=fb.next_events, clock=clock,
                               sleep=lambda s: None, wall=lambda: wall)
    return out, fb


# ----- the one allow path ------------------------------------------------------------------------
def test_signierter_klick_des_besitzers_gibt_frei(local, monkeypatch):
    monkeypatch.setattr(notify, "perm_wait_installed", lambda path=None: True)
    out, fb = _run(_perm(), [_answer("yes")])
    assert out == ALLOW
    assert fb.created[0]["question"].endswith(": git push origin main – erlauben?")   # wörtlich, ganz


def test_pfad_wird_wo_freigabe_moeglich_woertlich_gezeigt(local):
    out, fb = _run(_perm(cmd="git worktree remove /w/p/wt-a"), [_answer("yes")])
    assert fb.created[0]["question"].endswith(": git worktree remove /w/p/wt-a – erlauben?") and out == ALLOW
    assert notify.PERM_YES_NOTE in fb.created[0]["context"]                           # Punkt 8
    assert notify.open_slot(ms.session_hash("other-1")) is None


def test_hinweis_ja_gibt_frei_nur_wo_es_stimmt(local, monkeypatch):
    monkeypatch.setattr(notify, "perm_wait_installed", lambda path=None: False)
    _out, fb = _run(_perm(cmd="git worktree remove /w/p/wt-a"), [_answer("yes")])   # Hook nicht installiert
    assert notify.PERM_YES_NOTE not in json.dumps(fb.created)
    assert fb.created[0]["question"].endswith("remove wt-a – erlauben?")  # Pfad gekürzt wie immer
    assert _out is None
    monkeypatch.setattr(notify, "perm_wait_installed", lambda path=None: True)
    _out, fb = _run(_perm(sid="other-2", cmd="git add -A\ngit push"), [])  # nicht wörtlich
    assert notify.PERM_YES_NOTE not in json.dumps(fb.created)


def test_hinweis_nie_bei_remote_relay(relay, monkeypatch):
    monkeypatch.setattr(notify, "perm_wait_installed", lambda path=None: True)
    _out, fb = _run(_perm(), [])
    assert notify.PERM_YES_NOTE not in json.dumps(fb.created)


# ----- Punkt 2: only a signed click ----------------------------------------------------------------
@pytest.mark.parametrize("ans", [
    _answer("yes", via="voice"),                       # Stimme statt Klick
    _answer("yes", via="voice", signed=True),
    _answer("yes", signed=False),                      # Klick ohne Signatur
    _answer("yes", signed=None),                       # älterer Server ohne Feld
    _answer("yes", role="owner", signed=None),         # alte Prüfung answered_role=owner reicht nicht mehr
    _answer("yes", role="guest", signed=False),
    _answer("yes", via=None),
    _answer("other", text="mach mal"),
])
def test_nie_frei_ohne_signierten_klick(local, ans):
    out, _fb = _run(_perm(), [ans])
    assert out is None


@pytest.mark.parametrize("via", ["click", "voice"])
def test_nein_des_besitzers_lehnt_ab(local, via):
    out, _fb = _run(_perm(), [_answer("no", via=via, text="erst nach dem Call")])
    d = out["hookSpecificOutput"]["decision"]
    assert d["behavior"] == "deny" and "erst nach dem Call" in d["message"]


# ----- Punkt 1: local relay with signed owner_agent ---------------------------------------------------
def test_fremder_raum_remote_relay_nie_frei_nein_geht(relay):
    out, _fb = _run(_perm(), [_answer("yes")])
    assert out is None                                                     # helper-import/MCP: nie allow
    out, _fb = _run(_perm(sid="other-2", cmd="git push origin dev"), [_answer("no")])
    assert out["hookSpecificOutput"]["decision"]["behavior"] == "deny"     # deny bleibt erlaubt


def test_lokaler_join_ohne_owner_agent_nie_frei(tmp_path, monkeypatch):
    monkeypatch.setattr(ms, "_spawn_post", lambda *a: None)
    monkeypatch.setattr(notify, "perm_wait_installed", lambda path=None: True)
    _local(tmp_path, owner_agent=False)
    out, _fb = _run(_perm(), [_answer("yes")])
    assert out is None
    _local(tmp_path, owner_agent=True)                                     # Gegenprobe
    out, _fb = _run(_perm(sid="other-2", cmd="git push origin dev"), [_answer("yes")])
    assert out == ALLOW


def test_owner_agent_muss_echtes_true_sein(local, tmp_path):
    p = ms.relay_path()
    d = json.loads(p.read_text())
    d["owner_agent"] = "1"                                                 # kein bool: zählt nicht
    activity._write_private(p, json.dumps(d))
    out, _fb = _run(_perm(), [_answer("yes")])
    assert out is None


def test_join_schreibt_owner_agent_aus_dem_token(tmp_path):
    d = tmp_path / "j"
    d.mkdir()
    ms.write_local_relay(d, "r", owner_agent=True)
    assert ms.load_relay()["owner_agent"] is True
    ms.write_local_relay(d, "r")
    assert ms.load_relay()["owner_agent"] is False                         # Standard: nein


# ----- Punkt 3: bound to exactly this request -----------------------------------------------------------
def test_geaendertes_tool_input_bei_gleicher_karte_nie_frei(local):
    a = _perm(inp={"command": "git push origin main", "description": "push"})
    b = _perm(inp={"command": "git push origin main", "description": "push", "run_in_background": True})
    out, _fb = _run(a, [_answer("yes")], wait_raw=b)                       # gleiche Frage, anderer Input
    assert out is None
    out, _fb = _run(_perm(sid="other-2", inp={"command": "ls"}), [_answer("yes")])   # Gegenprobe
    assert out == ALLOW


def test_alter_slot_per_dedupe_nie_frei(local):
    out, _fb = _run(_perm(), [_answer("yes")], wall=T + asks.PERM_FRESH_S + 5)
    assert out is None


# ----- Punkt 4 / 5: whole one-line Bash, nothing blacked out ---------------------------------------------
@pytest.mark.parametrize("raw", [
    _perm(cmd="git add -A\ngit push"),                                     # mehrzeilig
    _perm(cmd="echo " + "x" * (notify.PERM_CARD_MAX + 5)),                 # würde gekürzt
    _perm(cmd="DB_PASSWORD=hunter22 ./migrate"),                           # Punkt 5
    _perm(cmd="mysql -u root -phunter22 db"),
    _perm(cmd="psql --password db"),
    _perm(cmd="git clone https://bob:hunter22@example.com/r.git"),
    _perm(cmd="curl -H 'Auth" + "orization: Bear" + "er abcdefgh12345'"),   # zerlegt (gitleaks curl-auth-header)
    _perm(tool="Write", inp={"file_path": "/w/p/a.txt", "content": "x"}),
    _perm(tool="Edit", inp={"file_path": "/w/p/a.txt", "old_string": "a", "new_string": "b"}),
    _perm(tool="mcp__github__merge_pr", inp={"pr": 1}),
])
def test_nur_ganzer_einzeiliger_bash_befehl(local, raw):
    fb, jobs = FakeBoard(), []
    notify.handle(raw, now=T, post=jobs.append)
    if not jobs:
        return                                                             # keine Karte: Dialog bleibt ohnehin
    fb.create(jobs[0])
    assert "hunter22" not in json.dumps(fb.created)                        # nie ein Geheimnis auf der Karte
    fb.queue = [_answer("yes")]
    out = asks.permission_wait(raw, next_events=fb.next_events, clock=Clock(), sleep=lambda s: None,
                               wall=lambda: T)
    assert out is None


@pytest.mark.parametrize("cmd,secret", [
    ("export MY_TOKEN=abc", True), ("mysql -p secret", True), ("x --password=y", True),
    ("git clone https://u:p@h/r", True), ("mkdir -p build", True),          # -p schwärzt immer: Terminal
    ("git push origin main", False), ("ls -la", False),
])
def test_schwaerzen_permission(cmd, secret):
    assert (notify.perm_scrub(cmd) != cmd) is secret
    assert bool(notify.bash_exact({"tool_name": "Bash", "tool_input": {"command": cmd}})) is not secret


# ----- rest --------------------------------------------------------------------------------------------
def test_timeout_laesst_dialog_stehen(local):
    out, _fb = _run(_perm(), [])
    assert out is None


def test_nur_fuer_helfer_eines_calls_und_abschaltbar(local, monkeypatch):
    fb = FakeBoard()
    assert asks.permission_wait(_perm(sid="JOINER"), next_events=fb.next_events, sleep=lambda s: None) is None
    monkeypatch.setenv(asks.PERM_WAIT_ENV, "0")
    assert asks.permission_wait(_perm(), next_events=fb.next_events, sleep=lambda s: None) is None
    monkeypatch.delenv(asks.PERM_WAIT_ENV)
    assert asks.permission_wait(json.dumps({"hook_event_name": "PermissionRequest", "session_id": "o",
                                            "tool_name": "AskUserQuestion"}), next_events=fb.next_events) is None


def test_ohne_call_nichts():
    assert asks.permission_wait(_perm(), next_events=FakeBoard().next_events, sleep=lambda s: None) is None


# ----- Punkt 6: off unless chosen locally ---------------------------------------------------------------
def _perm_hooks(path):
    hooks = json.loads(path.read_text())["hooks"].get("PermissionRequest") or []
    return [h for e in hooks for h in e["hooks"] if "permission-wait" in h["command"]]


def test_standard_aus_nur_mit_flag_oder_env(tmp_path, monkeypatch):
    monkeypatch.delenv(activity.PERM_WAIT_ON_ENV, raising=False)
    s = tmp_path / "settings.json"
    activity.install(s, exe="voicehook-agent-hook")
    assert _perm_hooks(s) == []                                            # Standard: aus
    assert "permission-wait" not in json.dumps(activity.snippet())
    activity.install(s, exe="voicehook-agent-hook", permission_wait=True)
    (h,) = _perm_hooks(s)
    assert h["command"].endswith("permission-wait || true") and h["timeout"] > asks.PERM_WAIT_S
    s2 = tmp_path / "s2.json"
    monkeypatch.setenv(activity.PERM_WAIT_ON_ENV, "1")
    activity.install(s2, exe="voicehook-agent-hook")
    assert len(_perm_hooks(s2)) == 1


def test_flag_wird_gelesen_und_nur_fuer_claude():
    opts, err = activity._install_opts(["--permission-wait"])
    assert not err and opts["permission_wait"] is True
    assert activity.main(["install", "--agent", "codex", "--permission-wait"]) == 2


def test_relay_json_schaltet_nie_ein():
    import inspect
    src = inspect.getsource(asks.permission_wait) + inspect.getsource(activity.install)
    assert "relay" not in inspect.getsource(activity.perm_wait_wanted)
    assert "PERM_WAIT_ON_ENV" in src or "perm_wait_wanted" in src


def test_auf_der_karte_gekuerzt_nie_frei(local):
    """Ein Befehl, der für sich passt, aber hinter Name und Verb auf der Karte gekürzt würde."""
    cmd = ("echo " + "wort " * 40)[:notify.PERM_CARD_MAX - 2].strip()
    assert notify.bash_exact({"tool_name": "Bash", "tool_input": {"command": cmd}}) == cmd
    out, fb = _run(_perm(cmd=cmd), [_answer("yes")])
    assert cmd not in fb.created[0]["question"] and out is None
