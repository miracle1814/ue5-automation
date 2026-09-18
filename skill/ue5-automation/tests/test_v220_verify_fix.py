# -*- coding: utf-8 -*-
"""
test_v220_verify_fix.py — v2.20 独立验证轮（E-1~E-13）回归测试
=============================================================
离线可跑（无需真实 UE）。对应 SKILL.md v0.15 更新日志各条目：
  E-1  log_parser drafts 绝对路径崩溃 / --output 绝对路径 / 退出码
  E-2  mermaid mmdc Windows 垫片解析 + OSError 降级 + 文件名防覆盖
  E-3  GT 模板编译 bool 定论（返回值说谎红线）
  E-4  blueprint_editor GUID 连线匹配 / 读取失败不假成功 / 全图节点搜索 / rollback
  E-6  deploy.py 引擎版本闸
  E-10 preflight 批量数组 / builder CLI spec 转换 / reader save_report
  E-11 bridge token 冷启动复用（不重新生成覆盖）
"""
import json
import os
import sys
import unittest
from unittest import mock

SCRIPTS = os.path.dirname(os.path.abspath(__file__))  # tests/ 的父目录下 scripts/
SCRIPTS = os.path.join(os.path.dirname(SCRIPTS), "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)


# ═══════════════════════════════════════════════════════════
# E-1 log_parser
# ═══════════════════════════════════════════════════════════

class E1LogParserTests(unittest.TestCase):
    LOG = ("[2026.09.18-10.00.00:123][456]LogBlueprintUserMessages: "
           "[BP_Fake] Error: complete mystery failure XYZ123 totally unmatched\n"
           "[2026.09.18-10.00.00:124][457]LogTemp: Display: ok\n")

    def _write_log(self, d):
        p = os.path.join(d, "ue.log")
        with open(p, "w", encoding="utf-8") as f:
            f.write(self.LOG)
        return p

    def test_sanitize_absolute_rejected_by_default(self):
        from log_parser import sanitize_output_path
        with self.assertRaises(ValueError):
            sanitize_output_path(os.path.join(os.path.dirname(os.path.abspath(__file__)), "x.json"))

    def test_sanitize_absolute_allowed_with_flag(self):
        from log_parser import sanitize_output_path
        out = sanitize_output_path(
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "x.json"),
            allow_absolute=True)
        self.assertTrue(os.path.isabs(out))

    def test_sanitize_still_rejects_dotdot(self):
        from log_parser import sanitize_output_path
        with self.assertRaises(ValueError):
            sanitize_output_path("../evil.json", allow_absolute=True)

    def test_diagnose_with_unknown_errors_writes_drafts(self):
        """E-1 主回归：未匹配错误行 + 绝对路径 output → 不崩 + drafts 落盘。"""
        import tempfile
        from log_parser import diagnose_log
        with tempfile.TemporaryDirectory() as d:
            log_path = self._write_log(d)
            out_path = os.path.join(d, "diag.json")
            report = diagnose_log(log_path, None, out_path)  # 修复前：ValueError
            self.assertEqual(len(report.unknown_errors), 1)
            drafts_dir = os.path.join(d, "drafts")
            self.assertTrue(os.path.isdir(drafts_dir))
            self.assertTrue(os.path.isfile(os.path.join(drafts_dir, "draft_1.json")))

    def test_auto_fix_stub_honest(self):
        """E-1 附带：无操作 auto-fix 桩不得声称 auto_applied。"""
        from log_parser import AutoFixExecutor
        from types import SimpleNamespace
        ex = AutoFixExecutor()
        match = SimpleNamespace(pattern_id="PTN-003", log_line="x", auto_fix=True)
        results = ex.execute_batch([match])
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].get("auto_applied"))


# ═══════════════════════════════════════════════════════════
# E-2 mermaid_render
# ═══════════════════════════════════════════════════════════

class E2MermaidTests(unittest.TestCase):
    def test_mmdc_missing_raises_render_error_not_filenotfound(self):
        """E-2：mmdc 不可执行时必须抛 MermaidRenderError（可降级），而非裸 FileNotFoundError。"""
        import mermaid_render as mr
        r = mr.MermaidRenderer(allow_external=False)
        with mock.patch.object(mr.shutil, "which", return_value=None):
            with self.assertRaises(mr.MermaidRenderError):
                r._render_mmdc("graph TD; A-->B;", "png", None, None, "default", "")

    def test_mmdc_launch_oserror_degrades(self):
        import mermaid_render as mr
        r = mr.MermaidRenderer(allow_external=False)
        with mock.patch.object(mr.shutil, "which", return_value="mmdc"), \
             mock.patch.object(mr.subprocess, "run",
                               side_effect=OSError("CreateProcess failed")):
            with self.assertRaises(mr.MermaidRenderError):
                r._render_mmdc("graph TD; A-->B;", "png", None, None, "default", "")

    def test_output_name_has_uniqueness_suffix(self):
        import inspect
        import mermaid_render as mr
        src = inspect.getsource(mr.MermaidRenderer._render_mmdc)
        self.assertIn("uuid.uuid4().hex[:6]", src, "输出文件名应含短随机后缀防同秒覆盖")


# ═══════════════════════════════════════════════════════════
# E-3 GT 模板编译 bool
# ═══════════════════════════════════════════════════════════

class E3GTTemplateTests(unittest.TestCase):
    TMPL = os.path.join(SCRIPTS, "game_thread_build.py.tmpl")

    def test_template_checks_compile_bool(self):
        with open(self.TMPL, "r", encoding="utf-8") as f:
            src = f.read()
        self.assertIn("_compile_ok = unreal.BlueprintPythonBridge.compile_blueprint(bp)", src)
        self.assertIn("compile_blueprint returned False", src)

    def test_template_checks_candidate_retry(self):
        with open(self.TMPL, "r", encoding="utf-8") as f:
            src = f.read()
        self.assertIn("_fn_name_hints(fn_name)[1:]", src)
        self.assertIn("已自动改用候选", src)


# ═══════════════════════════════════════════════════════════
# E-4 blueprint_editor
# ═══════════════════════════════════════════════════════════

def _mk_editor():
    from blueprint_editor import BlueprintEditor
    with mock.patch.object(BlueprintEditor, "__init__", lambda self: None):
        ed = BlueprintEditor()
        ed.bridge_url = "http://127.0.0.1:8889"
        ed.auto_snapshot = False
        ed.log_dir = None
        ed.token = "t"
        ed._history = []
        ed._snapshots = {}
        return ed


class E4BlueprintEditorTests(unittest.TestCase):
    def test_get_pin_links_guid_match(self):
        ed = _mk_editor()
        raw = {"success": True, "result": {"available": True, "graphs": [
            {"graph_name": "EventGraph", "connections": [
                {"from_node": "事件开始运行", "from_pin": "then",
                 "to_node": "打印字符串", "to_pin": "execute",
                 "from_guid": "A" * 32, "to_guid": "B" * 32}]}]}}
        with mock.patch.object(ed, "_post", return_value=raw):
            links = ed._get_pin_links("/Game/BP_X", "B" * 32, "execute")
        self.assertEqual(len(links), 1, "GUID 入参必须按 from_guid/to_guid 匹配（E-4）")

    def test_get_pin_links_read_failure_returns_none(self):
        ed = _mk_editor()
        with mock.patch.object(ed, "_post", return_value={"success": False, "detail": "boom"}):
            self.assertIsNone(ed._get_pin_links("/Game/BP_X", "PrintString", "InString"))

    def test_disconnect_read_failure_does_not_fake_success(self):
        """E-4 主回归：连线读取失败 ≠ 本就无连接 → 必须直调 /disconnect。"""
        from blueprint_editor import EditResult
        ed = _mk_editor()
        calls = []

        def fake_post(endpoint, data, timeout=30.0):
            calls.append(endpoint)
            if endpoint == "/command":
                return {"success": False, "detail": "bridge busy"}
            return {"success": True, "data": {"broken_links": [{"from": "A", "to": "B"}],
                                              "method": "python_api",
                                              "verified_disconnected": True}}

        with mock.patch.object(ed, "_post", side_effect=fake_post):
            result = ed.disconnect_pin("/Game/BP_X", "PrintString", "InString")
        self.assertIn("/disconnect", calls, "读取失败时必须直调 /disconnect 权威定论")
        self.assertTrue(result.success)

    def test_get_node_info_searches_all_graphs(self):
        """E-4b：不再限定 EventGraph —— read_nodes 不传 graph_name。"""
        ed = _mk_editor()
        raw = {"success": True, "result": {"available": True, "graphs": [
            {"graph_name": "EventGraph", "nodes": []},
            {"graph_name": "UserConstructionScript", "nodes": [
                {"title": "清理节点", "class": "K2Node_FunctionResult", "guid": "C" * 32, "pins": []}]}]}}
        captured = {}

        def fake_post(endpoint, data, timeout=15.0):
            captured.update(data)
            return raw

        with mock.patch.object(ed, "_post", side_effect=fake_post):
            info = ed._get_node_info("/Game/BP_X", "清理节点")
        self.assertEqual(info.get("node_exists"), True)
        self.assertEqual(captured["params"].get("graph_name", None), None,
                         "不应再传 graph_name='EventGraph'（非 EventGraph 节点此前永远找不到）")

    def test_rollback_reconnects_disconnected_links(self):
        """E-4c：rollback() 对 disconnect 操作由 frozen_state 重建连线。"""
        from blueprint_editor import EditOperation
        ed = _mk_editor()
        op = EditOperation(
            op_type="disconnect", blueprint_path="/Game/BP_X",
            params={"node_id": "打印字符串", "pin_name": "InString"},
            frozen_state={"existing_links": [
                {"from": "事件开始运行", "from_pin": "then",
                 "to": "打印字符串", "to_pin": "InString"}]},
            status="SUCCESS")
        ed._history.append(op)
        calls = []

        def fake_post(endpoint, data, timeout=30.0):
            calls.append((endpoint, data))
            if endpoint == "/connect":
                return {"success": True, "results": [{"status": "connected"}]}
            return {"success": True, "data": {}}

        with mock.patch.object(ed, "_post", side_effect=fake_post):
            out = ed.rollback(last_n=1)
        self.assertEqual(len(out["rolled_back"]), 1)
        self.assertEqual(calls[0][0], "/connect")
        self.assertEqual(calls[0][1]["connections"][0]["src_node"], "事件开始运行")


# ═══════════════════════════════════════════════════════════
# E-6 deploy.py 版本闸
# ═══════════════════════════════════════════════════════════

class E6DeployVersionGateTests(unittest.TestCase):
    def _load_deploy(self):
        bridge_dir = os.path.normpath(os.path.join(
            SCRIPTS, "..", "..", "..", "bridge", "BlueprintPythonBridge"))
        if not os.path.isfile(os.path.join(bridge_dir, "deploy.py")):
            self.skipTest("deploy.py 不在预期相对位置（非包内布局）")
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "deploy_v220", os.path.join(bridge_dir, "deploy.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_version_gate_51_pass(self):
        d = self._load_deploy()
        self.assertTrue(d._version_gate((5, 1, "5.1.1"), "测试", force=False))

    def test_version_gate_54_blocked(self):
        d = self._load_deploy()
        with mock.patch("builtins.print"):
            self.assertFalse(d._version_gate((5, 4, "5.4.0"), "测试", force=False))

    def test_version_gate_force_overrides(self):
        d = self._load_deploy()
        self.assertTrue(d._version_gate((5, 4, "5.4.0"), "测试", force=True))


# ═══════════════════════════════════════════════════════════
# E-10 preflight / builder / reader
# ═══════════════════════════════════════════════════════════

class E10MiscTests(unittest.TestCase):
    def test_preflight_batch_accepts_top_level_list(self):
        import preflight
        manifest = [{"name": "BP_A", "variables": [], "nodes": [], "connections": []}]
        with mock.patch.object(preflight, "run_preflight",
                               side_effect=lambda bp: mock.MagicMock(
                                   blueprint_name=bp["name"])):
            reports = preflight.run_batch(manifest)
        self.assertIn("BP_A", reports)

    def test_builder_spec_from_json(self):
        import blueprint_builder as bb
        spec = bb._spec_from_json({
            "name": "BP_X", "variables": [{"name": "Health", "type": "float"}],
            "nodes": [{"node_type": "event", "event_name": "ReceiveBeginPlay"}],
            "connections": [{"src_node": "A", "src_pin": "then",
                             "dst_node": "B", "dst_pin": "execute"}]})
        self.assertEqual(spec.variables[0].name, "Health")
        self.assertEqual(spec.nodes[0].event_name, "ReceiveBeginPlay")
        self.assertEqual(spec.connections[0].dst_pin, "execute")

    def test_reader_save_report_no_dir_prefix(self):
        import tempfile
        import blueprint_reader as br
        with tempfile.TemporaryDirectory() as d:
            cwd = os.getcwd()
            os.chdir(d)
            try:
                analysis = br.BlueprintAnalysis(
                    basic_info=br.BlueprintBasicInfo(name="BP_T", asset_path="/Game/BP_T",
                                                     parent_class="Actor"))
                reader = br.BlueprintReader(mode="embedded")
                out = reader.save_report(analysis, "report.md")  # 修复前：makedirs("") 崩
                self.assertTrue(os.path.isfile(os.path.join(d, "report.md")))
                self.assertTrue(os.path.isfile(os.path.join(d, "report.json")),
                                "JSON 应与 Markdown 分开落盘（splitext）")
            finally:
                os.chdir(cwd)


# ═══════════════════════════════════════════════════════════
# E-11 bridge token 冷启动复用
# ═══════════════════════════════════════════════════════════

class E11TokenReuseTests(unittest.TestCase):
    def test_bridge_reuses_existing_token_file(self):
        """修复后：导入 ue5_bridge 不得覆盖既有 token 文件。"""
        import subprocess
        appdata = os.environ.get("APPDATA", os.path.expanduser("~/AppData/Roaming"))
        token_file = os.path.join(appdata, "ue5_bridge", "token")
        env = dict(os.environ)
        env.pop("UE5_BRIDGE_TOKEN", None)
        env["UE5_BRIDGE_TEST_MODE"] = "1"
        env["UE5_BRIDGE_TOKEN"] = ""
        probe = (
            "import sys; sys.path.insert(0, %r); "
            "open(%r, 'w').write('SENTINEL_MARKER'); "
            "import ue5_bridge; "
            "open(%r, 'w').write(ue5_bridge._BRIDGE_TOKEN)" % (SCRIPTS, token_file, token_file)
        )
        # 用哨兵值预写 token 文件 → 导入 bridge 后文件内容必须仍是哨兵（=被复用）
        try:
            p = subprocess.run([sys.executable, "-c", probe], env=env,
                               capture_output=True, text=True, timeout=120)
            self.assertEqual(p.returncode, 0, p.stderr[-400:])
            reused = open(token_file, encoding="utf-8").read() == "SENTINEL_MARKER"
        finally:
            pass
        self.assertTrue(reused,
                        "bridge 导入时不得重新生成并覆盖既有 token 文件（E-11 冷启动竞态）")


if __name__ == "__main__":
    unittest.main()
