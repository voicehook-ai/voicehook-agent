"""0.35.9 Karte T4wKfeaJ: keine "Soll <Name> in den Call?"-Frage für Sessions, die schon im
Call sind (Malik wurde dreimal gefragt). Teilnehmerliste laufend aus den Joins des Raums."""
import json
import socket as so

from test_watch import NOW, Fake, _cand

from voicehook_agent import cli
from voicehook_agent import watch as W
from voicehook_agent import watchjoin as J

BRUNO = "[R] 🛡️ Bruno · Head of Infrastructure & Security · crown, Sessions, Secrets, Security"
MALIK = "Malik · Head of Data Pipeline"
BRUNO_SHORT = "[R] 🛡️ Bruno · Head of Infra & Security"   # <= 40 Zeichen (check_from)


def test_core_and_first_name_of_real_labels():
    assert J.core_name(BRUNO) == "bruno" and J.first_name(BRUNO) == "bruno"
    assert J.core_name(MALIK) == "malik" and J.first_name(MALIK) == "malik"
    assert J.core_name("[R] 🎙️ Ravi · Head of Voice Platform ·") == "ravi"
    assert J.first_name("Nora Schmidt - Calls") == "nora"
    assert J.core_name("🦊") == "" and J.first_name("") == ""


def test_participant_names_agents_only_identity_and_label():
    names = J.participant_names([
        {"identity": "malik-crown-5217", "name": MALIK},
        {"identity": "bruno-crown-0a1b", "name": BRUNO},
        {"identity": "delta-worker-1", "name": "Delta"},
        {"identity": "oliver-web", "name": "Oliver", "human": True},
    ])
    assert names == {"malik", "bruno"}


def test_named_in_call_matches_name_or_first_name():
    names = J.participant_names([{"identity": "malik-crown-5217", "name": MALIK}])
    assert J.named_in_call(MALIK, names)
    assert J.named_in_call("Malik", names)
    assert J.named_in_call("[R] 📊 Malik · Daten", names)
    assert not J.named_in_call(BRUNO, names)
    assert not J.named_in_call("Malik-Bericht 3f2a", names)   # ein fremder Titel, kein Vorname
    assert not J.named_in_call(MALIK, set())


def _live_join(root, room, ident, peers):
    d = root / room / ident
    d.mkdir(parents=True)
    srv = so.socket(so.AF_UNIX, so.SOCK_STREAM)
    srv.bind(str(d / "ctl.sock"))
    srv.listen(1)
    J.write_peers(d / J.PEERS_NAME, ident, peers)
    return d, srv


def test_room_participants_read_live_every_time_dead_join_ignored(tmp_path):
    sess = tmp_path / "s"
    d, srv = _live_join(sess, "r", "nora-crown-1111", [{"identity": "oliver", "name": "Oliver", "human": True}])
    dead = sess / "r" / "kian-crown-2222"
    dead.mkdir(parents=True)
    J.write_peers(dead / J.PEERS_NAME, "kian-crown-2222", [{"identity": "x-y-1", "name": "Geist"}])
    try:
        got = J.room_participants({"room": "r"}, sess_root=sess)
        assert {p["identity"] for p in got} == {"nora-crown-1111", "oliver"}
        # laufender Stand: Malik kommt dazu, die naechste Lesung sieht ihn
        J.write_peers(d / J.PEERS_NAME, "nora-crown-1111",
                      [{"identity": "oliver", "name": "Oliver", "human": True},
                       {"identity": "malik-crown-5217", "name": MALIK}])
        names = J.participant_names(J.room_participants({"room": "r"}, sess_root=sess))
        assert names == {"nora", "malik"}
    finally:
        srv.close()
    assert J.room_participants({"room": ""}, sess_root=sess) == []


def test_no_card_for_session_already_in_participant_list(monkeypatch):
    f = Fake(monkeypatch)
    parts = [{"identity": "malik-crown-5217", "name": MALIK, "human": False}]
    monkeypatch.setattr(J, "room_participants", lambda rel, **k: parts)
    f.unbound = [_cand(key="m1", label=MALIK), _cand(key="b1", label=BRUNO_SHORT, tp="/x/proj/b1.jsonl")]
    w = f.watcher(cmd=None)
    w.tick()
    qs = [p["question"] for p in f.proposals]
    assert len(qs) == 1 and "Bruno" in qs[0] and not any("Malik" in q for q in qs)


def test_three_ticks_never_ask_malik(monkeypatch):
    """Live 09.11.: Malik dreimal gefragt. Der Stand wird jeden Durchlauf neu gelesen."""
    f = Fake(monkeypatch)
    parts: list[dict] = []
    monkeypatch.setattr(J, "room_participants", lambda rel, **k: list(parts))
    monkeypatch.setattr(J, "REPROPOSE_S", 0.0)
    f.unbound = [_cand(key="m1", label=MALIK)]
    w = f.watcher(cmd=None)
    w.tick()
    assert len(f.proposals) == 1           # Positivkontrolle: noch nicht im Call -> gefragt
    parts.append({"identity": "malik-crown-5217", "name": MALIK, "human": False})
    for _ in range(3):
        f.t += 60
        f.unbound = [_cand(key="m1", label=MALIK, at=f.t - 10)]
        w.tick()
    assert len(f.proposals) == 1
    assert f.deleted == ["d00000001"]      # die offene Karte wurde zurueckgezogen
    st = J.load(w.rel)
    assert st["proposed"]["m1"]["answer"] == "im_call"


def test_open_card_withdrawn_when_session_joins(monkeypatch):
    f = Fake(monkeypatch)
    parts: list[dict] = []
    monkeypatch.setattr(J, "room_participants", lambda rel, **k: list(parts))
    f.unbound = [_cand(key="b1", label=BRUNO_SHORT)]
    w = f.watcher(cmd=None)
    w.tick()
    assert len(f.proposals) == 1 and f.deleted == []
    parts.append({"identity": "bruno-crown-0a1b", "name": "", "human": False})   # nur die Identity
    f.t += 30
    w.tick()
    assert f.deleted == ["d00000001"] and len(f.proposals) == 1


def test_call_members_counts_own_join_in_room(monkeypatch):
    monkeypatch.setattr(J, "room_joins", lambda rel, **k: ({"sess-malik"}, set()))
    keys, _ = W.call_members({"room": "r"}, NOW, joins=True)
    assert W.vmulti.session_hash("sess-malik") in keys
    keys, _ = W.call_members({"room": "r"}, NOW)
    assert W.vmulti.session_hash("sess-malik") not in keys   # discover() deckt sie nicht ab


class _P:
    def __init__(self, identity, kind=0, attrs=None, name=""):
        self.identity, self.kind, self.attributes, self.name = identity, kind, attrs or {}, name


class _Room:
    def __init__(self, ps):
        self.remote_participants = {p.identity: p for p in ps}


def test_join_writes_peers_on_every_change(tmp_path, monkeypatch):
    ctl = cli._Control.__new__(cli._Control)
    ctl.ident = "nora-crown-1111"
    ctl.peers_path = tmp_path / J.PEERS_NAME
    monkeypatch.setattr(cli, "_kind_label", lambda p: "user" if p.identity == "oliver" else "standard")
    ctl.write_peers(_Room([_P("oliver", name="Oliver")]))
    d = json.loads(ctl.peers_path.read_text())
    assert d["identity"] == "nora-crown-1111" and d["peers"] == [{"identity": "oliver", "name": "Oliver", "human": True}]
    ctl.write_peers(_Room([_P("oliver", name="Oliver"), _P("malik-crown-5217", attrs={"vh.name": MALIK})]))
    d = json.loads(ctl.peers_path.read_text())
    assert {"identity": "malik-crown-5217", "name": MALIK, "human": False} in d["peers"]
    assert "token" not in ctl.peers_path.read_text()


def test_hooks_installed_at_first_join(tmp_path):
    settings = tmp_path / ".claude" / "settings.json"
    assert cli.ensure_hooks(env={}, settings=settings) is None            # kein Claude Code hier
    settings.parent.mkdir()
    got = cli.ensure_hooks(env={}, settings=settings)
    assert got["_meta"] == "hooks" and got["installed"] is True
    assert cli.vlocal.claude_hooks_installed(settings)
    assert cli.ensure_hooks(env={}, settings=settings) is None            # schon da: still
    other = tmp_path / "o" / ".claude" / "settings.json"
    other.parent.mkdir(parents=True)
    assert cli.ensure_hooks(env={"VOICEHOOK_AUTO_HOOKS": "0"}, settings=other) is None
    assert not other.exists()


def test_hooks_of_other_version_never_touched_but_reported(tmp_path):
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir()
    assert cli.vactivity.install(settings, exe="/alt/bin/voicehook-agent-hook")[0] == 0   # andere Version / Pfad
    before = settings.read_text()
    got = cli.ensure_hooks(env={}, settings=settings)
    assert got["installed"] is False and got["state"] == "other"
    assert "hook install" in got["text"] and "VOICEHOOK_AUTO_HOOKS=0" in got["text"]
    assert settings.read_text() == before                               # nichts geaendert
    # nur ein Teil unserer Hooks: ebenfalls nicht anfassen
    d = json.loads(before)
    d["hooks"] = {"PreToolUse": d["hooks"]["PreToolUse"]}
    settings.write_text(json.dumps(d))
    assert cli.ensure_hooks(env={}, settings=settings)["state"] == "other"
    # fremde Hooks (nicht unsere) zaehlen nicht: dann wird installiert, fremde bleiben
    settings.write_text(json.dumps({"hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": "mein-tool"}]}]}}))
    got = cli.ensure_hooks(env={}, settings=settings)
    assert got["installed"] is True and "mein-tool" in settings.read_text()


def test_first_install_is_visible_on_stderr_in_json_mode(tmp_path, monkeypatch, capsys):
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir()
    monkeypatch.setattr(cli.vactivity, "default_settings_path", lambda: settings)
    got = cli._emit_hooks(True, env={})
    out, err = capsys.readouterr()
    assert got["installed"] is True
    assert "Claude-Code-Einstellungen geändert" in err and "VOICEHOOK_AUTO_HOOKS=0" in err
    assert json.loads(out.strip())["_meta"] == "hooks"
    assert cli._emit_hooks(True, env={}) is None                       # zweiter Join: still
    assert capsys.readouterr() == ("", "")
