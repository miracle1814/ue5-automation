# -*- coding: utf-8 -*-
"""ue5-automation v4.0 — 环境自检（新机部署前后都能跑；纯标准库）。

用法：
    python tools\\check_env.py
    python tools\\check_env.py --project "D:\\MyGame\\MyGame.uproject"

检查项：
  ① 包完整性（插件/两套 DLL/宿主脚本/UI/文档）
  ② 项目侧安装状态（插件、Content\\Python、Content\\ui、init_unreal.py、.uproject 启用）
  ③ 引擎侧（.uproject 的 EngineAssociation → 找引擎目录 → DLL 是否匹配族）
  ④ 引擎自带 Python 的依赖（fastapi / uvicorn / pydantic）
  ⑤ 运行时（桥是否在 8889 响应 / token 文件是否存在）
输出 PASS / WARN / FAIL 清单 + 修复建议。退出码 = FAIL 数量（0 = 全通过）。
"""
import argparse
import json
import os
import re
import subprocess
import sys

PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS = []


def rec(level, item, detail="", fix=""):
    RESULTS.append((level, item, detail, fix))
    print("[%s] %s%s%s" % (level, item,
                           (" | " + detail) if detail else "",
                           ("\n       → " + fix) if fix and level != "PASS" else ""))


def check_package():
    print("\n═══ ① 包完整性 ═══")
    need = [
        ("plugin/BlueprintPythonBridge/BlueprintPythonBridge.uplugin", "插件描述"),
        ("plugin/BlueprintPythonBridge/Binaries/Win64/UnrealEditor-BlueprintPythonBridge.dll",
         "5.1 DLL"),
        ("plugin/prebuilt-5.8/UnrealEditor-BlueprintPythonBridge.dll", "5.8 DLL"),
        ("plugin/prebuilt-5.8/UnrealEditor.modules", "5.8 modules"),
        ("host/python/ue5_bridge.py", "宿主桥脚本"),
        ("host/python/init_unreal.py", "自启模板"),
        ("host/python/game_thread_build.py.tmpl", "建图模板"),
        ("host/ui/index.html", "管理端页面"),
        ("docs/QUICKSTART.md", "快速开始"),
        ("docs/SKILL.md", "技能手册"),
    ]
    for rel, label in need:
        p = os.path.join(PKG_ROOT, rel.replace("/", os.sep))
        ok = os.path.isfile(p)
        rec("PASS" if ok else "FAIL", "包内 %s" % label,
            rel if ok else "缺失: " + rel,
            "" if ok else "重新解压完整压缩包")
    # 版本一致性
    ver_file = os.path.join(PKG_ROOT, "VERSION.txt")
    if os.path.isfile(ver_file):
        rec("PASS", "版本文件", open(ver_file, encoding="utf-8").read().strip()[:60])
    else:
        rec("WARN", "版本文件缺失", "VERSION.txt")


def read_engine_assoc(uproject):
    try:
        raw = open(uproject, encoding="utf-8-sig").read()
        m = re.search(r'"EngineAssociation"\s*:\s*"([^"]*)"', raw)
        return m.group(1).strip() if m else ""
    except Exception:
        return ""


def engine_family(assoc):
    m = re.match(r"^(\d+)\.(\d+)", assoc or "")
    if not m:
        return ""
    if m.group(1) == "5" and m.group(2) in ("1", "8"):
        return "5." + m.group(2)
    return "5.x"


def find_engine_dirs():
    """常见位置 + Epic 启动器清单（只读探测）。"""
    cands = []
    for drive in ("C:", "D:", "E:", "F:"):
        for base in ("Program Files\\Epic Games", "Epic Games", "uesoft\\UE",
                     "Program Files\\Unreal Engine"):
            cands.append(os.path.join(drive + os.sep, base))
    found = []
    for base in cands:
        if not os.path.isdir(base):
            continue
        for name in os.listdir(base):
            full = os.path.join(base, name)
            if re.match(r"^UE_5\.\d+$", name) and \
                    os.path.isfile(os.path.join(full, "Engine", "Build",
                                                "BatchFiles", "RunUAT.bat")):
                found.append(full)
    # Epic 启动器清单
    mani = os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"),
                        "Epic", "EpicGamesLauncher", "Data", "Manifests")
    if os.path.isdir(mani):
        for fn in os.listdir(mani):
            if not fn.endswith(".item"):
                continue
            try:
                d = json.load(open(os.path.join(mani, fn), encoding="utf-8"))
                loc = d.get("InstallLocation") or ""
                name = d.get("AppName") or ""
                if loc and "UnrealEngine" in name and \
                        os.path.isfile(os.path.join(loc, "Engine", "Build",
                                                    "BatchFiles", "RunUAT.bat")):
                    found.append(loc)
            except Exception:
                pass
    return sorted(set(found))


def find_engine_python(engine_dir):
    p = os.path.join(engine_dir, "Engine", "Binaries", "ThirdParty", "Python3",
                     "Win64", "python.exe")
    return p if os.path.isfile(p) else ""


def check_project(uproject):
    print("\n═══ ② 项目侧安装状态 ═══")
    proj_dir = os.path.dirname(uproject)
    plugin = os.path.join(proj_dir, "Plugins", "BlueprintPythonBridge")
    checks = [
        (os.path.isfile(os.path.join(plugin, "BlueprintPythonBridge.uplugin")), "插件已安装"),
        (os.path.isfile(os.path.join(plugin, "Binaries", "Win64",
                                    "UnrealEditor-BlueprintPythonBridge.dll")), "DLL 已就位"),
        (os.path.isfile(os.path.join(plugin, "Binaries", "Win64",
                                    "UnrealEditor.modules")), "modules 已就位（缺失会导致拒载）"),
        (os.path.isfile(os.path.join(proj_dir, "Content", "Python", "ue5_bridge.py")),
         "宿主脚本已安装"),
        (os.path.isfile(os.path.join(proj_dir, "Content", "Python", "init_unreal.py")),
         "自启脚本已安装"),
        (os.path.isfile(os.path.join(proj_dir, "Content", "ui", "index.html")),
         "管理端页面已安装"),
    ]
    for ok, label in checks:
        rec("PASS" if ok else "FAIL", label, "" , "" if ok else "运行 tools\\deploy_portable.py")
    try:
        data = json.load(open(uproject, encoding="utf-8-sig"))
        names = {p.get("Name"): p.get("Enabled") for p in data.get("Plugins", [])}
        ok = bool(names.get("BlueprintPythonBridge"))
        rec("PASS" if ok else "WARN", ".uproject 已启用插件",
            "BlueprintPythonBridge=%s PythonScriptPlugin=%s"
            % (names.get("BlueprintPythonBridge"), names.get("PythonScriptPlugin")),
            "" if ok else "部署脚本会自动写入；或 Edit→Plugins 手动启用")
    except Exception as e:
        rec("WARN", ".uproject 解析失败", str(e)[:60])

    print("\n═══ ③ 引擎与 DLL 匹配 ═══")
    assoc = read_engine_assoc(uproject)
    fam = engine_family(assoc)
    rec("PASS" if fam else "WARN", "EngineAssociation = %s" % (assoc or "(空)"),
        "引擎族 %s" % (fam or "未知"),
        "" if fam else "在 .uproject 中确认引擎版本")
    engines = find_engine_dirs()
    if engines:
        rec("PASS", "发现引擎 %d 个" % len(engines),
            ", ".join(os.path.basename(e) for e in engines[:4]))
    else:
        rec("WARN", "未在本机常见位置找到 UE 引擎",
            "", "若引擎在自定义目录属正常；编译/重建 DLL 时需要它")

    # modules 里的 BuildId 只能对应一个引擎，这里只提示族
    mod = os.path.join(proj_dir, "Plugins", "BlueprintPythonBridge", "Binaries",
                       "Win64", "UnrealEditor.modules")
    if os.path.isfile(mod):
        try:
            bid = str(json.load(open(mod, encoding="utf-8")).get("BuildId", ""))
            rec("PASS", "DLL modules BuildId", bid)
        except Exception:
            rec("WARN", "modules 读取失败", mod)

    # 依赖检查
    print("\n═══ ④ 引擎自带 Python 依赖 ═══")
    target_engine = next((e for e in engines
                          if fam and fam != "5.x" and
                          os.path.basename(e).endswith(fam)), None)
    if not target_engine and engines:
        target_engine = engines[0]
    if target_engine:
        py = find_engine_python(target_engine)
        if py:
            code = ("import sys\n"
                    "mods=[]\n"
                    "for m in ('fastapi','uvicorn','pydantic'):\n"
                    "    try:\n"
                    "        __import__(m); mods.append(m+':ok')\n"
                    "    except Exception as e:\n"
                    "        mods.append(m+':MISSING')\n"
                    "print(';'.join(mods))\n")
            try:
                out = subprocess.run([py, "-c", code], capture_output=True,
                                     text=True, timeout=60)
                txt = (out.stdout or "").strip()
                ok = "MISSING" not in txt
                rec("PASS" if ok else "FAIL",
                    "引擎 Python 依赖（%s）" % os.path.basename(target_engine), txt,
                    "" if ok else py + " -m pip install fastapi uvicorn pydantic")
            except Exception as e:
                rec("WARN", "依赖检查执行失败", str(e)[:60])
        else:
            rec("WARN", "未找到引擎自带 python.exe",
                "", "依赖检查跳过；桥启动时会在日志提示缺失")
    else:
        rec("WARN", "跳过依赖检查（未定位引擎）")


def check_runtime():
    print("\n═══ ⑤ 运行时（桥服务）═══")
    token_file = os.path.join(os.environ.get("APPDATA", ""), "ue5_bridge", "token")
    has_token = os.path.isfile(token_file)
    rec("PASS" if has_token else "WARN", "token 文件",
        token_file if has_token else "尚未生成（首次启动编辑器后自动生成）")
    tok = ""
    if has_token:
        try:
            tok = open(token_file, encoding="utf-8").read().strip()
        except Exception:
            pass
    try:
        import urllib.request
        rq = urllib.request.Request("http://127.0.0.1:8889/health",
                                    headers={"X-Skill-Token": tok})
        with urllib.request.urlopen(rq, timeout=4) as r:
            body = json.loads(r.read().decode())
        info = body.get("ue_info") or {}
        rec("PASS", "桥在线（127.0.0.1:8889）",
            "engine=%s project=%s" % (info.get("engine_version", "?")[:18],
                                      info.get("project_name", "?")))
        rec("PASS" if info.get("bridge_loaded") else "FAIL",
            "C++ 桥已加载", "bridge_loaded=%s" % info.get("bridge_loaded"),
            "" if info.get("bridge_loaded") else "DLL 未加载：检查 modules/引擎版本是否匹配")
    except Exception:
        rec("WARN", "桥未响应（编辑器未启动属正常）",
            "http://127.0.0.1:8889/health",
            "启动 UE 编辑器后重跑本检查")


def main():
    ap = argparse.ArgumentParser(description="ue5-automation 环境自检")
    ap.add_argument("--project", help=".uproject 路径或项目目录")
    args = ap.parse_args()
    print("ue5-automation v4.0 环境自检")
    check_package()
    up = args.project
    if not up:
        try:
            up = input("\n输入 UE 项目 .uproject 路径（可留空跳过项目检查）：").strip().strip('"')
        except Exception:
            up = ""
    if up:
        if os.path.isdir(up):
            cands = [f for f in os.listdir(up) if f.lower().endswith(".uproject")]
            up = os.path.join(up, cands[0]) if cands else up
        if os.path.isfile(up):
            check_project(up)
        else:
            rec("FAIL", "找不到 .uproject", up)
    else:
        print("\n(跳过项目检查)")
    check_runtime()

    fails = [r for r in RESULTS if r[0] == "FAIL"]
    warns = [r for r in RESULTS if r[0] == "WARN"]
    print("\n═══ 小结：%d 项检查 | PASS %d / WARN %d / FAIL %d ═══"
          % (len(RESULTS), len(RESULTS) - len(fails) - len(warns), len(warns), len(fails)))
    if fails:
        print("需处理：")
        for _, item, detail, fix in fails:
            print("  ✗ %s%s" % (item, (" | " + detail) if detail else ""))
            if fix:
                print("      → " + fix)
    return len(fails)


if __name__ == "__main__":
    sys.exit(main())
