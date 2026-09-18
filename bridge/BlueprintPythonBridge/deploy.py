# -*- coding: utf-8 -*-
"""
BlueprintPythonBridge 部署脚本（通用版）
========================================
将编译产物部署到 UE 项目或引擎插件目录。

用法:
    python deploy.py                      # 自动检测项目 + 引擎，部署到项目
    python deploy.py --target <path>      # 部署到指定 UE 项目
    python deploy.py --engine <path>      # 部署到指定引擎插件目录
    python deploy.py --auto-engine        # 自动检测引擎路径并部署
    python deploy.py --source <path>      # 指定编译产物源目录
    python deploy.py --zip                # 打包 ZIP（不安装）
    python deploy.py --clean              # 清理编译中间文件

UE 引擎路径自动检测顺序:
    ① 环境变量 UE5_ENGINE_PATH
    ② Windows 注册表 EpicGames 安装记录
    ③ 常见安装目录遍历
    ④ 都不行 → 提示用户用 --engine 手动指定

UE 项目路径自动检测顺序:
    ① 环境变量 UE5_PROJECT_PATH
    ② 从当前目录向上查找 .uproject 文件
    ③ --target 参数手动指定
"""

import argparse
import os
import shutil
import sys
import zipfile
from datetime import datetime

# ── 常量 ─────────────────────────────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# 常见 UE 引擎安装目录（按优先级排列）
_KNOWN_ENGINE_DIRS = [
    r"C:\Program Files\Epic Games\UE_5.1",
    r"C:\Program Files\Epic Games\UE_5.2",
    r"C:\Program Files\Epic Games\UE_5.3",
    r"C:\Program Files\Epic Games\UE_5.4",
    r"C:\Program Files\Epic Games\UE_5.5",
    r"D:\Epic Games\UE_5.1",
    r"E:\Epic Games\UE_5.1",
]

# 需要部署的文件（源路径 → 目标相对目录）
DEPLOY_FILES = {
    os.path.join("Binaries", "Win64", "UnrealEditor-BlueprintPythonBridge.dll"):
        os.path.join("Binaries", "Win64"),
    os.path.join("Binaries", "Win64", "UnrealEditor-BlueprintPythonBridge.pdb"):
        os.path.join("Binaries", "Win64"),
    "BlueprintPythonBridge.uplugin": "",
    os.path.join("Source", "BlueprintPythonBridge", "Public",
                 "BlueprintPythonBridge.h"):
        os.path.join("Source", "BlueprintPythonBridge", "Public"),
}

# T-20260915-UE5PKG-STANDALONE：可选文件（缺失不阻断部署）
#   .pdb = 调试符号，运行不需要；发布包按发布红线（BLACKLIST *.pdb）不随包。
OPTIONAL_DEPLOY_FILES = {
    os.path.join("Binaries", "Win64", "UnrealEditor-BlueprintPythonBridge.pdb"),
}

# 源文件替代路径：发布包内 bridge 为扁平布局（头文件在插件根，无 Source/ 层），
# 插件编译目录则为 Source/BlueprintPythonBridge/Public/ 布局 —— 两者内容一致
# （实测 md5 963999740465602F13498A7E1953C1D1 双向吻合）。
_DEPLOY_SRC_FALLBACK = {
    os.path.join("Source", "BlueprintPythonBridge", "Public",
                 "BlueprintPythonBridge.h"): [
        "BlueprintPythonBridge.h",
        os.path.join("Public", "BlueprintPythonBridge.h"),
    ],
}


def resolve_deploy_src(source_dir, src_rel):
    """解析可用的源文件路径；找不到任何替代时返回 None。"""
    full = os.path.join(source_dir, src_rel)
    if os.path.exists(full):
        return full
    for alt in _DEPLOY_SRC_FALLBACK.get(src_rel, []):
        cand = os.path.join(source_dir, alt)
        if os.path.exists(cand):
            return cand
    return None


# ── 引擎路径自动检测 ────────────────────────────────────

def _find_engine_from_registry():
    """从 Windows 注册表读取 Epic Games Launcher 安装的 UE 引擎路径。

    v2.20 修复（P2）：① Epic 非管理员安装把 InstalledDirectory 写在
    HKCU（旧实现只查 HKLM → 自动探测失败）；② 32 位 Python 受 WOW64
    重定向影响（加 KEY_WOW64_64KEY）。
    """
    if sys.platform != "win32":
        return None
    try:
        import winreg
        # Epic Games Launcher 注册表路径: 尝试多个版本
        for version_key in [r"5.1", r"5.2", r"5.3", r"5.4", r"5.5"]:
            for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
                try:
                    reg_path = (r"SOFTWARE\EpicGames\Unreal Engine"
                                + "\\" + version_key)
                    with winreg.OpenKey(root, reg_path, 0,
                                        winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
                        install_dir, _ = winreg.QueryValueEx(
                            key, "InstalledDirectory")
                        if install_dir and os.path.isdir(install_dir):
                            return install_dir
                except OSError:
                    continue
    except Exception:
        pass
    return None


def _find_engine_from_known_dirs():
    """遍历常见安装目录，查找 UE 引擎。"""
    for candidate in _KNOWN_ENGINE_DIRS:
        # 检查标志性文件存在
        engine_bin = os.path.join(candidate, "Engine", "Binaries",
                                  "Win64", "UnrealEditor.exe")
        engine_cmd = os.path.join(candidate, "Engine", "Build",
                                  "BatchFiles", "RunUAT.bat")
        if os.path.isfile(engine_bin) or os.path.isfile(engine_cmd):
            if os.path.isdir(os.path.join(candidate, "Engine", "Plugins")):
                return candidate
    return None


def find_engine_path():
    """
    自动检测 UE 引擎路径。
    返回路径字符串，找不到返回 None。
    """
    # ① 环境变量
    env_path = os.environ.get("UE5_ENGINE_PATH", "")
    if env_path and os.path.isdir(env_path):
        return env_path.rstrip("\\/")

    # ② 注册表
    reg_path = _find_engine_from_registry()
    if reg_path:
        return reg_path

    # ③ 常见目录
    known_path = _find_engine_from_known_dirs()
    if known_path:
        return known_path

    # ④ 找不到
    return None


def get_engine_plugins_dir(engine_path):
    """返回引擎的 Engine/Plugins 目录。"""
    return os.path.join(engine_path, "Engine", "Plugins")


# ── 项目路径自动检测 ────────────────────────────────────

def find_project_path(start_dir=None):
    """
    自动检测 UE 项目路径。
    从 start_dir 向上查找 .uproject 文件。
    返回项目根目录路径，找不到返回 None。
    """
    # ① 环境变量
    env_path = os.environ.get("UE5_PROJECT_PATH", "")
    if env_path:
        if os.path.isfile(env_path):
            return os.path.dirname(env_path)
        if os.path.isdir(env_path):
            return env_path

    # ② 从当前目录向上找 .uproject
    if start_dir is None:
        start_dir = SCRIPT_DIR
    current = os.path.abspath(start_dir)
    for _ in range(10):
        try:
            entries = os.listdir(current)
        except PermissionError:
            break
        for entry in entries:
            if entry.lower().endswith(".uproject"):
                return current
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent

    return None


# ── 源目录判定 ──────────────────────────────────────────

def resolve_source_dir(script_dir, user_specified=None):
    """
    判定编译产物源目录。
    ① 用户 --source 指定
    ② 脚本所在目录（如果包含 .uplugin 文件）
    ③ 向上查找 Plugins/BlueprintPythonBridge/
    """
    if user_specified:
        return os.path.abspath(user_specified)

    # 脚本所在目录就是插件根目录？
    if os.path.isfile(os.path.join(script_dir, "BlueprintPythonBridge.uplugin")):
        return script_dir

    # 向上查
    current = os.path.abspath(script_dir)
    for _ in range(8):
        candidate = os.path.join(current, "Plugins", "BlueprintPythonBridge")
        if os.path.isfile(os.path.join(candidate, "BlueprintPythonBridge.uplugin")):
            return candidate
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent

    # 回退到脚本所在目录
    return script_dir


# ── 核心功能 ────────────────────────────────────────────

def verify_source(source_dir):
    """验证编译产物是否存在（T-20260915-UE5PKG-STANDALONE：可选文件缺失不阻断）。"""
    missing = []
    for src_rel in DEPLOY_FILES:
        if resolve_deploy_src(source_dir, src_rel) is not None:
            continue
        if src_rel in OPTIONAL_DEPLOY_FILES:
            print(f"[SKIP] 可选文件缺失（不阻断部署）: {src_rel}")
            continue
        missing.append(os.path.join(source_dir, src_rel))
    if missing:
        print("\n[ERROR] 以下编译产物缺失，请先运行编译:")
        for m in missing:
            print(f"  - {m}")
        print(f"\n  源目录: {source_dir}")
        print("  提示: 用 --source 指定正确的编译产物目录")
        return False
    print(f"[OK] 编译产物已就绪（源: {source_dir}）")
    return True


# v2.20（P1）：随包 DLL 为 UE 5.1 预编译，跨引擎版本不通用（ABI 强绑定）。
# 部署到非 5.1.x 引擎/项目 → 编辑器模块加载失败 + UBT 无法重建（无 Build.cs）。
BUNDLED_DLL_ENGINE = (5, 1)


def check_engine_version(engine_path):
    """读 <engine>/Engine/Build/Build.version → (major, minor, version_str)；
    读取失败返回 None。"""
    ver_file = os.path.join(engine_path, "Engine", "Build", "Build.version")
    try:
        import json as _json
        with open(ver_file, "r", encoding="utf-8") as f:
            data = _json.load(f)
        major = int(data.get("MajorVersion", 0))
        minor = int(data.get("MinorVersion", 0))
        return (major, minor, f"{major}.{minor}.{data.get('PatchVersion', '?')}")
    except Exception:
        return None


def check_project_engine_version(project_root):
    """读 <project>/*.uproject 的 EngineAssociation → 字符串（如 '5.1'）；
    找不到 .uproject 或无该字段返回 None。"""
    try:
        import json as _json
        for entry in os.listdir(project_root):
            if entry.lower().endswith(".uproject"):
                with open(os.path.join(project_root, entry), "r",
                          encoding="utf-8-sig") as f:
                    data = _json.load(f)
                assoc = data.get("EngineAssociation")
                return str(assoc) if assoc else None
    except Exception:
        pass
    return None


def _version_gate(engine_or_project_version, context, force):
    """版本闸：随包 DLL 仅适用于 5.1.x。非 5.1.x 且未 --force → 拦截。

    engine_or_project_version: (major, minor, str) 或 '5.1' 形态字符串 / None
    返回 True = 放行。
    """
    if force:
        print("[WARN] --force 已指定：跳过引擎版本匹配检查（后果自负）")
        return True
    if engine_or_project_version is None:
        print(f"[WARN] 无法读取{context}的引擎版本 → 无法确认与随包 DLL "
              f"({BUNDLED_DLL_ENGINE[0]}.{BUNDLED_DLL_ENGINE[1]}) 匹配。")
        print("       如确认目标为 UE 5.1.x，用 --force 跳过本检查。")
        return False
    if isinstance(engine_or_project_version, tuple):
        major, minor, ver_str = engine_or_project_version
    else:
        # EngineAssociation 形如 "5.1" / "5.1.1" / 自定义名
        parts = str(engine_or_project_version).split(".")
        try:
            major, minor = int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
        except ValueError:
            print(f"[WARN] 引擎版本标识 {engine_or_project_version!r} 非标准版本号"
                  f"（自定义源码构建？）→ 无法确认与随包 DLL 匹配，用 --force 跳过。")
            return False
        ver_str = str(engine_or_project_version)
    if (major, minor) != BUNDLED_DLL_ENGINE:
        print(f"\n[ERROR] 版本不匹配：{context}为 UE {ver_str}，随包 DLL 仅适用于 "
              f"UE {BUNDLED_DLL_ENGINE[0]}.{BUNDLED_DLL_ENGINE[1]}.x（预编译，跨版本不通用）。")
        print("  处理：")
        print("  ① 先运行 skill 自检：python skill\\ue5-automation\\scripts\\check_ue_version.py")
        print("  ② 5.2~5.5 需重编译 bridge：见 skill\\ue5-automation\\UE_VERSION_GUIDE.md 路径 B")
        print("  ③ 确认要强行部署（不推荐）：加 --force")
        return False
    return True


def _copy_deploy_files(source_dir, target_plugin):
    """公共拷贝逻辑（v2.20：OSError 可读化，不再裸 traceback）。"""
    copied = 0
    skipped = 0
    for src_rel, dst_rel in DEPLOY_FILES.items():
        src_path = resolve_deploy_src(source_dir, src_rel)
        if src_path is None:
            skipped += 1
            print(f"[SKIP] 源文件缺失（已跳过）: {src_rel}")
            continue
        dst_path = os.path.join(target_plugin, dst_rel,
                                os.path.basename(src_rel))
        os.makedirs(os.path.dirname(dst_path), exist_ok=True)
        shutil.copy2(src_path, dst_path)
        copied += 1

    print(f"[OK] 已部署 {copied} 个文件到: {target_plugin}"
          f"{f'（跳过 {skipped} 个可选文件）' if skipped else ''}")
    return target_plugin


def deploy_to_target(source_dir, target_root, force=False):
    """将编译产物部署到目标项目的 Plugins 目录。"""
    # v2.20（P2）：--target 不校验是否真是 UE 项目 → 路径手滑时静默部署到错误位置
    has_uproject = any(f.lower().endswith(".uproject")
                       for f in os.listdir(target_root)
                       if os.path.isfile(os.path.join(target_root, f)))
    if not has_uproject and not force:
        print(f"\n[ERROR] 目标目录下没有 .uproject 文件，疑似不是 UE 项目根目录: {target_root}")
        print("  请确认路径（应为包含 <项目名>.uproject 的目录）；确认无误可加 --force。")
        sys.exit(1)
    assoc = check_project_engine_version(target_root)
    if not _version_gate(assoc, "目标项目引擎", force):
        sys.exit(1)

    target_plugin = os.path.join(target_root, "Plugins", "BlueprintPythonBridge")
    try:
        return _copy_deploy_files(source_dir, target_plugin)
    except PermissionError as e:
        print(f"\n[ERROR] 没有写入权限（{e}）。")
        print("  处理：① 关闭 UE 编辑器（DLL 可能被锁定）② 以管理员身份重试 "
              "③ 或改用 --target 部署到你有写权限的项目 Plugins/ 目录。")
        sys.exit(1)
    except OSError as e:
        print(f"\n[ERROR] 部署失败: {e}")
        sys.exit(1)


def deploy_to_engine_plugins(source_dir, engine_path, force=False):
    """部署到引擎的 Engine/Plugins 目录。"""
    ver = check_engine_version(engine_path)
    if not _version_gate(ver, "目标引擎", force):
        sys.exit(1)

    target = os.path.join(engine_path, "Engine", "Plugins",
                          "BlueprintPythonBridge")
    try:
        return _copy_deploy_files(source_dir, target)
    except PermissionError as e:
        print(f"\n[ERROR] 没有写入权限（{e}）——写引擎目录通常需要管理员。")
        print("  处理：① 以管理员身份重试 ② 或改用 --target 部署到项目 "
              "Plugins/ 目录（推荐，无需管理员）。")
        sys.exit(1)
    except OSError as e:
        print(f"\n[ERROR] 部署失败: {e}")
        sys.exit(1)


def package_zip(source_dir):
    """打包为 ZIP。"""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    zip_name = f"BlueprintPythonBridge_{timestamp}.zip"
    zip_path = os.path.join(SCRIPT_DIR, zip_name)

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for src_rel, dst_rel in DEPLOY_FILES.items():
            src_path = resolve_deploy_src(source_dir, src_rel)
            if src_path is None:
                print(f"[SKIP] 源文件缺失（未入 ZIP）: {src_rel}")
                continue
            arcname = os.path.join("BlueprintPythonBridge", dst_rel,
                                   os.path.basename(src_rel))
            zf.write(src_path, arcname)

    size_kb = os.path.getsize(zip_path) / 1024
    print(f"[OK] 打包完成: {zip_path} ({size_kb:.1f} KB)")
    return zip_path


def clean_intermediate(source_dir):
    """清理编译中间文件（Intermediate 目录）。"""
    intermediate = os.path.join(source_dir, "Intermediate")
    if os.path.exists(intermediate):
        shutil.rmtree(intermediate)
        print(f"[OK] 已清理: {intermediate}")
    else:
        print(f"[SKIP] 中间文件目录不存在: {intermediate}")


# ── CLI 入口 ────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="BlueprintPythonBridge 部署工具（通用版）"
    )
    parser.add_argument("--target", type=str, default=None,
                        help="目标 UE 项目路径")
    parser.add_argument("--engine", type=str, default=None,
                        help="UE 引擎根目录路径")
    parser.add_argument("--auto-engine", action="store_true",
                        help="自动检测引擎路径并部署到引擎")
    parser.add_argument("--source", type=str, default=None,
                        help="编译产物源目录（默认: 脚本所在目录）")
    parser.add_argument("--zip", action="store_true",
                        help="打包为 ZIP 文件")
    parser.add_argument("--clean", action="store_true",
                        help="清理编译中间文件")
    parser.add_argument("--force", action="store_true",
                        help="跳过引擎版本匹配检查 / 项目目录校验（不推荐）")
    args = parser.parse_args()

    # 解析源目录
    source_dir = resolve_source_dir(SCRIPT_DIR, args.source)

    # --clean 不依赖源验证
    if args.clean:
        clean_intermediate(source_dir)
        return

    # 验证源文件
    if not verify_source(source_dir):
        sys.exit(1)

    did_something = False

    # --zip
    if args.zip:
        package_zip(source_dir)
        did_something = True

    # --engine（手动指定引擎路径）
    if args.engine:
        engine_path = args.engine
        if not os.path.isdir(engine_path):
            print(f"\n[ERROR] 指定的引擎路径无效: {engine_path}")
            sys.exit(1)
        deploy_to_engine_plugins(source_dir, engine_path, force=args.force)
        did_something = True

    # --auto-engine
    if args.auto_engine:
        engine_path = find_engine_path()
        if not engine_path:
            print("\n[ERROR] 无法自动检测 UE 引擎路径。")
            print("  请用 --engine <path> 手动指定引擎根目录。")
            print("  或设置环境变量 UE5_ENGINE_PATH。")
            sys.exit(1)
        print(f"[INFO] 自动检测引擎: {engine_path}")
        deploy_to_engine_plugins(source_dir, engine_path, force=args.force)
        did_something = True

    # --target（手动指定项目路径）
    if args.target:
        target = args.target
        if not os.path.isdir(target):
            print(f"\n[ERROR] 指定的项目路径无效: {target}")
            sys.exit(1)
        deploy_to_target(source_dir, target, force=args.force)
        did_something = True

    # 默认：自动检测项目路径并部署
    if not did_something:
        project_path = find_project_path()
        if not project_path:
            print("\n[ERROR] 无法自动检测 UE 项目路径。")
            print("  请用 --target <path> 指定项目路径。")
            print("  或设置环境变量 UE5_PROJECT_PATH。")
            print("  或确保当前目录层级下有 .uproject 文件。")
            sys.exit(1)
        print(f"[INFO] 自动检测项目: {project_path}")
        deploy_to_target(source_dir, project_path, force=args.force)

    print("\n[OK] 部署完成。请确保 UE 编辑器已关闭（避免 DLL 锁定）。")


if __name__ == "__main__":
    main()
