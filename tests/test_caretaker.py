"""0.34.0 caretaker: connect (device flow), daemon loop, helper binding, autostart files."""
from __future__ import annotations

import json
import os
import stat
from typing import ClassVar

import pytest

from voicehook_agent import caretaker as C
from voicehook_agent import device as D
from voicehook_agent import multisession as vmulti
from voicehook_agent import watch as W

NOW = 1_800_000_000.0
BASE = "https://vh.test"
DEVTOK = "dv-" + "x" * 16     # fake, only compared, never printed
RELTOK = "rl-" + "y" * 16
HELTOK = "hl-" + "z" * 16


class Net:
    """Fake server for device.http_request."""

    def __init__(self):
        self.polls = []            # scripted poll answers (status, body)
        self.calls = []            # scripted /calls answers: list of call lists or ints (status)
        self.log = []
        self.cred_status = 200
        self.exp = NOW + 3600

    def __call__(self, method, url, body=None, token="", timeout=15.0):
        path = url.replace(BASE, "")
        self.log.append((method, path.split("?")[0], token, body))
        if path == "/api/device/start":
            return 200, {"device_code": "DC1", "user_code": "ABCD-EFGH",
                         "verify_url": BASE + "/device", "interval": 1, "expires_in": 600}
        if path == "/api/device/poll":
            return self.polls.pop(0) if self.polls else (428, {})
        if token != DEVTOK:
            return 401, {}
        if path.startswith("/api/device/calls?"):
            r = self.calls.pop(0) if len(self.calls) > 1 else self.calls[0]
            return (r, {}) if isinstance(r, int) else (200, {"calls": r})
        if path.endswith("/caretaker"):
            return self.cred_status, {"url": BASE + "/api/activity/relay", "token": RELTOK,
                                      "expires_at": self.exp}
        if path.endswith("/helpers"):
            return 200, {"helpers": [{"label": l, "url": BASE + "/api/activity/relay",
                                      "token": HELTOK, "expires_at": self.exp}
                                     for l in body["labels"]]}
        return 404, {}


def _connect(net, **kw):
    out = []
    t = [NOW]
    rc = D.connect("box", http=net, sleep=lambda s: t.__setitem__(0, t[0] + s), clock=lambda: t[0],
                   out=out.append, open_url=kw.get("open_url"), env={"VOICEHOOK_API": BASE})
    return rc, out


def test_connect_pending_then_token_0600():
    net = Net()
    net.polls = [(428, {}), (428, {}), (200, {"device_token": DEVTOK, "account_hint": "o***@x.de"})]
    opened = []
    rc, out = _connect(net, open_url=opened.append)
    assert rc == 0
    assert opened == [BASE + "/device"]
    assert any("ABCD-EFGH" in l and BASE + "/device" in l for l in out)
    p = D.device_path()
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(p.parent).st_mode) == 0o700
    assert D.load_device()["device_token"] == DEVTOK
    assert DEVTOK not in "\n".join(out) and "DC1" not in "\n".join(out)


def test_connect_denied_and_expired():
    for status, word in ((403, "denied"), (410, "expired")):
        net = Net()
        net.polls = [(status, {})]
        rc, out = _connect(net)
        assert rc == 1 and word in out[-1]
        assert D.load_device() is None


def test_connect_expires_by_clock():
    net = Net()          # always pending
    rc, out = _connect(net)
    assert rc == 1 and "expired" in out[-1]


def test_public_view_has_no_token():
    D.save_device(DEVTOK, "hint", BASE, "box", NOW)
    assert DEVTOK not in json.dumps(D.public_view(D.load_device()))
    assert D.clear_device() and D.load_device() is None


def test_device_json_group_readable_is_ignored():
    p = D.save_device(DEVTOK, "h", BASE, "box", NOW)
    os.chmod(p, 0o644)
    assert D.load_device() is None


def test_http_error_text_has_no_token_or_url(monkeypatch):
    import urllib.request

    def boom(*a, **k):
        raise OSError(f"{BASE}/x {DEVTOK}")
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    try:
        D.http_request("GET", BASE + "/x", None, DEVTOK)
    except D.DeviceError as e:
        assert DEVTOK not in str(e) and BASE not in str(e) and e.status == 0
    else:
        raise AssertionError


def test_api_base_rejects_plain_http():
    import pytest
    with pytest.raises(D.DeviceError):
        D.api_base({"VOICEHOOK_API": "http://evil.example"})
    assert D.api_base({"VOICEHOOK_API": "http://localhost:8080/"}) == "http://localhost:8080"


# ------------------------------------------------------------------ daemon
class FakeWatcher:
    instances: ClassVar[list] = []

    def __init__(self, **kw):
        self.kw = kw
        self.fails = 0
        self.ticks = 0
        self.shut = 0
        self.result = W.OK
        FakeWatcher.instances.append(self)

    def tick(self):
        self.ticks += 1
        return self.result

    def delay(self):
        return 45.0

    def shutdown(self):
        self.shut += 1


class Clock:
    def __init__(self):
        self.t = NOW

    def __call__(self):
        return self.t


def _caretaker(net, clk, fd=7):
    FakeWatcher.instances = []
    D.save_device(DEVTOK, "h", BASE, "box", NOW)
    c = C.Caretaker(clock=clk, http=net, watcher_factory=FakeWatcher, lock_fn=lambda: os.dup(2))
    return c


def _call(room, started=NOW, present=True):
    return {"room": room, "started_at": started, "owner_present": present}


def test_loop_starts_ticks_and_stops_per_call():
    net, clk = Net(), Clock()
    c = _caretaker(net, clk)
    net.calls = [[]]
    c.step()
    assert c.watcher is None
    net.calls = [[_call("r1")]]
    c.step()
    w = FakeWatcher.instances[0]
    assert c.room == "r1" and w.ticks == 1
    assert w.kw["cred_fn"](clk())["token"] == RELTOK
    assert w.kw["cred_fn"](clk())["call_id"] == "r1"
    clk.t += 10
    c.step()
    assert w.ticks == 1            # not due yet (45 s)
    clk.t += 40
    c.step()
    assert w.ticks == 2
    net.calls = [[]]               # call gone
    c.step()
    assert c.watcher is None and w.shut == 1 and c.room == ""


def test_owner_not_present_is_not_served_and_newest_wins():
    net, clk = Net(), Clock()
    c = _caretaker(net, clk)
    net.calls = [[_call("old", NOW - 100), _call("idle", NOW + 5, present=False)]]
    c.step()
    assert c.room == "old"
    net.calls = [[_call("old", NOW - 100), _call("new", NOW)]]
    c.step()
    assert c.room == "new"
    assert FakeWatcher.instances[0].shut == 1 and len(FakeWatcher.instances) == 2


def test_revoked_device_token_is_forgotten_and_daemon_idles():
    net, clk = Net(), Clock()
    c = _caretaker(net, clk)
    net.calls = [401]
    assert c.step() == C.IDLE_S
    assert D.load_device() is None
    assert c.step() == C.IDLE_S


def test_network_error_backs_off_and_keeps_call():
    net, clk = Net(), Clock()
    c = _caretaker(net, clk)
    net.calls = [[_call("r1")]]
    c.step()
    net.calls = [503]
    d = c.step()
    assert d >= 10 and c.room == "r1"
    d2 = c.step()
    assert d2 > d


def test_watcher_end_retries_later_with_fresh_credential():
    net, clk = Net(), Clock()
    c = _caretaker(net, clk)
    net.calls = [[_call("r1")]]
    c.step()
    FakeWatcher.instances[0].result = W.END
    net.cred_status = 503           # renewal fails too: the watcher ends
    clk.t += 50
    c.step()
    assert c.watcher is None
    net.cred_status = 200
    c.step()
    assert c.watcher is None        # retry gap
    clk.t += C.RETRY_MAX_S + 1
    c.step()
    assert c.watcher is not None and len(FakeWatcher.instances) == 2


def test_watch_lock_busy_does_not_start():
    net, clk = Net(), Clock()
    D.save_device(DEVTOK, "h", BASE, "box", NOW)
    c = C.Caretaker(clock=clk, http=net, watcher_factory=FakeWatcher, lock_fn=lambda: None)
    net.calls = [[_call("r1")]]
    c.step()
    assert c.watcher is None
    assert not any(l[1].endswith("/caretaker") for l in net.log)


def test_credential_renewed_before_expiry_keeps_call_identity():
    net, clk = Net(), Clock()
    net.exp = NOW + 700
    c = _caretaker(net, clk)
    net.calls = [[_call("r1")]]
    c.step()
    clk.t += 200
    net.exp = NOW + 5000
    c.step()
    rel = c.rel
    assert rel["expires_at"] == NOW + 5000 and rel["call_id"] == "r1"


def test_bind_writes_helpers_0600_and_cleans_on_end():
    net, clk = Net(), Clock()
    c = _caretaker(net, clk)
    net.calls = [[_call("r1")]]
    c.step()
    c.bind(c.rel, [{"key": "k1", "label": "api 1a2b", "tp": ""},
                   {"key": "k2", "label": "web 3c4d", "tp": ""}])
    files = sorted(vmulti.helpers_dir().glob("*.json"))
    assert [f.name for f in files] == ["api-1a2b.json", "web-3c4d.json"]
    for f in files:
        assert stat.S_IMODE(os.stat(f).st_mode) == 0o600
        d = json.loads(f.read_text())
        assert d["room"] == "r1" and d["token"] == HELTOK
    n = sum(1 for l in net.log if l[1].endswith("/helpers"))
    c.bind(c.rel, [{"key": "k1", "label": "api 1a2b", "tp": ""}])
    assert sum(1 for l in net.log if l[1].endswith("/helpers")) == n    # fresh: no request
    net.calls = [[]]
    c.step()
    assert list(vmulti.helpers_dir().glob("*.json")) == []


def test_bind_uses_registry_label_of_the_session(tmp_path):
    net, clk = Net(), Clock()
    c = _caretaker(net, clk)
    net.calls = [[_call("r1")]]
    c.step()
    tp = str(tmp_path / "s.jsonl")
    st = W.vact.state_root() / vmulti.STATE_NAME
    st.write_text(json.dumps({"sessions": {"k1": {"label": "Crown-Fix", "tp": tp}}}))
    c.bind(c.rel, [{"key": "k1", "label": "tmp 9f9f", "tp": tp}])
    assert [f.name for f in vmulti.helpers_dir().glob("*.json")] == ["Crown-Fix.json"]


def test_log_has_no_token(tmp_path):
    net, clk = Net(), Clock()
    c = _caretaker(net, clk)
    net.calls = [[_call("r1")]]
    c.step()
    c.bind(c.rel, [{"key": "k1", "label": "api 1a2b", "tp": ""}])
    text = (W.vact.state_root() / C.LOG_NAME).read_text()
    assert text and DEVTOK not in text and RELTOK not in text and HELTOK not in text


# ------------------------------------------------------------------ watcher integration
def test_watcher_uses_injected_credential_and_binds_on_ja(monkeypatch):
    rel = {"url": BASE + "/api/activity/relay", "token": RELTOK, "room": "r1", "call_id": "r1",
           "expires_at": NOW + 3600, "written_at": NOW, "owner_pid": 0, "owner_session": ""}
    bound = []
    monkeypatch.setattr(W, "server_interval", lambda r: ("ok", None))
    monkeypatch.setattr(W, "credential", lambda now=None: None)     # classic path must not be used
    cands = [{"key": f"k{i}", "label": f"s{i}", "tp": f"/t/{i}.jsonl", "active_at": NOW} for i in (1, 2)]
    posted = []
    monkeypatch.setattr(W, "post_proposal", lambda r, b: (posted.append(b), {"ok": True, "id": "D1"})[1])
    answers = []
    monkeypatch.setattr(W, "poll_answers", lambda r: answers)
    monkeypatch.setattr(W, "delete_decision", lambda *a: {"ok": True})
    monkeypatch.setattr(W, "post_board", lambda *a: {"ok": True, "status": 200})
    monkeypatch.setattr(W, "get_status", lambda *a: (404, {}))
    w = W.Watcher(clock=lambda: NOW, discover_fn=lambda r, n: [], cmd=None, related_fn=lambda *a: True,
                  unbound_fn=lambda *a, **k: cands, cred_fn=lambda t=None: rel,
                  bind_fn=lambda r, items: bound.append(sorted(i["label"] for i in items)))
    monkeypatch.setattr(W.J, "subscribed_sessions", lambda r, taken: [])
    assert w.tick() == W.OK
    assert posted and posted[0]["question"].startswith("Soll s1 mit Thema ")   # 0.35.0: single cards
    answers.append({"type": "decision", "id": "D1", "verdict": "yes"})
    assert w.tick() == W.OK
    assert any(b and b[0] in ("s1", "s2") for b in bound)


def test_fallback_without_device_json_classic_spawn(monkeypatch):
    # no device.json: caretaker is not active, the classic detached watcher may start
    assert D.load_device() is None and D.caretaker_active() is False
    monkeypatch.delenv("VOICEHOOK_WATCH", raising=False)
    monkeypatch.setattr(W, "credential", lambda now=None: {"x": 1})
    started = []
    assert W.spawn_detached(popen=lambda *a, **k: started.append(a)) is True
    assert started


def test_classic_spawn_yields_to_running_caretaker(monkeypatch):
    D.save_device(DEVTOK, "h", BASE, "box", NOW)
    monkeypatch.delenv("VOICEHOOK_WATCH", raising=False)
    monkeypatch.setattr(W, "credential", lambda now=None: {"x": 1})
    fd = D.acquire_daemon_lock()
    try:
        assert D.caretaker_active() is True
        assert W.spawn_detached(popen=lambda *a, **k: (_ for _ in ()).throw(AssertionError)) is False
    finally:
        os.close(fd)


# ------------------------------------------------------------------ autostart
class Runner:
    def __init__(self, linger="Linger=yes", rc=0):
        self.cmds = []
        self.linger, self.rc = linger, rc

    def __call__(self, cmd, **kw):
        self.cmds.append(cmd)

        class R:
            returncode = self.rc
            stdout = self.linger if cmd[0] == "loginctl" else ""
        return R()


def test_unit_file_content_has_no_token(tmp_path):
    runner, out = Runner(linger="Linger=no"), []
    D.save_device(DEVTOK, "h", BASE, "box", NOW)
    rc = C.install(home=tmp_path, platform="linux", runner=runner, argv=["/x/voicehook-agent", "caretaker", "run"],
                   env={"PATH": "/usr/bin", "VOICEHOOK_API": BASE}, out=out.append)
    assert rc == 0
    text = C.unit_path(tmp_path).read_text()
    assert "Restart=always" in text and 'ExecStart="/x/voicehook-agent" "caretaker" "run"' in text
    assert "Environment=\"PATH=/usr/bin\"" in text
    assert DEVTOK not in text and RELTOK not in text and "token" not in text.lower()
    assert ["systemctl", "--user", "enable", "--now", C.UNIT_NAME] in runner.cmds
    assert any("enable-linger" in l for l in out)


def test_launchd_plist_and_uninstall(tmp_path):
    import plistlib
    runner = Runner()
    assert C.install(home=tmp_path, platform="darwin", runner=runner, argv=["/x/va", "caretaker", "run"],
                     env={"PATH": "/usr/bin"}, out=lambda s: None, uid=501) == 0
    d = plistlib.loads(C.plist_path(tmp_path).read_bytes())
    assert d["KeepAlive"] is True and d["ProgramArguments"] == ["/x/va", "caretaker", "run"]
    assert ["launchctl", "bootstrap", "gui/501", str(C.plist_path(tmp_path))] in runner.cmds
    assert C.installed(tmp_path, "darwin")
    assert C.uninstall(home=tmp_path, platform="darwin", runner=runner, out=lambda s: None, uid=501) == 0
    assert not C.installed(tmp_path, "darwin")


def test_uninstall_linux_and_status(tmp_path):
    runner, out = Runner(), []
    C.install(home=tmp_path, platform="linux", runner=runner, argv=["/x/va"], env={"PATH": "/b"}, out=out.append)
    assert C.installed(tmp_path, "linux")
    D.save_device(DEVTOK, "acct", BASE, "box", NOW)
    lines = []
    v = C.status(home=tmp_path, platform="linux", out=lines.append)
    assert v["connected"] and v["autostart"] and v["daemon"] is False
    assert DEVTOK not in "".join(lines) and "installed" in lines[0]
    C.uninstall(home=tmp_path, platform="linux", runner=runner, out=out.append)
    assert not C.installed(tmp_path, "linux")
    assert ["systemctl", "--user", "disable", "--now", C.UNIT_NAME] in runner.cmds


def test_cli_connect_status_disconnect(capsys, monkeypatch):
    from voicehook_agent import cli
    D.save_device(DEVTOK, "acct", BASE, "box", NOW)
    monkeypatch.setattr("sys.argv", ["voicehook-agent", "connect", "--status"])
    try:
        cli.main()
    except SystemExit as e:
        assert e.code == 0
    o = capsys.readouterr().out
    assert "yes (acct)" in o and DEVTOK not in o
    monkeypatch.setattr("sys.argv", ["voicehook-agent", "connect", "--disconnect"])
    try:
        cli.main()
    except SystemExit:
        pass
    assert D.load_device() is None


# ------------------------------------------------------------------ server deviations (PR 758)
def test_poll_429_slow_down_backs_off_interval():
    net = Net()
    net.polls = [(429, {"status": "slow_down"}), (200, {"device_token": DEVTOK, "account_hint": "h"})]
    sleeps, t = [], [NOW]

    def sl(s):
        sleeps.append(s)
        t[0] += s
    rc = D.connect("box", http=net, sleep=sl, clock=lambda: t[0], out=lambda s: None,
                   open_url=lambda u: None, env={"VOICEHOOK_API": BASE})
    assert rc == 0 and sleeps == [1.0, 6.0]


def test_poll_unknown_code_410_is_expired():
    net = Net()
    net.polls = [(410, {})]
    rc, out = _connect(net)
    assert rc == 1 and "expired" in out[-1]


def test_caretaker_409_and_429_keep_the_call_and_retry_later():
    for status, first_gap in ((409, C.SOON_S), (429, C.RETRY_START_S)):
        net, clk = Net(), Clock()
        c = _caretaker(net, clk)
        net.cred_status = status
        net.calls = [[_call("r1")]]
        c.step()
        assert c.watcher is None
        c.step()
        assert c.watcher is None                      # still within the gap
        net.cred_status = 200
        clk.t += first_gap + 1
        c.step()
        assert c.watcher is not None and c.room == "r1"


def test_helpers_409_backs_off_without_dropping_the_call(monkeypatch):
    net, clk = Net(), Clock()
    c = _caretaker(net, clk)
    net.calls = [[_call("r1")]]
    c.step()
    real = net.__call__
    state = {"status": 409}

    def http(method, url, body=None, token="", timeout=15.0):
        if url.endswith("/helpers") and state["status"]:
            return state["status"], {}
        return real(method, url, body, token, timeout)
    c.http = http
    items = [{"key": "k1", "label": "api 1a2b", "tp": ""}]
    c.bind(c.rel, items)
    assert list(vmulti.helpers_dir().glob("*.json")) == [] and c.watcher is not None
    state["status"] = 0
    c.bind(c.rel, items)                               # inside the backoff: no request
    assert list(vmulti.helpers_dir().glob("*.json")) == []
    clk.t += C.BIND_BACKOFF_S + 1
    c.bind(c.rel, items)
    assert len(list(vmulti.helpers_dir().glob("*.json"))) == 1


def test_410_after_server_restart_gets_new_credential_while_call_listed():
    net, clk = Net(), Clock()
    c = _caretaker(net, clk)
    net.calls = [[_call("r1")]]
    c.step()
    w = FakeWatcher.instances[0]
    w.result = W.END                                   # relay answered 410 three times
    clk.t += 50
    n = sum(1 for l in net.log if l[1].endswith("/caretaker"))
    c.step()
    assert sum(1 for l in net.log if l[1].endswith("/caretaker")) == n + 1
    assert c.watcher is w and w.shut == 0 and c.room == "r1"
    net.calls = [[]]                                   # /calls no longer lists it: now it is over
    c.step()
    assert c.watcher is None and w.shut == 1


# ------------------------------------------------------------------ Ja holt die Session (Bug 28.10.)
def test_step_never_hammers_calls_when_server_answers_at_once():
    """Live 28.10.: a running call made GET /calls?wait=25 return at once and step() returned 0
    -> ~400 polls a minute, the device rate limit (600/10 min) empty, every /helpers 429."""
    net, clk = Net(), Clock()
    c = _caretaker(net, clk)
    net.calls = [[_call("r1")]]
    c.step()                                       # starts the call, ticks once
    d = c.step()                                   # server answered at once (clock unchanged)
    assert d >= C.CALLS_MIN_GAP_S - 0.01 or d == pytest.approx(c.next_tick - clk.t)
    assert d > 0
    gets = [l for l in net.log if l[1] == "/api/device/calls"]
    assert len(gets) == 2


def test_long_poll_wait_ends_at_the_next_tick():
    net, clk = Net(), Clock()
    seen = []
    real = net.__call__

    def http(method, url, body=None, token="", timeout=15.0):
        if "wait=" in url:
            seen.append(int(url.split("wait=")[1]))
        return real(method, url, body, token, timeout)
    c = _caretaker(net, clk)
    c.http = http
    net.calls = [[_call("r1")]]
    c.step()                                       # tick at NOW, next tick NOW + 45
    clk.t += 30
    c.step()
    assert seen[0] == D.CALLS_WAIT_S and seen[1] == 15   # waits only until the tick is due


def test_bind_returns_the_reason_per_label(monkeypatch):
    net, clk = Net(), Clock()
    c = _caretaker(net, clk)
    net.calls = [[_call("r1")]]
    c.step()
    real = net.__call__
    state = {"status": 429}

    def http(method, url, body=None, token="", timeout=15.0):
        if url.endswith("/helpers") and state["status"]:
            return state["status"], {}
        return real(method, url, body, token, timeout)
    c.http = http
    items = [{"key": "k1", "label": "api 1a2b", "tp": ""}]
    got = c.bind(c.rel, items)
    assert "Ratenlimit" in got["api 1a2b"]
    assert "Ratenlimit" in c.bind(c.rel, items)["api 1a2b"]      # inside the backoff: same reason
    state["status"] = 0
    clk.t += C.BIND_BACKOFF_S + 1
    assert c.bind(c.rel, items) == {"api 1a2b": ""}              # positive control
    assert c.bind(c.rel, items) == {"api 1a2b": ""}              # fresh entry: ok without request


def _fetch_watcher(monkeypatch, bind_result, post_status=200):
    rel = {"url": BASE + "/api/activity/relay", "token": RELTOK, "room": "r1", "call_id": "r1",
           "expires_at": NOW + 3600, "written_at": NOW, "owner_pid": 0, "owner_session": ""}
    monkeypatch.setattr(W, "server_interval", lambda r: ("ok", None))
    monkeypatch.setattr(W, "credential", lambda now=None: None)
    cands = [{"key": "k1", "label": "s1", "tp": "/t/1.jsonl", "active_at": NOW}]
    answers, boards = [], []
    monkeypatch.setattr(W, "post_proposal", lambda r, b: {"ok": True, "id": "D1"})
    monkeypatch.setattr(W, "poll_answers", lambda r: answers)
    monkeypatch.setattr(W, "delete_decision", lambda *a: {"ok": True})

    def post_board(r, label, body):
        boards.append((label, body))
        note = body == {} or str(body.get("doing") or "").startswith("Konnte ")
        if note or post_status < 300:
            return {"ok": True, "status": 200}
        return {"ok": False, "status": post_status}
    monkeypatch.setattr(W, "post_board", post_board)
    monkeypatch.setattr(W, "post_activity", lambda *a: {"ok": True})
    monkeypatch.setattr(W, "get_status", lambda *a: (404, {}))
    monkeypatch.setattr(W, "classify_state", lambda s, now: (W.ARBEITET, NOW))
    monkeypatch.setattr(W, "observable", lambda s: True)
    monkeypatch.setattr(W.Watcher, "duty_kanban", lambda self, *a: False)
    subs = []
    monkeypatch.setattr(W.J, "subscribed_sessions", lambda r, taken: list(subs))
    clk = Clock()
    w = W.Watcher(clock=clk, discover_fn=lambda r, n: [], cmd=None, related_fn=lambda *a: True,
                  unbound_fn=lambda *a, **k: [x for x in cands if not subs], cred_fn=lambda t=None: rel,
                  bind_fn=lambda r, items: {i["label"]: bind_result for i in items})
    assert w.tick() == W.OK                         # single card "s1 dazuholen?"
    answers.append({"type": "decision", "id": "D1", "verdict": "yes"})
    subs.append({"key": "k1", "label": "s1", "tp": "/t/1.jsonl"})
    clk.t += W.TICK_S
    w.tick()
    clk.t += W.TICK_S
    w.tick()
    return w, boards, clk


def _notes(boards):
    # 0.35.0: the note and the watcher board share the sender `s1 (Session Watcher)`
    return [b for lab, b in boards if lab == W.fetch_label("s1")
            and (b == {} or str(b.get("doing") or "").startswith("Konnte "))]


def test_ja_with_failed_helper_credential_reports_why(monkeypatch):
    _w, boards, _clk = _fetch_watcher(monkeypatch, "der Server zu viele Anfragen dieses Rechners abgelehnt hat")
    notes = _notes(boards)
    assert notes, "no feedback card: the Ja failed silently"
    assert "s1" in notes[0]["doing"] and "nicht" in notes[0]["doing"] and "zu viele Anfragen" in notes[0]["doing"]
    assert len([n for n in notes if n.get("doing")]) == 1        # once per reason, no spam


def test_ja_with_rejected_board_reports_why(monkeypatch):
    _w, boards, _clk = _fetch_watcher(monkeypatch, "", post_status=422)
    notes = _notes(boards)
    assert notes and "422" in notes[0]["doing"]


def test_ja_ok_shows_session_and_no_failure_note(monkeypatch):
    w, boards, _clk = _fetch_watcher(monkeypatch, "")
    assert not _notes(boards)                                     # positive control
    assert any(lab in ("s1", W.sub_label("s1")) and b.get("doing") for lab, b in boards)
    assert w.fetch == {}


def test_failure_note_cleared_once_the_session_arrives(monkeypatch):
    state = {"why": "noch kein Mensch im Call ist"}
    w, boards, clk = _fetch_watcher(monkeypatch, "x")
    w.bind_fn = lambda r, items: {i["label"]: state["why"] for i in items}
    assert _notes(boards)
    state["why"] = ""
    clk.t += W.TICK_S
    w.mem.clear()                                                 # force a post this tick
    w.tick()
    last = [b for lab, b in boards if lab == W.fetch_label("s1")][-1]
    assert not str(last.get("doing") or "").startswith("Konnte ") and w.fetch == {}   # board replaced the note
