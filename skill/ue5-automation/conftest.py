# -*- coding: utf-8 -*-
"""
conftest.py — pytest 统一 fixtures
==================================
用于 ue5-automation Skill 测试套件。

Fixtures:
  - skill_root:        Skill 根目录绝对路径
  - scripts_path:      自动添加 scripts/ 到 sys.path（autouse，session 级）
  - mock_reader_data:  加载 mock_reader_output.json（module 级缓存）
  - temp_output_dir:   临时输出目录（function 级，自动清理）
  - ue_mock:           Mock UE 环境（unreal 模块替代品）

自定义标记:
  - @pytest.mark.ue_online   需要 UE 编辑器在线的测试（默认跳过）
  - @pytest.mark.slow        慢速测试（>5秒）

会话启动时自动：
  1. 注入 scripts/ 到 sys.path
  2. 注册自定义标记
"""

import os
import sys
import json
import tempfile
import pytest
from unittest.mock import MagicMock

# ══════════════════════════════════════════════════════════════
# 路径常量
# ══════════════════════════════════════════════════════════════

SKILL_ROOT = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_PATH = os.path.join(SKILL_ROOT, "scripts")
TESTS_PATH = os.path.join(SKILL_ROOT, "tests")


# ══════════════════════════════════════════════════════════════
# Session 级 Fixtures
# ══════════════════════════════════════════════════════════════

@pytest.fixture(scope="session")
def skill_root():
    """Skill 根目录绝对路径。"""
    return SKILL_ROOT


@pytest.fixture(scope="session", autouse=True)
def scripts_path():
    """
    自动添加 scripts/ 到 sys.path。
    session 级 autouse：整个测试会话只执行一次，所有测试共享。
    替代各测试文件中硬编码的 sys.path.insert。
    """
    if SCRIPTS_PATH not in sys.path:
        sys.path.insert(0, SCRIPTS_PATH)
    return SCRIPTS_PATH


# ══════════════════════════════════════════════════════════════
# Module 级 Fixtures
# ══════════════════════════════════════════════════════════════

@pytest.fixture(scope="module")
def mock_reader_data():
    """加载 Mock reader 输出 JSON（module 级缓存，同模块共享）。"""
    mock_path = os.path.join(TESTS_PATH, "mock_reader_output.json")
    if not os.path.exists(mock_path):
        pytest.skip(f"Mock 数据文件不存在: {mock_path}")
    with open(mock_path, "r", encoding="utf-8") as f:
        return json.load(f)


# ══════════════════════════════════════════════════════════════
# Function 级 Fixtures
# ══════════════════════════════════════════════════════════════

@pytest.fixture
def temp_output_dir():
    """临时输出目录（function 级，测试结束后自动清理）。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


@pytest.fixture(scope="session")
def kb_available():
    """
    检测 KB 语义搜索服务（127.0.0.1:8890）是否可达。

    返回 True/False。使用此 fixture 的测试应自行决定是否 skip：
        if not kb_available:
            pytest.skip("KB 服务不可达")
    """
    import urllib.request
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:8890/v1/search?q=test&top_k=1&kb=agent_knowledge",
            method="GET"
        )
        urllib.request.urlopen(req, timeout=3)
        return True
    except Exception:
        return False


@pytest.fixture
def ue_exec_mock(monkeypatch):
    """
    Mock ue_send.py 的 subprocess.run 调用。

    拦截任何涉及 'ue_send' 的命令行，返回模拟的 CompletedProcess，
    stdout 包含最小有效 JSON 探针结果。
    用于离线测试中不依赖 UE 编辑器的场景。
    """
    import subprocess as sp

    original_run = sp.run

    def _mock_run(cmd_args, **kwargs):
        is_ue = False
        if isinstance(cmd_args, list):
            is_ue = any("ue_send" in str(a) for a in cmd_args)
        elif isinstance(cmd_args, str):
            is_ue = "ue_send" in cmd_args
        if is_ue:
            return sp.CompletedProcess(
                args=cmd_args if isinstance(cmd_args, list) else [],
                returncode=0,
                stdout=(
                    '__JSON_RESULT__'
                    '{"ok":true,"level":"L1",'
                    '"basic_info":{"name":"BP_Mock","asset_path":"/Game/Mock/BP_Mock",'
                    '"parent_class":"Actor","blueprint_type":"Blueprint","interfaces":[],'
                    '"node_count_total":2,"graph_count":1},'
                    '"variables":[{"var_name":"Health","var_type":"Float",'
                    '"default_value":"100.0","is_exposed":true,"is_array":false}],'
                    '"graphs":{"EventGraph":{"graph_name":"EventGraph","node_count":2,'
                    '"nodes":[{"title":"BeginPlay","node_class":"K2Node_Event"}],'
                    '"connections":[]}},'
                    '"connections":[],"errors":[],"warnings":[]}'
                    '__JSON_END__'
                ),
                stderr="",
            )
        return original_run(cmd_args, **kwargs)

    monkeypatch.setattr(sp, "run", _mock_run)
    return monkeypatch


@pytest.fixture
def ue_mock():
    """
    Mock UE 环境：将 'unreal' 模块注入 sys.modules。
    
    提供常用 UE API 的 MagicMock 替身，避免离线环境 import 失败。
    使用 autouse=False（按需调用），避免对所有测试注入副作用。
    """
    if "unreal" not in sys.modules:
        ue_mock_module = MagicMock()
        ue_mock_module.EditorAssetLibrary = MagicMock()
        ue_mock_module.EditorAssetLibrary.load_asset = MagicMock()
        ue_mock_module.EditorAssetLibrary.list_assets = MagicMock(return_value=[])
        ue_mock_module.log = MagicMock()
        ue_mock_module.log_warning = MagicMock()
        ue_mock_module.log_error = MagicMock()
        sys.modules["unreal"] = ue_mock_module
    yield sys.modules["unreal"]


# ══════════════════════════════════════════════════════════════
# Pytest 配置钩子
# ══════════════════════════════════════════════════════════════

def pytest_configure(config):
    """注册自定义标记 + 配置。"""
    config.addinivalue_line(
        "markers",
        "ue_online: 需要 UE 编辑器在线运行的测试（CI 中默认跳过）"
    )
    config.addinivalue_line(
        "markers",
        "ue_offline: 不需要真 UE 的断连/兜底测试（T-20260805-UE-FULL-CLOSURE P1-1 新增）"
    )
    config.addinivalue_line(
        "markers",
        "slow: 慢速测试（>5秒，CI 中可选跳过）"
    )
    config.addinivalue_line(
        "markers",
        "requires_kb: 需要 KB 语义搜索服务（8890）可用"
    )


def pytest_collection_modifyitems(config, items):
    """
    自动 skip：
    - ue_online 标记（未传 --ue-online 时跳过）
    - requires_kb 标记（KB 8890 不可达时跳过）
    """
    # UE 在线检查
    if not config.getoption("--ue-online", default=False):
        skip_ue = pytest.mark.skip(reason="需要 --ue-online 选项启用 UE 在线测试")
        for item in items:
            if "ue_online" in item.keywords:
                item.add_marker(skip_ue)

    # KB 可用性检查
    import urllib.request
    kb_ok = False
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:8890/v1/search?q=test&top_k=1&kb=agent_knowledge",
            method="GET"
        )
        urllib.request.urlopen(req, timeout=2)
        kb_ok = True
    except Exception:
        pass

    if not kb_ok:
        skip_kb = pytest.mark.skip(reason="KB 语义搜索服务 (127.0.0.1:8890) 不可达")
        for item in items:
            if "requires_kb" in item.keywords:
                item.add_marker(skip_kb)


def pytest_addoption(parser):
    """添加 --ue-online 自定义选项。"""
    parser.addoption(
        "--ue-online",
        action="store_true",
        default=False,
        help="运行需要 UE 编辑器在线的测试"
    )
