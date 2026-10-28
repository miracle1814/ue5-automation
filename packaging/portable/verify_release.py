#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ue5-automation 发布自检（v6.5 · 无需 UE）。

用法（仓库根或移植包根）：
    python packaging\\portable\\verify_release.py          # 源码仓库
    python tools\\verify_release.py                        # 移植包
    python tools\\verify_release.py --json
    python tools\\verify_release.py --write-manifest
    python tools\\verify_release.py --compare D:\\other\\RELEASE-MANIFEST.json

检查：
  1. 版本号（VERSION.txt 或 manifest.json）；
  2. MCP profile 计数 + 白名单命令数；
  3. 安全不变量（日志越界 / 幂等冲突 / 引擎资产写拒绝 / 场景高危 /
     请求体上限 / token 无默认值）；
  4. 可选：离线测试全绿（仅源码仓库；移植包不含 tests）；
  5. 可选：逐文件 SHA256 清单生成/对比。
退出码：0 = 全过；1 = 有失败；2 = 环境/导入错误。
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import platform
import subprocess
import sys
import tempfile
import time
from pathlib import Path

VERSION_EXPECT = "v6.5.4"
PROFILE_EXPECT = {"gateway": 12, "core": 18, "full": 34}
COMMAND_EXPECT = 101
MANIFEST_NAME = "RELEASE-MANIFEST.json"
EXCLUDE_DIRS = {".git", "__pycache__", ".pytest_cache", ".mypy_cache",
                "dist", "Saved", "Intermediate", "node_modules"}
EXCLUDE_SUFFIXES = {".pyc", ".pyo", ".log"}
EXCLUDE_FILES = {MANIFEST_NAME, "token"}

HERE = Path(__file__).resolve().parent


def find_root() -> Path:
    """同时支持仓库（packaging/portable/）与移植包（tools/）两种布局。"""
    for cand in (HERE.parent, HERE.parent.parent, HERE.parent.parent.parent):
        if (cand / "host" / "python" / "ue5_bridge.py").is_file():
            return cand
        if (cand / "skill" / "ue5-automation" / "scripts" / "ue5_bridge.py").is_file():
            return cand
    env = os.environ.get("UE5_REPO_ROOT")
    if env and Path(env).is_dir():
        return Path(env)
    return HERE.parent


ROOT = find_root()


def py_dir() -> Path:
    p = ROOT / "host" / "python" / "ue5_bridge.py"
    if p.is_file():
        return p.parent
    return ROOT / "skill" / "ue5-automation" / "scripts"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def iter_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDE_DIRS)
        for name in sorted(filenames):
            if name in EXCLUDE_FILES:
                continue
            if Path(name).suffix.lower() in EXCLUDE_SUFFIXES:
                continue
            p = Path(dirpath) / name
            if not p.is_file():
                continue
            yield p.relative_to(root).as_posix()


def version_info() -> dict:
    out = {"expected": VERSION_EXPECT, "found": "", "source": ""}
    vtxt = ROOT / "VERSION.txt"
    if vtxt.is_file():
        text = vtxt.read_text(encoding="utf-8", errors="replace")
        out["found"] = text.splitlines()[0].strip() if text else ""
        out["source"] = "VERSION.txt"
        return out
    mf = ROOT / "manifest.json"
    if mf.is_file():
        try:
            data = json.loads(mf.read_text(encoding="utf-8"))
            out["found"] = str(data.get("package_version") or "")
            out["source"] = "manifest.json"
        except Exception as e:
            out["error"] = str(e)
    return out


def import_bridge():
    sys.path.insert(0, str(py_dir()))
    os.environ.setdefault("UE5_BRIDGE_TEST_MODE", "1")
    with contextlib.redirect_stdout(sys.stderr):
        import ue5_bridge as b  # noqa: E402
    return b


def check(report: dict, name: str, ok: bool, detail=None):
    report["checks"].append({"name": name, "ok": bool(ok), "detail": detail})
    if not ok:
        report["ok"] = False


def check_version(report: dict):
    vi = version_info()
    ok = VERSION_EXPECT in str(vi.get("found") or "")
    check(report, "version", ok, vi)


def check_profiles(report: dict, b):
    try:
        counts = {}
        for profile in ("gateway", "core", "full"):
            old = os.environ.get("UE5_MCP_PROFILE")
            os.environ["UE5_MCP_PROFILE"] = profile
            try:
                counts[profile] = len(b._mcp_tool_defs_core())
            finally:
                if old is None:
                    os.environ.pop("UE5_MCP_PROFILE", None)
                else:
                    os.environ["UE5_MCP_PROFILE"] = old
        cmds = len(b._COMMAND_WHITELIST)
        ok = counts == PROFILE_EXPECT and cmds == COMMAND_EXPECT
        check(report, "tool_profiles", ok, {
            "counts": counts, "expected": PROFILE_EXPECT,
            "commands": cmds, "commands_expected": COMMAND_EXPECT,
        })
    except Exception as e:
        check(report, "tool_profiles", False,
              {"error": "%s: %s" % (type(e).__name__, e)})


def check_security(report: dict, b):
    details = {}
    ok = True

    # 1) 日志越界：临时 monkeypatch 允许根，避免把系统临时目录当“外部”
    allowed = Path(tempfile.mkdtemp())
    outside = Path(tempfile.mkdtemp())
    good = allowed / "ue.log"
    bad = outside / "secret.log"
    good.write_text("OK\n", encoding="utf-8")
    bad.write_text("SECRET\n", encoding="utf-8")
    old_roots = b._allowed_log_roots
    try:
        b._allowed_log_roots = lambda: [os.path.normcase(str(allowed))]
        b._cmd_logs_read(10, pattern="OK", log_file=str(good))
        details["log_allowed"] = True
        try:
            b._cmd_logs_read(10, pattern="SECRET", log_file=str(bad))
            details["log_outside_rejected"] = False
            ok = False
        except b.BridgeError as e:
            details["log_outside_rejected"] = e.category == "logs"
    except Exception as e:
        details["log_error"] = "%s: %s" % (type(e).__name__, e)
        ok = False
    finally:
        b._allowed_log_roots = old_roots

    # 2) 幂等键跨命令冲突
    with b._LOCKS_LOCK:
        b._ASSET_LOCKS.clear()
        b._SESSION_LOCKS.clear()
    with b._IDEMPOTENCY_LOCK:
        b._IDEMPOTENCY.clear()

    def fake(**kw):
        return {"success": True}

    b._dispatch_with_tx("apply_graph", fake, bp_path="/Game/BP_X",
                        graph_spec={}, idempotency_key="verify-key")
    try:
        b._dispatch_with_tx("build_blueprint", fake,
                            idempotency_key="verify-key")
        details["idempotency_conflict"] = False
        ok = False
    except b.BridgeError as e:
        details["idempotency_conflict"] = e.category == "idempotency_conflict"

    # 3) 引擎资产写拒绝
    try:
        b._cmd_save_asset("/Engine/EngineMaterials/DefaultMaterial")
        details["engine_asset_rejected"] = False
        ok = False
    except b.BridgeError as e:
        details["engine_asset_rejected"] = e.category == "param_type"

    # 4) 场景高危默认拒绝
    details["scenario_destructive_default"] = bool(
        b._scenario_is_destructive("delete_asset", {}))

    # 5) 请求体上限存在
    details["http_body_limit"] = int(getattr(b, "_MAX_REQUEST_BYTES", 0))

    # 6) token 无 test123 回退
    try:
        import ue5_runner
        old_env = os.environ.get("UE5_BRIDGE_TOKEN")
        old_appdata = os.environ.get("APPDATA")
        os.environ.pop("UE5_BRIDGE_TOKEN", None)
        os.environ["APPDATA"] = tempfile.mkdtemp()
        try:
            tok = ue5_runner._resolve_bridge_token()
        finally:
            if old_env is None:
                os.environ.pop("UE5_BRIDGE_TOKEN", None)
            else:
                os.environ["UE5_BRIDGE_TOKEN"] = old_env
            if old_appdata is None:
                os.environ.pop("APPDATA", None)
            else:
                os.environ["APPDATA"] = old_appdata
        details["token_no_default"] = (tok == "")
    except Exception as e:
        details["token_no_default"] = False
        details["token_error"] = "%s: %s" % (type(e).__name__, e)

    for key in ("log_allowed", "log_outside_rejected",
                "idempotency_conflict", "engine_asset_rejected",
                "scenario_destructive_default", "token_no_default"):
        if not details.get(key):
            ok = False
    if details.get("http_body_limit", 0) <= 0:
        ok = False
    check(report, "security_invariants", ok, details)


def check_tests(report: dict, run: bool):
    if not run:
        return
    tests = ROOT / "skill" / "ue5-automation" / "tests"
    if not tests.is_dir():
        check(report, "offline_tests", True,
              {"skipped": True, "reason": "移植包不含 tests（源码仓库执行）"})
        return
    t0 = time.time()
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", str(tests)],
        cwd=str(ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace")
    tail = (proc.stdout or "")[-2000:]
    check(report, "offline_tests", proc.returncode == 0, {
        "returncode": proc.returncode,
        "duration_s": round(time.time() - t0, 1),
        "tail": tail,
    })


def build_manifest() -> dict:
    files = {}
    for rel in iter_files(ROOT):
        p = ROOT / rel
        try:
            files[rel] = {"size": p.stat().st_size, "sha256": sha256_file(p)}
        except OSError as e:
            files[rel] = {"error": str(e)}
    return {
        "name": "ue5-automation",
        "version": VERSION_EXPECT,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "file_count": len(files),
        "files": files,
    }


def compare_manifests(a: dict, b: dict) -> dict:
    fa, fb = a.get("files") or {}, b.get("files") or {}
    ka, kb = set(fa), set(fb)
    changed = []
    for rel in sorted(ka & kb):
        if fa[rel].get("sha256") != fb[rel].get("sha256"):
            changed.append({"path": rel, "a": fa[rel].get("sha256"),
                            "b": fb[rel].get("sha256")})
    return {
        "a_version": a.get("version"), "b_version": b.get("version"),
        "added_in_b": sorted(kb - ka), "missing_in_b": sorted(ka - kb),
        "changed": changed, "same": len(ka & kb) - len(changed),
        "ok": not (kb - ka) and not (ka - kb) and not changed,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="ue5-automation 发布自检（无需 UE）")
    ap.add_argument("--json", action="store_true", help="JSON 输出")
    ap.add_argument("--write-manifest", action="store_true",
                    help="生成 RELEASE-MANIFEST.json")
    ap.add_argument("--compare", default="",
                    help="与另一份 RELEASE-MANIFEST.json 对比")
    ap.add_argument("--run-tests", action="store_true",
                    help="额外跑源码仓库的离线测试（较慢）")
    args = ap.parse_args(argv)

    report = {"ok": True, "root": str(ROOT), "checks": []}
    try:
        b = import_bridge()
    except Exception as e:
        print("无法导入 ue5_bridge：%s: %s" % (type(e).__name__, e),
              file=sys.stderr)
        print("请确认本工具位于仓库/移植包内，且已安装 fastapi/uvicorn/pydantic。",
              file=sys.stderr)
        return 2

    check_version(report)
    check_profiles(report, b)
    check_security(report, b)
    check_tests(report, args.run_tests)

    manifest_path = ROOT / MANIFEST_NAME
    if args.write_manifest:
        mf = build_manifest()
        manifest_path.write_text(json.dumps(mf, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
        check(report, "manifest_write", True,
              {"path": str(manifest_path), "file_count": mf["file_count"]})
    if args.compare:
        try:
            a = json.loads(manifest_path.read_text(encoding="utf-8"))
            bmf = json.loads(Path(args.compare).read_text(encoding="utf-8"))
            cmp = compare_manifests(a, bmf)
            check(report, "manifest_compare", cmp["ok"], cmp)
        except Exception as e:
            check(report, "manifest_compare", False,
                  {"error": "%s: %s" % (type(e).__name__, e)})

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print("ue5-automation 发布自检")
        print("根目录: %s" % ROOT)
        print("-" * 64)
        for c in report["checks"]:
            print("%s  %s" % ("OK " if c["ok"] else "FAIL", c["name"]))
            if not c["ok"]:
                print("      %s" % json.dumps(c["detail"], ensure_ascii=False))
        print("-" * 64)
        print("结论：%s" % ("通过" if report["ok"] else "存在失败项"))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
