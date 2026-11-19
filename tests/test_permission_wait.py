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
    ev = {"type": "decision", "id": "d00000000", "verdict": verdict, "by": "Oliver", "number": 3, "question": "q",
          "immutable": True, "rev": 1}
    if via:
        ev["answered_via"] = via
    if role:
        ev["answered_role"] = role
    if signed is not None:
        ev["answered_owner_signed"] = signed
    if text:
        ev["text"] = text
    return ev


class Server:
    """The board behind notify.create (#196 Feldvertrag): records each decide body and answers like v4
    fix/perm-card-immutable (ok, id, rev 1, immutable echoed). `answer` overrides fields."""

    def __init__(self, **answer):
        self.bodies: list[dict] = []
        self.answer = answer

    def __call__(self, job, body):
        self.bodies.append(dict(body))
        if body.get("delete"):
            return {"ok": True}
        out = {"ok": True, "id": "d00000000", "number": 3, "rev": 1}
        if body.get("immutable") is True:
            out["immutable"] = True
        out.update(self.answer)
        return out


def _run(raw, answers, wait_raw=None, wall=T, server=None, monkeypatch=None):
    """The parallel permission-request hook (notify.handle) creates the card for `raw` through the real
    notify.create against `server`; permission-wait then runs for `wait_raw` (default: the same request)."""
    fb, clock = FakeBoard(), Clock()
    srv = server or Server()
    jobs: list = []
    notify.handle(raw, now=T, post=jobs.append)
    assert jobs, "permission-request legte keine Entscheidung an"
    old = notify._decide
    notify._decide = srv
    try:
        notify.create(jobs[0])
    finally:
        notify._decide = old
    shown = next(b["question"] for b in srv.bodies if not b.get("delete"))
    fb.created = [dict(b, id="d00000000") for b in srv.bodies if not b.get("delete")]
    fb.queue = [dict(a, question=shown) if a.get("question") == "q" else a for a in answers]
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


def test_standard_aus_nur_mit_flag_env_zaehlt_nie(tmp_path, monkeypatch):
    s = tmp_path / "settings.json"
    activity.install(s, exe="voicehook-agent-hook")
    assert _perm_hooks(s) == []                                            # Standard: aus
    assert "permission-wait" not in json.dumps(activity.snippet())
    activity.install(s, exe="voicehook-agent-hook", permission_wait=True)
    (h,) = _perm_hooks(s)
    assert h["command"].endswith("permission-wait || true") and h["timeout"] > asks.PERM_WAIT_S
    s2 = tmp_path / "s2.json"
    monkeypatch.setenv("VOICEHOOK_PERMISSION_WAIT", "1")                   # Re-Review 7: Join-Install mit Env
    activity.install(s2, exe="voicehook-agent-hook")
    assert _perm_hooks(s2) == []


def test_flag_wird_gelesen_und_nur_fuer_claude():
    opts, err = activity._install_opts(["--permission-wait"])
    assert not err and opts["permission_wait"] is True
    assert activity.main(["install", "--agent", "codex", "--permission-wait"]) == 2


def test_relay_json_schaltet_nie_ein():
    import inspect
    assert "relay" not in inspect.getsource(activity.perm_wait_wanted)
    assert "environ" not in inspect.getsource(activity.perm_wait_wanted)


# ----- Re-Review Ines (395884b) ----------------------------------------------------------------------------
def test_antwort_mit_anderem_kartentext_nie_frei(local):
    """HOCH 1: the card was rewritten on the server (merge/update/rebind) to something harmless."""
    a = _answer("yes")
    a["question"] = "Hooks will ausführen: ls – erlauben?"
    out, _fb = _run(_perm(cmd="echo; rm -rf ~"), [a])
    assert out is None


def _without_question(ev):
    return {k: v for k, v in ev.items() if k != "question"}


@pytest.mark.parametrize("make", [
    lambda: dict(_answer("yes"), merged=True),     # Server hat die Karte zusammengeführt (Text = unsere Frage)
    lambda: _without_question(_answer("yes")),     # älterer Server ohne question
    lambda: dict(_answer("yes"), question=""),
])
def test_zusammengefuehrt_oder_ohne_kartentext_nie_frei(local, make):
    out, _fb = _run(_perm(), [make()])
    assert out is None
    out, _fb = _run(_perm(sid="other-2", cmd="git push origin dev"), [_answer("yes")])   # Gegenprobe
    assert out == ALLOW


@pytest.mark.parametrize("extra", [{"dangerouslyDisableSandbox": True}, {"run_in_background": True}, {"x": 1}])
def test_nur_erlaubte_schluessel_in_tool_input(local, extra):
    out, _fb = _run(_perm(inp={"command": "git push origin main", "description": "push", "timeout": 5, **extra}),
                    [_answer("yes")])
    assert out is None
    out, _fb = _run(_perm(sid="other-2", inp={"command": "git push origin dev", "description": "d", "timeout": 5}),
                    [_answer("yes")])
    assert out == ALLOW


@pytest.mark.parametrize("cmd", [
    ("curl -u ad" "min:pw https://h"), "export db_password=x", "my_token=abc ./run", "x --pass=y",
    ("curl -H 'Auth" "orization: Basic Zm9v'"), "redis-cli -a geheim", "openssl enc -k geheim",
    "MY_AUTH=1 ./run", "creds_file=x ./run",
])
def test_schwaerzen_re_review(cmd):
    assert notify.perm_scrub(cmd) != cmd
    assert notify.bash_exact({"tool_name": "Bash", "tool_input": {"command": cmd}}) == ""


@pytest.mark.parametrize("cmd", [
    "git push оrigin main",          # kyrillisches o
    "echo \u05d0\u05d1 ls",           # hebräisch
    "echo \u3164 ls",                # Hangul-Füller
    "echo e\u0301 ls",               # Kombinationszeichen
    "echo \u202e ls",                # bidi override
])
def test_nur_ascii_und_druckbar(cmd):
    assert notify.bash_exact({"tool_name": "Bash", "tool_input": {"command": cmd}}) == ""


def test_subagent_nie_frei_und_kein_hinweis(local):
    raw = json.loads(_perm())
    raw["agent_id"] = "sub-1"
    assert notify.can_allow(raw, ms.load_relay()) is False
    assert notify.perm_hash(raw) != notify.perm_hash(json.loads(_perm()))


# ----- every allow gate on its own (Re-Review 8) ---------------------------------------------------------------
def _gate_args(**over):
    event = json.loads(_perm())
    q = "Hooks will ausführen: git push origin main – erlauben?"
    rel = {"kind": ms.LOCAL, "owner_agent": True}
    slot = {"h": notify.perm_hash(event), "id": "d1", "at": T, "imm": {"id": "d1", "rev": 1, "question": q}}
    ev = {"question": q, "id": "d1", "immutable": True, "rev": 1}
    args = {"event": event, "rel": rel, "slot": slot, "question": q, "t0": T, "ev": ev, "dec_id": "d1"}
    args.update(over)
    return args


def test_alle_gates_offen_gibt_frei():
    assert asks.allow_gates(**_gate_args()) == ""


@pytest.mark.parametrize("over,why", [
    ({"rel": {"kind": "remote", "owner_agent": True}}, "remote_relay"),       # Remote mit owner_agent: true
    ({"rel": {"kind": ms.LOCAL, "owner_agent": "1"}}, "no_owner_agent"),
    ({"event": dict(json.loads(_perm()), agent_id="s")}, "subagent"),
    ({"event": json.loads(_perm(cmd="git push origin main\n"))}, ""),         # Rand-Whitespace ist ok
    ({"event": json.loads(_perm(inp={"command": "git push origin main", "run_in_background": True}))},
     "not_exact_bash"),                                                         # Karte passt, Input nicht
    ({"question": "Hooks will ausführen: git push – erlauben?"}, "card_differs"),
    ({"ev": {"question": "Hooks will ausführen: git push origin main – erlauben?", "merged": True}}, "merged"),
    ({"slot": {"id": "d1", "at": T, "imm": None}}, "hash_mismatch"),
    ({"ev": {}}, "answer_text_differs"),
    ({"ev": {"question": "Hooks will ausführen: ls – erlauben?"}}, "answer_text_differs"),
    ({"slot": {"h": "x", "id": "d1", "at": T}}, "hash_mismatch"),
    ({"dec_id": "d2"}, "slot_id_differs"),
    ({"t0": T + asks.PERM_FRESH_S + 1}, "stale_slot"),
    ({"t0": T - asks.PERM_FRESH_S - 1}, "stale_slot"),                          # Slot aus der Zukunft
])
def test_jedes_gate_einzeln(over, why):
    args = _gate_args(**over)
    if "event" in over and why in ("subagent", ""):
        args["slot"] = dict(args["slot"], h=notify.perm_hash(args["event"]))
    assert asks.allow_gates(**args) == why


@pytest.mark.parametrize("at", [float("nan"), float("inf"), "x", None])
def test_slot_zeit_kaputt_nie_frei(at):
    args = _gate_args()
    args["slot"] = dict(args["slot"], at=at)
    assert asks.allow_gates(**args) == "stale_slot"


# ----- #196 Feldvertrag (Ines 19.11., v4 fix/perm-card-immutable) -------------------------------------------
def test_immutable_wird_beim_anlegen_gesendet(local):
    srv = Server()
    out, _fb = _run(_perm(), [_answer("yes")], server=srv)
    (body,) = [b for b in srv.bodies if not b.get("delete")]
    assert body["immutable"] is True and out == ALLOW


def test_ohne_freigabe_moeglichkeit_kein_immutable(relay):
    srv = Server()
    _run(_perm(), [], server=srv)
    assert all("immutable" not in b for b in srv.bodies)                  # Remote: normale Karte


@pytest.mark.parametrize("answer", [{"rev": 2}, {"rev": True}, {"immutable": False}, {"immutable": "true"},
                                    {"merged": True}, {"rev": None}])
def test_anlege_antwort_nicht_unveraenderlich_nie_frei(local, answer):
    srv = Server(**answer)
    out, _fb = _run(_perm(), [_answer("yes")], server=srv)
    assert out is None


@pytest.mark.parametrize("patch", [{"rev": 2}, {"rev": True}, {"rev": "1"}, {"immutable": False},
                                   {"immutable": 1}, {"id": "d99999999"}])
def test_antwort_ereignis_nicht_unveraenderlich_nie_frei(local, patch):
    out, _fb = _run(_perm(), [dict(_answer("yes"), **patch)])
    assert out is None


@pytest.mark.parametrize("drop", ["rev", "immutable"])
def test_antwort_ohne_felder_nie_frei(local, drop):
    ev = {k: v for k, v in _answer("yes").items() if k != drop}
    out, _fb = _run(_perm(), [ev])
    assert out is None


def test_fremde_id_im_slot_nie_frei():
    args = _gate_args()
    args["slot"] = dict(args["slot"], imm={"id": "d9", "rev": 1, "question": args["question"]})
    assert asks.allow_gates(**args) == "not_immutable"
    args = _gate_args(ev={"question": _gate_args()["question"], "id": "d9", "immutable": True, "rev": 1})
    assert asks.allow_gates(**args) == "answer_not_immutable"


def test_relay_reicht_immutable_unveraendert_durch():
    from voicehook_agent import relayops
    assert relayops._valid_decide({"question": "q", "immutable": True}) == {"question": "q", "immutable": True}
    assert relayops._valid_decide({"question": "q", "immutable": "true"}) is None      # kein echter bool
    assert relayops.decide_body({"question": "q", "immutable": True})["immutable"] is True


def test_rpc_body_traegt_immutable():
    import inspect

    from voicehook_agent import cli
    src = inspect.getsource(cli._decide_rpc)
    assert '("immutable", True if req.get("immutable") is True else None)' in src
