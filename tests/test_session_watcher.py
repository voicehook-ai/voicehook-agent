"""0.35.0 Session Watcher (Oliver 29.10.2026): readable names without ids, topics, the
session list with persistent switches, subagents under their session, cards only for real
activity related to the call."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from voicehook_agent import multisession as vmulti
from voicehook_agent import sessionnames as N
from voicehook_agent import watch as W
from voicehook_agent import watchjoin as J
from voicehook_agent import watchsessions as WS

NOW = 1_800_000_000.0
REAL_GET_PICKS = WS.get_picks


def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


def _user(text, ts=NOW - 60, cwd="/home/x/shop", **extra):
    return json.dumps({"type": "user", "timestamp": _iso(ts), "cwd": cwd,
                       "message": {"role": "user", "content": text}, **extra}) + "\n"


def _assistant(text, ts=NOW - 30):
    return json.dumps({"type": "assistant", "timestamp": _iso(ts),
                       "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}) + "\n"


def _session(root, sid, lines, project="-home-x-shop", mtime=NOW - 5):
    d = root / project
    d.mkdir(parents=True, exist_ok=True)
    tp = d / f"{sid}.jsonl"
    tp.write_text("".join(lines))
    os.utime(tp, (mtime, mtime))
    return tp


# ----- names ---------------------------------------------------------------------------
def test_label_never_hex_folder_unique_then_derived_name_wins(tmp_path):
    a = vmulti.session_label({"session_id": "s-a", "cwd": "/home/oliver"})
    b = vmulti.session_label({"session_id": "s-b", "cwd": "/home/oliver"})
    assert (a, b) == ("oliver", "oliver 2")
    assert vmulti.session_label({"session_id": "s-a", "cwd": "/home/oliver"}, "oliver " + vmulti.label_suffix("s-a")) \
        == "oliver"                                       # an old hex label in the cache is dropped
    N.store(vmulti.session_hash("s-a"), NOW, name="Resend-Einrichtung", topic_text="DNS prüfen")
    assert vmulti.session_label({"session_id": "s-a", "cwd": "/home/oliver"}) == "Resend-Einrichtung"
    tr = tmp_path / "t.jsonl"
    tr.write_text(json.dumps({"type": "custom-title", "customTitle": "Mein Name"}) + "\n")
    assert vmulti.session_label({"session_id": "s-a", "cwd": "/x", "transcript_path": str(tr)}) == "Mein Name"


def test_random_slugs_and_hex_never_become_names():
    assert N.is_slug("crown-moonlit-pascal") and not N.is_slug("voicehook-dev-2") and not N.is_slug("SE-UI")
    assert N.parse_derived('{"name": "crown-moonlit-pascal", "thema": "x y"}') == ("", "x y")
    assert N.parse_derived('{"name": "Kartoffel-Lasttest", "thema": "Last messen"}') == ("Kartoffel-Lasttest", "Last messen")
    assert N.parse_derived("kein json") == ("", "")
    assert N.parse_derived('{"name": "a3f9c2d1e0", "thema": "ab12cd34ef nachsehen"}') == ("", "")


def test_orders_skip_tool_results_meta_and_wrappers(tmp_path):
    tp = tmp_path / "s.jsonl"
    tp.write_text(
        _user("<command-name>/clear</command-name>")
        + _user("Richte Resend für die Domain ein")
        + json.dumps({"type": "user", "message": {"content": [{"type": "tool_result", "content": "x"}]}}) + "\n"
        + _user("intern", isMeta=True)
        + _user('<cross-session-message from="bridge:x" from-name="crown-moonlit-pascal">Prüfe die DNS-Einträge</cross-session-message>'))
    first, last = N.orders(str(tp))
    assert first == "Richte Resend für die Domain ein"
    assert last == "Prüfe die DNS-Einträge" and "moonlit" not in last


def test_real_activity_uses_entry_time_not_file_time(tmp_path):
    tp = tmp_path / "s.jsonl"
    tp.write_text(_user("Mach was", ts=NOW - 4000) + json.dumps({"type": "last-prompt"}) + "\n")
    os.utime(tp, (NOW, NOW))
    assert abs(N.real_activity(str(tp)) - (NOW - 4000)) < 1
    tp.write_text(_user("Mach was", ts=NOW - 4000) + _assistant("ok", ts=NOW - 20))
    assert abs(N.real_activity(str(tp)) - (NOW - 20)) < 1


def test_project_of_worktree_is_main_repo(tmp_path):
    wt = tmp_path / "voicehook-v4-watcher"
    wt.mkdir()
    (wt / ".git").write_text(f"gitdir: {tmp_path}/voicehook-v4/.git/worktrees/voicehook-v4-watcher\n")
    assert N.project_of(str(wt)) == "voicehook-v4"
    assert N.project_of(str(wt / "src" / "deep")) == "voicehook-v4"          # below the toplevel
    repo = tmp_path / "nexperts-sales-engine"
    (repo / ".git").mkdir(parents=True)
    assert N.project_of(str(repo / "react-embeds")) == "nexperts-sales-engine"
    clone = tmp_path / "voicehook-v4-call-link-fix"
    (clone / ".git").mkdir(parents=True)
    (clone / ".git" / "config").write_text('[core]\n[remote "origin"]\n\turl = https://github.com/voicehook-ai/voicehook-v4.git\n')
    assert N.project_of(str(clone)) == "voicehook-v4"                         # a second clone: the repository
    assert N.project_of(str(tmp_path / "plain")) == "" and N.project_of("") == ""   # no repository: no project


def test_tradingbot_session_with_stale_title_gets_no_invented_project(monkeypatch, tmp_path):
    """Bug 29.10. (Oliver): the session "Tradingbot-Anbindung" was filed under "Inbox Kunde".
    Its data: cwd = the home directory (no repository), /rename title from an earlier job
    ("[R] Sales Engine · Helfer · Inbox Kunde JEV"), latest ai-title "Trading bots Anbindungen".
    Project only from the git repository of the CURRENT cwd: none here -> '' (page: Sonstiges),
    never from the name. Positive control: a session in a repository keeps its project."""
    home = tmp_path / "home"
    (home / "inbox-macbook").mkdir(parents=True)
    repo = home / "nexperts-sales-engine"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    root = tmp_path / "claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(root))
    p = root / "projects"
    _session(p, "trade1", [_user("Arbeite in der Inbox", cwd=str(home / "inbox-macbook")),
                           json.dumps({"type": "custom-title", "customTitle": "[R] Sales Engine · Helfer · Inbox Kunde JEV"}) + "\n",
                           _user("Binde die Trading-Bots an", cwd=str(home)),
                           json.dumps({"type": "ai-title", "aiTitle": "Trading bots Anbindungen"}) + "\n"],
             project="-home")
    _session(p, "se1", [_user("Fix den Login", cwd=str(repo / "react-embeds"))], project="-home-nexperts")
    f = _Fake(monkeypatch)
    f.watcher().tick()
    by = {e["topic"]: e for e in f.lists[-1]}
    trade = by["Trading bots Anbindungen"]
    assert trade["project"] == "" and "inbox" not in trade["project"].lower()
    se = next(e for e in f.lists[-1] if e is not trade)
    assert se["project"] == "nexperts-sales-engine"                          # positive control
    assert N.project_of(str(home / "inbox-macbook")) == "" and N.project_of(str(home)) == ""


def test_subagents_from_meta_description_only(tmp_path):
    tp = tmp_path / "parent.jsonl"
    tp.write_text(_user("x"))
    d = tmp_path / "parent" / "subagents"
    d.mkdir(parents=True)
    (d / "agent-a1.jsonl").write_text(_user("GEHEIMER PROMPT", isSidechain=True))
    (d / "agent-a1.meta.json").write_text(json.dumps({"description": "Tests reparieren", "finished": False}))
    got = N.subagents(str(tp))
    assert [(s["name"], s["finished"]) for s in got] == [("Tests reparieren", False)]
    assert "GEHEIM" not in json.dumps(got)


# ----- relation + cards ------------------------------------------------------------------
def test_related_same_project_shared_word_or_picked():
    c = {"key": "k", "label": "Resend-Einrichtung", "project": "voicehook-v4"}
    assert J.related(c, {"voicehook-v4"}, set(), set())
    assert J.related({**c, "project": "x"}, set(), {"Resend Doku"}, set())
    assert J.related({**c, "project": "x"}, set(), set(), {"k"})
    assert not J.related({**c, "project": "x"}, {"salesengine"}, {"SE-UI"}, set())   # positive control


class _Fake:
    def __init__(self, monkeypatch, sessions=(), unbound=()):
        self.posts, self.props, self.lists, self.picks = [], [], [], []
        self.rel = {"url": "https://vh.test/api/activity/relay", "token": "tok-" + "a" * 12,
                    "room": "r", "expires_at": NOW + 3600, "owner_pid": 0, "owner_session": ""}
        monkeypatch.setattr(W, "credential", lambda now=None: self.rel)
        monkeypatch.setattr(W, "usable_credentials", lambda now=None: [self.rel])
        monkeypatch.setattr(W, "server_interval", lambda rel: ("ok", None))
        monkeypatch.setattr(W, "post_board", lambda rel, label, body: self.posts.append((label, body)) or {"ok": True})
        monkeypatch.setattr(W, "post_activity", lambda *a: {"ok": True})
        monkeypatch.setattr(W, "get_status", lambda *a: (404, {}))
        monkeypatch.setattr(W, "post_proposal", lambda rel, b: self.props.append(b) or {"ok": True, "id": f"d{len(self.props)}"})
        monkeypatch.setattr(W, "poll_answers", lambda rel: [])
        monkeypatch.setattr(WS, "post_list", lambda rel, entries: self.lists.append(entries) or 200)
        monkeypatch.setattr(WS, "get_picks", lambda rel: (200, [self.picks.pop(0)] if self.picks else []))
        self.sessions = list(sessions)
        self.unbound = list(unbound)

    def watcher(self, **kw):
        return W.Watcher(clock=lambda: NOW, discover_fn=lambda rel, now: self.sessions,
                         unbound_fn=lambda *a, **k: [dict(u) for u in self.unbound], cmd=kw.pop("cmd", None), **kw)


def test_no_card_without_relation_card_with_same_project(monkeypatch, tmp_path):
    root = tmp_path / "claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(root))
    (tmp_path / "shop" / ".git").mkdir(parents=True)                     # the session works in a repository
    mine = _session(root / "projects", "mine", [_user("Baue die Seite", cwd=str(tmp_path / "shop"))])
    cand = {"key": "u1", "label": "Fremd", "tp": str(tmp_path / "u1.jsonl"), "active_at": NOW - 30,
            "project": "other", "folder": "other"}
    f = _Fake(monkeypatch, sessions=[{"key": "m", "label": "Shop", "agent": "claude", "tp": str(mine),
                                      "ev": "", "ev_text": "", "ev_at": 0.0}], unbound=[cand])
    w = f.watcher()
    w.tick()
    assert f.props == []                                   # other project, no shared word
    f.unbound = [{**cand, "key": "u2", "project": "shop"}]
    w.tick()
    assert len(f.props) == 1 and f.props[0]["question"].startswith("Soll Fremd mit Thema ")
    assert f.props[0]["question"].endswith(" in den Call?")


def test_card_in_english_when_the_call_is_english(monkeypatch):
    f = _Fake(monkeypatch, unbound=[{"key": "u1", "label": "Shop", "tp": "/x/u1.jsonl", "active_at": NOW - 30}])
    w = f.watcher(related_fn=lambda *a: True)
    w.lang = "en"
    monkeypatch.setattr(W, "server_interval", lambda rel: ("old", None))
    w.tick()
    assert f.props[0]["question"].startswith("Should Shop join the call with topic ")


def test_derive_names_model_once_per_new_order_no_content_in_log(monkeypatch, tmp_path):
    tp = tmp_path / "s1.jsonl"
    tp.write_text(_user("Richte Resend für unsere Domain ein, Zugang liegt im Tresor"))
    calls = []

    def run(cmd, input=None, **kw):
        calls.append(input)

        class R:
            returncode = 0
            stdout = json.dumps({"result": '{"name": "Resend-Einrichtung", "thema": "Domain einrichten"}',
                                 "usage": {"input_tokens": 300, "output_tokens": 20}})
        return R()
    monkeypatch.setattr(W.subprocess, "run", run)
    f = _Fake(monkeypatch)
    w = f.watcher(cmd=["fake"])
    s = {"key": "k1", "label": "oliver", "tp": str(tp), "agent": "claude"}
    w.derive_names([s], NOW)
    w.derive_names([s], NOW + 60)                          # same order: no second call
    assert len(calls) == 1 and "Resend" in calls[0]
    assert N.derived_name("k1") == "Resend-Einrichtung" and N.topic("k1") == "Domain einrichten"
    logtxt = (W.vact.state_root() / W.LOG_NAME).read_text()
    assert "Tresor" not in logtxt and "Resend" not in logtxt
    assert W.fixed_sentence(W.ARBEITET, 10, N.topic("k1")) == "Arbeitet gerade am Thema Domain einrichten."


# ----- session list ----------------------------------------------------------------------
def test_list_groups_data_subagents_under_parent_and_picks_persist(monkeypatch, tmp_path):
    root = tmp_path / "claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(root))
    p = root / "projects"
    tp = _session(p, "main1", [_user("Baue die Seite"), _assistant("mache ich")])
    old = _session(p, "old1", [_user("Alte Sache", ts=NOW - 3 * 86400)], mtime=NOW - 3 * 86400)
    _session(p, "ancient", [_user("x")], mtime=NOW - 30 * 86400)          # older than LIST_DAYS
    d = tp.parent / "main1" / "subagents"
    d.mkdir(parents=True)
    (d / "agent-z9.jsonl").write_text(_assistant("ok", ts=NOW - 10))
    os.utime(d / "agent-z9.jsonl", (NOW - 10, NOW - 10))
    (d / "agent-z9.meta.json").write_text(json.dumps({"description": "Tests reparieren", "finished": False}))
    old_sub = d / "agent-old.jsonl"                                       # finished long ago: not listed
    old_sub.write_text(_assistant("x", ts=NOW - 86400))
    os.utime(old_sub, (NOW - 86400, NOW - 86400))
    (d / "agent-old.meta.json").write_text(json.dumps({"description": "Alt", "finished": True}))
    f = _Fake(monkeypatch)
    w = f.watcher()
    w.tick()
    entries = f.lists[-1]
    names = [e["name"] for e in entries]
    assert names == ["shop", "Tests reparieren", "shop 2"]
    top, sub, older = entries
    assert sub["parent"] == top["id"] and sub["parent_name"] == "shop" and sub["agent"] == "subagent"
    assert sub["topic"] == ""                              # the description is the name already
    assert top["project"] == sub["project"] == ""   # no repository: no project; subagents inherit
    assert older["last_active"] < NOW - 2 * 86400 and top["on"] is False and top["in_call"] is False
    assert all("main1" not in json.dumps(e) and "/" not in e["name"] for e in entries)   # no ids, no paths
    # switch on in the list: kept on the machine, subscribed now
    f.picks = [{"id": top["id"], "on": True, "at": NOW}]
    w.tick()
    key = vmulti.session_hash("main1")
    assert N.picks()[key] is True and key in J.load(w.pick(NOW))["subscribed"]
    # a new call (new watcher, empty call state): switched-on sessions join by themselves
    J.clear()
    w2 = f.watcher()
    w2.tick()
    w2.tick()
    assert key in J.load(w2.pick(NOW))["subscribed"]
    # switch off: never covered, never proposed
    f.picks = [{"id": top["id"], "on": False, "at": NOW}]
    w2.tick()
    assert N.picks()[key] is False and key not in J.load(w2.pick(NOW))["subscribed"]
    assert str(old)  # listed above as "shop 2"


def test_subagent_picked_shows_belongs_to_and_gets_no_card(monkeypatch, tmp_path):
    root = tmp_path / "claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(root))
    tp = _session(root / "projects", "main1", [_user("Baue die Seite")])
    d = tp.parent / "main1" / "subagents"
    d.mkdir(parents=True)
    (d / "agent-z9.jsonl").write_text(_assistant("ok", ts=NOW - 10))
    os.utime(d / "agent-z9.jsonl", (NOW - 10, NOW - 10))
    (d / "agent-z9.meta.json").write_text(json.dumps({"description": "Tests reparieren"}))
    f = _Fake(monkeypatch)
    w = f.watcher(related_fn=lambda *a: True)
    w.tick()
    sub = next(e for e in f.lists[-1] if e["parent"])
    f.picks = [{"id": sub["id"], "on": True, "at": NOW}]
    w.tick()
    w.tick()
    boards = [(lab, b) for lab, b in f.posts if lab.startswith("Tests reparieren")]
    assert boards and boards[-1][1]["doing"].startswith("gehört zu: shop.")
    assert not any("Tests reparieren" in p["question"] for p in f.props)


def test_list_route_missing_is_quiet(monkeypatch):
    f = _Fake(monkeypatch)
    monkeypatch.setattr(WS, "post_list", lambda rel, entries: 404)
    w = f.watcher()
    w.lister.sig = ""
    w.duty_sessions(f.rel, [], NOW)
    assert w.lister.off_until > NOW


def test_kanban_403_said_once_then_rests(monkeypatch):
    f = _Fake(monkeypatch)
    acts = []
    monkeypatch.setattr(W, "post_activity", lambda rel, label, text: acts.append((label, text)) or {"ok": True})
    w = f.watcher()
    w.rel = f.rel
    assert w._kanban_status(403, NOW, "Shop") is True
    assert w._kanban_status(403, NOW + 1, "Shop") is True
    assert len(acts) == 1 and acts[0][0] == "Shop (Session Watcher)"
    assert acts[0][1].startswith("Kanban für Shop nicht gepflegt, weil der Zugang keinen Agenten-Schlüssel hat.")
    assert w.kanban_off["Shop"] > NOW
    s = {"key": "k", "label": "Shop", "agent": "claude", "tp": "/x.jsonl"}
    assert w.duty_kanban(f.rel, s, "Shop", W.ARBEITET, {}, NOW + 2) is False   # rests, no request


def test_account_saved_choice_is_taken_over(monkeypatch, tmp_path):
    """0.35.0 multi-device: the server keeps the switch on the account ("saved"); a fresh
    machine state (reinstall, new call) takes it over once."""
    root = tmp_path / "claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(root))
    _session(root / "projects", "main1", [_user("Baue die Seite")])
    f = _Fake(monkeypatch)
    w = f.watcher()
    w.tick()
    sid = f.lists[-1][0]["id"]
    monkeypatch.setattr(W.vrelayops, "_http", lambda rel, m, url, body, to: (200, {"picks": [], "saved": {sid: True}}))
    _st, got = REAL_GET_PICKS(f.rel)
    assert got == [{"id": sid, "on": True, "saved": True}]
    monkeypatch.setattr(WS, "get_picks", lambda rel: (200, got))
    w.tick()
    assert N.picks()[vmulti.session_hash("main1")] is True


# ----- 0.35.4: answers of resting sessions, info cards -------------------------------------
def _covered(tp, key="k1", label="Resend-Einrichtung", ev="stop", ev_at=NOW - 400):
    return {"key": key, "label": label, "agent": "claude", "tp": str(tp), "ev": ev, "ev_text": "", "ev_at": ev_at}


def test_resting_session_answers_go_to_its_local_inbox(monkeypatch, tmp_path):
    """Karten abräumen: a session that finished its turn runs no hook; its device's watcher
    fetches the answers into the session's local inbox (the server acknowledges the fetch).
    Positive control: an active session is never fetched for."""
    from voicehook_agent import relayops as R
    tp = tmp_path / "s.jsonl"
    tp.write_text(_user("Richte Resend ein", ts=NOW - 900) + _assistant("fertig", ts=NOW - 400))
    os.utime(tp, (NOW - 400, NOW - 400))
    calls = []
    monkeypatch.setattr(R, "remote_next", lambda rel, frm, t: calls.append(frm) or
                        [{"type": "decision", "id": "d1", "verdict": "yes"}])
    f = _Fake(monkeypatch, sessions=[_covered(tp)])
    w = f.watcher()
    w.tick()
    assert calls == ["Resend-Einrichtung"]
    inbox = R.take_jsonl(R.inbox_path("Resend-Einrichtung"), lambda o: True)
    assert inbox == [{"type": "decision", "id": "d1", "verdict": "yes"}]     # never lost
    w.tick()
    assert calls == ["Resend-Einrichtung"]                                   # not again within DELIVER_EVERY_S
    calls.clear()
    tp.write_text(_user("Weiter", ts=NOW - 10) + _assistant("arbeite", ts=NOW - 5))
    os.utime(tp, (NOW - 5, NOW - 5))
    f2 = _Fake(monkeypatch, sessions=[_covered(tp, key="k2", label="Aktiv", ev="", ev_at=0.0)])
    f2.watcher().tick()
    assert calls == []                                                       # active: its hooks fetch


def test_info_card_once_per_finished_order_with_own_links_only(monkeypatch, tmp_path):
    from voicehook_agent import relayops as R
    tp = tmp_path / "s.jsonl"
    cards = []
    monkeypatch.setattr(R, "remote_decide", lambda rel, frm, body: cards.append((frm, body)) or {"ok": True, "id": "i1"})
    monkeypatch.setattr(R, "remote_next", lambda *a: [])
    tp.write_text(_user("Baue den Resend-Versand", ts=NOW - 600)
                  + _assistant("PR https://github.com/o/r/pull/42 ist gemergt, siehe https://evil.example/x", ts=NOW - 5))
    os.utime(tp, (NOW - 5, NOW - 5))
    t = [NOW]
    f = _Fake(monkeypatch, sessions=[_covered(tp, ev="", ev_at=0.0)])
    w = W.Watcher(clock=lambda: t[0], discover_fn=lambda rel, now: f.sessions, unbound_fn=lambda *a, **k: [], cmd=None)
    w.tick()                                   # arbeitet (since NOW)
    t[0] = NOW + 200
    f.sessions = [_covered(tp, ev="stop", ev_at=NOW + 190)]
    w.tick()                                   # arbeitet -> still after 200 s of work
    assert len(cards) == 1
    frm, body = cards[0]
    assert frm == "Resend-Einrichtung (Session Watcher)" and body["card_type"] == "done"
    assert body["title"].startswith("Die Session Resend-Einrichtung hat den Auftrag ") and body["ttl_s"] == W.INFO_TTL_S
    assert body["evidence"] == [{"label": "Pull Request 42", "url": "https://github.com/o/r/pull/42"}]   # no foreign link
    # same order again: no second card; a quick turn (< INFO_MIN_WORK_S): none
    t[0] += 100
    f.sessions = [_covered(tp, ev="", ev_at=0.0)]
    os.utime(tp, (t[0], t[0]))
    w.tick()
    t[0] += 30
    f.sessions = [_covered(tp, ev="stop", ev_at=t[0])]
    w.tick()
    assert len(cards) == 1
