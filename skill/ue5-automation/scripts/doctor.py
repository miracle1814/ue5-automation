#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ue5-automation doctor — 本机 bridge 体检。

用法：
    python scripts/doctor.py
    python scripts/doctor.py --json
    python scripts/doctor.py --port 8890 --token <token>

只依赖 Python 标准库；退出码 0 = 通过，1 = 有 FAIL。
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


def http_json(url, token, timeout):
    req = urllib.request.Request(url, headers={"X-Skill-Token": token})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode("utf-8", "replace")
    return json.loads(raw)


def main():
    ap = argparse.ArgumentParser(description="ue5-automation bridge doctor")
    ap.add_argument("--port", type=int, default=int(os.environ.get("UE5_BRIDGE_PORT", "8889")))
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--token", default=None)
    ap.add_argument("--timeout", type=float, default=8.0)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    checks = []

    def add(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    token = read_token(args.token)
    add("token", bool(token),
        "found" if token else "missing: %%APPDATA%%\\ue5_bridge\\token or UE5_BRIDGE_TOKEN")

    health = None
    if token:
        try:
            health = http_json("http://%s:%d/health" % (args.host, args.port),
                               token, args.timeout)
            add("health", health.get("status") == "ok",
                "status=%s" % health.get("status"))
        except urllib.error.HTTPError as e:
            add("health", False, "HTTP %s (token mismatch?)" % e.code)
        except Exception as e:
            add("health", False, "%s" % e)

    if health:
        ue = health.get("ue_info") or {}
        add("bridge_loaded", bool(ue.get("bridge_loaded")),
            "engine=%s project=%s" % (ue.get("engine_version", "?"),
                                      ue.get("project_name", "?")))
        add("bridge_version", bool(health.get("bridge_version")),
            "version=%s" % health.get("bridge_version", "?"))
        add("project_dir", bool(ue.get("project_dir")),
            ue.get("project_dir", "?"))

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
            print("[%s] %-14s %s" % ("PASS" if c["ok"] else "FAIL",
                                     c["name"], c["detail"]))
        print("\n%s" % ("PASS" if out["ok"] else "FAIL"))
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
