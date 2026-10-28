#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_compile_structured.py — C++ 结构化编译诊断接入（离线契约）
================================================================
T-20261027-UE5BRIDGE-COMPILE-STRUCT（v4.6）

网络依赖为零：bridge 用替身，unreal 用 fake_unreal。锁定：

  ① 优先路径：有 `compile_blueprint_structured` → 只编译一次，不再调
     `compile_blueprint`；errors 来自 token，`may_be_stale=False`、
     `errors_scope=compiler_results_log`。
  ② 回退路径：方法缺失 / 抛异常 / 返回 ok:false / JSON 损坏 → 旧日志路径
     （`compile_blueprint` 被调用；行为与 v4.5 完全一致）。
  ③ 字段契约：`_structured_bp_errors` 产出的 entry 与日志路径同形状，
     `node_guid/node_name/graph/asset` 来自 token；warning 不混进 errors。
  ④ 去重：同一 (message, node_guid) 只留一条（编译器会对多节点重复报同一句）。
"""

import json
import os
import sys
import tempfile

os.environ["UE5_BRIDGE_TEST_MODE"] = "1"

_SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPTS_DIR = os.path.join(_SKILL_ROOT, "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import pytest  # noqa: E402
import ue5_bridge as B  # noqa: E402
import fake_unreal as fu  # noqa: E402

BP = "/Game/Test/BP_StructDiag"


class StructuredBridge:
    """替身：可配置结构化返回值 / 异常；记录调用顺序。"""

    def __init__(self, payload=None, raise_exc=False, legacy_ok=True):
        self.payload = payload
        self.raise_exc = raise_exc
        self.legacy_ok = legacy_ok
        self.calls = []

    def compile_blueprint_structured(self, bp):
        self.calls.append("structured")
        if self.raise_exc:
            raise RuntimeError("structured boom")
        if isinstance(self.payload, str):
            return self.payload
        return json.dumps(self.payload)

    def compile_blueprint(self, bp):
        self.calls.append("legacy")
        return self.legacy_ok


class LegacyOnlyBridge:
    def __init__(self):
        self.calls = []

    def compile_blueprint(self, bp):
        self.calls.append("legacy")
        return True


def _payload(compiled=True, errors=None, warnings=None):
    return {
        "ok": True,
        "compiled": compiled,
        "status": "BS_UpToDate" if compiled else "BS_Error",
        "status_code": 3 if compiled else 2,
        "asset": BP,
        "num_errors": len(errors or []),
        "num_warnings": len(warnings or []),
        "errors": errors or [],
        "warnings": warnings or [],
        "messages": [],
    }


def _err(message, guid="ABCDEF0123456789ABCDEF0123456789",
         node_name="Branch", graph="EventGraph", asset=BP):
    return {"severity": "error", "message": message, "node_guid": guid,
            "node_name": node_name, "graph": graph, "asset": asset,
            "nodes": [{"node_guid": guid, "node_name": node_name,
                       "graph": graph, "asset": asset}]}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    fu.reset_fake()
    fu.create_test_blueprint(BP)
    tmp = tempfile.TemporaryDirectory()
    log_path = os.path.join(tmp.name, "Project.log")
    with open(log_path, "w", encoding="utf-8") as f:
        f.write("LogInit: test log\n")
    fu.Paths.set_log_dir(tmp.name)
    monkeypatch.setattr(B, "BRIDGE_AVAILABLE", True)
    monkeypatch.setattr(B, "_BRIDGE_TOKEN", "structured-test-token")
    yield
    tmp.cleanup()


def _use(monkeypatch, bridge):
    monkeypatch.setattr(B, "bridge", bridge)
    return bridge


# ══════════════════════════════════════════════════════════
#  ① 优先路径
# ══════════════════════════════════════════════════════════

def test_structured_preferred_and_single_compile(monkeypatch):
    fb = _use(monkeypatch, StructuredBridge(_payload(compiled=True)))
    r = B._cmd_compile_and_diagnose(BP)
    assert r["structured"] is True
    assert r["errors_scope"] == "compiler_results_log"
    assert r["compiled"] is True and r["success"] is True
    assert r["compile_status"] == "BS_UpToDate"
    assert fb.calls == ["structured"], fb.calls
    assert r["num_errors"] == 0


def test_structured_errors_carry_node_guid(monkeypatch):
    _use(monkeypatch, StructuredBridge(_payload(
        compiled=False,
        errors=[_err("未找到变量 Target，请先定义")],
    )))
    r = B._cmd_compile_and_diagnose(BP)
    assert r["compiled"] is False and r["success"] is False
    assert r["compile_status"] == "BS_Error"
    e = r["errors"][0]
    assert e["message"] == "未找到变量 Target，请先定义"
    assert e["node_guid"] == "ABCDEF0123456789ABCDEF0123456789"
    assert e["node_name"] == "Branch"
    assert e["graph"] == "EventGraph"
    assert e["asset"] == BP
    assert e["source"] == "compiler_results_log"
    assert e["may_be_stale"] is False
    assert e["nodes"][0]["node_guid"] == e["node_guid"]


def test_structured_warnings_do_not_pollute_errors(monkeypatch):
    _use(monkeypatch, StructuredBridge(_payload(
        compiled=True, errors=[],
        warnings=[_err("函数未被使用")],
    )))
    r = B._cmd_compile_and_diagnose(BP)
    assert r["errors"] == []
    assert any("函数未被使用" in w for w in r["warnings"])


# ══════════════════════════════════════════════════════════
#  ② 回退路径
# ══════════════════════════════════════════════════════════

def test_fallback_when_method_missing(monkeypatch):
    fb = _use(monkeypatch, LegacyOnlyBridge())
    r = B._cmd_compile_and_diagnose(BP)
    assert r["structured"] is False
    assert r["errors_scope"] == "log_tail_window"
    assert fb.calls == ["legacy"]


def test_fallback_when_structured_raises(monkeypatch):
    fb = _use(monkeypatch, StructuredBridge(_payload(), raise_exc=True))
    r = B._cmd_compile_and_diagnose(BP)
    assert r["structured"] is False
    assert fb.calls == ["structured", "legacy"]


def test_fallback_when_ok_false(monkeypatch):
    fb = _use(monkeypatch, StructuredBridge({"ok": False, "error": "no transactor"}))
    r = B._cmd_compile_and_diagnose(BP)
    assert r["structured"] is False
    assert fb.calls == ["structured", "legacy"]


def test_fallback_when_json_broken(monkeypatch):
    fb = _use(monkeypatch, StructuredBridge("not-a-json"))
    r = B._cmd_compile_and_diagnose(BP)
    assert r["structured"] is False
    assert fb.calls == ["structured", "legacy"]


def test_legacy_compile_failure_still_fail_loud(monkeypatch):
    fb = _use(monkeypatch, StructuredBridge(_payload(), legacy_ok=False))
    fb.payload = None  # 没有结构化方法可用的等价：payload 为 None → 返回 "null"
    r = B._cmd_compile_and_diagnose(BP)
    # "null" 不是合法 dict → 回退 legacy；legacy 返回 False → compiled=False
    assert r["compiled"] is False and r["success"] is False


# ══════════════════════════════════════════════════════════
#  ③ 字段转换：_structured_bp_errors
# ══════════════════════════════════════════════════════════

def test_converter_shape_and_dedupe():
    payload = _payload(compiled=False, errors=[
        _err("同一个错误"),
        _err("同一个错误"),                     # 同 (msg,guid) → 去重
        _err("另一个错误", guid=None, node_name="", graph=""),
    ])
    errors, warnings = B._structured_bp_errors(payload)
    assert len(errors) == 2
    assert errors[0]["message"] == "同一个错误"
    assert errors[1]["node_guid"] is None
    assert warnings == []


def test_converter_pin_hint():
    payload = _payload(compiled=False, errors=[
        _err("Cannot connect pin Exec to Target"),
    ])
    errors, _ = B._structured_bp_errors(payload)
    # 引脚提示是可选增强：能抽到就带，抽不到也不影响主字段
    assert errors[0]["message"].startswith("Cannot connect pin")


def test_converter_rule_mapping_uses_patterns():
    # 用规则库真实存在的一条：未命中 → Unclassified（契约：绝不编造）
    payload = _payload(compiled=False, errors=[
        _err("完全不在规则库里的新型错误 zzz"),
    ])
    errors, _ = B._structured_bp_errors(payload)
    assert errors[0]["code"] == "CompileError.Unclassified"
    assert errors[0]["pitfall_ref"] is None
    assert errors[0]["fix_hint"] is None


# ══════════════════════════════════════════════════════════
#  ④ compile_blueprint 回归：键集合不变
# ══════════════════════════════════════════════════════════

def test_compile_blueprint_keys_unchanged(monkeypatch):
    _use(monkeypatch, StructuredBridge(_payload(compiled=True)))
    r = B._cmd_compile_blueprint(BP)
    for k in ("success", "compiled", "compile_status"):
        assert k in r, k
    assert r["success"] is True
