# -*- coding: utf-8 -*-
"""检查 MCP 工具面 profile（v6.5）—— 无需 UE，需要桥依赖。

用法：
    python tools\\check_tool_profile.py
    python tools\\check_tool_profile.py --json

以 TEST_MODE 导入 ue5_bridge，分别设置 UE5_MCP_PROFILE=gateway/core/full，
调用 _mcp_tool_defs_core()。期望 12 / 18 / 34，白名单 101 条。
"""
import argparse
import json
import os
import sys

PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY_DIR = os.path.join(PKG_ROOT, "host", "python")
if not os.path.isfile(os.path.join(PY_DIR, "ue5_bridge.py")):
    PY_DIR = os.path.join(PKG_ROOT, "skill", "ue5-automation", "scripts")
if PY_DIR not in sys.path:
    sys.path.insert(0, PY_DIR)

os.environ.setdefault("UE5_BRIDGE_TEST_MODE", "1")

try:
    import ue5_bridge
except Exception as exc:  # pragma: no cover
    print("无法导入 ue5_bridge：%r" % (exc,), file=sys.stderr)
    print("请确认本脚本位于移植包 tools\\ 下、host/python 完整，"
          "且已安装 fastapi/uvicorn/pydantic。", file=sys.stderr)
    sys.exit(2)

EXPECTED = {"gateway": 12, "core": 18, "full": 34}
COMMAND_EXPECT = 101


def catalog(profile):
    old = os.environ.get("UE5_MCP_PROFILE")
    os.environ["UE5_MCP_PROFILE"] = profile
    try:
        tools = ue5_bridge._mcp_tool_defs_core()
    finally:
        if old is None:
            os.environ.pop("UE5_MCP_PROFILE", None)
        else:
            os.environ["UE5_MCP_PROFILE"] = old
    return [str(t.get("name", "")) for t in tools]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()
    result = {}
    ok = True
    for profile in ("gateway", "core", "full"):
        names = catalog(profile)
        result[profile] = {
            "count": len(names), "expected": EXPECTED[profile],
            "ok": len(names) == EXPECTED[profile], "tools": names,
        }
        ok = ok and result[profile]["ok"]
    cmds = len(ue5_bridge._COMMAND_WHITELIST)
    result["commands"] = {"count": cmds, "expected": COMMAND_EXPECT,
                          "ok": cmds == COMMAND_EXPECT}
    ok = ok and result["commands"]["ok"]

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("MCP 工具面 profile（v6.5）")
        print("-" * 64)
        for profile in ("gateway", "core", "full"):
            r = result[profile]
            print("%s  %-8s %2d/%2d  %s" % (
                "OK " if r["ok"] else "FAIL", profile, r["count"],
                r["expected"], ", ".join(r["tools"])))
        r = result["commands"]
        print("%s  commands %d/%d" % ("OK " if r["ok"] else "FAIL",
                                      r["count"], r["expected"]))
        print("-" * 64)
        print("结论：%s" % ("通过" if ok else "不符合预期"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
