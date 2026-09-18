# Copyright Epic Games, Inc. All Rights Reserved.
"""ue5-automation-extras — 把 ue5-automation skill 的能力注册进官方 MCP。

随 ue5-automation v3.0 发布包交付（bridge/ue58-extras/）。

本插件为纯 Content 插件（无 C++ 模块，照 ZCodeMCPExtras 同款已验证模式）：
Unreal 会为每个启用插件运行其 ``Content/Python/init_unreal.py``，但**不会**
运行项目自身 ``Content/Python`` 下的 init_unreal.py —— 所以引导必须以插件形式发布。

工具分两类：
  · 纯 Python 工具（本文件即可用）：UE 状态 / PIE 状态 / 日志 / Actor 查询与传送 /
    蓝图编译 —— 任何 5.8 项目装上即用。
  · 节点级图工具（graph_nodes / graph_connect）：需要 BlueprintPythonBridge
    C++ 插件（本包 bridge/ 源码用 Source/build_for_engine.bat 对 5.8 重编译后部署）。
    未部署时这些工具会给出**可读的缺失提示**，绝不假装成功。

加载到已运行的编辑器（Output Log 控制台 Cmd 框）：

    py "import init_unreal"
"""

import os
import sys

import unreal

PROJECT_PYTHON_DIR = os.path.join(unreal.Paths.project_content_dir(), "Python")

# 工具集实现放在插件自身 Content/Python 下（本文件的同目录）—— 插件目录
# 由 UBT 动态加载时不一定在 sys.path，显式补一次（与项目级方案同思路）。
PLUGIN_PYTHON_DIR = os.path.dirname(os.path.abspath(__file__))
for _d in (PROJECT_PYTHON_DIR, PLUGIN_PYTHON_DIR):
    if _d and os.path.isdir(_d) and _d not in sys.path:
        sys.path.append(_d)

try:
    # Registration 定义在 registration 子模块；包 __init__ 只再导出
    # tool_call / reload_module / tool_raising_exceptions。
    from toolset_registry.registration import Registration
    from ue5_automation_toolset import UE5AutomationExtras
except Exception as error:  # pylint: disable=broad-except
    unreal.log_warning(f"[ue5-automation-extras] Could not import the toolset module: {error}")
else:
    try:
        if Registration([UE5AutomationExtras]).register():
            unreal.log("[ue5-automation-extras] Registered toolset "
                       "ue5_automation_toolset.UE5AutomationExtras.")
        else:
            unreal.log_warning("[ue5-automation-extras] Toolset registry is unavailable; "
                               "toolset not registered.")
    except Exception as error:  # pylint: disable=broad-except
        unreal.log_warning(f"[ue5-automation-extras] Registration failed: {error}")
