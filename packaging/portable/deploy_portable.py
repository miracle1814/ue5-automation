# -*- coding: utf-8 -*-
"""ue5-automation v4.0 — 一键部署到 UE 项目（Windows / 纯标准库 / 零依赖）。

用法（在解压后的包根目录执行）：
    python tools\\deploy_portable.py --project "D:\\MyGame\\MyGame.uproject"
    python tools\\deploy_portable.py            # 交互式：直接输入/粘贴 .uproject 路径

它会自动：
  1) 读 .uproject 的 EngineAssociation，选对引擎版本的 DLL（5.1 / 5.8 随包）
  2) 备份并安装插件到 <项目>/Plugins/BlueprintPythonBridge
  3) 把宿主脚本装到 <项目>/Content/Python、管理端页面装到 <项目>/Content/ui
  4) 写 init_unreal.py（编辑器启动时自动拉起桥服务）
  5) 把插件与 Python 插件写进 .uproject 的 Plugins 列表（确保启用）
  6) 打印后续步骤与依赖自检指引

只读/幂等：重复执行会先备份再覆盖；--dry-run 只打印计划不落盘。
"""
import argparse
import json
import os
import re
import shutil
import sys
from datetime import datetime

PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN_SRC = os.path.join(PKG_ROOT, "plugin", "BlueprintPythonBridge")
PRE58_SRC = os.path.join(PKG_ROOT, "plugin", "prebuilt-5.8")
HOST_PY_SRC = os.path.join(PKG_ROOT, "host", "python")
HOST_UI_SRC = os.path.join(PKG_ROOT, "host", "ui")

INIT_UNREAL = '''# ue5-automation 桥自启（部署脚本生成）
# 编辑器启动时由 PythonScriptPlugin 执行：拉起 ue5_bridge（127.0.0.1:8889）。
# 失败只记日志，绝不阻断编辑器启动。
import traceback

try:
    import ue5_bridge

    ue5_bridge.start()
except Exception:
    try:
        import unreal

        unreal.log_error("[ue5-automation] bridge 启动失败:\\n" + traceback.format_exc())
    except Exception:
        print("[ue5-automation] bridge 启动失败:\\n" + traceback.format_exc())
'''

STAMP = datetime.now().strftime("%Y%m%d-%H%M%S")


def info(msg):
    print("[deploy] " + msg)


def warn(msg):
    print("[WARN] " + msg)


def fail(msg):
    print("[FAIL] " + msg)
    return 1


def resolve_uproject(raw):
    p = (raw or "").strip().strip('"').strip("'")
    if not p:
        return None
    if os.path.isdir(p):
        cands = [f for f in os.listdir(p) if f.lower().endswith(".uproject")]
        return os.path.join(p, cands[0]) if len(cands) == 1 else None
    if os.path.isfile(p) and p.lower().endswith(".uproject"):
        return p
    # 允许粘贴带空格/斜杠混用的路径
    alt = p.replace("/", os.sep)
    if os.path.isfile(alt):
        return alt
    return None


def read_engine_association(uproject_path):
    try:
        raw = open(uproject_path, encoding="utf-8-sig").read()
        m = re.search(r'"EngineAssociation"\s*:\s*"([^"]*)"', raw)
        return (m.group(1).strip() if m else "")
    except Exception:
        return ""


def engine_family(assoc):
    """'5.1' → '5.1'；'5.8' → '5.8'；其它 5.x → '5.x'。"""
    m = re.match(r"^(\d+)\.(\d+)", assoc or "")
    if not m:
        return ""
    major, minor = m.group(1), m.group(2)
    if major == "5" and minor in ("1", "8"):
        return "5." + minor
    return "5.x"


def backup_file(path):
    if os.path.isfile(path):
        dst = path + ".bak-" + STAMP
        shutil.copy2(path, dst)
        return dst
    return None


def copytree_merge(src, dst, dry):
    """复制目录（合并覆盖），dry=True 只打印。"""
    if dry:
        info("  (dry) 复制 %s → %s" % (src, dst))
        return
    os.makedirs(dst, exist_ok=True)
    for name in os.listdir(src):
        s = os.path.join(src, name)
        d = os.path.join(dst, name)
        if os.path.isdir(s):
            if name in ("__pycache__",):
                continue
            copytree_merge(s, d, dry)
        else:
            shutil.copy2(s, d)


def ensure_plugins_in_uproject(uproject_path, dry):
    """确保 .uproject 的 Plugins 列表启用本插件与 PythonScriptPlugin。"""
    raw = open(uproject_path, encoding="utf-8-sig").read()
    try:
        data = json.loads(raw)
    except Exception:
        warn(".uproject 不是合法 JSON，跳过自动启用（请在编辑器 Edit→Plugins 手动启用）")
        return False
    plugins = data.setdefault("Plugins", [])
    wanted = ["BlueprintPythonBridge", "PythonScriptPlugin",
              "ue5-automation-bootstrap"]
    changed = False
    for name in wanted:
        entry = next((p for p in plugins if p.get("Name") == name), None)
        if entry is None:
            plugins.append({"Name": name, "Enabled": True})
            changed = True
        elif not entry.get("Enabled"):
            entry["Enabled"] = True
            changed = True
    if not changed:
        info("  .uproject 已启用所需插件")
        return False
    if dry:
        info("  (dry) 将写回 .uproject（启用 BlueprintPythonBridge / PythonScriptPlugin）")
        return True
    bak = backup_file(uproject_path)
    if bak:
        info("  已备份 .uproject → %s" % os.path.basename(bak))
    with open(uproject_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent="\t")
        f.write("\n")
    info("  已把插件写入 .uproject（Enabled=True）")
    return True


def main():
    ap = argparse.ArgumentParser(description="ue5-automation 一键部署")
    ap.add_argument("--project", help=".uproject 路径或其所在目录")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划，不落盘")
    args = ap.parse_args()

    raw = args.project
    if not raw:
        print("请把 UE 项目的 .uproject 文件拖到这里（或粘贴路径），回车：")
        raw = input("> ")
    uproject = resolve_uproject(raw)
    if not uproject:
        return fail("找不到 .uproject：%s（请给文件路径，或只含一个 .uproject 的项目目录）" % raw)
    project_dir = os.path.dirname(uproject)
    info("项目: " + uproject)

    assoc = read_engine_association(uproject)
    fam = engine_family(assoc)
    info("EngineAssociation: %s → 引擎族 %s" % (assoc or "(空)", fam or "(未知)"))
    if not fam:
        warn("读不到引擎版本；将按包内 5.1 DLL 部署，若不是 5.1 请在编辑器内重新编译")
    if fam == "5.x":
        warn("引擎 %s 不在随包预编译范围内（仅 5.1 / 5.8）——插件源码已随包，"
             "需用该引擎的 BuildPlugin 自行编译（见 docs/QUICKSTART.md 末节）" % assoc)

    if not os.path.isdir(PLUGIN_SRC):
        return fail("包内缺少 plugin/BlueprintPythonBridge")
    if not os.path.isdir(HOST_PY_SRC):
        return fail("包内缺少 host/python")

    dest_plugin = os.path.join(project_dir, "Plugins", "BlueprintPythonBridge")
    dest_py = os.path.join(project_dir, "Content", "Python")
    dest_ui = os.path.join(project_dir, "Content", "ui")

    # v4.6：纯 Python 引导插件（5.8 起项目级 init 不再自动执行）
    boot_src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "plugin", "ue5-automation-bootstrap")
    if os.path.isdir(boot_src):
        dest_boot = os.path.join(project_dir, "Plugins", "ue5-automation-bootstrap")
        info("①b 安装引导插件 → Plugins/ue5-automation-bootstrap")
        if args.dry_run:
            info("  (dry) 将复制引导插件（自动拉起桥服务）")
        else:
            copytree_merge(boot_src, dest_boot, False)
            info("  已安装（EnabledByDefault=true）")
    else:
        warn("包内缺少 plugin/ue5-automation-bootstrap（5.8 可能不会自启）")

    info("① 安装插件 → Plugins/BlueprintPythonBridge")
    if os.path.isdir(dest_plugin) and not args.dry_run:
        bak = dest_plugin + ".bak-" + STAMP
        shutil.copytree(dest_plugin, bak)
        info("  已备份旧插件 → %s" % os.path.basename(bak))
    copytree_merge(PLUGIN_SRC, dest_plugin, args.dry_run)

    if fam == "5.8":
        info("② 覆盖 5.8 DLL（BuildId 由 UnrealEditor.modules 提供）")
        bin_dir = os.path.join(dest_plugin, "Binaries", "Win64")
        if os.path.isdir(PRE58_SRC):
            for name in os.listdir(PRE58_SRC):
                src = os.path.join(PRE58_SRC, name)
                if args.dry_run:
                    info("  (dry) 复制 %s → Binaries/Win64" % name)
                else:
                    os.makedirs(bin_dir, exist_ok=True)
                    shutil.copy2(src, os.path.join(bin_dir, name))
                    info("  " + name)
        else:
            warn("包内缺少 plugin/prebuilt-5.8")
    else:
        info("② 使用包内 5.1 DLL（无需覆盖）")

    info("③ 安装宿主脚本 → Content/Python")
    copytree_merge(HOST_PY_SRC, dest_py, args.dry_run)

    info("④ 安装管理端页面 → Content/ui")
    copytree_merge(HOST_UI_SRC, dest_ui, args.dry_run)

    info("⑤ 写 init_unreal.py（编辑器启动自启桥）")
    init_path = os.path.join(dest_py, "init_unreal.py")
    if args.dry_run:
        info("  (dry) 写 %s" % init_path)
    else:
        bak = backup_file(init_path)
        if bak:
            info("  已备份原 init_unreal.py → %s" % os.path.basename(bak))
        with open(init_path, "w", encoding="utf-8") as f:
            f.write(INIT_UNREAL)
        info("  " + init_path)

    info("⑥ 启用插件（写 .uproject）")
    ensure_plugins_in_uproject(uproject, args.dry_run)

    summary = r"""
=====================================================================
 部署完成。下一步：
   1) 启动/重启 UE 编辑器，打开 Output Log 找到：
        [ue5_bridge] [OK] 服务已在后台启动 -> http://127.0.0.1:8889/docs
   2) 浏览器打开管理端（token 在 %APPDATA%\ue5_bridge\token 文件里）：
        http://127.0.0.1:8889/ui?token=<粘贴 token>
   3) 若日志提示 FastAPI/uvicorn 缺失，用**引擎自带 Python** 安装：
        <UE安装目录>\Engine\Binaries\ThirdParty\Python3\Win64\python.exe -m pip install fastapi uvicorn pydantic
   4) 跑一次环境自检（可选）：
        python tools\check_env.py --project "__PROJ__"
=====================================================================
"""
    print(summary.replace("__PROJ__", uproject))
    return 0


if __name__ == "__main__":
    sys.exit(main())
