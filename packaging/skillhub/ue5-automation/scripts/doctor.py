#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ue5-automation doctor — 本机 bridge 体检。

用法：
    python scripts/doctor.py
    python scripts/doctor.py --json
    python scripts/doctor.py --port 8890 --token <token> --retries 3

只依赖 Python 标准库；退出码 0 = 通过，1 = 有 FAIL。
health 解析与 smoke.py 共用 bridge_client.normalize_health，避免两套口径。
"""
import argparse
import json
import os
import sys

from bridge_client import BridgeClient, BridgeError, read_token


def main():
    ap = argparse.ArgumentParser(description="ue5-automation bridge doctor")
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("UE5_BRIDGE_PORT", "8889")))
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--token", default=None)
    ap.add_argument("--timeout", type=float, default=8.0)
    ap.add_argument("--retries", type=int, default=2,
                    help="连接失败/5xx 的重试次数（默认 2）")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    checks = []

    def add(name, ok, detail="", **extra):
        item = {"name": name, "ok": bool(ok), "detail": detail}
        item.update(extra)
        checks.append(item)

    token = read_token(args.token)
    add("token", bool(token),
        "found" if token else "missing: %APPDATA%\\ue5_bridge\\token or UE5_BRIDGE_TOKEN")

    health = None
    if token:
        client = BridgeClient(args.host, args.port, token,
                              timeout=args.timeout, retries=args.retries)
        try:
            health = client.health()
            add("health", health.get("status") == "ok",
                "status=%s" % (health.get("status") or "?"))
        except BridgeError as e:
            add("health", False, e.message, **e.to_dict())

    if health:
        add("bridge_loaded", health.get("bridge_loaded"),
            "engine=%s project=%s" % (health.get("engine_version") or "?",
                                      health.get("project_name") or "?"))
        add("bridge_version", bool(health.get("bridge_version")),
            "version=%s" % (health.get("bridge_version") or "?"))
        add("project_dir", bool(health.get("project_dir")),
            health.get("project_dir") or "?")

    failed = [c for c in checks if not c["ok"]]
    out = {
        "ok": not failed,
        "host": args.host,
        "port": args.port,
        "checks": checks,
        "health": health,
    }
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        for c in checks:
            detail = c["detail"]
            if not c["ok"] and c.get("code"):
                detail = "%s: %s" % (c["code"], detail)
                if c.get("hint"):
                    detail += "（%s）" % c["hint"]
            print("[%s] %-14s %s" % ("PASS" if c["ok"] else "FAIL",
                                     c["name"], detail))
        print("\n%s" % ("PASS" if out["ok"] else "FAIL"))
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
