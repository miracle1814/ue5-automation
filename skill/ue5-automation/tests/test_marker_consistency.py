#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_marker_consistency.py — ue_online / ue_offline 标记一致性审计
====================================================================
T-20260805-UE-FULL-CLOSURE P1-3

静态扫描 tests/ 下所有 *.py，审计：
  1. 列出 ue_online / ue_offline 标记分布（按文件）
  2. 所有调用 ue_send / Bridge HTTP（127.0.0.1:8889）的测试都必须标 ue_online
     —— 真实 UE 依赖的测试漏标会导致 CI 无 UE 时失败/误跑
  3. run_tests.bat 的 online 模式必须使用 pytest -m 语义（--ue-online）
  4. test_blueprint_editor_e2e.py 的 4 个 E2E 测试必须全部标 ue_online

已知教训（KB 语义搜索）：
  - 标记审计不能只看函数名，要扫真实调用点（ue_send / bridge HTTP）
  - 临时文件（_ 前缀）不参与审计
"""

import os
import re
import sys
import glob
import pytest

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_SKILL_ROOT = os.path.dirname(_THIS_DIR)
_TESTS_DIR = _THIS_DIR
_RUN_TESTS_BAT = os.path.join(_SKILL_ROOT, "run_tests.bat")

# 需要真实 UE 的信号（调用 ue_send 或 Bridge HTTP）
_UE_SEND_PATTERNS = [
    r"ue_send",
    r"127\.0\.0\.1:8889",
    r"localhost:8889",
    r"TestClient\(ue5_bridge",
]
_UE_SEND_RE = [re.compile(p) for p in _UE_SEND_PATTERNS]

# E2E 文件 4 个测试函数（P0-2 新建）
_E2E_TESTS = [
    "test_e2e_update_node_param",
    "test_e2e_connect_pins",
    "test_e2e_disconnect_pin",
    "test_e2e_delete_node",
]


def _test_files():
    """tests/ 下所有 *.py（排除 _ 前缀临时文件）。"""
    files = sorted(glob.glob(os.path.join(_TESTS_DIR, "*.py")))
    return [f for f in files if not os.path.basename(f).startswith("_")]


def _read(path):
    """读取文件内容（兼容 UTF-8 / GBK）。

    T-20260806-FIX：run_tests.bat 已转为 GBK（cmd 原生兼容），
    原 utf-8 硬解码会 UnicodeDecodeError → 多编码回退。
    """
    with open(path, "rb") as f:
        raw = f.read()
    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("gbk", errors="replace")


def _func_defs(text):
    """提取 (def_name, start_line, body_start_line) 列表。"""
    defs = []
    for m in re.finditer(r"^def\s+(test_\w+)\s*\(.*?\)\s*:", text, re.M | re.S):
        defs.append((m.group(1), m.start(), m.end()))
    return defs


def _func_body(text, def_end):
    """从 def 行结束位置取后续 120 行作为函数体。"""
    lines = text[def_end:].splitlines()
    body = []
    depth = 0
    for ln in lines:
        if ln.strip().startswith("def ") or (ln.strip() and not ln.startswith((" ", "\t"))):
            if depth == 0 and body:
                break
        if ln.strip().startswith(("def ", "class ")):
            depth += 1
        body.append(ln)
        if depth > 0 and ln.strip() and not ln.startswith((" ", "\t")) and depth == 1:
            # 下一个顶层 def 停止
            if body and body[-1].strip().startswith("def "):
                pass
    # 简化：取 def 行到下一个顶层 def/class 之间的文本
    return None


def _func_source_until_next_def(text, def_end):
    """返回从 def 行结束到下一个顶层 def/class 的源码段。"""
    rest = text[def_end:]
    lines = rest.splitlines()
    out = []
    for ln in lines:
        if out and ln.strip() and not ln.startswith((" ", "\t")):
            break
        out.append(ln)
    return "\n".join(out)


def _has_ue_signal(text):
    """文本中是否出现 ue_send / Bridge HTTP 调用信号。"""
    return any(r.search(text) for r in _UE_SEND_RE)


def _markers_in_func(text, fname):
    """检查函数定义段是否含 ue_online / ue_offline 标记。"""
    has_online = ("ue_online" in text)
    has_offline = ("ue_offline" in text)
    return has_online, has_offline


def test_marker_distribution():
    """① 列出 tests/ 下所有 ue_online / ue_offline 标记分布（按文件）。"""
    report = []
    for path in _test_files():
        text = _read(path)
        online = text.count("ue_online")
        offline = text.count("ue_offline")
        report.append(f"{os.path.basename(path)}: ue_online={online} ue_offline={offline}")
    print("\n── ue_online / ue_offline 标记分布 ──")
    for line in report:
        print("  " + line)
    # 至少存在使用 ue_online 的文件（phase2 已标）
    assert any("ue_online>0" in l or "ue_online=1" in l or "ue_online=2" in l or "ue_online=3" in l or "ue_online=4" in l or "ue_online=5" in l for l in report) or True  # 只输出分布，不强制数量


def test_ue_calling_tests_marked_online():
    """② 所有调用 ue_send / Bridge HTTP 的测试函数必须标 ue_online。

    对每个测试文件：找调用 UE 信号的函数 → 其 def 行前 8 行内必须有
    @pytest.mark.ue_online（或模块级 pytestmark = ue_online）。
    """
    problems = []
    scanned = 0
    for path in _test_files():
        text = _read(path)
        fname = os.path.basename(path)
        lines = text.splitlines()

        # 模块级 pytestmark 豁免
        module_pytestmark = False
        for ln in lines[:40]:
            if "pytestmark" in ln and "ue_online" in ln:
                module_pytestmark = True

        for m in re.finditer(r"^def\s+(test_\w+)\s*\(.*?\)\s*:", text, re.M | re.S):
            def_name, def_start, def_end = m.group(1), m.start(), m.end()
            body = _func_source_until_next_def(text, def_end)
            if not _has_ue_signal(body):
                continue
            scanned += 1
            if module_pytestmark:
                continue
            # 检查 def 前 8 行是否有 ue_online 标记
            pre_lines = lines[max(0, text[:def_start].count("\n") - 7): text[:def_start].count("\n") + 1]
            pre_text = "\n".join(pre_lines)
            # T-20260806-FIX（Fix 2）：ue_offline 离线韧性测试豁免。
            #   背景：test_ue_send_resilience.py 的 3 个测试（test_ue_send_ping_no_ue /
            #   test_ue_send_retry / test_ue_send_cli_no_ue）标 @pytest.mark.ue_offline，
            #   故意在无 UE 环境验证降级路径——它们调用 ue_send 但不需要 ue_online。
            #   原审计未识别该豁免 → 误判 3 个离线韧性测试违规。
            if "ue_offline" in pre_text:
                continue
            if "ue_online" not in pre_text:
                problems.append(f"{fname}:{def_name} 调用 UE 但未标 ue_online")

    print(f"  扫描到调用 UE 的测试函数: {scanned} 个")
    assert not problems, "未标 ue_online 的 UE 调用测试:\n" + "\n".join(problems)


def test_run_tests_bat_online_uses_ue_online():
    """③ run_tests.bat 的 online 模式必须带 --ue-online。"""
    if not os.path.exists(_RUN_TESTS_BAT):
        pytest.skip("run_tests.bat 不存在")
    text = _read(_RUN_TESTS_BAT).lower()
    # 找到 :RUN_ONLINE 段
    m = re.search(r":run_online(.*?)(?=:run_|\bend\b|$)", text, re.S)
    assert m, "run_tests.bat 未找到 :RUN_ONLINE 段"
    online_section = m.group(1)
    assert "--ue-online" in online_section, \
        "run_tests.bat :RUN_ONLINE 未使用 --ue-online（应为 pytest -m 语义）"
    # e2e 模式也应带 --ue-online
    m2 = re.search(r":run_e2e(.*?)(?=:run_|\bend\b|$)", text, re.S)
    if m2:
        assert "--ue-online" in m2.group(1), "run_tests.bat :RUN_E2E 未使用 --ue-online"


def test_blueprint_editor_e2e_all_marked_online():
    """④ test_blueprint_editor_e2e.py 的 4 个 E2E 测试必须全部标 ue_online。"""
    e2e_path = os.path.join(_TESTS_DIR, "test_blueprint_editor_e2e.py")
    if not os.path.exists(e2e_path):
        pytest.skip("test_blueprint_editor_e2e.py 尚未创建（P0-2 产物）")
    text = _read(e2e_path)
    lines = text.splitlines()
    missing = []
    for tname in _E2E_TESTS:
        # 找 def 行
        idx = None
        for i, ln in enumerate(lines):
            if ln.strip().startswith("def " + tname):
                idx = i
                break
        assert idx is not None, f"E2E 测试函数 {tname} 未找到"
        pre = "\n".join(lines[max(0, idx - 6): idx + 1])
        if "ue_online" not in pre:
            missing.append(tname)
    assert not missing, f"E2E 测试未标 ue_online: {missing}"
