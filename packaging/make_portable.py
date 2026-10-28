# -*- coding: utf-8 -*-
"""从仓库生成「可移植包」zip（T-20261027-PORTABLE）。

用法（仓库根目录）：
    python packaging/make_portable.py            # 输出 dist/ue5-automation-v<ver>-portable.zip
    python packaging/make_portable.py --out D:\\x.zip

包结构：
    install.bat / README-新机测试.md / VERSION.txt
    plugin/BlueprintPythonBridge/     ← uplugin + 头文件 + Source + 5.1 DLL
    plugin/prebuilt-5.8/              ← 5.8 DLL + modules
    host/python/                      ← Content/Python 内容（*.py + *.tmpl）
    host/ui/index.html                ← 管理端
    tools/deploy_portable.py          ← 一键部署
    tools/check_env.py                ← 环境自检
    docs/…                            ← QUICKSTART / SKILL / pitfalls / 验证记录 / 报告

只依赖标准库；排除 __pycache__ 与 .bak。
"""
import argparse
import hashlib
import io
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG_DIR = os.path.join(REPO, "packaging", "portable")
PLUGIN = os.path.join(REPO, "bridge", "BlueprintPythonBridge")
PRE58 = os.path.join(REPO, "bridge", "prebuilt-5.8")
BOOTSTRAP = os.path.join(PKG_DIR, "bootstrap-plugin")
SCRIPTS = os.path.join(REPO, "skill", "ue5-automation", "scripts")
UI = os.path.join(REPO, "skill", "ue5-automation", "ui")
DOCS = os.path.join(REPO, "skill", "ue5-automation", "docs")
SKILL_MD = os.path.join(REPO, "skill", "ue5-automation", "SKILL.md")
MANIFEST = os.path.join(REPO, "manifest.json")

INIT_UNREAL = '''# ue5-automation 桥自启（打包生成）
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


def read_version():
    try:
        d = json.load(io.open(MANIFEST, encoding="utf-8"))
        return d.get("package_version", "v0.0"), d.get("skill_version", "v0.0")
    except Exception:
        return "v0.0", "v0.0"


def git_commit():
    try:
        return subprocess.run(["git", "-C", REPO, "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True).stdout.strip()
    except Exception:
        return ""


def copy_tree(src, dst, exts=None, skip_names=()):
    os.makedirs(dst, exist_ok=True)
    for name in os.listdir(src):
        if name in skip_names or name == "__pycache__" or ".bak-" in name:
            continue
        s = os.path.join(src, name)
        d = os.path.join(dst, name)
        if os.path.isdir(s):
            copy_tree(s, d, exts, skip_names)
        else:
            if exts and os.path.splitext(name)[1].lower() not in exts:
                continue
            shutil.copy2(s, d)


def write_release_manifest(root, version):
    """包根生成逐文件 SHA256 清单（verify_release.py --compare 用）。"""
    files = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            d for d in dirnames
            if d not in ("__pycache__", ".pytest_cache", ".git"))
        for name in sorted(filenames):
            if name in ("RELEASE-MANIFEST.json",) or name.endswith((".pyc", ".pyo")):
                continue
            p = os.path.join(dirpath, name)
            rel = os.path.relpath(p, root).replace(os.sep, "/")
            h = hashlib.sha256()
            with open(p, "rb") as f:
                for chunk in iter(lambda: f.read(1024 * 1024), b""):
                    h.update(chunk)
            files[rel] = {"size": os.path.getsize(p), "sha256": h.hexdigest()}
    mf = {
        "name": "ue5-automation",
        "version": version,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "file_count": len(files),
        "files": files,
    }
    with io.open(os.path.join(root, "RELEASE-MANIFEST.json"), "w",
                 encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(mf, ensure_ascii=False, indent=2) + "\n")
    return len(files)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", help="输出 zip 路径（默认 dist/ue5-automation-<ver>-portable.zip）")
    args = ap.parse_args()
    pkg_ver, skill_ver = read_version()
    out = args.out or os.path.join(REPO, "dist",
                                   "ue5-automation-%s-portable.zip" % pkg_ver)
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)

    stage = tempfile.mkdtemp(prefix="ue5auto-pkg-")
    root = os.path.join(stage, "ue5-automation-%s-portable" % pkg_ver)
    try:
        # ── 组装 ──
        copy_tree(PLUGIN, os.path.join(root, "plugin", "BlueprintPythonBridge"),
                  skip_names=("Out", "Config", "Intermediate", "__pycache__"))
        copy_tree(PRE58, os.path.join(root, "plugin", "prebuilt-5.8"))
        # v4.6：5.8 不再执行项目级 init_unreal.py → 随包一个纯 Python 引导插件
        if os.path.isdir(BOOTSTRAP):
            copy_tree(BOOTSTRAP, os.path.join(root, "plugin"))
        copy_tree(SCRIPTS, os.path.join(root, "host", "python"),
                  exts=(".py", ".tmpl"), skip_names=("tests", "__pycache__"))
        # 包内不放临时/演示脚本
        for fn in os.listdir(os.path.join(root, "host", "python")):
            if fn.startswith("_"):
                os.remove(os.path.join(root, "host", "python", fn))
        copy_tree(UI, os.path.join(root, "host", "ui"))
        with io.open(os.path.join(root, "host", "python", "init_unreal.py"),
                     "w", encoding="utf-8", newline="\n") as f:
            f.write(INIT_UNREAL)
        copy_tree(PKG_DIR, os.path.join(root, "tools"), skip_names=("bootstrap-plugin",))
        os.makedirs(os.path.join(root, "docs"), exist_ok=True)
        for src, dst in ((os.path.join(DOCS, "QUICKSTART.md"), "QUICKSTART.md"),
                         (os.path.join(DOCS, "pitfalls.md"), "pitfalls.md"),
                         (os.path.join(DOCS, "VERIFICATION-RELEASE-3.9.md"),
                          "VERIFICATION-RELEASE-3.9.md"),
                         (os.path.join(DOCS, "MCP-ACCESS.md"), "MCP-ACCESS.md"),
                         (os.path.join(DOCS, "RELEASE-v4.5-任务级撤销.md"),
                          "RELEASE-v4.5-任务级撤销.md"),
                         (os.path.join(DOCS, "RELEASE-v4.6-结构化编译诊断.md"),
                          "RELEASE-v4.6-结构化编译诊断.md"),
                         (os.path.join(DOCS, "RELEASE-v4.9-第一梯队P0.md"),
                          "RELEASE-v4.9-第一梯队P0.md"),
                         (os.path.join(DOCS, "RELEASE-v5.0-资产治理.md"),
                          "RELEASE-v5.0-资产治理.md"),
                         (os.path.join(DOCS, "RELEASE-v6.0-最终版.md"),
                          "RELEASE-v6.0-最终版.md"),
                         (os.path.join(DOCS, "SECURITY-AUDIT-v6.4.md"),
                          "SECURITY-AUDIT-v6.4.md"),
                         (os.path.join(DOCS,
                                       "RELEASE-v6.5-多Agent与稳定性.md"),
                          "RELEASE-v6.5-多Agent与稳定性.md"),
                         (os.path.join(DOCS,
                                       "MERGE-v6.5-已优化版本A.md"),
                          "MERGE-v6.5-已优化版本A.md"),
                         (os.path.join(DOCS,
                                       "CROSS-VALIDATION-v6.5.md"),
                          "CROSS-VALIDATION-v6.5.md"),
                         (os.path.join(DOCS, "ENVIRONMENT-v6.5.md"),
                          "ENVIRONMENT-v6.5.md"),
                         (os.path.join(DOCS, "DELIVERY-v6.5.md"),
                          "DELIVERY-v6.5.md"),
                         (SKILL_MD, "SKILL.md")):
            if os.path.isfile(src):
                shutil.copy2(src, os.path.join(root, "docs", dst))
        # v4.6 分层：SKILL.md 的详细走查放在 docs/reference/（缺失会让链接失效）
        ref = os.path.join(DOCS, "reference")
        if os.path.isdir(ref):
            copy_tree(ref, os.path.join(root, "docs", "reference"))
        rep = os.path.join(DOCS, "reports")
        if os.path.isdir(rep):
            copy_tree(rep, os.path.join(root, "docs", "reports"))
        # README 与 VERSION
        rd = os.path.join(PKG_DIR, "README-portable.md")
        if os.path.isfile(rd):
            shutil.copy2(rd, os.path.join(root, "README-新机测试.md"))
        # 合规：Apache-2.0 许可证 + 第三方声明必须随分发物一起提供
        for _legal in ("LICENSE", "THIRD_PARTY_NOTICES.md"):
            _src = os.path.join(REPO, _legal)
            if os.path.isfile(_src):
                shutil.copy2(_src, os.path.join(root, _legal))
        with io.open(os.path.join(root, "VERSION.txt"), "w",
                     encoding="utf-8", newline="\n") as f:
            f.write("ue5-automation %s（skill %s）\n"
                    "源码提交: %s\n"
                    "包含: 插件双引擎 DLL（UE 5.1 / 5.8）+ 宿主脚本 + 管理端 + 文档\n"
                    "      一键部署（tools/deploy_portable.py）+ 环境自检（tools/check_env.py）\n"
                    % (pkg_ver, skill_ver, git_commit() or "?"))

        # v6.5：包根写逐文件 SHA256 清单（跨机对比用；token/日志已排除）
        mf_count = write_release_manifest(root, pkg_ver)
        print("发布清单: RELEASE-MANIFEST.json (%d 文件)" % mf_count)

        # ── 打 zip ──
        if os.path.isfile(out):
            os.remove(out)
        count = 0
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
            for r2, dns, fns in os.walk(root):
                dns[:] = [d for d in dns if d not in ("__pycache__", ".git")]
                for fn in fns:
                    full = os.path.join(r2, fn)
                    rel = os.path.relpath(full, stage)
                    z.write(full, rel)
                    count += 1
        size_mb = os.path.getsize(out) / 1024 / 1024
        print("输出: %s" % out)
        print("文件: %d | 大小: %.2f MB" % (count, size_mb))
        return 0
    finally:
        shutil.rmtree(stage, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
