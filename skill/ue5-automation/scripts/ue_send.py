"""
UE 远程执行 — ue_send 精简版
用法: python ue_send.py "print('hello')"

本文件由 ue5-automation Skill 分发，不依赖固定 UE 安装路径。
引擎路径优先级：
  1. 环境变量 UE_ENGINE_PATH（指向 Engine 目录，如 D:/uesoft/UE/UE_5.1/Engine）
  2. 环境变量 UE5_ENGINE_PATH
  3. 常见默认路径（Epic Launcher 各盘符 × UE_5.1~5.5）+ 注册表探到的引擎目录

注意：Remote Execution 的命令执行阶段（run_command）本身无宿主侧超时 ——
编辑器弹出模态对话框时可能一直阻塞；发现阶段受 timeout 参数控制。
"""
import sys
import os

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)


def _find_remote_execution():
    """查找 remote_execution 模块所在路径。"""
    candidates = []

    # 环境变量
    for env in ("UE_ENGINE_PATH", "UE5_ENGINE_PATH"):
        val = os.environ.get(env, "")
        if val:
            candidates.append(os.path.join(val, "Plugins", "Experimental", "PythonScriptPlugin", "Content", "Python"))
            # 也支持直接指向 Engine 目录
            candidates.append(os.path.join(val, "Engine", "Plugins", "Experimental", "PythonScriptPlugin", "Content", "Python"))

    # 常见默认路径（v2.20：动态覆盖各盘符 Epic 默认安装 × UE 5.1~5.5，
    #   旧实现硬编码到 5.3 且含开发机私有路径 → 5.4/5.5 默认安装机探测失败）
    for _drive in ("C:", "D:", "E:", "F:"):
        for _ver in ("5.1", "5.2", "5.3", "5.4", "5.5"):
            candidates.append(
                os.path.join(_drive + os.sep, "Program Files", "Epic Games",
                             "UE_" + _ver, "Engine", "Plugins", "Experimental",
                             "PythonScriptPlugin", "Content", "Python"))
            candidates.append(
                os.path.join(_drive + os.sep, "UE_" + _ver, "Engine", "Plugins",
                             "Experimental", "PythonScriptPlugin", "Content", "Python"))

    # 注册表探测（Epic Launcher 写入 InstalledDirectory；HKCU 优先于 HKLM）
    try:
        import winreg
        for _root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                _key = winreg.OpenKey(_root,
                                      r"SOFTWARE\EpicGames\Unreal Engine",
                                      0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY)
            except OSError:
                continue
            try:
                _i = 0
                while True:
                    try:
                        _sub = winreg.EnumKey(_key, _i)
                        _i += 1
                    except OSError:
                        break
                    try:
                        _sk = winreg.OpenKey(_key, _sub, 0,
                                             winreg.KEY_READ | winreg.KEY_WOW64_64KEY)
                        _inst, _ = winreg.QueryValueEx(_sk, "InstalledDirectory")
                        winreg.CloseKey(_sk)
                    except OSError:
                        continue
                    if _inst:
                        candidates.append(os.path.join(
                            str(_inst), "Engine", "Plugins", "Experimental",
                            "PythonScriptPlugin", "Content", "Python"))
            finally:
                winreg.CloseKey(_key)
    except ImportError:
        pass  # 非 Windows

    for path in candidates:
        if os.path.isdir(path):
            return path

    return None


_REMOTE_EXEC_DIR = _find_remote_execution()
if _REMOTE_EXEC_DIR:
    sys.path.insert(0, _REMOTE_EXEC_DIR)

try:
    import remote_execution
except ImportError:
    print("[错误] 无法导入 remote_execution。请设置 UE_ENGINE_PATH 环境变量指向 UE Engine 目录。")
    print(f"已尝试路径: {_REMOTE_EXEC_DIR or '(none)'}")
    sys.exit(1)


# ── 节点缓存：首次发现后缓存 node_id，避免重复多播发现 ──
_CACHED_NODE = None

def send(code, timeout=15):
    """向 UE 发送 Python 代码。

    timeout 仅控制**发现阶段**等待秒数（v2.20：旧实现形参存在但从未使用，
    发现等待硬编码 15s）；执行阶段（run_command）受 UE Remote Execution
    协议限制无宿主侧超时，编辑器弹模态框时可能阻塞。

    返回码：0=成功 / 1=未发现 UE 节点 / 2=执行失败 / 3=调用异常。
    """
    global _CACHED_NODE
    rex = remote_execution.RemoteExecution()
    rex.start()

    import time

    # 如果有缓存节点，先等 2 秒看它是否出现在列表中（UDP 偶尔丢包但节点还在）
    if _CACHED_NODE:
        for _ in range(4):
            time.sleep(0.5)
            for n in rex.remote_nodes:
                if n.get('node_id') == _CACHED_NODE:
                    node = n
                    break
            else:
                continue
            break
        else:
            _CACHED_NODE = None  # 缓存失效

    # 多播发现：最多等 timeout 秒，失败后重试 2 次
    if not _CACHED_NODE:
        node = None
        for attempt in range(3):
            waited = 0
            while not rex.remote_nodes and waited < timeout:
                time.sleep(0.5)
                waited += 0.5
            if rex.remote_nodes:
                node = rex.remote_nodes[0]
                _CACHED_NODE = node.get('node_id')
                break
            else:
                try:
                    rex.stop()
                except Exception:
                    pass
                if attempt < 2:
                    time.sleep(2)
                    rex = remote_execution.RemoteExecution()
                    rex.start()
        if node is None:
            _CACHED_NODE = None
            print("[错误] 未发现 UE 节点")
            try:
                rex.stop()
            except Exception:
                pass
            return 1

    print(f"[连接] {node.get('project_name','?')}")

    try:
        rex.open_command_connection(node['node_id'])
        result = rex.run_command(code, unattended=True, exec_mode=remote_execution.MODE_EXEC_FILE)
    except Exception as e:
        # P1-1 断连兜底：remote_execution 调用异常 → 明确错误码 3，不吐 raw traceback
        # v2.20：异常路径同时重置节点缓存（旧实现残留死节点 → 长驻宿主每次先耗 2s 验证）
        print(f"[错误] UE 远程执行调用异常: {e}")
        _CACHED_NODE = None
        try:
            rex.stop()
        except Exception:
            pass
        return 3

    if result:
        for entry in result.get('output', []):
            try:
                sys.stdout.write(entry.get('output', ''))
            except UnicodeEncodeError:
                sys.stdout.write(entry.get('output', '').encode('ascii', 'replace').decode('ascii'))
        if not result.get('success', True):
            print(f"\n[错误] {result.get('result', '')}")

    rex.stop()
    if result and not result.get('success', True):
        return 2
    return 0


# ── Bridge 真实加载验证（T-20260805-UE-E2E-FIX）──────────────────
# 原 bridge_loaded 判定只看目录存在 → 误判。此处改为：
#   ① 尝试 import BlueprintPythonBridge（独立 Python 模块）
#   ② 验证 unreal.BlueprintPythonBridge（C++ 插件暴露属性）
#   ③ 实测 get_all_graphs / list_nodes 返回真实数据
_BRIDGE_DIAG_CODE = r'''
import json
import unreal

diag = {"bridge_loaded": False, "check": {}, "error": ""}

# ① import 独立模块
try:
    import BlueprintPythonBridge as BPB
    diag["check"]["import_module"] = True
    diag["check"]["module_file"] = str(getattr(BPB, "__file__", "n/a"))
except Exception as e:
    diag["check"]["import_module"] = False
    diag["check"]["import_module_error"] = str(e)

# ② unreal 属性（C++ 插件真实暴露面）
try:
    b = unreal.BlueprintPythonBridge
    diag["check"]["unreal_attr"] = True
    diag["check"]["unreal_type"] = str(type(b))
    methods = sorted([m for m in dir(b) if not m.startswith("_")])
    diag["check"]["method_count"] = len(methods)
    diag["check"]["has_get_all_graphs"] = hasattr(b, "get_all_graphs")
    diag["check"]["has_list_nodes"] = hasattr(b, "list_nodes")
except Exception as e:
    diag["check"]["unreal_attr"] = False
    diag["check"]["unreal_attr_error"] = str(e)

# ③ 实测 API（真实加载成功 → get_all_graphs 能拿到图）
try:
    bp = unreal.EditorAssetLibrary.load_asset("/Game/BP_HelloMirror")
    if bp:
        gs = unreal.BlueprintPythonBridge.get_all_graphs(bp)
        diag["check"]["api_probe"] = True
        diag["graph_count"] = len(gs)
        diag["graph_names"] = [g.get_name() for g in gs]
        total = 0
        for g in gs:
            try:
                total += len(unreal.BlueprintPythonBridge.list_nodes(g))
            except Exception:
                pass
        diag["total_nodes"] = total
    else:
        diag["check"]["api_probe"] = False
        diag["check"]["api_probe_error"] = "BP_HelloMirror 加载失败"
except Exception as e:
    diag["check"]["api_probe"] = False
    diag["check"]["api_probe_error"] = str(e)

diag["bridge_loaded"] = bool(
    diag["check"].get("unreal_attr") and diag["check"].get("api_probe")
)
print("__BRIDGE_DIAG__" + json.dumps(diag, ensure_ascii=False, default=str) + "__BRIDGE_DIAG_END__")
'''


def bridge_diag():
    """发送 bridge 真实加载验证探针到 UE，解析并打印诊断结果。

    v2.20 修复（P2）：旧实现仅转发输出、不解析 marker，且无论 bridge_loaded
    真假一律退出码 0 → 自动化流程无法判断。现解析 __BRIDGE_DIAG__ 标记、
    打印结构化 JSON，并按结果设退出码：0=已加载 / 4=未加载 / 1/3=链路失败。
    """
    import io
    import json
    import contextlib
    import re as _re
    _buf = io.StringIO()
    with contextlib.redirect_stdout(_buf):
        rc = send(_BRIDGE_DIAG_CODE)
    output = _buf.getvalue()
    # send() 内部已把 UE 输出打到（重定向后的）stdout，这里原样回放
    sys.stdout.write(output)
    m = _re.search(r"__BRIDGE_DIAG__(.*?)__BRIDGE_DIAG_END__", output, _re.DOTALL)
    if rc == 0 and m:
        try:
            diag = json.loads(m.group(1))
        except Exception:
            print("[bridge-diag] 诊断 JSON 解析失败（marker 命中但内容异常）")
            return 4
        loaded = bool(diag.get("bridge_loaded"))
        print("[bridge-diag] bridge_loaded=%s method_count=%s graph_count=%s"
              % (loaded, diag.get("check", {}).get("method_count", "?"),
                 diag.get("graph_count", "?")))
        return 0 if loaded else 4
    print("[bridge-diag] 未取得诊断结果（rc=%s）" % rc)
    return rc or 4


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("用法: python ue_send.py \"python代码\"")
        print("      python ue_send.py --bridge-diag   # Bridge 真实加载诊断")
        sys.exit(1)
    if sys.argv[1] == "--bridge-diag":
        sys.exit(bridge_diag())
    sys.exit(send(sys.argv[1]))
