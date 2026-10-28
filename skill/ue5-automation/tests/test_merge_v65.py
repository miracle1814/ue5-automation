# -*- coding: utf-8 -*-
"""v6.5 外部优化分支合并回归：多 Agent 锁/幂等/回执、HTTP 上限、AnimBP、token。

这些用例只走离线 fake_unreal，不触达 UE；真机行为由 README-新机测试.md
的 ①–④ 与 docs/CROSS-VALIDATION-v6.5.md 覆盖。
"""
import asyncio
import os
import sys
import tempfile

import pytest

os.environ["UE5_BRIDGE_TEST_MODE"] = "1"
_SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPTS = os.path.join(_SKILL_ROOT, "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import ue5_bridge as B  # noqa: E402
import fake_unreal  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_state():
    with B._LOCKS_LOCK:
        B._ASSET_LOCKS.clear()
        B._SESSION_LOCKS.clear()
    with B._IDEMPOTENCY_LOCK:
        B._IDEMPOTENCY.clear()
    with B._RECEIPT_LOCK:
        B._RECEIPT_HISTORY.clear()
    yield
    with B._LOCKS_LOCK:
        B._ASSET_LOCKS.clear()
        B._SESSION_LOCKS.clear()
    with B._IDEMPOTENCY_LOCK:
        B._IDEMPOTENCY.clear()
    with B._RECEIPT_LOCK:
        B._RECEIPT_HISTORY.clear()


# ── 显式会话锁 ────────────────────────────────────────────────

def test_session_lock_conflict_release_and_reacquire():
    r = B._cmd_session_lock_acquire("agentA", ["/Game/BP_Test"], 60)
    assert r["success"] and r["acquired"] == ["/game/bp_test"]
    with pytest.raises(B.BridgeError) as ei:
        B._cmd_session_lock_acquire("agentB", ["/Game/BP_Test"], 60)
    assert ei.value.category == "session_lock"
    assert B._cmd_session_lock_release("agentA")["count"] == 1
    assert B._cmd_session_lock_acquire(
        "agentB", ["/Game/BP_Test"], 60)["acquired"] == ["/game/bp_test"]


def test_session_lock_force_steals_and_reports():
    B._cmd_session_lock_acquire("agentA", ["/Game/BP_Test"], 60)
    r = B._cmd_session_lock_acquire("agentB", ["/Game/BP_Test"], 60, force=True)
    assert r["stolen"] and r["stolen"][0]["from"] == "agentA"
    assert B._cmd_session_lock_status()["locks"][0]["owner"] == "agentB"


def test_session_lock_limits_and_nan_ttl():
    with pytest.raises(B.BridgeError):
        B._cmd_session_lock_acquire("x" * 129, ["/Game/BP"], 60)
    with pytest.raises(B.BridgeError):
        B._cmd_session_lock_acquire("a", ["/Game/" + "x" * 600], 60)
    old = B._SESSION_LOCK_MAX_PER_OWNER
    B._SESSION_LOCK_MAX_PER_OWNER = 2
    try:
        B._cmd_session_lock_acquire("agentA", ["/Game/BP_1", "/Game/BP_2"], 60)
        with pytest.raises(B.BridgeError):
            B._cmd_session_lock_acquire("agentA", ["/Game/BP_3"], 60)
    finally:
        B._SESSION_LOCK_MAX_PER_OWNER = old
    r = B._cmd_session_lock_acquire("agentB", ["/Game/BP_NaN"], float("nan"))
    assert r["ttl"] == B._SESSION_LOCK_TTL_DEFAULT


def test_session_lock_blocks_task_write_and_vice_versa():
    B._cmd_session_lock_acquire("agentA", ["/Game/BP_Test"], 60)
    with pytest.raises(B.BridgeError) as ei:
        B._asset_lock_acquire("/Game/BP_Test", "agentB")
    assert ei.value.category == "asset_locked"

    with B._LOCKS_LOCK:
        B._ASSET_LOCKS.clear()
        B._SESSION_LOCKS.clear()
    B._asset_lock_acquire("/Game/BP_Test", "TASK-A")
    with pytest.raises(B.BridgeError) as ei:
        B._cmd_session_lock_acquire("agentB", ["/Game/BP_Test"], 60, force=True)
    assert ei.value.category == "session_lock"


# ── 幂等键 ───────────────────────────────────────────────────

def test_idempotency_replay_and_conflict():
    calls = {"n": 0}

    def fake_apply(bp_path, graph_spec=None, replace=False):
        calls["n"] += 1
        return {"success": True, "bp_path": bp_path, "n": calls["n"]}

    r1 = B._dispatch_with_tx(
        "apply_graph", fake_apply, bp_path="/Game/BP_Test",
        graph_spec={"nodes": []}, idempotency_key="k1")
    r2 = B._dispatch_with_tx(
        "apply_graph", fake_apply, bp_path="/Game/BP_Test",
        graph_spec={"nodes": []}, idempotency_key="k1")
    assert calls["n"] == 1
    assert r1["n"] == 1 and r2["n"] == 1
    assert r2.get("idempotent_replay") is True

    with pytest.raises(B.BridgeError) as ei:
        B._dispatch_with_tx("apply_graph", fake_apply, bp_path="/Game/BP_Other",
                            graph_spec={"nodes": []}, idempotency_key="k1")
    assert ei.value.category == "idempotency_conflict"


def test_idempotency_does_not_cache_failure():
    calls = {"n": 0}

    def flaky(**kw):
        calls["n"] += 1
        return {"success": False, "_fail_loud": True, "error": "boom"}

    B._dispatch_with_tx("apply_graph", flaky, bp_path="/Game/BP",
                        graph_spec={}, idempotency_key="bad")
    B._dispatch_with_tx("apply_graph", flaky, bp_path="/Game/BP",
                        graph_spec={}, idempotency_key="bad")
    assert calls["n"] == 2


def test_dispatch_metadata_is_not_passed_to_command():
    seen = {}

    def fake(**kw):
        seen.update(kw)
        return {"success": True}

    B._dispatch_with_tx("apply_graph", fake, bp_path="/Game/BP",
                        graph_spec={}, session_id="agentA",
                        idempotency_key="k1")
    assert "session_id" not in seen and "idempotency_key" not in seen


# ── 回执历史 ─────────────────────────────────────────────────

def test_receipt_history_and_replay_marker():
    ctx = B._receipt_begin("apply_graph", {"bp_path": "/Game/No/BP"})
    r = B._receipt_finish(ctx, {"success": True, "idempotent_replay": True})
    assert r["receipt"]["receipt_id"].startswith("rcpt_")
    assert r["receipt"]["idempotent_replay"] is True
    assert r["receipt"]["note"].startswith("幂等重放")
    hist = B._cmd_receipt_list(5)
    assert hist["count"] >= 1 and hist["receipts"][0]["command"] == "apply_graph"


# ── HTTP 请求体上限 ──────────────────────────────────────────

class _State:
    pass


class _Req:
    def __init__(self, content_length):
        self.method = "POST"
        self.headers = {"content-length": str(content_length)}
        self.state = _State()


async def _never_called(_request):
    raise AssertionError("call_next should not run for rejected bodies")


def test_http_body_limit_rejects_large_and_invalid():
    resp = asyncio.run(B.add_trace_id(
        _Req(B._MAX_REQUEST_BYTES + 1), _never_called))
    assert resp.status_code == 413

    resp = asyncio.run(B.add_trace_id(_Req("not-a-number"), _never_called))
    assert resp.status_code == 400
    assert B._MAX_REQUEST_BYTES > 0


# ── AnimBP 只读 ──────────────────────────────────────────────

def test_animbp_probe_and_read_fail_loud():
    fake_unreal._FAKE_ASSETS.clear()
    path = "/Game/BP_NotAnim"
    fake_unreal._FAKE_ASSETS[path] = fake_unreal.FakeBlueprint(path)
    try:
        r = B._cmd_animbp_probe(path)
        assert r["success"] is True
        d = r["data"]
        assert d["is_animbp"] is False and d["writes_supported"] is False
        assert "warning" in d
        with pytest.raises(B.BridgeError) as ei:
            B._cmd_animbp_read(path)
        assert ei.value.category == "animbp"
        with pytest.raises(B.BridgeError) as ei:
            B._cmd_animbp_read("/Game/DoesNotExist")
        assert ei.value.category == "asset_load"
        with pytest.raises(B.BridgeError):
            B._cmd_animbp_probe("/Game/" + "x" * 600)
    finally:
        fake_unreal._FAKE_ASSETS.clear()


class _FakeAnimClass:
    def get_name(self):
        return "AnimBlueprint"


class _FakeAnimGraph:
    def __init__(self, name):
        self._name = name

    def get_name(self):
        return self._name


class _FakeAnimBP:
    def get_class(self):
        return _FakeAnimClass()

    def get_animation_graphs(self):
        return [_FakeAnimGraph("AnimGraph"), _FakeAnimGraph("Locomotion")]


def test_animbp_read_uses_get_animation_graphs():
    """v6.5：5.8 实测 API 路径（UPROPERTY 多为 protected）。"""
    fake_unreal._FAKE_ASSETS.clear()
    path = "/Game/ABP_Fake"
    fake_unreal._FAKE_ASSETS[path] = _FakeAnimBP()
    try:
        probe = B._cmd_animbp_probe(path)["data"]
        assert probe["is_animbp"] is True
        assert probe["apis"]["get_animation_graphs"] is True
        r = B._cmd_animbp_read(path, include_nodes=True)
        assert r["success"] is True and r["supported"] is True
        assert r["graph_count"] == 2
        assert r["graphs"][0]["name"] == "AnimGraph"
        assert r["graphs"][0]["nodes_available"] is False
        assert "节点级属性未暴露" in r["warning"]
    finally:
        fake_unreal._FAKE_ASSETS.clear()


# ── token 不再回退 test123 ───────────────────────────────────

def test_token_missing_is_empty_not_test123(monkeypatch, tmp_path):
    import ue5_runner
    monkeypatch.delenv("UE5_BRIDGE_TOKEN", raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert ue5_runner._resolve_bridge_token() == ""
    monkeypatch.setenv("UE5_BRIDGE_TOKEN", "abc123")
    assert ue5_runner._resolve_bridge_token() == "abc123"
