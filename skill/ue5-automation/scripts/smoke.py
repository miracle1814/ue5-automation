#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ue5-automation smoke — 5 分钟主路径冒烟。

用法：
    python scripts/smoke.py
    python scripts/smoke.py --json
    python scripts/smoke.py --name BP_Smoke --pkg /Game/Test/

会在项目内创建/替换 `<pkg><name>`（默认 /Game/Test/BP_Smoke），不会自动删除。
只依赖 Python 标准库；退出码 0 = 全通过，1 = 有失败。
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request


def read_token(explicit=None):
    if explicit:
        return explicit.strip()
    env = os.environ.get("UE5_BRIDGE_TOKEN", "").strip()
    if env:
        return env
    appdata = os.environ.get("APPDATA") or os.path.expanduser("~")
    path = os.path.join(appdata, "ue5_bridge", "token")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


class Bridge(object):
    def __init__(self, host, port, token, timeout):
        self.url = "http://%s:%d/command" % (host, port)
        self.token = token
        self.timeout = timeout

    def call(self, command, params=None):
        body = json.dumps({"command": command, "params": params or {}}).encode("utf-8")
        req = urllib.request.Request(
            self.url, data=body,
            headers={"Content-Type": "application/json", "X-Skill-Token": self.token})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            raise RuntimeError("HTTP %s: %s" % (e.code, e.read().decode("utf-8", "replace")[:300]))
        data = json.loads(raw)
        if not data.get("success"):
            raise RuntimeError(data.get("error") or raw[:300])
        return data.get("result") or {}


def main():
    ap = argparse.ArgumentParser(description="ue5-automation smoke test")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=int(os.environ.get("UE5_BRIDGE_PORT", "8889")))
    ap.add_argument("--token", default=None)
    ap.add_argument("--timeout", type=float, default=60.0)
    ap.add_argument("--name", default="BP_Smoke")
    ap.add_argument("--pkg", default="/Game/Test/")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    token = read_token(args.token)
    if not token:
        print("[FAIL] token 缺失：%APPDATA%\\ue5_bridge\\token 或 UE5_BRIDGE_TOKEN")
        return 1

    bridge = Bridge(args.host, args.port, token, args.timeout)
    steps = []

    def run(name, fn):
        try:
            detail = fn()
            steps.append({"step": name, "ok": True, "detail": detail})
            if not args.json:
                print("[PASS] %-22s %s" % (name, detail))
            return True
        except Exception as e:
            steps.append({"step": name, "ok": False, "detail": str(e)})
            if not args.json:
                print("[FAIL] %-22s %s" % (name, e))
            return False

    state = {}
    bp_path = args.pkg.rstrip("/") + "/" + args.name

    def step_health():
        r = bridge.call("health")
        state["health"] = r
        if not r.get("bridge_loaded"):
            raise RuntimeError("bridge_loaded=false")
        return "engine=%s project=%s" % (r.get("engine_version", "?"),
                                         r.get("project_name", "?"))

    def step_build():
        def build():
            return bridge.call("build_blueprint", {
                "bp_name": args.name, "pkg": args.pkg, "parent": "Actor"})

        try:
            r = build()
        except RuntimeError as e:
            r = {"success": False, "error": str(e)}
        # 重复运行：目标资产已存在 → 只删除这个固定的测试资产后重建
        if not r.get("success") and "已存在" in str(r.get("error") or ""):
            bridge.call("delete_asset", {"asset_path": bp_path, "confirm": True})
            try:
                r = build()
            except RuntimeError as e:
                r = {"success": False, "error": str(e)}
        if not r.get("success"):
            raise RuntimeError(r.get("error") or "success=false")
        return bp_path

    def step_search():
        r = bridge.call("search_blueprint_nodes", {
            "bp_path": bp_path, "query": "BeginPlay", "limit": 10})
        total = r.get("total_matches", 0)
        if not total:
            raise RuntimeError("BeginPlay 候选为空")
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
            raise RuntimeError("verified=false: %s" % (r.get("errors") or r))
        state["apply"] = r
        return "nodes=%s edges=%s" % (r.get("nodes_applied"), r.get("edges_applied"))

    def step_compile():
        r = bridge.call("compile_and_diagnose", {"bp_path": bp_path})
        if not r.get("compiled"):
            raise RuntimeError("compiled=false: %s" % (r.get("errors") or r))
        return "status=%s errors=%s" % (r.get("compile_status"), r.get("num_errors"))

    def step_read_pin():
        guids = (state.get("apply") or {}).get("node_guids") or {}
        guid = guids.get("print")
        if not guid:
            raise RuntimeError("apply_graph 未返回 node_guids.print")
        r = bridge.call("get_pin_default_value", {
            "bp_path": bp_path, "node_id": guid, "pin_name": "InString"})
        value = r.get("value")
        if value != "Hello from skill smoke":
            raise RuntimeError("readback=%r" % value)
        return "InString=%r" % value

    def step_save():
        r = bridge.call("save_asset", {"asset_path": bp_path})
        if not r.get("saved"):
            raise RuntimeError("saved=false")
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
