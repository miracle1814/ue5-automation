# -*- coding: utf-8 -*-
"""D-1 前置解析守卫 · 离线单测（T-20260912-UE5BRIDGE-PYWRAP-L2-GUARD）

被测对象（products of this task）：
  · `ue5_bridge._pywrap_guard_engine_class_path` —— 引擎类/枚举/结构/接口路径前置守卫
  · `ue5_bridge._cmd_add_implemented_interface` —— P0 崩溃命令的「不进入引擎」保证
  · `ue5_bridge._cmd_read_nodes` —— `graph_name` 对 `graphs[]` 的过滤语义（R3 新增）

🔴 R3 收口（T-20260912-UE5BRIDGE-PYWRAP-L2-ONLINE-R3 · maintainer授权）：
  ① **Tier B 硬化**：裸短名解析不到 = 与全路径同口径 **fail-loud**（原「软告警放行」废止）——
     事故依据 = 裸短名透传引擎致 UnrealEditor 硬崩（报告 §16 / raw `b5_addiface_bare_shortname.json`）。
     ⚠️ **不是**「裸短名一刀切拒绝」：可解析的合法裸短名照常放行（本节 3 条正路径回归锁死）。
     ⚠️ 预检误拒风险 + 离线替身覆盖盲区 = 报告 §21 如实标注；真机验证挂 `-R5`
        （在线闸门 = `tests\test_pywrap_l2_online.py::TestR3TierBHardening`）。
  ② **`read_nodes(graph_name=…)` 过滤**：有值 → 只返回该图；未指定 → 全量（向后兼容）。

事故背景（P0 崩溃级）：`add_implemented_interface` 把用户字符串原样以 FName 交给
  `FBlueprintEditorUtils::ImplementNewInterface`（C++：BlueprintPythonBridge.cpp:2841-2843
  无解析/守卫）→ 引擎内部 `check(InterfaceClass)`（BlueprintEditorUtils.cpp:6463）断言 →
  **UnrealEditor 硬崩**（2026-09-12 19:1x 实测 PID 68416；
  raw 证据 output\\T-...-L2-ONLINE\\raw\\b5_addiface_bogus.json）。

为什么单列一个文件：
  ① 283 条基线套件（tests/test_pywrap_l2.py 等）保持可对照，计数不混入本单新增；
  ② 离线替身 `fake_unreal.load_class` **恒返回 FakeClass**（不区分可解析/不可解析）→
     「不可解析接口路径」这条负路径在替身下天然不可达（假阳性 success=true）。
     本文件用 monkeypatch 把替身改成「解析不到」，从而在离线侧也能运行守卫逻辑。
  ③ 真实引擎侧行为由在线段 tests/test_pywrap_l2_online.py 验证
     （test_add_implemented_interface_unresolvable_fail_loud = D-1 回归闸）。
"""
import os
import sys

import pytest

os.environ["UE5_BRIDGE_TEST_MODE"] = "1"
os.environ.setdefault("UE5_BRIDGE_TOKEN", "guard-offline-token")

SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(SKILL_ROOT, "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import ue5_bridge as B    # noqa: E402  真实模块（TEST_MODE → fake_unreal 注入）
import fake_unreal as fu  # noqa: E402  test double

FAIL_PATH = "/Script/Engine.NoSuchInterface_PywrapL2"   # 与在线段同一条 b5_addiface_bogus
FAIL_SHORT = "NoSuchBareShortName_PywrapL2"             # 同在线段 b5_addiface_bare_shortname


def _resolve_only(monkeypatch, ok_paths):
    """替身解析设施「可用」：仅 ok_paths 命中返回对象，其余返回 None。

    用于**正路径回归**：证明合法裸短名/全路径仍被放行（R3 不是一刀切拒绝）。
    """
    monkeypatch.setattr(B.unreal, "load_class",
                        lambda outer, path: object() if path in ok_paths else None,
                        raising=False)
    monkeypatch.setattr(B.unreal, "load_object",
                        lambda outer, path: object() if path in ok_paths else None,
                        raising=False)


def _kill_resolvers(monkeypatch):
    """替身解析设施「可用但解析不到」（模拟引擎侧找不到该类）。"""
    monkeypatch.setattr(B.unreal, "load_class", lambda outer, path: None,
                        raising=False)
    monkeypatch.delattr(B.unreal, "load_object", raising=False)


def _no_resolvers(monkeypatch):
    """解析设施完全不可用（模拟替身无 load_class/load_object → 应放行留痕）。"""
    monkeypatch.delattr(B.unreal, "load_class", raising=False)
    monkeypatch.delattr(B.unreal, "load_object", raising=False)


# ══════════════════════════════════════════════════════════════
# 1 · 守卫分级语义
# ══════════════════════════════════════════════════════════════

class TestGuardTiers:

    def test_empty_value_fail_loud(self):
        with pytest.raises(B.BridgeError) as ei:
            B._pywrap_guard_engine_class_path("", "interface_class_path",
                                              "interface_add")
        assert "不可为空" in str(ei.value)

    def test_resolved_path_passthrough_no_warning(self):
        raw, warns = B._pywrap_guard_engine_class_path(
            "/Script/Engine.Actor", "target_class_name", "param_type",
            kind="class", label="add_cast_node")
        assert raw == "/Script/Engine.Actor"
        assert warns == []

    def test_tier_a_full_path_unresolvable_raises(self, monkeypatch):
        _kill_resolvers(monkeypatch)
        with pytest.raises(B.BridgeError) as ei:
            B._pywrap_guard_engine_class_path(
                FAIL_PATH, "interface_class_path", "interface_add",
                kind="interface", label="add_implemented_interface")
        msg = str(ei.value)
        assert "拒绝调用引擎" in msg
        assert ei.value.category == "interface_add"
        assert FAIL_PATH in msg

    def test_tier_a_game_path_missing_asset_raises(self, monkeypatch):
        _kill_resolvers(monkeypatch)
        monkeypatch.setattr(B.unreal.EditorAssetLibrary, "does_asset_exist",
                            lambda path: False)
        with pytest.raises(B.BridgeError):
            B._pywrap_guard_engine_class_path(
                "/Game/Test/BPI_Ghost.BPI_Ghost_C", "interface_class_path",
                "interface_add", kind="interface")

    def test_tier_a_game_path_existing_asset_lenient(self, monkeypatch):
        """宽松放行：类加载器未命中但资产存在（防误伤蓝图生成类 `*_C`）。"""
        _kill_resolvers(monkeypatch)
        monkeypatch.setattr(B.unreal.EditorAssetLibrary, "does_asset_exist",
                            lambda path: True)
        raw, warns = B._pywrap_guard_engine_class_path(
            "/Game/Test/BPI_Foo.BPI_Foo_C", "interface_class_path",
            "interface_add", kind="interface")
        assert raw == "/Game/Test/BPI_Foo.BPI_Foo_C"
        assert warns and "宽松放行" in warns[0]

    def test_tier_b_short_name_unresolvable_raises(self, monkeypatch):
        """🔴 R3 硬化（原「软告警放行」已废止）：裸短名解析不到 → fail-loud。"""
        _kill_resolvers(monkeypatch)
        with pytest.raises(B.BridgeError) as ei:
            B._pywrap_guard_engine_class_path(
                FAIL_SHORT, "enum_name", "param_type", kind="enum")
        msg = str(ei.value)
        assert "拒绝调用引擎" in msg, msg
        assert FAIL_SHORT in msg
        assert ei.value.category == "param_type"

    def test_positive_bare_short_name_resolved_by_prefix_still_passes(
            self, monkeypatch):
        """正路径回归 ①：合法裸短名经模块前缀展开命中 → 原值放行 + 零 warning。"""
        _resolve_only(monkeypatch, {"/Script/Engine.Actor"})
        raw, warns = B._pywrap_guard_engine_class_path(
            "Actor", "target_class_name", "param_type",
            kind="class", label="add_cast_node")
        assert raw == "Actor"
        assert warns == [], warns

    def test_positive_bare_short_name_raw_value_still_passes(self, monkeypatch):
        """正路径回归 ②：短名原值本身可命中（已加载对象）→ 放行。"""
        _resolve_only(monkeypatch, {"KismetSystemLibrary"})
        raw, warns = B._pywrap_guard_engine_class_path(
            "KismetSystemLibrary", "factory_class_name", "param_type",
            kind="class", label="add_async_action_node")
        assert raw == "KismetSystemLibrary"
        assert warns == [], warns

    def test_positive_full_path_still_passes(self, monkeypatch):
        """正路径回归 ③：全路径可解析 → 放行（Tier A 未被 R3 破坏）。"""
        _resolve_only(monkeypatch, {"/Script/Engine.Actor"})
        raw, warns = B._pywrap_guard_engine_class_path(
            "/Script/Engine.Actor", "target_class_name", "param_type",
            kind="class")
        assert raw == "/Script/Engine.Actor"
        assert warns == [], warns

    def test_infra_unavailable_passthrough_with_warning(self, monkeypatch):
        _no_resolvers(monkeypatch)
        raw, warns = B._pywrap_guard_engine_class_path(
            FAIL_PATH, "interface_class_path", "interface_add",
            kind="interface")
        assert raw == FAIL_PATH
        assert warns and "未经前置解析" in warns[0]


# ══════════════════════════════════════════════════════════════
# 2 · 命令级：「不进入引擎」保证（引擎断言硬崩的离线等价闸门）
# ══════════════════════════════════════════════════════════════

class TestCmdNeverReachesEngine:

    def test_unresolvable_full_path_never_calls_engine(self, monkeypatch):
        """🔴 核心断言：解析失败 → BridgeError，且**引擎 API 一次都没被调用**。"""
        _kill_resolvers(monkeypatch)
        called = []

        def _spy(bp, path):
            called.append(path)
            return True

        monkeypatch.setattr(B.bridge, "add_implemented_interface",
                            staticmethod(_spy))
        with pytest.raises(B.BridgeError) as ei:
            B._cmd_add_implemented_interface("/Game/Test/BP_Guard",
                                             FAIL_PATH)
        assert called == [], f"D-1 守卫未生效：不可解析路径仍被递给引擎 {called}"
        assert "拒绝调用引擎" in str(ei.value)

    def test_bare_shortname_unresolvable_never_calls_engine(self, monkeypatch):
        """🔴 R3 核心断言（§16 事故回归闸）：不可解析**裸短名** →

        `BridgeError` 且**引擎 API 一次都没被调用**（硬化前该调用 = 编辑器硬崩）。

        事故原文（20:21:10 实测）：`raw\\b5_addiface_bare_shortname.json`
          `add_implemented_interface(interface_class_path=FAIL_SHORT)`
          → `Assertion failed: !InterfaceClassPathName.IsNull()`
            @ `BlueprintEditorUtils.cpp:6457` → UnrealEditor 硬崩（PID 59488 死）。
        """
        _kill_resolvers(monkeypatch)
        called = []

        def _spy(bp, path):
            called.append(path)
            return True

        monkeypatch.setattr(B.bridge, "add_implemented_interface",
                            staticmethod(_spy))
        with pytest.raises(B.BridgeError) as ei:
            B._cmd_add_implemented_interface("/Game/Test/BP_Bare",
                                             FAIL_SHORT)
        assert called == [], f"R3 硬化未生效：不可解析裸短名仍被递给引擎 {called}"
        assert "拒绝调用引擎" in str(ei.value)

    @pytest.mark.parametrize("cmd,param,kind", [
        ("_cmd_add_cast_node", "target_class_name", "class"),
        ("_cmd_add_class_cast_node", "target_class_name", "class"),
        ("_cmd_add_struct_node", "struct_name", "struct"),
        ("_cmd_add_enum_literal_node", "enum_name", "enum"),
        ("_cmd_add_async_action_node", "factory_class_name", "class"),
        ("_cmd_add_implemented_interface", "interface_class_path", "interface"),
    ])
    def test_guard_family_blocks_bare_shortname(self, monkeypatch, cmd, param,
                                                kind):
        """R3 覆盖面：class/enum/struct/interface 透传族对裸短名**同口径硬拦**。"""
        _kill_resolvers(monkeypatch)
        with pytest.raises(B.BridgeError) as ei:
            B._pywrap_guard_engine_class_path(
                FAIL_SHORT, param, "param_type", kind=kind, label=cmd)
        assert "拒绝调用引擎" in str(ei.value)


# ══════════════════════════════════════════════════════════════
# 3 · R3 · `read_nodes(graph_name=…)` graphs[] 过滤（离线逻辑闸）
# ══════════════════════════════════════════════════════════════
# 事实来源（raw\c2_nodes.json / c1_pins_inline.json 实测）：
#   入参 graph_name="EventGraph" 时 graphs[] 仍带回 4 张图 ——
#     UserConstructionScript(1) / EventGraph(32) / EdGraph_0(2) / MathExpression(4)
#   其中 UserConstructionScript 的 K2Node_FunctionEntry guid（run_3 实测）
#     = 90F6532345C515D5A7256181B964153A → 供 get_node_by_guid 跨图误取（§17.2）。
# R3 口径（maintainer授权 · 原 §17.3 待裁项已裁决）：
#   · graph_name 有值 → graphs[] 只返回该图
#   · 未指定（None / ""）→ 保持全量（向后兼容）

UCS_GUID_RUN3 = "90F6532345C515D5A7256181B964153A"
EG_GUID = "C15478144EF33B28B87656BF96C9F7B7"


def _fake_l2():
    """两图载荷（形状对齐真实 /command read_nodes 响应 result）。"""
    return {
        "available": True,
        "graphs": [
            {"graph_name": "UserConstructionScript", "node_count": 1,
             "connection_count": 0,
             "nodes": [{"title": "Construction Script",
                        "class": "K2Node_FunctionEntry",
                        "guid": UCS_GUID_RUN3, "pos": [0, 0],
                        "pins": [{"name": "then"}]}]},
            {"graph_name": "EventGraph", "node_count": 1,
             "connection_count": 0,
             "nodes": [{"title": "事件开始运行", "class": "K2Node_Event",
                        "guid": EG_GUID, "pos": [0, 0],
                        "pins": [{"name": "then"}, {"name": "OutputDelegate"}]}]},
        ],
    }


@pytest.fixture()
def _patched_read_nodes(monkeypatch):
    monkeypatch.setattr(B, "_resolve_blueprint", lambda p: object())
    monkeypatch.setattr(B, "_read_l2_nodes",
                        lambda bp, include_connections=False: _fake_l2())


class TestReadNodesGraphFilter:

    def test_eventgraph_only(self, _patched_read_nodes):
        """🔴 R3 回归闸：graph_name="EventGraph" → 无 UserConstructionScript、
        无 guid 90F6532345C515D5A7256181B964153A。"""
        res = B._cmd_read_nodes("/Game/Test/BP_Filter", "EventGraph")
        names = [g.get("graph_name") for g in res["graphs"]]
        assert names == ["EventGraph"], names
        guids = [n.get("guid") for g in res["graphs"]
                 for n in g.get("nodes", [])]
        assert UCS_GUID_RUN3 not in guids, guids
        assert guids == [EG_GUID], guids

    def test_ucs_only(self, _patched_read_nodes):
        res = B._cmd_read_nodes("/Game/Test/BP_Filter",
                                "UserConstructionScript")
        names = [g.get("graph_name") for g in res["graphs"]]
        assert names == ["UserConstructionScript"], names
        assert UCS_GUID_RUN3 in [n.get("guid") for g in res["graphs"]
                                 for n in g.get("nodes", [])]

    def test_unspecified_keeps_all_backward_compatible(
            self, _patched_read_nodes):
        """向后兼容：未指定 graph_name → 全量（旧调用方「一次拿全图」不破）。"""
        assert len(B._cmd_read_nodes("/Game/Test/BP_Filter")["graphs"]) == 2
        assert len(B._cmd_read_nodes("/Game/Test/BP_Filter", "")["graphs"]) == 2
        assert len(B._cmd_read_nodes("/Game/Test/BP_Filter", None)["graphs"]) == 2

    def test_unknown_graph_returns_empty_not_error(self, _patched_read_nodes):
        res = B._cmd_read_nodes("/Game/Test/BP_Filter", "NoSuchGraph_R3")
        assert res["graphs"] == []

    def test_whitelist_lambda_default_is_none(self):
        """派单§2 口径：白名单入口默认值 = None（未指定 = 全量），非 "EventGraph"。"""
        with open(os.path.join(SCRIPTS_DIR, "ue5_bridge.py"),
                  encoding="utf-8") as f:
            src = f.read()
        assert 'lambda bp_path, graph_name=None, **kw: _cmd_read_nodes' in src

# ══════════════════════════════════════════════════════════
# R4 · 遗留 L1 闭环：在线 fixture 的「实现指纹」判据（离线可验部分）
# ══════════════════════════════════════════════════════════
def _online_mod():
    """取在线用例模块的纯函数（`tests/` 入 sys.path；该文件离线收集期本就 import）。"""
    _tests_dir = os.path.dirname(os.path.abspath(__file__))
    if _tests_dir not in sys.path:
        sys.path.insert(0, _tests_dir)
    import test_pywrap_l2_online as online
    return online


def _variant(tmp_path, old=None, new=None):
    """把 `B.__file__` 复制成变体（可选**定点**替换），返回变体路径。

    字节级复制（`rb`/`wb`）：不做换行归一化——「未改动副本」必须在字节层
    与原文件一致，否则 CRLF/LF 漂移会污染指纹比对（CI Windows 检出实测踩中）。
    """
    with open(B.__file__, "rb") as f:
        src = f.read()
    if old is not None:
        old_b, new_b = old.encode("utf-8"), (new or "").encode("utf-8")
        assert old_b in src, "变体锚点未命中（ue5_bridge.py 结构变化 → 本用例需同步）"
        src = src.replace(old_b, new_b, 1)
    p = os.path.join(str(tmp_path), "ue5_bridge_variant.py")
    with open(p, "wb") as f:
        f.write(src)
    return p


#: Tier B 硬化（R3）的**既有分支**尾部：R3 已把「软告警放行」改成 fail-loud。
#: 变体 = 还原成 R3 前的软告警 return → **白名单一字未变，实现已变**（R5 实测形态）。
_TIERB_FAIL_LOUD = (
    "    raise BridgeError(\n"
    '        f"{label or category}: {param} 为短名但无法解析为可加载的 {kind} → "'
)
_TIERB_SOFT_WARN = (
    '    return raw, [f"{param} 未经前置解析（R4 变体：模拟 R3 前 Tier B 软告警）: {raw}"]\n'
    "    raise BridgeError(\n"
    '        f"{label or category}: {param} 为短名但无法解析为可加载的 {kind} → "'
)


class TestL2ImplFingerprint:
    """在线用例 `bridge_module_fresh` 的判据升级（派单 D3 · 纯离线可验部分）。

    指纹 = `L2-IMPL/<guard 行号>/<guard co_code sha1[:12]>/<模块文件 sha1[:12]>/<字节数>`；
    在线 fixture 比对「UE **进程内**模块指纹」vs「**磁盘**模块指纹」。
    本节覆盖验证三件套之 ①「指纹不一致场景**能被检出**」+ ②「一致时行为不变」。
    """

    def test_disk_formula_matches_module_constant(self):
        """①a 双份公式一致：fixture 侧 `disk_l2_impl_id()` == 模块 import 时自算常量。

        防「两处公式漂移」—— 漂移会让在线 fixture 把**新鲜模块**误判为陈旧（每次空热载）。
        """
        online = _online_mod()
        disk = online.disk_l2_impl_id()
        assert online._IMPL_ID_RE.match(disk), "指纹格式非法: %s" % disk
        assert disk == B._PYWRAP_L2_IMPL_ID, (
            "双份公式不一致：磁盘复算=%s / 模块自算=%s" % (disk, B._PYWRAP_L2_IMPL_ID))

    def test_fingerprint_binds_guard_line(self):
        """指纹第 2 段 == guard `co_firstlineno`（R5 事故定位锚点：`GUARD_FIRSTLINENO=1883`）。"""
        line = B._pywrap_guard_engine_class_path.__code__.co_firstlineno
        assert B._PYWRAP_L2_IMPL_ID.split("/")[1] == str(line)
        assert "L2-IMPL/%d/" % line in B._PYWRAP_L2_IMPL_ID

    def test_unchanged_copy_same_fingerprint(self, tmp_path):
        """①c 未改动副本 → 指纹不变（**无误报**）。"""
        online = _online_mod()
        copy = _variant(tmp_path)
        assert online.disk_l2_impl_id(B.__file__) == B._PYWRAP_L2_IMPL_ID
        assert online.disk_l2_impl_id(copy) == B._PYWRAP_L2_IMPL_ID

    def test_branch_only_hardening_detected(self, tmp_path):
        """①b **关键**：只改既有分支（Tier B 硬拦 → 软告警回退）必须被检出；旧判据看不见。

        R5 实测形态：白名单 47 键不变（21 条 L2 命令全在），但进程内 Tier B 仍软告警
        → 首条闸门直跑会复现报告 §16 硬崩。
        """
        online = _online_mod()
        v = _variant(tmp_path, _TIERB_FAIL_LOUD, _TIERB_SOFT_WARN)
        with open(v, "r", encoding="utf-8") as f:
            text = f.read()
        assert all(c in text for c in online.L2_COMMANDS), "变体应保持白名单不变（旧判据盲区）"
        assert online.disk_l2_impl_id(v) != B._PYWRAP_L2_IMPL_ID, "新判据未检出既有分支改动"

    def test_whitelist_additive_change_detected(self, tmp_path):
        """白名单**新增**（旧判据能查到的形态）同样被检出 → 新判据是超集，不丢能力。"""
        online = _online_mod()
        v = _variant(tmp_path, "_COMMAND_WHITELIST = {",
                     '_COMMAND_WHITELIST = {\n    "_r4_probe_noop": lambda **kw: None,')
        assert online.disk_l2_impl_id(v) != B._PYWRAP_L2_IMPL_ID

    def test_reload_decision_logic(self):
        """①d/② 热载判定：不一致 → reload；一致 → **跳过**（行为不变）；白名单兜底仍生效。"""
        online = _online_mod()
        fresh = B._PYWRAP_L2_IMPL_ID
        assert online.needs_reload(fresh, fresh, 0) is False
        assert online.needs_reload(fresh, "L2-IMPL/1/aaaaaaaaaaaa/bbbbbbbbbbbb/1", 0) is True
        assert online.needs_reload(fresh, fresh, 3) is True
        assert online.needs_reload(fresh, online._NO_GUARD_ID, 0) is True

    def test_probe_source_compiles(self):
        """①e 注入 UE 的探针源码可编译（防探针语法错 → 在线自愈静默失效）。"""
        online = _online_mod()
        lit = "[" + ",".join(repr(c) for c in online.L2_COMMANDS) + "]"
        for do_reload in (False, True):
            code = online.impl_probe_code(lit, do_reload=do_reload)
            compile(code, "<ue_impl_probe>", "exec")
            assert "BRIDGE_IMPL inproc=" in code
            assert "_COMMAND_WHITELIST" in code
            if do_reload:
                assert "importlib.reload(B)" in code
