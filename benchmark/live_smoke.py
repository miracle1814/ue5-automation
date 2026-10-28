#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Live smoke benchmark — checks a running editor bridge (read-only).

Usage:
    python benchmark/live_smoke.py                 # token from %APPDATA%/ue5_bridge/token
    UE5_BRIDGE_TOKEN=xxx python benchmark/live_smoke.py
Exit code 0 only when all checks pass.
"""
import json
import os
import sys
import time
import urllib.request

BASE = os.environ.get("UE5_BRIDGE_URL", "http://127.0.0.1:8889")


def _token():
    env = os.environ.get("UE5_BRIDGE_TOKEN")
    if env:
        return env.strip()
    p = os.path.join(os.environ.get("APPDATA", ""), "ue5_bridge", "token")
    try:
        return open(p, encoding="utf-8").read().strip()
    except OSError:
        return ""


TOKEN = _token()


def _get(path):
    req = urllib.request.Request(BASE + path,
                                 headers={"X-Skill-Token": TOKEN})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except Exception as e:
        return None, str(e)


def _rpc(method, params=None):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                       "params": params or {}}).encode()
    req = urllib.request.Request(BASE + "/mcp", data=body,
                                 headers={"Content-Type": "application/json",
                                          "X-Skill-Token": TOKEN},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def main():
    checks, t0 = [], time.time()
    status, body = _get("/health")
    checks.append({"check": "health", "ok": status == 200,
                   "detail": (json.loads(body).get("ue_info", {})
                              if status == 200 else body[:200])})
    status, body = _get("/diag")
    methods = (json.loads(body).get("all_methods") or []) if status == 200 else []
    checks.append({"check": "diag", "ok": status == 200 and len(methods) > 50,
                   "detail": {"methods": len(methods)}})
    try:
        tools = _rpc("tools/list")["result"]["tools"]
        checks.append({"check": "mcp_tools", "ok": len(tools) >= 5,
                       "detail": {"count": len(tools)}})
    except Exception as e:
        checks.append({"check": "mcp_tools", "ok": False, "detail": str(e)})
    status, _ = _get("/ui")
    checks.append({"check": "ui_shell", "ok": status == 200,
                   "detail": {"status": status}})
    ok = all(c["ok"] for c in checks)
    print(json.dumps({"ok": ok, "duration_s": round(time.time() - t0, 2),
                      "checks": checks}, ensure_ascii=False, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
