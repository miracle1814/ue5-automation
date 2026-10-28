# -*- coding: utf-8 -*-
"""verify_scenario（v6.2）：场景化验收 + JUnit 证据。"""
import os
import sys
import xml.etree.ElementTree as ET

os.environ["UE5_BRIDGE_TEST_MODE"] = "1"
_SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPTS = os.path.join(_SKILL_ROOT, "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import pytest  # noqa: E402
import ue5_bridge as B  # noqa: E402


@pytest.fixture
def fake_ue(monkeypatch):
    state = {"pie": False, "log": 0}

    def pie_start(mode="simulate"):
        state["pie"] = True
        return {"success": True, "data": {"is_playing": True}}

    def pie_stop():
        state["pie"] = False
        return {"success": True}

    def assert_ok(*a, **k):
        return {"success": True, "ok": True, "verified": True,
                "actor": "A", "property": "hp", "expected": "1", "actual": "1"}

    def shot(*a, **k):
        return {"success": True, "ok": True,
                "data": {"saved_path": "C:/shot.png"}}

    def logs(*a, **k):
        return {"success": True, "data": {"count": state["log"], "lines": []}}

    monkeypatch.setattr(B, "_cmd_pie_start", pie_start)
    monkeypatch.setattr(B, "_cmd_pie_stop", pie_stop)
    monkeypatch.setattr(B, "_cmd_verify_behavior", assert_ok)
    monkeypatch.setattr(B, "_capture_cmd_dispatch", shot)
    monkeypatch.setattr(B, "_cmd_logs_read", logs)
    monkeypatch.setattr(B, "_GT_TASK_RUNNING", False)
    state["assert_ok"] = assert_ok
    return state


def test_scenario_passes_and_writes_junit(fake_ue, tmp_path):
    fake_ue["log"] = 1
    out = str(tmp_path / "s.xml")
    r = B._cmd_verify_scenario("door_opens", [
        {"type": "wait", "seconds": 0},
        {"type": "assert", "actor_name": "A", "property_name": "hp", "expected": "1"},
        {"type": "screenshot", "out_path": "a.png"},
        {"type": "log_contains", "pattern": "entered", "timeout": 0.2},
    ], start_pie=True, stop_pie=True, junit_path=out)
    assert r["verified"] is True and r["success"] is True
    assert r["failed_count"] == 0 and len(r["steps"]) == 4
    assert os.path.isfile(out)
    root = ET.parse(out).getroot()
    assert root.tag == "testsuite" and root.attrib["tests"] == "4"
    assert root.attrib["failures"] == "0"


def test_scenario_failure_stops_and_skips(fake_ue, tmp_path, monkeypatch):
    def bad(*a, **k):
        return {"success": False, "ok": False, "verified": False,
                "error": "期望 2 实际 1", "actor": "A", "property": "hp"}

    monkeypatch.setattr(B, "_cmd_verify_behavior", bad)
    r = B._cmd_verify_scenario("door_fails", [
        {"type": "assert", "actor_name": "A", "property_name": "hp",
         "expected": "2"},
        {"type": "screenshot"},
    ], start_pie=True, junit_path=str(tmp_path / "f.xml"))
    assert r["verified"] is False
    assert r["failed_count"] == 1
    statuses = [s["status"] for s in r["steps"]]
    assert statuses == ["failed", "skipped"]
    assert r["_fail_loud"] is True


def test_scenario_pie_start_failure_raises(monkeypatch):
    monkeypatch.setattr(B, "_cmd_pie_start",
                        lambda mode="simulate": {"success": False, "error": "no pie"})
    with pytest.raises(B.BridgeError) as ei:
        B._cmd_verify_scenario("x", [{"type": "wait", "seconds": 0}])
    assert ei.value.category == "scenario"


def test_scenario_rejects_inside_gt(monkeypatch):
    monkeypatch.setattr(B, "_GT_TASK_RUNNING", True)
    with pytest.raises(B.BridgeError) as ei:
        B._cmd_verify_scenario("x", [{"type": "wait"}])
    assert "GT" in str(ei.value)


def test_scenario_unknown_step_fails_loud(fake_ue, tmp_path):
    r = B._cmd_verify_scenario("bad", [{"type": "teleport"}],
                               start_pie=False,
                               junit_path=str(tmp_path / "b.xml"))
    assert r["verified"] is False and r["failed_count"] == 1


def test_scenario_accepts_action_aliases(fake_ue, tmp_path):
    """v6.5：action/assert_property/assert_log 与 type 写法等价。"""
    fake_ue["log"] = 1
    r = B._cmd_verify_scenario("aliases", [
        {"action": "wait", "seconds": 0},
        {"action": "assert_property", "actor_name": "A",
         "property_name": "hp", "expected": "1"},
        {"action": "assert_log", "pattern": "entered", "timeout": 0.2},
    ], start_pie=False, junit_path=str(tmp_path / "a.xml"))
    assert r["verified"] is True, r
    assert [s["type"] for s in r["steps"]] == ["wait", "assert", "log_contains"]


def test_scenario_command_step_runs(fake_ue, tmp_path, monkeypatch):
    monkeypatch.setitem(B._COMMAND_WHITELIST, "health",
                        lambda **kw: {"success": True, "ok": True})
    r = B._cmd_verify_scenario("cmd", [
        {"type": "command", "command": "health"},
    ], start_pie=False, junit_path=str(tmp_path / "c.xml"))
    assert r["verified"] is True, r
    assert r["steps"][0]["detail"]["command"] == "health"


def test_scenario_destructive_command_rejected(fake_ue, tmp_path):
    r = B._cmd_verify_scenario("danger", [
        {"type": "command", "command": "delete_asset",
         "params": {"asset_path": "/Game/BP_X", "confirm": True}},
    ], start_pie=False, junit_path=str(tmp_path / "d.xml"))
    assert r["verified"] is False
    assert "默认禁止" in r["steps"][0]["error"]


def test_scenario_destructive_command_allowed(fake_ue, tmp_path, monkeypatch):
    monkeypatch.setitem(B._COMMAND_WHITELIST, "delete_asset",
                        lambda **kw: {"success": True, "ok": True})
    r = B._cmd_verify_scenario("danger_ok", [
        {"type": "command", "command": "delete_asset",
         "params": {"asset_path": "/Game/BP_X", "confirm": True}},
    ], start_pie=False, allow_destructive=True,
        junit_path=str(tmp_path / "e.xml"))
    assert r["verified"] is True, r


def test_scenario_rejects_recursive_batch(fake_ue, tmp_path):
    r = B._cmd_verify_scenario("recursive", [
        {"type": "command", "command": "editor_batch", "params": {"steps": []}},
    ], start_pie=False, junit_path=str(tmp_path / "r.xml"))
    assert r["verified"] is False
    assert "不允许" in r["steps"][0]["error"]


def test_scenario_total_timeout(fake_ue, tmp_path):
    r = B._cmd_verify_scenario("slow", [
        {"type": "wait", "seconds": 1.2},
        {"type": "command", "command": "health"},
    ], start_pie=False, timeout_s=0.5,
        junit_path=str(tmp_path / "t.xml"))
    assert r["verified"] is False
    assert any(s["type"] == "timeout" for s in r["steps"]), r["steps"]
