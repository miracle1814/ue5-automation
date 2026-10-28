#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_screenshot.py — 截图反馈闭环（ue5_screenshot / capture 三命令 · 离线）
=====================================================================
T-20261024-UE5BRIDGE-SCREENSHOT（纯 Python 侧 · 离线；C++ 桥为离线草稿，
落盘/读回链路由 fake_unreal 的【真实 PNG】替身承载——纯 stdlib 手写 PNG，
不引 Pillow）。

锁定契约（对齐 P0 规格书与 UEBox 五条经验）：
  ① 目录计数回归：白名单 75→78、MCP 工具 26→27（其余计数测试在此同步）。
  ② capture_viewport / capture_pie / project_actors_to_screen 三命令契约：
     success 键存在、saved_path 落约定目录、bytes 回填、camera_source 溯源、
     PIE 未运行 fail-loud、pending 轮询两条路径（等到 / 超时 fail-loud）。
  ③ 路径防逃逸（双层）：宿主侧 _capture_check_out_path 预检 +
     替身（镜像 C++）的目录前缀裁决，越界一律 fail-loud。
  ④ 降级纪律：旧 DLL（无 capture_* API）→ 干净报错，绝不静默换路。
  ⑤ MCP ue5_screenshot：image content block（base64）+ text 元数据
     （base64 不进文本块，控 token）；Pillow 可选——缺失跳过标注/压缩
     并告警（绝不因缺 Pillow 失败）；标注只画 visible=true 的投影；
     超长边且无 Pillow → 图不进上下文只回路径（防 base64 灌爆）。
  ⑥ GT 死锁规避：pending 收尾必须在非 GT 线程（_GT_TASK_RUNNING 模拟
     GT 上下文 → 标记 + _capture_finish_pending 收尾，含 editor_batch 步骤递归）。

层级：离线（UE5_BRIDGE_TEST_MODE=1 + fake_unreal 注入），不依赖 UE。
作者：dev（代码开发 Agent）· 日期：2026-10-24
"""

import base64
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

# ── 必须在 import ue5_bridge 之前设置环境变量 ──
os.environ["UE5_BRIDGE_TEST_MODE"] = "1"
os.environ["UE5_BRIDGE_TOKEN"] = "integration-test-token"

SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(SKILL_ROOT, "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import ue5_bridge            # noqa: E402  真实模块（测试模式 → fake_unreal 注入）
import fake_unreal as fu     # noqa: E402  test double
from fastapi.testclient import TestClient  # noqa: E402

TOKEN = "integration-test-token"
client = TestClient(ue5_bridge.app)


def _h():
    return {"X-Skill-Token": TOKEN}


def _cmd(name, **params):
    """POST /command 快捷（返回 dict）。"""
    r = client.post("/command", json={"command": name, "params": params}, headers=_h())
    assert r.status_code == 200, r.text
    return r.json()


def _mcp_call(name, args=None):
    """MCP tools/call 快捷（返回 (result_dict, is_error)）。"""
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": name, "arguments": args or {}}}
    resp = client.post("/mcp", json=payload, headers=_h())
    assert resp.status_code == 200, resp.text
    body = resp.json()["result"]
    return body, bool(body.get("isError"))


def _json_of(block):
    return json.loads(block["text"])


class _ScreenshotBase(unittest.TestCase):
    """公共夹具：项目目录注入临时路径（截图落盘链路写真实验证）。"""

    def setUp(self):
        fu.reset_fake()
        self._tmp = tempfile.mkdtemp(prefix="ue5_shot_test_")
        # project_dir 注入 → fake 替身把 PNG 落 <tmp>/Saved/Screenshots/ue5-automation/
        fu.Paths.set_project_dir(self._tmp)
        self._shot_dir = os.path.join(self._tmp, "Saved", "Screenshots", "ue5-automation")

    def tearDown(self):
        fu.Paths.set_project_dir(None)  # 复位默认假路径（防污染其他测试文件）
        fu.reset_fake()
        shutil.rmtree(self._tmp, ignore_errors=True)


# ══════════════════════════════════════════════════════════
# ① 目录计数回归
# ══════════════════════════════════════════════════════════

class TestCounts(unittest.TestCase):

    def test_whitelist_78(self):
        # 75 → 78（T-20261024-UE5BRIDGE-SCREENSHOT：capture 三命令）
        self.assertEqual(len(ue5_bridge._COMMAND_WHITELIST), 101,
                         "v6.5：95 + 多 Agent/AnimBP 6 条")

    def test_capture_commands_registered(self):
        for c in ("capture_viewport", "capture_pie", "project_actors_to_screen"):
            self.assertIn(c, ue5_bridge._COMMAND_WHITELIST, c)

    def test_mcp_tools_27(self):
        payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        tools = client.post("/mcp", json=payload, headers=_h()).json()["result"]["tools"]
        names = {t["name"] for t in tools}
        # T-20261028-TOOL-RESTORE：默认全量 34
        self.assertEqual(len(tools), 34, "tools/list 默认全量 34")
        self.assertIn("ue5_screenshot", names)

    def test_docstring_lists_capture(self):
        doc = ue5_bridge.execute_command.__doc__ or ""
        self.assertIn("%d 条" % len(ue5_bridge._COMMAND_WHITELIST), doc)
        for c in ("capture_viewport", "capture_pie", "project_actors_to_screen"):
            self.assertIn(c, doc, c)


# ══════════════════════════════════════════════════════════
# ② capture_viewport / capture_pie 命令契约
# ══════════════════════════════════════════════════════════

class TestCaptureViewport(_ScreenshotBase):

    def test_active_viewport_success_contract(self):
        out = _cmd("capture_viewport")
        self.assertTrue(out["success"])
        data = out["result"]["data"]
        self.assertTrue(data["ok"])
        self.assertEqual(data["method"], "scene_capture")
        self.assertEqual(data["camera_source"], "viewport")
        # 落约定目录 + 真实 PNG + bytes 回填
        self.assertTrue(data["saved_path"].replace("\\", "/").startswith(
            self._shot_dir.replace("\\", "/")), data["saved_path"])
        self.assertGreater(data["bytes"], 0)
        self.assertTrue(os.path.isfile(data["saved_path"]))
        with open(data["saved_path"], "rb") as f:
            self.assertEqual(f.read(8), b"\x89PNG\r\n\x1a\n")

    def test_out_name_respected(self):
        out = _cmd("capture_viewport", out_path="cases/shot_a.png")
        data = out["result"]["data"]
        self.assertIn("shot_a.png", data["saved_path"])
        self.assertTrue(os.path.isfile(data["saved_path"]))

    def test_agent_camera_provenance(self):
        out = _cmd("capture_viewport", camera_mode="agent_camera",
                   pitch=-30.0, yaw=45.0, roll=0.0)
        data = out["result"]["data"]
        self.assertEqual(data["camera_source"], "agent_camera")
        self.assertEqual(data["camera_rotation"], [-30.0, 45.0, 0.0])

    def test_bad_camera_mode_fails_loud(self):
        out = _cmd("capture_viewport", camera_mode="drone")
        self.assertFalse(out["success"])
        self.assertIn("camera_mode", out["error"])

    def test_pending_poll_waits_for_file(self):
        """延迟一帧模拟（0.3s 后台落盘）→ 非调度线程轮询等到。"""
        fu.set_capture_delay(0.3)
        out = _cmd("capture_viewport")
        self.assertTrue(out["success"])
        data = out["result"]["data"]
        self.assertNotIn("pending", data, "轮询完成后 pending 必须被消化")
        self.assertGreater(data["bytes"], 0)
        self.assertTrue(os.path.isfile(data["saved_path"]))

    def test_pending_timeout_fails_loud(self):
        """文件永远不出现 → 2s 超时 fail-loud（绝不报成功）。"""
        fu.set_capture_not_write(True)
        out = _cmd("capture_viewport")  # 走默认 2s 超时（~2s 用例）
        self.assertFalse(out["success"])
        self.assertIn("未就绪", out["error"])
        # TEST_MODE 下命令在本线程内联轮询 → 抛 BridgeError → 端点 except 分支
        # （result=None）；GT 标记路径的 _fail_loud 收尾由 TestPendingFinishHook 覆盖。

    def test_sync_verify_backfills_bytes(self):
        """同步路线（非 pending）：bytes 必须回填（防御性读回验证）。"""
        out = _cmd("capture_viewport")
        data = out["result"]["data"]
        self.assertGreater(data["bytes"], 0)


class TestCapturePie(_ScreenshotBase):

    def test_pie_not_running_fails_loud(self):
        out = _cmd("capture_pie")
        self.assertFalse(out["success"])
        self.assertIn("PIE", out["error"])

    def test_pie_running_success(self):
        fu.set_pie_world(True)
        out = _cmd("capture_pie", out_path="pie_shot.png")
        self.assertTrue(out["success"])
        data = out["result"]["data"]
        self.assertEqual(data["method"], "screenshot_request")
        self.assertEqual(data["camera_source"], "player")
        self.assertEqual(data["world"], "UEDPIE_0_FakeMap")
        self.assertTrue(os.path.isfile(data["saved_path"]))

    def test_pie_pending_timeout_fails_loud(self):
        fu.set_pie_world(True)
        fu.set_capture_not_write(True)
        out = _cmd("capture_pie")
        self.assertFalse(out["success"])
        self.assertIn("未就绪", out["error"])


# ══════════════════════════════════════════════════════════
# ③ 路径防逃逸（宿主预检 + 替身 C++ 镜像，双层）
# ══════════════════════════════════════════════════════════

class TestPathEscape(unittest.TestCase):
    """越界样本表驱动 —— 全部 fail-loud（规格书：防路径逃逸）。"""

    ESCAPE_SAMPLES = [
        "/Game/evil.png",          # 引擎路径
        "C:/evil/x.png",           # 盘符绝对路径
        "/tmp/evil.png",           # POSIX 绝对路径
        "..\\evil.png",            # 反斜杠 + ..
        "cases/../../evil.png",    # 相对 + 穿越
    ]

    def test_host_precheck_rejects(self):
        for s in self.ESCAPE_SAMPLES:
            with self.assertRaises(ue5_bridge.BridgeError, msg=s) as ctx:
                ue5_bridge._capture_check_out_path(s)
            self.assertIn("越界", str(ctx.exception), s)

    def test_command_layer_rejects(self):
        for s in self.ESCAPE_SAMPLES:
            out = _cmd("capture_viewport", out_path=s)
            self.assertFalse(out["success"], s)
            self.assertIn("越界", out["error"], s)

    def test_fake_bridge_mirrors_cpp_prefix_check(self):
        """替身（镜像 C++ BridgeNormalizeScreenshotPath）：绕过宿主预检的
        绝对路径/穿越路径在"桥"层也被拒 —— 双层防线各自成立。"""
        for s in ("C:/evil/x.png", "a/../../evil.png"):
            raw = fu.BlueprintPythonBridge.capture_viewport(s)
            data = json.loads(raw)
            self.assertFalse(data["ok"], s)
            self.assertIn("越界", data["error"], s)

    def test_host_allows_relative_with_subdir(self):
        self.assertEqual(ue5_bridge._capture_check_out_path("cases/shot_a.png"),
                         "cases/shot_a.png")
        self.assertEqual(ue5_bridge._capture_check_out_path(""), "")


# ══════════════════════════════════════════════════════════
# ④ project_actors_to_screen 契约（可见性过滤 / 未知名如实报）
# ══════════════════════════════════════════════════════════

class TestProjectActors(_ScreenshotBase):

    PROJECTIONS = [
        {"name": "Cube_A", "x": 100, "y": 80, "visible": True, "found": True,
         "world_location": [0.0, 0.0, 0.0]},
        {"name": "Cube_B", "x": -50, "y": -80, "visible": False, "found": True},
    ]

    def test_projections_match_and_filter(self):
        fu.set_projection_result(self.PROJECTIONS)
        out = _cmd("project_actors_to_screen",
                   actor_names=["Cube_A", "Cube_B", "Ghost"])
        self.assertTrue(out["success"])
        data = out["result"]["data"]
        self.assertEqual(data["visible_count"], 1)
        by_name = {p["name"]: p for p in data["projections"]}
        self.assertTrue(by_name["Cube_A"]["visible"])
        self.assertFalse(by_name["Cube_B"]["visible"])
        # 未注入的 actor：如实报 visible=false + found=false（绝不编造坐标）
        self.assertFalse(by_name["Ghost"]["found"])
        self.assertFalse(by_name["Ghost"]["visible"])

    def test_empty_names_fails_loud(self):
        out = _cmd("project_actors_to_screen", actor_names=[])
        self.assertFalse(out["success"])
        self.assertIn("actor_names", out["error"])

    def test_bad_camera_mode_fails_loud(self):
        out = _cmd("project_actors_to_screen", actor_names=["A"], camera_mode="fpv")
        self.assertFalse(out["success"])


# ══════════════════════════════════════════════════════════
# ⑤ 降级纪律（旧 DLL 无 capture_* API）
# ══════════════════════════════════════════════════════════

class TestDegrade(unittest.TestCase):
    """桥 DLL 旧版无这些命令 → 干净降级报错（fail-loud，不假成功）。"""

    def setUp(self):
        fu.reset_fake()

    def tearDown(self):
        fu.reset_fake()

    def test_missing_api_reports_rebuild_hint(self):
        with mock.patch.object(fu.BlueprintPythonBridge, "capture_viewport",
                               create=True) as _m:
            # 删属性模拟旧 DLL（mock.patch.object delattr 语义用 delete=True）
            pass
        with mock.patch("ue5_bridge.BRIDGE_AVAILABLE", True), \
             mock.patch.object(ue5_bridge, "bridge",
                               type("OldDLL", (), {"get_version": staticmethod(lambda: "9.9.9")})()):
            out = _cmd("capture_viewport")
            self.assertFalse(out["success"])
            self.assertIn("重新编译", out["error"])

            out2 = _cmd("capture_pie")
            self.assertFalse(out2["success"])
            self.assertIn("重新编译", out2["error"])

            out3 = _cmd("project_actors_to_screen", actor_names=["A"])
            self.assertFalse(out3["success"])
            self.assertIn("重新编译", out3["error"])

    def test_bridge_unavailable_fails_clean(self):
        with mock.patch.object(ue5_bridge, "BRIDGE_AVAILABLE", False):
            out = _cmd("capture_viewport")
            self.assertFalse(out["success"])
            self.assertIn("重新编译", out["error"])


# ══════════════════════════════════════════════════════════
# ⑥ GT 死锁规避：pending 标记 + 端点层收尾（含 editor_batch 递归）
# ══════════════════════════════════════════════════════════

class TestPendingFinishHook(_ScreenshotBase):
    """模拟 GT 上下文（_GT_TASK_RUNNING=True）：命令只挂 _pending_poll 标记，
    _capture_finish_pending 在"HTTP 线程"收尾 —— GT 上等文件 = 阻塞帧推进 = 死锁。"""

    def test_gt_context_returns_marker_then_finish_completes(self):
        fu.set_capture_delay(0.2)  # 产生 pending（否则无标记可挂）
        with mock.patch.object(ue5_bridge, "_GT_TASK_RUNNING", True):
            r = ue5_bridge._cmd_capture_viewport()
        self.assertTrue(r["_pending_poll"], "GT 上下文必须只挂标记")
        self.assertTrue(r["data"]["pending"], "data.pending 如实保留")
        # 端点层（HTTP 线程语义）收尾
        done = ue5_bridge._capture_finish_pending(r)
        self.assertTrue(done["success"])
        self.assertNotIn("_pending_poll", done)
        self.assertNotIn("pending", done["data"])
        self.assertGreater(done["data"]["bytes"], 0)

    def test_finish_timeout_marks_fail_loud(self):
        fu.set_capture_not_write(True)
        with mock.patch.object(ue5_bridge, "_GT_TASK_RUNNING", True):
            r = ue5_bridge._cmd_capture_viewport()
        done = ue5_bridge._capture_finish_pending(r)  # ~2s 超时
        self.assertFalse(done["success"])
        self.assertTrue(done.get("_fail_loud"))
        self.assertIn("未就绪", done["error"])

    def test_editor_batch_step_finished_recursively(self):
        fu.set_capture_delay(0.2)
        with mock.patch.object(ue5_bridge, "_GT_TASK_RUNNING", True):
            batch = ue5_bridge._cmd_editor_batch(
                [{"command": "capture_viewport", "params": {}},
                 {"command": "pie_state", "params": {}}], True)
        self.assertIn("steps", batch)
        # 模拟 /command 端点在 HTTP 线程的收尾钩子
        done = ue5_bridge._capture_finish_pending(batch)
        step0 = done["steps"][0]["result"]["data"]
        self.assertGreater(step0["bytes"], 0)
        self.assertNotIn("pending", step0)

    def test_command_endpoint_finishes_pending_automatically(self):
        """经 /command + GT 标记模拟 → 端点钩子自动收尾（整链路）。"""
        fu.set_capture_delay(0.2)
        with mock.patch.object(ue5_bridge, "_GT_TASK_RUNNING", True):
            out = _cmd("capture_pie")  # PIE 未运行 → fail（与 GT 无关的分段校验）
        self.assertFalse(out["success"])

        fu.set_pie_world(True)
        with mock.patch.object(ue5_bridge, "_GT_TASK_RUNNING", True):
            out = _cmd("capture_pie")
        self.assertTrue(out["success"], out)
        self.assertNotIn("_pending_poll", out["result"])
        self.assertGreater(out["result"]["data"]["bytes"], 0)


# ══════════════════════════════════════════════════════════
# ⑦ MCP 工具 ue5_screenshot（27 号）
# ══════════════════════════════════════════════════════════

class _FakePilImage:
    """假 PIL.Image 实例（覆盖 ue5_screenshot 用到的面）。"""

    def __init__(self, w, h):
        self.width, self.height = int(w), int(h)

    def load(self):
        return self

    def convert(self, mode):
        return self

    def resize(self, size, resample=None):
        self.width, self.height = int(size[0]), int(size[1])
        return self

    def save(self, buf, format=None, quality=None):
        buf.write(b"FAKE-JPEG-BYTES")


class _FakePilShim:
    """假 PIL 三模块（Image/ImageDraw/ImageFont）—— 覆盖"有 Pillow"分支。"""

    def __init__(self, w=320, h=240):
        self._size = (w, h)

    class _ImageMod:
        @staticmethod
        def open(path):
            with open(path, "rb") as f:
                dims = ue5_bridge._png_dimensions(f.read())
            return _FakePilImage(*(dims or (320, 240)))

        LANCZOS = 1  # 压缩分支的枚举访问

    class _DrawMod:
        class Draw:  # noqa: E302  标注画的全部方法 = no-op
            def __init__(self, img):
                self.img = img

            def line(self, *a, **kw):
                pass

            def rectangle(self, *a, **kw):
                pass

            def rounded_rectangle(self, *a, **kw):
                pass

            def text(self, *a, **kw):
                pass

            def textbbox(self, xy, text, font=None):
                return (0, 0, len(text) * 8, 14)

    class _FontMod:
        @staticmethod
        def truetype(path, size):
            return object()

        @staticmethod
        def load_default():
            return None


class TestMcpScreenshot(_ScreenshotBase):

    def setUp(self):
        super().setUp()

    def _with_fake_pil(self):
        shim = _FakePilShim()
        return {"Image": shim._ImageMod, "ImageDraw": shim._DrawMod,
                "ImageFont": shim._FontMod}

    # ── 无 Pillow（mock 注入 _load_pillow → None：任何机器都走真分支，
    #    不再依赖"本机恰好没装 Pillow"——外出机没有而家里有，曾致 2 例假失败 ──

    @mock.patch.object(ue5_bridge, "_load_pillow", lambda: None)
    def test_no_pillow_png_passthrough_and_metadata(self):
        body, is_err = _mcp_call("ue5_screenshot", {"source": "editor_viewport"})
        self.assertFalse(is_err)
        content = body["content"]
        # content[0] = image block（PNG 直出，≤ max_dimension 默认 768）
        self.assertEqual(content[0]["type"], "image")
        self.assertEqual(content[0]["mimeType"], "image/png")
        raw = base64.b64decode(content[0]["data"])
        self.assertEqual(raw[:8], b"\x89PNG\r\n\x1a\n")
        meta = _json_of(content[1])
        self.assertTrue(meta["success"])
        self.assertTrue(meta["saved_path"].replace("\\", "/").startswith(
            self._shot_dir.replace("\\", "/")))
        self.assertIn("camera_used", meta)
        self.assertEqual(meta["camera_used"]["camera_source"], "viewport")
        self.assertFalse(meta["annotated"])
        self.assertTrue(any("Pillow" in w for w in meta["warnings"]),
                        "缺 Pillow 必须告警")
        # base64 绝不进 text 块（控 token）
        self.assertNotIn("b'\x89PNG", content[1]["text"])
        self.assertNotIn("_mcp_content", content[1]["text"])

    @mock.patch.object(ue5_bridge, "_load_pillow", lambda: None)
    def test_oversize_without_pillow_no_image_block(self):
        """超长边 + 无 Pillow（无法压缩）→ 图不进上下文只回路径（防 base64 灌爆）。"""
        fu.set_capture_size((1200, 800))
        body, is_err = _mcp_call("ue5_screenshot", {"source": "editor_viewport",
                                                    "max_dimension": 768})
        self.assertFalse(is_err)
        meta = _json_of(body["content"][0])  # 无 image block → text 块在第 0 位
        self.assertTrue(meta["success"])
        self.assertTrue(any("不返回图像内容" in w for w in meta["warnings"]))
        self.assertEqual(meta["returned_mime"], "")

    def test_agent_camera_requires_camera(self):
        body, is_err = _mcp_call("ue5_screenshot", {"source": "agent_camera"})
        self.assertTrue(is_err)
        self.assertIn("camera", body["content"][0]["text"])

    def test_bad_source_is_error(self):
        body, is_err = _mcp_call("ue5_screenshot", {"source": "drone"})
        self.assertTrue(is_err)
        self.assertIn("source", body["content"][0]["text"])

    def test_pie_not_running_is_error(self):
        body, is_err = _mcp_call("ue5_screenshot", {"source": "pie"})
        self.assertTrue(is_err)
        self.assertIn("PIE", body["content"][0]["text"])

    def test_corrupt_file_fails_loud(self):
        """落盘的是坏 PNG（魔数对但 IHDR 损坏）→ 读回验证 fail-loud。

        冻结"发起"步骤（不重新落盘覆盖坏文件），只验证读回管线。"""
        r = json.loads(fu.BlueprintPythonBridge.capture_viewport("corrupt.png"))
        path = r["saved_path"]
        with open(path, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)  # 魔数对、结构烂

        def _frozen_dispatch(cmd, **params):
            return {"success": True, "data": {"ok": True, "saved_path": path,
                                              "camera_source": "viewport",
                                              "method": "scene_capture"}}

        with mock.patch.object(ue5_bridge, "_capture_cmd_dispatch", _frozen_dispatch), \
             mock.patch.object(ue5_bridge, "_load_pillow", lambda: None):
            with self.assertRaises(ue5_bridge.BridgeError) as ctx:
                ue5_bridge._mcp_impl_screenshot({"source": "editor_viewport"})
            self.assertIn("PNG", str(ctx.exception))

    # ── 有 Pillow（假 PIL shim 覆盖真实分支逻辑）──

    def test_with_pillow_annotation_and_compression(self):
        """Pillow 在 → 标注（只画 visible）+ 长边压缩 + JPEG q80 base64。"""
        fu.set_projection_result([
            {"name": "Cube_A", "x": 100, "y": 80, "visible": True, "found": True},
            {"name": "Cube_B", "x": -50, "y": -80, "visible": False, "found": True},
        ])
        fu.set_capture_size((1200, 800))  # > 768 → 触发压缩
        with mock.patch.object(ue5_bridge, "_load_pillow", lambda: self._with_fake_pil()), \
             mock.patch.object(ue5_bridge, "_screenshot_projection_names",
                               lambda data, source: ["Cube_A", "Cube_B"]):
            body, is_err = _mcp_call("ue5_screenshot",
                                     {"source": "editor_viewport", "annotate": True})
        self.assertFalse(is_err)
        content = body["content"]
        self.assertEqual(content[0]["type"], "image")
        self.assertEqual(content[0]["mimeType"], "image/jpeg")
        self.assertEqual(base64.b64decode(content[0]["data"]), b"FAKE-JPEG-BYTES")
        meta = _json_of(content[1])
        self.assertTrue(meta["annotated"])
        self.assertEqual(meta["annotated_actors"], ["Cube_A"],
                         "视锥外的 Cube_B 绝不标注")
        self.assertEqual(meta["returned_size"], [768, 512])  # 等比缩
        self.assertTrue(any("压缩" in w for w in meta["warnings"]))

    def test_with_pillow_no_visible_projection(self):
        fu.set_projection_result([
            {"name": "Cube_A", "x": 3000, "y": -3000, "visible": False, "found": True},
        ])
        with mock.patch.object(ue5_bridge, "_load_pillow", lambda: self._with_fake_pil()), \
             mock.patch.object(ue5_bridge, "_screenshot_projection_names",
                               lambda data, source: ["Cube_A"]):
            body, is_err = _mcp_call("ue5_screenshot", {"source": "editor_viewport"})
        self.assertFalse(is_err)
        meta = _json_of(body["content"][-1])
        self.assertFalse(meta["annotated"])
        self.assertTrue(any("visible=true" in w for w in meta["warnings"]))

    def test_max_dimension_clamped(self):
        """max_dimension 钳制到 [64, 2048]（防 1px 无意义 / 防上下文爆炸）；
        小图不放大（长边 ≤ 上限 → 原尺寸直出 JPEG）。"""
        with mock.patch.object(ue5_bridge, "_load_pillow", lambda: self._with_fake_pil()):
            out = ue5_bridge._mcp_impl_screenshot(
                {"source": "editor_viewport", "max_dimension": 99999})
            self.assertEqual(out["returned_size"], [320, 240])  # 320 ≤ 2048 → 不缩放
            self.assertEqual(out["returned_mime"], "image/jpeg")

    def test_annotated_pie_source(self):
        """source=pie 成功路径 + 标注世界选择不炸（fake PIE 运行）。"""
        fu.set_pie_world(True)
        fu.set_projection_result([
            {"name": "Cube_A", "x": 64, "y": 64, "visible": True, "found": True},
        ])
        with mock.patch.object(ue5_bridge, "_load_pillow", lambda: self._with_fake_pil()), \
             mock.patch.object(ue5_bridge, "_screenshot_projection_names",
                               lambda data, source: ["Cube_A"]):
            body, is_err = _mcp_call("ue5_screenshot", {"source": "pie"})
        self.assertFalse(is_err)
        meta = _json_of(body["content"][1])
        self.assertEqual(meta["source"], "pie")
        self.assertTrue(meta["annotated"])


# ══════════════════════════════════════════════════════════
# ⑧ 纯函数单测（无 IO / 无状态）
# ══════════════════════════════════════════════════════════

class TestPureHelpers(unittest.TestCase):

    def test_png_dimensions_real_and_garbage(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "a.png")
            fu._fake_write_png(p, 7, 5)
            with open(p, "rb") as f:
                self.assertEqual(ue5_bridge._png_dimensions(f.read()), (7, 5))
        self.assertIsNone(ue5_bridge._png_dimensions(b"not a png at all"))
        self.assertIsNone(ue5_bridge._png_dimensions(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32))

    def test_wait_file_timeout_short(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ue5_bridge.BridgeError) as ctx:
                ue5_bridge._capture_wait_file(os.path.join(tmp, "nope.png"),
                                              timeout_s=0.3)
            self.assertIn("未就绪", str(ctx.exception))

    def test_wait_file_size_stability(self):
        """存在但持续增长 → 继续等；稳定后返回 bytes（不因 0 字节提前放行）。"""
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "g.png")
            with open(p, "wb") as f:
                f.write(b"\x89PNG\r\n\x1a\n")
            # 恒定大小 → 稳定判定两次采样后放行（快速 timeout）
            st = ue5_bridge._capture_wait_file(p, timeout_s=2.0)
            self.assertGreater(st["bytes"], 0)

    def test_reset_fake_restores_capture_faults(self):
        fu.set_pie_world(True)
        fu.set_capture_delay(5.0)
        fu.set_capture_not_write(True)
        fu.set_projection_result([{"name": "A", "x": 0, "y": 0, "visible": True}])
        fu.reset_fake()
        self.assertFalse(json.loads(fu.BlueprintPythonBridge.capture_pie(""))["ok"],
                         "reset 后 PIE 应回到未运行")
        out = ue5_bridge._cmd_capture_viewport()
        self.assertTrue(out["success"], "reset 后 capture_viewport 应回到即时成功")


if __name__ == "__main__":
    unittest.main()
