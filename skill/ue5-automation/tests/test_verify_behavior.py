# -*- coding: utf-8 -*-
"""verify_behavior 契约测试（v4.9 · 离线）

语义验证是"编译通过 ≠ 行为正确"的补丁：读运行时属性、与期望比对、可选截图。
本文件锁定：
  ① PIE 未运行 → fail-loud（提示 pie_start），绝不返回假 verified；
  ② exact / 数值容差 / contains 三种比对语义；
  ③ 断言失败 → success=false + _fail_loud（MCP isError 可见）；
  ④ 截图失败不翻转 verified（证据失败 ≠ 行为失败），但要有 warning；
  ⑤ 非法 world/match → 参数错误。
"""
import os
import sys

os.environ["UE5_BRIDGE_TEST_MODE"] = "1"

_SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPTS = os.path.join(_SKILL_ROOT, "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import pytest  # noqa: E402
import ue5_bridge as B  # noqa: E402


@pytest.fixture
def playing(monkeypatch):
    monkeypatch.setattr(B, "_cmd_pie_state",
                        lambda: {"success": True, "data": {"is_playing": True}})
    monkeypatch.setattr(B, "_cmd_capture_pie",
                        lambda out_path="", width=0, height=0: {
                            "success": True, "data": {"saved_path": out_path}})
    monkeypatch.setattr(B, "_cmd_capture_viewport",
                        lambda out_path="", **kw: {"success": True,
                                                  "data": {"saved_path": out_path}})


def _props(value, vtype="str"):
    return lambda actor, prop, world="editor", limit=200: {
        "success": True,
        "data": {"actor": actor, "world": world, "property": prop,
                 "value": value, "value_type": vtype},
    }


def test_exact_match_passes(playing, monkeypatch):
    monkeypatch.setattr(B, "_cmd_get_actor_properties", _props("DoorOpen"))
    r = B._cmd_verify_behavior("BP_Door_1", "state", "DoorOpen", capture=False)
    assert r["verified"] is True and r["success"] is True
    assert r["actual"] == "DoorOpen"
    assert "error" not in r


def test_mismatch_is_fail_loud(playing, monkeypatch):
    monkeypatch.setattr(B, "_cmd_get_actor_properties", _props("DoorClosed"))
    r = B._cmd_verify_behavior("BP_Door_1", "state", "DoorOpen", capture=False)
    assert r["verified"] is False
    assert r["success"] is False and r["_fail_loud"] is True
    assert "DoorOpen" in r["error"] and "DoorClosed" in r["error"]


def test_numeric_tolerance(playing, monkeypatch):
    monkeypatch.setattr(B, "_cmd_get_actor_properties", _props(1.00004, "float"))
    ok = B._cmd_verify_behavior("A", "hp", "1.0", tolerance=0.001, capture=False)
    bad = B._cmd_verify_behavior("A", "hp", "1.0", tolerance=0.0, capture=False)
    assert ok["verified"] is True
    assert bad["verified"] is False


def test_json_quoted_string_is_unwrapped(playing, monkeypatch):
    """get_actor_properties 的 value 是 JSON 形态（字符串带引号）→ 按裸值比较。"""
    monkeypatch.setattr(B, "_cmd_get_actor_properties", _props('"DoorOpen"'))
    r = B._cmd_verify_behavior("A", "state", "DoorOpen", capture=False)
    assert r["verified"] is True, r
    assert r["actual"] == "DoorOpen"
    # 期望值带引号也应等价
    r2 = B._cmd_verify_behavior("A", "state", '"DoorOpen"', capture=False)
    assert r2["verified"] is True, r2


def test_contains_mode(playing, monkeypatch):
    monkeypatch.setattr(B, "_cmd_get_actor_properties",
                        _props("Portal entered: 1", "str"))
    r = B._cmd_verify_behavior("A", "message", "portal entered",
                               match="contains", capture=False)
    assert r["verified"] is True


def test_pie_not_running_fails_loud(monkeypatch):
    monkeypatch.setattr(B, "_cmd_pie_state",
                        lambda: {"success": True, "data": {"is_playing": False}})
    with pytest.raises(B.BridgeError) as ei:
        B._cmd_verify_behavior("A", "hp", "1")
    assert ei.value.category == "pie_state"
    assert "pie_start" in (ei.value.suggestion or "")


def test_invalid_world_and_match(playing, monkeypatch):
    monkeypatch.setattr(B, "_cmd_get_actor_properties", _props("x"))
    with pytest.raises(B.BridgeError):
        B._cmd_verify_behavior("A", "p", "x", world="server")
    with pytest.raises(B.BridgeError):
        B._cmd_verify_behavior("A", "p", "x", match="glob")


def test_capture_failure_does_not_flip_verified(playing, monkeypatch):
    monkeypatch.setattr(B, "_cmd_get_actor_properties", _props("ok"))
    monkeypatch.setattr(B, "_cmd_capture_pie",
                        lambda out_path="", width=0, height=0: {
                            "success": False, "error": "PIE 截图失败"})
    r = B._cmd_verify_behavior("A", "p", "ok", capture=True)
    assert r["verified"] is True and r["success"] is True
    assert r["screenshot"]["success"] is False
    assert any("截图失败" in w for w in r.get("warnings") or [])


def test_property_read_failure_is_bridge_error(playing, monkeypatch):
    monkeypatch.setattr(B, "_cmd_get_actor_properties",
                        lambda *a, **k: {"success": False, "error": "actor not found"})
    with pytest.raises(B.BridgeError) as ei:
        B._cmd_verify_behavior("Missing", "hp", "1")
    assert ei.value.category == "actor_property"


def test_whitelist_registered():
    assert "verify_behavior" in B._COMMAND_WHITELIST
