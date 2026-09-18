#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""UE 版本自检 —— 判定当前引擎版本能否直接用本发布包，并给出下一步。

只读工具：不写文件、不改环境变量、不改注册表。

用法：
    python scripts/check_ue_version.py                          # 自动探测引擎
    python scripts/check_ue_version.py --engine "C:\\Program Files\\Epic Games\\UE_5.1"
    python scripts/check_ue_version.py --online                 # 追加在线探测（需编辑器+bridge 在跑）
    python scripts/check_ue_version.py --json                   # 机器可读输出

退出码：
    0 = UE 5.1.x      -> 开箱即用（UE_VERSION_GUIDE.md 路径 A）
    2 = UE 5.2 - 5.5  -> 需重编译 bridge（路径 B）
    3 = 其他/未知版本、或未检测到引擎（路径 C / 手动指定 --engine）

数据来源（权威）：
    - 离线：<引擎根>\\Engine\\Build\\Build.version（引擎自带 JSON）
    - 在线：http://127.0.0.1:<port>/health 的 ue_info.engine_version
            http://127.0.0.1:<port>/diag   的 bridge_available / all_methods

对应文档：UE_VERSION_GUIDE.md（版本适配与升级指南）
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import urllib.error
import urllib.request

GUIDE = "UE_VERSION_GUIDE.md"

# 判定区间
COMPAT = (5, 1)                      # 随包预编译 DLL 的实测版本
REBUILD_RANGE = {(5, 2), (5, 3), (5, 4), (5, 5)}   # 需自行重编译（架构上可行，未逐版本实测）
MIN_SUPPORTED = (5, 1)               # 4.27 及更早：不在适配范围

# UE 5.1 随包基线（实测 2026-09-15，同一台编辑器 /diag 输出）——
# 仅作为"升级后是否退化"的对照参考，非硬性判据（不同引擎版本上方法集本就会增减）。
BASELINE_51 = {"method_count": 80, "missing_apis": 3}

EXIT_OK = 0
EXIT_REBUILD = 2
EXIT_UNKNOWN = 3


# ── 版本读取 ─────────────────────────────────────────────────────
def read_build_version(engine_root):
    """读 <引擎根>/Engine/Build/Build.version，返回 dict 或 None。"""
    p = os.path.join(engine_root, "Engine", "Build", "Build.version")
    if not os.path.isfile(p):
        return None
    try:
        with open(p, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    data["_path"] = p
    return data


def version_tuple(bv):
    try:
        return (int(bv.get("MajorVersion", 0)), int(bv.get("MinorVersion", 0)),
                int(bv.get("PatchVersion", 0)))
    except Exception:
        return (0, 0, 0)


# ── 引擎定位（只读）───────────────────────────────────────────────
def candidate_from_registry():
    """扫 HKLM/HKCU 下 Epic 的引擎安装记录（仅 Windows，失败即忽略）。"""
    roots = []
    try:
        import winreg  # noqa: F401  (仅 Windows 存在)
    except ImportError:
        return roots
    for hive_name, hive in (("HKLM", winreg.HKEY_LOCAL_MACHINE),
                            ("HKCU", winreg.HKEY_CURRENT_USER)):
        try:
            base = winreg.OpenKey(hive, r"SOFTWARE\EpicGames\Unreal Engine")
        except OSError:
            continue
        i = 0
        while True:
            try:
                sub = winreg.EnumKey(base, i)
            except OSError:
                break
            i += 1
            try:
                with winreg.OpenKey(base, sub) as k:
                    path, _ = winreg.QueryValueEx(k, "InstalledDirectory")
                if path and os.path.isdir(path):
                    roots.append((f"registry {hive_name}\\SOFTWARE\\EpicGames\\Unreal Engine\\{sub}", path))
            except OSError:
                continue
    return roots


def candidate_from_common_dirs():
    roots = []
    patterns = []
    for drive in ("C:", "D:", "E:", "F:"):
        patterns.append(f"{drive}\\Program Files\\Epic Games\\UE_*")
        patterns.append(f"{drive}\\Epic Games\\UE_*")
        patterns.append(f"{drive}\\Program Files (x86)\\Epic Games\\UE_*")
    for pat in patterns:
        for path in sorted(glob.glob(pat)):
            if os.path.isdir(path):
                roots.append(("常见安装目录", path))
    return roots


def locate_engines(explicit=None):
    """返回 [(来源, 引擎根)] —— 按优先级去重。"""
    found = []
    seen = set()

    def push(src, path):
        if not path:
            return
        path = os.path.abspath(path)
        key = path.lower()
        if key in seen:
            return
        if not os.path.isdir(path):
            return
        seen.add(key)
        found.append((src, path))

    if explicit:
        push("--engine 指定", explicit)
    push("环境变量 UE5_ENGINE_PATH", os.environ.get("UE5_ENGINE_PATH"))
    for src, path in candidate_from_registry():
        push(src, path)
    for src, path in candidate_from_common_dirs():
        push(src, path)
    return found


# ── 判定 ────────────────────────────────────────────────────────
def classify(major, minor):
    if (major, minor) == COMPAT:
        return ("A", EXIT_OK, "开箱即用（随包预编译 DLL 的实测版本）",
                "按 DEPLOY.md 部署即可；本文其余章节无需阅读。")
    if (major, minor) in REBUILD_RANGE:
        return ("B", EXIT_REBUILD, "需重编译 bridge（架构上可行，未逐版本实测）",
                "走路径 B：取得 bridge 源码 -> 按目标引擎编译 -> 替换 DLL -> 跑验收清单。"
                "拿不到源码时先看路径 8「出口与降级」。")
    if major < MIN_SUPPORTED[0] or (major == 5 and minor < MIN_SUPPORTED[1]):
        return ("X", EXIT_UNKNOWN, "不在适配范围（4.27 及更早）",
                "UEdGraph / Editor 子系统 API 层差异过大，无升级出口；"
                "请改用 UE 5.1.x（或换用其他支持你所持版本的工具）。")
    return ("C", EXIT_UNKNOWN, "版本未经验证（未知）",
            "走路径 C：先按路径 B 完整编译一遍，拿到具体编译错误后按错误分类表处理。")


# ── 在线探测（可选）──────────────────────────────────────────────
def resolve_token():
    """与 ue5_runner 同口径：环境变量 UE5_BRIDGE_TOKEN -> %APPDATA%/ue5_bridge/token -> test123。"""
    tok = os.environ.get("UE5_BRIDGE_TOKEN")
    if tok:
        return tok.strip()
    appdata = os.environ.get("APPDATA")
    cands = []
    if appdata:
        cands.append(os.path.join(appdata, "ue5_bridge", "token"))
    cands.append(os.path.join(os.path.expanduser("~"), "AppData", "Roaming", "ue5_bridge", "token"))
    for c in cands:
        try:
            if os.path.isfile(c):
                with open(c, "r", encoding="utf-8", errors="replace") as f:
                    v = f.read().strip()
                if v:
                    return v
        except Exception:
            continue
    return "test123"


def http_get(path, port, token, timeout=5.0):
    url = f"http://127.0.0.1:{int(port)}{path}"
    req = urllib.request.Request(url)
    req.add_header("X-Skill-Token", token)
    # v2.20 修复（P2）：旧实现走全局 opener → 遵循系统/环境代理设置。
    #   企业代理或脏 DNS 下 127.0.0.1 请求可能被送进代理 → 误报「编辑器未启动」。
    #   本机探测必须直连（ProxyHandler({}) = 禁用一切代理）。
    _opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with _opener.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def online_probe(port):
    """返回 (结果 dict, 错误提示 str|None)。"""
    token = resolve_token()
    out = {"http_port": port}
    for name, path in (("health", "/health"), ("diag", "/diag")):
        try:
            out[name] = http_get(path, port, token)
        except urllib.error.HTTPError as e:
            if e.code == 403:
                return out, ("HTTP 403：X-Skill-Token 不匹配。检查 %APPDATA%\\ue5_bridge\\token "
                             "与编辑器内 ue5_bridge 使用的 token 是否一致。")
            return out, f"HTTP {e.code}：{path} 请求失败。"
        except urllib.error.URLError as e:
            return out, ("连接不上 127.0.0.1:%d（编辑器未启动 / bridge 未加载 / 端口被占）。"
                         "此错误与引擎版本无关，先按 docs/SOP.md 排查连通性。" % port)
        except Exception as e:
            return out, f"{path} 探测异常：{type(e).__name__}: {e}"
    return out, None


# ── 输出 ────────────────────────────────────────────────────────
def emit(human_lines, payload, as_json):
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for line in human_lines:
            print(line)


def main(argv=None):
    ap = argparse.ArgumentParser(description="UE 版本自检（只读）")
    ap.add_argument("--engine", default=None, help="UE 引擎根目录（如 C:\\Program Files\\Epic Games\\UE_5.1）")
    ap.add_argument("--online", action="store_true", help="追加在线探测（需 UE 编辑器与 bridge 在跑）")
    ap.add_argument("--port", type=int, default=8889, help="bridge HTTP 端口（默认 8889）")
    ap.add_argument("--json", action="store_true", help="输出 JSON（机器可读）")
    args = ap.parse_args(argv)

    lines = []
    payload = {"tool": "check_ue_version", "guide": GUIDE}

    # 显式指定的引擎目录必须存在 —— 不静默回退到自动探测（避免"我指定了 A，它却报 B"）
    if args.engine and not os.path.isdir(args.engine):
        payload.update({"status": "bad_engine_arg", "engine_arg": args.engine,
                        "exit_code": EXIT_UNKNOWN})
        emit([f"[X] --engine 指定的目录不存在：{args.engine}",
              "    请给出 UE 引擎根目录（其下应有 Engine\\Build\\Build.version）。",
              f"    判定与升级路径见 {GUIDE}"], payload, args.json)
        return EXIT_UNKNOWN

    engines = locate_engines(args.engine)
    # 优先挑「读得到 Build.version」的引擎
    picked = None
    for src, root in engines:
        bv = read_build_version(root)
        if bv:
            picked = (src, root, bv)
            break
    if picked is None and engines:
        picked = (engines[0][0], engines[0][1], None)

    if picked is None:
        payload.update({"status": "no_engine", "exit_code": EXIT_UNKNOWN})
        emit(["[?] 未检测到 UE 引擎安装。",
              "    - 手动指定：python scripts\\check_ue_version.py --engine \"<引擎根>\"",
              "    - 或设置环境变量 UE5_ENGINE_PATH",
              f"    - 判定与升级路径见 {GUIDE}"], payload, args.json)
        return EXIT_UNKNOWN

    src, root, bv = picked
    if bv is None:
        payload.update({"status": "no_build_version", "engine_root": root,
                        "exit_code": EXIT_UNKNOWN})
        emit([f"[?] 引擎根：{root}",
              "    但找不到 Engine\\Build\\Build.version（可能不是完整引擎安装）。",
              f"    详见 {GUIDE} 路径 C。"], payload, args.json)
        return EXIT_UNKNOWN

    major, minor, patch = version_tuple(bv)
    branch = str(bv.get("BranchName") or "unknown")
    changelist = bv.get("Changelist")
    cls, code, verdict, next_step = classify(major, minor)

    tag = {"A": "[OK]", "B": "[!]", "C": "[?]", "X": "[X]"}.get(cls, "[?]")
    lines.append("=" * 62)
    lines.append("UE 版本自检")
    lines.append("=" * 62)
    lines.append(f"  引擎根    : {root}")
    lines.append(f"  来源      : {src}")
    lines.append(f"  版本      : {major}.{minor}.{patch}   (branch={branch}, changelist={changelist})")
    lines.append(f"  判定      : {tag} {verdict}")
    lines.append(f"  下一步    : {next_step}")

    payload.update({
        "status": "classified",
        "engine_root": root,
        "detected_by": src,
        "engine_version": f"{major}.{minor}.{patch}",
        "branch": branch,
        "changelist": changelist,
        "classification": cls,
        "verdict": verdict,
        "next_step": next_step,
        "exit_code": code,
    })

    if args.online:
        res, err = online_probe(args.port)
        if err:
            lines.append(f"  在线探测  : [X] {err}")
            payload["online"] = {"ok": False, "error": err}
        else:
            ue_info = (res.get("health") or {}).get("ue_info") or {}
            online_ver = str(ue_info.get("engine_version") or "unknown")
            diag = res.get("diag") or {}
            methods = diag.get("all_methods")
            n = len(methods) if isinstance(methods, list) else diag.get("method_count")
            matrix = diag.get("key_api_matrix") or {}
            missing_keys = sorted(k for k, v in matrix.items() if not v)
            ma_raw = diag.get("missing_apis")
            ma_n = len(ma_raw) if isinstance(ma_raw, (list, tuple)) else ma_raw
            avail = diag.get("bridge_available")
            lines.append(f"  在线探测  : [OK] 已运行编辑器 engine_version={online_ver}")
            lines.append(f"              bridge_available={avail} · method_count="
                         f"{n if n is not None else 'N/A'} · missing_apis={ma_n}")
            lines.append(f"              关键 API 缺失项（{len(missing_keys)}）："
                         f"{', '.join(missing_keys) if missing_keys else '无'}")
            lines.append(f"              对照 5.1 基线：method_count={BASELINE_51['method_count']} · "
                         f"missing_apis={BASELINE_51['missing_apis']}（判据与含义见 {GUIDE} §6）")
            payload["online"] = {"ok": True, "engine_version": online_ver,
                                 "bridge_available": avail,
                                 "method_count": n,
                                 "missing_apis": ma_n,
                                 "key_api_missing": missing_keys}
            if not avail:
                lines.append("              [X] bridge 未加载 —— 写通道不可用；"
                             "只读分析会自动降级 L1（详见指南 §8）。")
            if online_ver != "unknown" and not online_ver.startswith(f"{major}.{minor}"):
                lines.append("              [!] 注意：在线编辑器版本与磁盘引擎版本不一致，"
                             "请确认运行的是目标引擎。")

    lines.append(f"  指南      : {GUIDE}")
    lines.append("=" * 62)
    emit(lines, payload, args.json)
    return code


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
