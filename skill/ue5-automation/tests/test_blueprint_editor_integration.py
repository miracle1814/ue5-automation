#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_blueprint_editor_integration.py — ue5_bridge 端点集成测试
================================================================
测试层级定位（T-20260801-FIX-ENDPOINTS · 与maintainer确认边界）：

| 层级 | 是否替身 | 说明 |
|:---|:---:|:---|
| 单元测试 | ✅ Mock | fake_unreal.py 直接喂桩（test_blueprint_editor_mock.py）|
| **集成测试（本文件）** | ⚠️ **test double** | **真实启动 ue5_bridge.py**（UE5_BRIDGE_TEST_MODE=1 注入
  fake_unreal 替身）+ **真实 FastAPI/Starlette 生命周期** + **真实 HTTP 客户端**
  （httpx TestClient 走完整中间件栈：认证 / CORS / 路由 / 异常处理）|
| E2E 测试 | ❌ 全真实 | 连真 UE5 Editor（Phase 2 · 待 UE 在线）|

> ⚠️ 本文件是**集成测试**：HTTP 层与端点逻辑全真实执行（非 Mock _post），
> 仅 UE5 编辑器侧用 `fake_unreal`（test double）替身——这是 test double，
> 不是"非 Mock"空承诺。GameThread queue → tick worker → 结果回传全链路真实运行。

验收覆盖（≥6 项，实际 10 项）：
  1. /disconnect 成功断开（method=python_api）→ broken_links 正确
  2. /disconnect break_link_to 故障注入 → 降级 catalog_only
  3. /disconnect 跨图同属校验 → 跨图连线被跳过，同图连线正常断开
  4. /disconnect 节点不存在 → success=False + detail 含"节点不存在"
  5. /delete_node 成功删除（三步删除）→ broken_links + removed_from_graph
  6. /delete_node dry_run → 仅统计不删除
  7. /delete_node 节点不存在 → success=False + detail="节点不存在"
  8. /delete_node 蓝图不存在 → success=False
  9. 未知端点 → HTTP 404
  10. 缺 token → HTTP 403
  11. 并发断线 → GameThread 串行化，两个请求均成功且结果一致（加分项）

已知教训（KB 语义搜索 · 2026-08-03 命中）：
  - Mock 测试掩盖端点缺失（审计 R8）→ 本文件不 Mock 任何 HTTP 调用
  - 端点返回结构必须与 blueprint_editor.py 调用方对齐（data.broken_links / method）
  - 跨 Pin 操作必须校验同图归属（已验证经验）

作者：dev（代码开发 Agent）
日期：2026-08-03
"""

import os
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor

# ── 必须在 import ue5_bridge 之前设置环境变量 ──
os.environ["UE5_BRIDGE_TEST_MODE"] = "1"
os.environ["UE5_BRIDGE_TOKEN"] = "integration-test-token"

SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(SKILL_ROOT, "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import ue5_bridge            # noqa: E402  真实模块（测试模式 → fake_unreal 注入）
import fake_unreal as fu     # noqa: E402  test double
from fastapi.testclient import TestClient  # noqa: E402  真实 HTTP 客户端

BP_PATH = "/Game/Test/BP_TestActor"


def _build_test_blueprint():
    """构造测试蓝图：
    EventGraph: BeginPlay.then ──▶ PrintString.execute
    FuncGraph:  OtherNode（跨图节点）
    跨图连线：PrintString.execute ← OtherNode.execute（非法跨图，用于同属校验）
    """
    bp = fu.create_test_blueprint(BP_PATH)
    eg = bp.graphs["EventGraph"]

    begin = eg.add_node(fu.FakeNode("BeginPlay", "K2Node_Event", 0, 0))
    begin.add_pin("then", "EGPD_Output", "exec")

    ps = eg.add_node(fu.FakeNode("PrintString", "K2Node_CallFunction", 100, 0))
    ps.add_pin("execute", "EGPD_Input", "exec")
    ps.add_pin("InString", "EGPD_Input", "string")

    fu.connect_pins_by_name(eg, "BeginPlay", "then", "PrintString", "execute")

    fg = bp.add_graph(fu.FakeGraph("FuncGraph"))
    other = fg.add_node(fu.FakeNode("OtherNode", "K2Node_CallFunction", 0, 0))
    other.add_pin("execute", "EGPD_Input", "exec")
    # 跨图连线：PrintString.execute ← OtherNode.execute（模拟非法跨图）
    fu.connect_across_graphs(eg, "PrintString", "execute", fg, "OtherNode", "execute")
    return bp


class TestDisconnectEndpoint(unittest.TestCase):
    """/disconnect 端点集成测试（真实 HTTP + 真实端点逻辑）。"""

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(ue5_bridge.app)

    def setUp(self):
        fu.reset_fake()
        self.bp = _build_test_blueprint()
        self.headers = {"X-Skill-Token": ue5_bridge._BRIDGE_TOKEN}

    def test_disconnect_success_python_api(self):
        """① 同图连线断开成功 → method=python_api + broken_links 正确"""
        resp = self.client.post("/disconnect", json={
            "blueprint_path": BP_PATH,
            "node_id": "BeginPlay",
            "pin_name": "then",
        }, headers=self.headers)

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["success"])
        self.assertEqual(body["data"]["method"], "python_api")
        self.assertTrue(body["data"]["verified_disconnected"])
        links = body["data"]["broken_links"]
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0]["to"], "PrintString")
        # 验证真实断开：目标 pin 不再连接
        node = self.bp.graphs["EventGraph"]._nodes["BeginPlay"]
        self.assertFalse(node.find_pin("then").is_connected)

    def test_disconnect_catalog_only_fallback(self):
        """② break_link_to 故障注入 → method=catalog_only（降级路径）"""
        fu.set_break_link_failure(True)
        try:
            resp = self.client.post("/disconnect", json={
                "blueprint_path": BP_PATH,
                "node_id": "BeginPlay",
                "pin_name": "then",
            }, headers=self.headers)

            self.assertEqual(resp.status_code, 200)
            body = resp.json()
            self.assertEqual(body["data"]["method"], "catalog_only")
            self.assertFalse(body["data"]["verified_disconnected"])
            self.assertIn("catalog_only", body["detail"])
            # 连接未被真断
            node = self.bp.graphs["EventGraph"]._nodes["BeginPlay"]
            self.assertTrue(node.find_pin("then").is_connected)
        finally:
            fu.set_break_link_failure(False)

    def test_disconnect_cross_graph_skipped(self):
        """③ 跨图同属校验：跨图连线被跳过，同图连线正常断开"""
        resp = self.client.post("/disconnect", json={
            "blueprint_path": BP_PATH,
            "node_id": "PrintString",
            "pin_name": "execute",
        }, headers=self.headers)

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        # 同图连线（BeginPlay.then → PrintString.execute）断开成功
        self.assertTrue(body["success"])
        self.assertEqual(body["data"]["method"], "python_api")
        self.assertEqual(len(body["data"]["broken_links"]), 1)
        # 跨图连线被跳过并记录
        skipped = body["data"].get("skipped_cross_graph", [])
        self.assertEqual(len(skipped), 1)
        self.assertIn("OtherNode", skipped[0]["to_node"])
        # 验证：跨图连线仍然存在（未误删）
        ps = self.bp.graphs["EventGraph"]._nodes["PrintString"]
        other = self.bp.graphs["FuncGraph"]._nodes["OtherNode"]
        self.assertIn(other.find_pin("execute"), ps.find_pin("execute").linked_to)

    def test_disconnect_node_not_found(self):
        """④ 节点不存在 → success=False + detail 含'节点不存在'"""
        resp = self.client.post("/disconnect", json={
            "blueprint_path": BP_PATH,
            "node_id": "NonExistentNode",
            "pin_name": "then",
        }, headers=self.headers)

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertFalse(body["success"])
        self.assertIn("节点不存在", body["detail"])

    def test_disconnect_concurrent_calls(self):
        """⑪ 并发断线 → GameThread 串行化，两个请求均成功且互不干扰"""
        # 构造两个独立蓝图，分别并发断开
        bp2_path = "/Game/Test/BP_TestActor2"
        bp2 = fu.create_test_blueprint(bp2_path)
        begin2 = bp2.graphs["EventGraph"].add_node(fu.FakeNode("BeginPlay", "K2Node_Event", 0, 0))
        begin2.add_pin("then", "EGPD_Output", "exec")  # 有节点但无连接

        def _disconnect(path, node_id, pin_name):
            return self.client.post("/disconnect", json={
                "blueprint_path": path,
                "node_id": node_id,
                "pin_name": pin_name,
            }, headers=self.headers)

        with ThreadPoolExecutor(max_workers=2) as pool:
            f1 = pool.submit(_disconnect, BP_PATH, "BeginPlay", "then")
            f2 = pool.submit(_disconnect, bp2_path, "BeginPlay", "then")

        r1, r2 = f1.result(), f2.result()
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 200)
        b1, b2 = r1.json(), r2.json()
        self.assertTrue(b1["success"] and b2["success"])
        self.assertEqual(len(b1["data"]["broken_links"]), 1)
        self.assertEqual(len(b2["data"]["broken_links"]), 0)  # bp2 无连接

    def test_disconnect_requires_token(self):
        """⑩ 缺 token → HTTP 403"""
        resp = self.client.post("/disconnect", json={
            "blueprint_path": BP_PATH,
            "node_id": "BeginPlay",
            "pin_name": "then",
        })
        self.assertEqual(resp.status_code, 403)


class TestDeleteNodeEndpoint(unittest.TestCase):
    """/delete_node 端点集成测试（真实 HTTP + 真实端点逻辑）。"""

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(ue5_bridge.app)

    def setUp(self):
        fu.reset_fake()
        self.bp = _build_test_blueprint()
        self.headers = {"X-Skill-Token": ue5_bridge._BRIDGE_TOKEN}

    def test_delete_node_success(self):
        """⑤ 三步删除成功 → broken_links 统计 + removed_from_graph"""
        resp = self.client.post("/delete_node", json={
            "blueprint_path": BP_PATH,
            "node_id": "PrintString",
        }, headers=self.headers)

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["success"])
        data = body["data"]
        self.assertEqual(data["method"], "python_api")
        self.assertTrue(data["removed_from_graph"])
        self.assertTrue(data["marked_dirty"])
        # PrintString 有 2 条连线：同图 BeginPlay.then + 跨图 OtherNode.execute
        # 跨图被跳过 → broken_links == 1（同图）
        self.assertEqual(data["broken_links"], 1)
        self.assertEqual(data["skipped_cross_graph_count"], 1)
        # 验证节点确实消失
        self.assertIsNone(self.bp.graphs["EventGraph"]._nodes.get("PrintString"))
        # 蓝图被标记脏
        self.assertTrue(self.bp.dirty)

    def test_delete_node_dry_run(self):
        """⑥ dry_run → 仅统计不删除"""
        resp = self.client.post("/delete_node", json={
            "blueprint_path": BP_PATH,
            "node_id": "PrintString",
            "dry_run": True,
        }, headers=self.headers)

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["success"])
        data = body["data"]
        self.assertTrue(data["dry_run"])
        self.assertGreaterEqual(data["broken_links"], 1)
        self.assertFalse(data["removed_from_graph"])
        # 验证节点仍然存在
        self.assertIsNotNone(self.bp.graphs["EventGraph"]._nodes.get("PrintString"))

    def test_delete_node_not_found(self):
        """⑦ 节点不存在 → success=False + detail='节点不存在'（幂等安全：重复删也返回 False）"""
        for _ in range(2):  # 连续两次，验证幂等安全（不崩溃、明确报错）
            resp = self.client.post("/delete_node", json={
                "blueprint_path": BP_PATH,
                "node_id": "NonExistentNode",
            }, headers=self.headers)

            self.assertEqual(resp.status_code, 200)
            body = resp.json()
            self.assertFalse(body["success"])
            self.assertIn("节点不存在", body["detail"])

    def test_delete_node_blueprint_not_found(self):
        """⑧ 蓝图不存在 → success=False"""
        resp = self.client.post("/delete_node", json={
            "blueprint_path": "/Game/Not/Exist",
            "node_id": "PrintString",
        }, headers=self.headers)

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertFalse(body["success"])
        self.assertIn("蓝图不存在", body["detail"])


class TestHttpContract(unittest.TestCase):
    """HTTP 契约（路由层）。"""

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(ue5_bridge.app)

    def test_unknown_endpoint_returns_404(self):
        """⑨ 未注册端点 → HTTP 404"""
        resp = self.client.post("/nonexistent_endpoint", json={})
        self.assertEqual(resp.status_code, 404)

    def test_disconnect_route_registered(self):
        """/disconnect 路由真实存在（非 404）"""
        resp = self.client.post("/disconnect", json={
            "blueprint_path": BP_PATH,
            "node_id": "BeginPlay",
            "pin_name": "then",
        }, headers={"X-Skill-Token": ue5_bridge._BRIDGE_TOKEN})
        self.assertNotEqual(resp.status_code, 404)

    def test_delete_node_route_registered(self):
        """/delete_node 路由真实存在（非 404）"""
        resp = self.client.post("/delete_node", json={
            "blueprint_path": BP_PATH,
            "node_id": "BeginPlay",
        }, headers={"X-Skill-Token": ue5_bridge._BRIDGE_TOKEN})
        self.assertNotEqual(resp.status_code, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
