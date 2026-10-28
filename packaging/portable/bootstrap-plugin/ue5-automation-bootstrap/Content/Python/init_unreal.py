# ue5-automation bootstrap — starts the bridge from plugin init.
# UE 5.8+ 不再执行项目自身 Content/Python/init_unreal.py，插件 init 是唯一自动入口。
# ue5_bridge.start() 幂等（端口已监听则直接返回），与项目级 init 同时存在也无害。
import os
import sys
import traceback

import unreal

_PROJECT_PY = os.path.join(unreal.Paths.project_content_dir(), "Python")
if _PROJECT_PY not in sys.path:
    sys.path.append(_PROJECT_PY)

try:
    import ue5_bridge

    ue5_bridge.start()
    unreal.log("[ue5-automation] bridge started via bootstrap plugin")
except Exception:
    unreal.log_error("[ue5-automation] bridge 启动失败:\n" + traceback.format_exc())
