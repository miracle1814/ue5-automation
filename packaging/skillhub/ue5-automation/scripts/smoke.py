#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ue5-automation smoke — 5 分钟主路径冒烟。

用法：
    python scripts/smoke.py
    python scripts/smoke.py --json
    python scripts/smoke.py --name BP_Smoke --pkg /Game/Test/ --retries 2

会在项目内创建/替换 `<pkg><name>`（默认 /Game/Test/BP_Smoke），不会自动删除。
只依赖 Python 标准库；退出码 0 = 全通过，1 = 有失败。
health 解析与 doctor.py 共用 bridge_client.normalize_health。
"""
import argparse
import json
import os
import sys

from bridge_client import BridgeClient, BridgeError, read_token


def main():
    ap = argparse.ArgumentParser(description="ue5-automation smoke test")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("UE5_BRIDGE_PORT", "8889")))
    ap.add_argument("--token", default=None)
    ap.add_argument("--timeout", type=float, default=60.0)
    ap.add_argument("--retries", type=int, default=2,
                    help="连接失败/5xx 的重试次数（默认 2）")
    ap.add_argument("--name", default="BP_Smoke")
    ap.add_argument("--pkg", default="/Game/Test/")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    token = read_token(args.token)
    if not token:
        print("[FAIL] token 缺失：%APPDATA%\\ue5_bridge\\token 或 UE5_BRIDGE_TOKEN")
        return 1

    bridge = BridgeClient(args.host, args.port, token,
                          timeout=args.timeout, retries=args.retries)
    steps = []

    def fail(name, message, code="unexpected_error", hint="", http_status=None):
        detail = "%s: %s" % (code, message)
        if hint:
            detail += "（%s）" % hint
        steps.append({
            "step": name,
            "ok": False,
            "detail": detail,
            "code": code,
            "message": str(message),
            "hint": hint,
            "http_status": http_status,
        })
        if not args.json:
            print("[FAIL] %-22s %s" % (name, detail))
        return False

    def run(name, fn):
        try:
            detail = fn()
            steps.append({"step": name, "ok": True, "detail": detail})
            if not args.json:
                print("[PASS] %-22s %s" % (name, detail))
            return True
        except BridgeError as e:
            return fail(name, e.message, e.code, e.hint, e.http_status)
        except Exception as e:
            return fail(name, str(e))

    state = {}
    bp_path = args.pkg.rstrip("/") + "/" + args.name

    def step_health():
        r = bridge.health()
        state["health"] = r
        if r.get("status") != "ok" or not r.get("bridge_loaded"):
            raise BridgeError(
                "health 未通过：status=%s bridge_loaded=%s"
                % (r.get("status") or "?", r.get("bridge_loaded")),
                "health_failed",
                hint="确认 UE 编辑器已启动，且 bridge 服务在 127.0.0.1:%d" % args.port)
        return "engine=%s project=%s" % (r.get("engine_version") or "?",
                                         r.get("project_name") or "?")

    def step_build():
        def build():
            return bridge.call("build_blueprint", {
                "bp_name": args.name, "pkg": args.pkg, "parent": "Actor"})

        r = build()
        if not r.get("success") and "已存在" in str(r.get("error") or ""):
            # 重复运行：目标资产已存在 → 只删除这个固定的测试资产后重建
            bridge.call("delete_asset", {"asset_path": bp_path, "confirm": True})
            r = build()
        if not r.get("success"):
            raise BridgeError(
                r.get("error") or "build_blueprint success=false",
                "build_failed",
                hint="确认 /Game/Test 可写；重复运行时脚本会先删除 BP_Smoke")
        return bp_path

    def step_search():
        r = bridge.call("search_blueprint_nodes", {
            "bp_path": bp_path, "query": "BeginPlay", "limit": 10})
        total = r.get("total_matches", 0)
        if not total:
            raise BridgeError("BeginPlay 候选为空", "search_empty",
                              hint="先确认 build_blueprint 已成功，再检查事件目录")
        return "matches=%s" % total

    def step_apply():
        spec = {
            "graph": "EventGraph",
            "nodes": [
                {"id": "begin", "kind": "event", "target": "ReceiveBeginPlay"},
                {"id": "print", "kind": "function",
                 "class": "/Script/Engine.KismetSystemLibrary",
                 "target": "PrintString"},
            ],
            "edges": [{"from": "begin.then", "to": "print.execute"}],
            "pin_defaults": {"print.InString": "Hello from skill smoke"},
        }
        r = bridge.call("apply_graph", {
            "bp_path": bp_path, "replace": True, "graph_spec": spec})
        if not r.get("verified"):
            raise BridgeError(
                "verified=false: %s" % (r.get("errors") or r),
                "apply_unverified",
                hint="先 read_blueprint 看实际节点/引脚，再改 graph_spec")
        state["apply"] = r
        return "nodes=%s edges=%s" % (r.get("nodes_applied"), r.get("edges_applied"))

    def step_compile():
        r = bridge.call("compile_and_diagnose", {"bp_path": bp_path})
        if not r.get("compiled"):
            raise BridgeError(
                "compiled=false: %s" % (r.get("errors") or r),
                "compile_failed",
                hint="按 compile_and_diagnose 返回的 node_guid 定位节点")
        return "status=%s errors=%s" % (r.get("compile_status"), r.get("num_errors"))

    def step_read_pin():
        guids = (state.get("apply") or {}).get("node_guids") or {}
        guid = guids.get("print")
        if not guid:
            raise BridgeError("apply_graph 未返回 node_guids.print",
                              "missing_node_guid",
                              hint="确认 apply_graph 返回里含 node_guids")
        r = bridge.call("get_pin_default_value", {
            "bp_path": bp_path, "node_id": guid, "pin_name": "InString"})
        value = r.get("value")
        if value != "Hello from skill smoke":
            raise BridgeError("readback=%r" % value, "readback_mismatch",
                              hint="确认节点 GUID 与引脚名正确")
        return "InString=%r" % value

    def step_save():
        r = bridge.call("save_asset", {"asset_path": bp_path})
        if not r.get("saved"):
            raise BridgeError("saved=false", "save_failed",
                              hint="确认资产未被占用且磁盘可写")
        return "saved=true"

    for name, fn in [
        ("health", step_health),
        ("build_blueprint", step_build),
        ("search_nodes", step_search),
        ("apply_graph", step_apply),
        ("compile", step_compile),
        ("read_pin", step_read_pin),
        ("save_asset", step_save),
    ]:
        if not run(name, fn):
            break

    ok = all(s["ok"] for s in steps)
    out = {"ok": ok, "blueprint": bp_path, "steps": steps}
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print("\n%s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
