#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ue5_runner.py — UE5 自动化混合执行器
=====================================
读写分通道：
  读 / 健康检查 → ue_send → UE Remote Execution（不阻塞 GameThread）
  写 / 编译 / 连线 → ue5_bridge HTTP 服务（在 UE 内后台运行，不卡 GameThread）

能力：
  - health()            检查 UE 连通性 + Bridge 插件状态
  - read()              L1/L2 蓝图读取
  - build()             创建蓝图资产 + 变量 + 节点 + 连线 + 编译
  - connect()           引脚连线 + 双向验证
  - run_python()        执行任意 Python 代码（带返回解析）
  - ensure_http_bridge  自动检测并远程启动 ue5_bridge HTTP 服务

作者：maintainer
日期：2026-07-24
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# ═══════════════════════════════════════════════════════════
# 路径发现
# ═══════════════════════════════════════════════════════════

_WORKSPACE = os.environ.get(
    "COPAW_WORKSPACE",
    os.path.join(os.path.expanduser("~"), ".copaw", "workspaces", "default")
)

# T-20260915-UE5PKG-STANDALONE：脚本自身目录。发布包/独立部署布局下
# ue_send.py / ue5_bridge.py 与 ue5_runner.py 同处 <pkg>\skill\ue5-automation\scripts\，
# 必须优先按 __file__ 定位，否则陌生机器（无 ~/.copaw/workspaces）整条写通道构造即失败。
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def _find_ue_send() -> Optional[str]:
    """查找 ue_send.py，返回第一个存在的路径。

    检测顺序：① 环境变量 UE_SEND_PATH → ② 脚本自身目录（发布包布局）
    → ③ workspace/scripts → ④ workspace/skills/ue5-automation/scripts
    → ⑤ workspace 递归搜索（最多 4 层）
    """
    env = os.environ.get("UE_SEND_PATH", "")
    if env and os.path.isfile(env):
        return env

    # ② 脚本自身目录（独立部署/发布包：ue_send.py 与 ue5_runner.py 同级）
    here = os.path.join(_SCRIPT_DIR, "ue_send.py")
    if os.path.isfile(here):
        return here

    local = os.path.join(_WORKSPACE, "scripts", "ue_send.py")
    if os.path.isfile(local):
        return local

    skill_local = os.path.join(
        _WORKSPACE, "skills", "ue5-automation", "scripts", "ue_send.py"
    )
    if os.path.isfile(skill_local):
        return skill_local

    for root, dirs, files in os.walk(_WORKSPACE):
        depth = root.replace(_WORKSPACE, "").count(os.sep)
        if depth > 4:
            dirs.clear()
            continue
        if "ue_send.py" in files:
            return os.path.join(root, "ue_send.py")

    return None


def _find_bridge_script() -> Optional[str]:
    """查找 ue5_bridge.py 路径。"""
    candidates = [
        # 脚本自身目录（发布包/独立部署：ue5_bridge.py 与 ue5_runner.py 同级）
        os.path.join(_SCRIPT_DIR, "ue5_bridge.py"),
        os.path.join(_WORKSPACE, "skills", "ue5-automation", "scripts", "ue5_bridge.py"),
        os.path.join(_WORKSPACE, "scripts", "ue5_bridge.py"),
    ]
    for p in candidates:
        if os.path.isfile(p):
            return p
    for root, dirs, files in os.walk(_WORKSPACE):
        if "ue5_bridge.py" in files:
            return os.path.join(root, "ue5_bridge.py")
    return None


# ═══════════════════════════════════════════════════════════
# 核心执行
# ═══════════════════════════════════════════════════════════

@dataclass
class UERunResult:
    ok: bool
    stdout: str = ""
    stderr: str = ""
    returncode: int = 0
    data: Dict[str, Any] = field(default_factory=dict)
    error: str = ""


def _resolve_bridge_token() -> str:
    """解析 Bridge 鉴权 token。

    优先级：环境变量 UE5_BRIDGE_TOKEN → %APPDATA%/ue5_bridge/token → "test123"。

    ⚠️ 修复 T-20260909-BRIDGE-TOKEN-SOURCE：旧实现默认 "test123" 且从不读真实
    token 文件，导致任何未显式传 http_token 的调用（CLI 入口、Agent 侧脚本）
    恒 403 → ensure_http_bridge() 误判「bridge 未运行」→ 最终报
    「HTTP bridge 启动后无响应」。真实 token 由 Bridge 首次启动时随机生成，
    落盘于 %APPDATA%/ue5_bridge/token。环境变量优先，保证离线测试可控。
    """
    env = os.environ.get("UE5_BRIDGE_TOKEN")
    if env:
        return env
    candidates = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        candidates.append(os.path.join(appdata, "ue5_bridge", "token"))
    candidates.append(os.path.join(os.path.expanduser("~"), "AppData",
                                   "Roaming", "ue5_bridge", "token"))
    for path in candidates:
        try:
            with open(path, "r", encoding="utf-8") as f:
                tok = f.read().strip()
            if tok:
                return tok
        except OSError:
            continue
    return "test123"


class UE5Runner:
    """UE5 混合执行器：读用 ue_send，写用 HTTP bridge。"""

    def __init__(self, ue_send_path: Optional[str] = None,
                 http_host: str = "127.0.0.1",
                 http_port: int = 8889,
                 http_token: Optional[str] = None,
                 bridge_script_path: Optional[str] = None):
        self.ue_send_path = ue_send_path or _find_ue_send()
        if not self.ue_send_path:
            raise RuntimeError(
                "ue_send.py 未找到。请设置 UE_SEND_PATH 环境变量，"
                "或将其与 ue5_runner.py 放在同一目录，"
                "或放在 workspace/scripts/ue_send.py"
            )
        self.http_host = http_host
        self.http_port = http_port
        self.http_token = http_token or _resolve_bridge_token()
        self.bridge_script_path = bridge_script_path or _find_bridge_script()
        self._base_url = f"http://{http_host}:{http_port}"
        self._bridge_lock = threading.Lock()  # T-20260727-006: 防并发重复启动

    # ── ue_send 底层 ─────────────────────────────────────

    def run_python(self, code: str, timeout: int = 60,
                   result_marker: str = "__JSON_RESULT__",
                   end_marker: str = "__JSON_END__") -> UERunResult:
        """通过 ue_send 远程执行 Python 代码。

        结果通过临时文件回传（避免 remote_execution 输出缓冲区截断大 JSON）。
        远程代码中若定义了 `result` 变量，会被自动写入结果文件。
        """
        result_file = os.path.join(
            tempfile.gettempdir(),
            f"ue_runner_result_{os.getpid()}_{int(time.time()*1000)}.json"
        )

        wrapper_template = r'''
_UE_RESULT_FILE = r"__RESULT_FILE__"
result = None
__USER_CODE__
try:
    import json as _json
    with open(_UE_RESULT_FILE, "w", encoding="utf-8") as _f:
        _json.dump(result if result is not None else {"ok": False, "error": "no result"}, _f, ensure_ascii=False, default=str)
except Exception as _e:
    try:
        with open(_UE_RESULT_FILE, "w", encoding="utf-8") as _f:
            _f.write(_json.dumps({"ok": False, "error": "write result failed: " + str(_e)}, ensure_ascii=False))
    except Exception:
        pass
'''
        wrapped_code = (wrapper_template
                        .replace("__RESULT_FILE__", result_file)
                        .replace("__USER_CODE__", code))

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", encoding="utf-8", delete=False
        ) as f:
            f.write(wrapped_code)
            tmp_path = f.name

        try:
            cmd = [
                sys.executable, self.ue_send_path,
                f"exec(open(r'{tmp_path}', encoding='utf-8').read())"
            ]
            proc = subprocess.run(
                cmd,
                capture_output=True, text=True, timeout=timeout,
                cwd=os.path.dirname(self.ue_send_path),
            )
        except subprocess.TimeoutExpired:
            return UERunResult(
                ok=False, error=f"ue_send 执行超时 ({timeout}s)",
                returncode=-1
            )
        except Exception as e:
            return UERunResult(ok=False, error=f"ue_send 调用异常: {e}")
        finally:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass

        result = UERunResult(
            ok=proc.returncode == 0,
            stdout=proc.stdout,
            stderr=proc.stderr,
            returncode=proc.returncode,
        )

        if os.path.isfile(result_file):
            try:
                with open(result_file, "r", encoding="utf-8") as f:
                    result.data = json.load(f)
                result.ok = True
            except Exception as e:
                result.error = f"读取结果文件失败: {e}"
            finally:
                try:
                    os.unlink(result_file)
                except Exception:
                    pass
        else:
            output = proc.stdout + proc.stderr
            idx_start = output.find(result_marker)
            idx_end = output.find(end_marker)
            if idx_start != -1 and idx_end != -1 and idx_end > idx_start:
                json_str = output[idx_start + len(result_marker):idx_end]
                try:
                    result.data = json.loads(json_str)
                except json.JSONDecodeError as e:
                    result.error = f"JSON 解析失败: {e}"

        if not result.ok and not result.error:
            result.error = (proc.stdout + proc.stderr)[:500]

        return result

    # ── HTTP bridge 底层 ───────────────────────────────────

    def _http_request(self, method: str, endpoint: str,
                      payload: Optional[Dict] = None,
                      timeout: int = 120) -> Dict[str, Any]:
        """向 ue5_bridge HTTP 服务发请求。"""
        url = f"{self._base_url}{endpoint}"
        headers = {
            "X-Skill-Token": self.http_token,
            "Accept": "application/json",
        }
        data = None
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(
            url, data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            try:
                detail = json.loads(body)
            except Exception:
                detail = body
            return {
                "ok": False,
                "error": f"HTTP {e.code}: {detail}",
            }
        except Exception as e:
            return {"ok": False, "error": f"HTTP 请求失败: {e}"}

    def _http_bridge_alive(self, timeout: int = 5) -> bool:
        try:
            req = urllib.request.Request(
                f"{self._base_url}/health",
                headers={
                    "Accept": "application/json",
                    # ⚠️ 修复 T-20260909-BRIDGE-ALIVE-TOKEN：/health 自 N-002 起需鉴权，
                    # 旧实现漏发 X-Skill-Token → 恒 403 → 恒判「bridge 未运行」→
                    # ensure_http_bridge() 每次误判并重试，最终报「HTTP bridge 启动后无响应」，
                    # 导致 /build、/connect 走 ue5_runner 在线必失败。
                    "X-Skill-Token": self.http_token or "",
                },
                method="GET"
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return data.get("status") == "ok"
        except Exception:
            return False

    def ensure_http_bridge(self, timeout: int = 30) -> Dict[str, Any]:
        """检测 HTTP bridge 是否运行，未运行则通过 ue_send 远程启动。
        T-20260727-006: 加锁防多线程并发重复启动。"""
        with self._bridge_lock:
            if self._http_bridge_alive():
                return {"ok": True, "started": False, "url": self._base_url}

            if not self.bridge_script_path:
                return {"ok": False, "error": "未找到 ue5_bridge.py，无法启动 HTTP 服务"}

            script_path = self.bridge_script_path.replace("\\", "/")
            code = f'''
import sys
sys.path.insert(0, r"{os.path.dirname(self.bridge_script_path)}")
import ue5_bridge
ue5_bridge.start(host="{self.http_host}", port={self.http_port})
'''
            res = self.run_python(code, timeout=timeout)
            if not res.ok:
                return {"ok": False, "error": f"启动 HTTP bridge 失败: {res.error or res.stdout[:500]}"}

            # 等待服务就绪
            for _ in range(int(timeout / 0.5)):
                if self._http_bridge_alive():
                    return {"ok": True, "started": True, "url": self._base_url}
                time.sleep(0.5)

            return {"ok": False, "error": "HTTP bridge 启动后无响应"}

    # ── 业务接口 ───────────────────────────────────────────

    def health(self) -> Dict[str, Any]:
        """检查 UE 连通性和 Bridge 插件状态。"""
        code = r'''
import json
import unreal

result = {"ok": False, "ue_connected": True, "errors": []}
try:
    result["engine_version"] = str(unreal.SystemLibrary.get_engine_version())
    result["project_name"] = unreal.SystemLibrary.get_game_name()
    result["project_dir"] = str(unreal.Paths.project_dir())
except Exception as e:
    result["errors"].append(f"UE info: {e}")

try:
    bridge = unreal.BlueprintPythonBridge
    result["bridge_available"] = True
    result["bridge_apis"] = [a for a in dir(bridge) if not a.startswith("_")]
except AttributeError:
    result["bridge_available"] = False
    result["errors"].append("BlueprintPythonBridge 插件未加载")

result["ok"] = len(result["errors"]) == 0
print("__JSON_RESULT__" + json.dumps(result, ensure_ascii=False, default=str) + "__JSON_END__")
'''
        res = self.run_python(code, timeout=10)
        return res.data if res.data else {"ok": False, "error": res.error or "无响应"}

    def read(self, blueprint_path: str, level: str = "L2",
             include_connections: bool = True) -> Dict[str, Any]:
        """读取蓝图。level: L1 / L2 / full。"""
        if not blueprint_path.startswith("/Game/"):
            blueprint_path = f"/Game/{blueprint_path}"

        code = (_READ_BLUEPRINT_CODE
                .replace("__BLUEPRINT_PATH__", repr(blueprint_path))
                .replace("__LEVEL__", repr(level))
                .replace("__INCLUDE_CONNECTIONS__", str(include_connections)))
        res = self.run_python(code, timeout=60)
        return res.data if res.data else {"ok": False, "error": res.error or "无响应"}

    def build(self, spec: Dict[str, Any]) -> Dict[str, Any]:
        """创建蓝图（通过 HTTP bridge）。

        spec 示例：
        {
            "name": "BP_Test",
            "package_path": "/Game/Blueprints/",
            "parent_class": "Actor",
            "variables": [{"name": "a", "type": "bool"}],
            "events": [{"event_name": "EventBeginPlay", "pos": [0, 0]}],
            # ↑ D-5（T-20260917-D5D6-CONTRACT-FIX）：`event_name` 为**主字段**；
            #   `name` 为**兼容别名**（等价可用，两者都给且值不同时以 event_name
            #   为准并在返回体 warnings 中告警）；两者都缺 → /build 结构化 fail-loud
            #   （错误点名 events[i] + 实收 keys）
            # ↑ D-8（T-20260917-D8-FUNCTIONS-CONTRACT）：`function_name` 为**主字段**，
            #   `name` 为**兼容别名**（等价可用）；`function_path` 为主字段
            #   （`/Script/模块.类名:函数名`），缺省时若给了 `target_class` → 由
            #   `<target_class>:<函数名>` 推导等价路径。三者任选、可混用；
            #   主字段与别名都给且值不同 → 以 function_name 为准并在返回体
            #   warnings 中告警；两者都缺/为空且无 function_path → /build
            #   结构化 fail-loud（错误点名 functions[i] + 实收 keys）
            "functions": [{"function_name": "PrintString",
                           "target_class": "/Script/Engine.KismetSystemLibrary"}],
            "connections": [{"src_node": "EventBeginPlay", "src_pin": "then",
                             "dst_node": "PrintString", "dst_pin": "execute"}]
        }
        """
        ensure = self.ensure_http_bridge()
        if not ensure["ok"]:
            return ensure

        # 兼容 ue5_runner 与 ue5_bridge 的字段命名
        payload = dict(spec)
        if "name" in payload and "blueprint_name" not in payload:
            payload["blueprint_name"] = payload.pop("name")
        if "package_path" in payload:
            payload["package_path"] = payload["package_path"].rstrip("/") + "/"
        if "auto_compile" in payload and "compile_after" not in payload:
            payload["compile_after"] = payload.pop("auto_compile")

        return self._http_request("POST", "/build", payload, timeout=120)

    def connect(self, blueprint_path: str,
                src_node: str, src_pin: str,
                dst_node: str, dst_pin: str) -> Dict[str, Any]:
        """连线 + 双向验证（通过 HTTP bridge）。"""
        ensure = self.ensure_http_bridge()
        if not ensure["ok"]:
            return ensure

        if not blueprint_path.startswith("/Game/"):
            blueprint_path = f"/Game/{blueprint_path}"

        payload = {
            "blueprint_path": blueprint_path,
            "connections": [{
                "src_node": src_node, "src_pin": src_pin,
                "dst_node": dst_node, "dst_pin": dst_pin,
            }]
        }
        return self._http_request("POST", "/connect", payload, timeout=60)


# ═══════════════════════════════════════════════════════════
# 远程执行代码模板
# ═══════════════════════════════════════════════════════════

_READ_BLUEPRINT_CODE = r'''
import json
import unreal

bp_path = __BLUEPRINT_PATH__
level = __LEVEL__
include_connections = __INCLUDE_CONNECTIONS__

result = {
    "ok": False,
    "level": level,
    "basic_info": {},
    "variables": [],
    "graphs": {},
    "connections": [],
    "errors": [],
}

try:
    bp = unreal.EditorAssetLibrary.load_asset(bp_path)
    if not bp:
        result["errors"].append(f"加载失败: {bp_path}")
        print("__JSON_RESULT__" + json.dumps(result, ensure_ascii=False, default=str) + "__JSON_END__")
        raise SystemExit(0)

    # 父类 / 蓝图类型：UE5.1 Python 的 Blueprint 对象未暴露 parent_class
    # （bp.parent_class() / get_editor_property("parent_class") 实测均失败）
    # → 走 AssetRegistry 标签（ParentClass / NativeParentClass / BlueprintType）
    parent_name = "Unknown"
    bp_type = "Blueprint"
    try:
        _ad = unreal.EditorAssetLibrary.find_asset_data(bp_path)
        _raw_parent = ""
        for _tag in ("ParentClass", "NativeParentClass"):
            try:
                _val = _ad.get_tag_value(_tag)
            except:
                _val = None
            if _val and str(_val) != "None":
                _raw_parent = str(_val)
                break
        if _raw_parent:
            _tail = _raw_parent.split("'")[-2] if "'" in _raw_parent else _raw_parent
            parent_name = _tail.split(".")[-1] or _raw_parent
        try:
            _raw_type = str(_ad.get_tag_value("BlueprintType") or "")
        except:
            _raw_type = ""
        _type_map = {
            "BPTYPE_Normal": "Blueprint",
            "BPTYPE_Const": "ConstBlueprint",
            "BPTYPE_MacroLibrary": "MacroLibrary",
            "BPTYPE_Interface": "Interface",
            "BPTYPE_LevelScript": "LevelScript",
            "BPTYPE_FunctionLibrary": "FunctionLibrary",
        }
        if _raw_type:
            bp_type = _type_map.get(_raw_type, _raw_type)
    except Exception as e:
        result["warnings"] = result.get("warnings", []) + [f"父类/类型读取降级: {e}"]

    result["basic_info"] = {
        "name": bp.get_name(),
        "asset_path": bp_path,
        "parent_class": parent_name,
        "blueprint_type": bp_type,
    }

    # 变量：UE5.1 Python 绑定未暴露 new_variables（get_editor_property 实测失败）
    # → 优先 C++ 反射 unreal.BlueprintPythonBridge.read_variables(bp)（与 L2 节点同通道）
    def _field(obj, *names):
        for _n in names:
            try:
                _v = getattr(obj, _n)
            except:
                continue
            if _v is not None:
                return _v
        return ""

    try:
        _bcls = unreal.BlueprintPythonBridge
        _raw_vars = _bcls.read_variables(bp) if hasattr(_bcls, "read_variables") else None
        if _raw_vars is None:
            raise RuntimeError("bridge 无 read_variables")
        for var in _raw_vars:
            result["variables"].append({
                "var_name": str(_field(var, "variable_name", "VariableName")),
                "var_type": str(_field(var, "variable_type", "VariableType")),
                "category": str(_field(var, "category", "Category") or "Default"),
                "default_value": str(_field(var, "default_value", "DefaultValue")),
                "is_exposed": bool(_field(var, "is_editable", "bIsEditable") or False),
                "is_array": bool(_field(var, "is_array", "bIsArray") or False),
            })
    except Exception as e:
        result["warnings"] = result.get("warnings", []) + [f"变量读取降级(C++ 不可用): {e}"]
        try:
            new_vars = bp.get_editor_property("new_variables")
            for var in (new_vars or []):
                try:
                    result["variables"].append({
                        "var_name": str(var.get_editor_property("var_name")),
                        "var_type": str(var.get_editor_property("var_type")),
                        "category": str(var.get_editor_property("category") or "Default"),
                        "default_value": "",
                        "is_exposed": True,
                        "is_array": False,
                    })
                except:
                    pass
        except:
            pass

    # L2 节点（轻量：只读节点标题/类名/GUID，不循环取 pin）
    if level in ("L2", "full"):
        try:
            bridge = unreal.BlueprintPythonBridge
            all_graphs = bridge.get_all_graphs(bp)
            for graph in (all_graphs or []):
                try:
                    gname = graph.get_name()
                except:
                    continue

                node_infos = bridge.list_nodes(graph)
                nodes = []
                for ni in (node_infos or []):
                    title = ""
                    if hasattr(ni, "node_title"):
                        title = str(ni.node_title)
                    elif hasattr(ni, "NodeTitle"):
                        title = str(ni.NodeTitle)
                    nclass = ""
                    if hasattr(ni, "node_class"):
                        nclass = str(ni.node_class)
                    elif hasattr(ni, "NodeClass"):
                        nclass = str(ni.NodeClass)

                    nodes.append({
                        "title": title,
                        "node_class": nclass,
                        "node_guid": str(getattr(ni, "node_guid", getattr(ni, "NodeGuid", ""))),
                        "is_event": nclass.startswith("K2Node_Event") if nclass else False,
                    })

                result["graphs"][gname] = {
                    "graph_name": gname,
                    "node_count": len(nodes),
                    "nodes": nodes,
                }

            result["basic_info"]["graph_count"] = len(result["graphs"])
            result["basic_info"]["node_count_total"] = sum(
                g.get("node_count", 0) for g in result["graphs"].values()
            )
        except Exception as e:
            result["errors"].append(f"L2 读取失败: {e}")

    # 连线拓扑单独尝试，失败不阻塞主读取
    if level in ("L2", "full") and include_connections:
        try:
            bridge = unreal.BlueprintPythonBridge
            all_graphs = bridge.get_all_graphs(bp)
            for graph in (all_graphs or []):
                try:
                    gname = graph.get_name()
                    conns = bridge.get_node_connections(graph)
                    for conn in (conns or []):
                        result["connections"].append({
                            "from_node": str(getattr(conn, "from_node_title", getattr(conn, "FromNodeTitle", ""))),
                            "from_pin": str(getattr(conn, "from_pin_name", getattr(conn, "FromPinName", ""))),
                            "from_guid": str(getattr(conn, "from_node_guid", getattr(conn, "FromNodeGuid", ""))),
                            "to_node": str(getattr(conn, "to_node_title", getattr(conn, "ToNodeTitle", ""))),
                            "to_pin": str(getattr(conn, "to_pin_name", getattr(conn, "ToPinName", ""))),
                            "to_guid": str(getattr(conn, "to_node_guid", getattr(conn, "ToNodeGuid", ""))),
                        })
                except Exception as e:
                    result["warnings"] = result.get("warnings", []) + [f"图 {gname} 连线读取失败: {e}"]
        except Exception as e:
            result["warnings"] = result.get("warnings", []) + [f"连线读取失败: {e}"]

    result["ok"] = True

except Exception as e:
    result["errors"].append(f"异常: {e}")

print("__JSON_RESULT__" + json.dumps(result, ensure_ascii=False, default=str) + "__JSON_END__")
'''


_BUILD_BLUEPRINT_CODE = r'''
# 已弃用：build 现在走 HTTP bridge。保留模板仅作历史参考。
result = {"ok": False, "error": "build 已迁移到 HTTP bridge"}
print("__JSON_RESULT__" + json.dumps(result, ensure_ascii=False, default=str) + "__JSON_END__")
'''


_CONNECT_PINS_CODE = r'''
# 已弃用：connect 现在走 HTTP bridge。保留模板仅作历史参考。
result = {"ok": False, "error": "connect 已迁移到 HTTP bridge"}
print("__JSON_RESULT__" + json.dumps(result, ensure_ascii=False, default=str) + "__JSON_END__")
'''


# ═══════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════

def main():
    import argparse
    parser = argparse.ArgumentParser(description="UE5 automation hybrid runner")
    subparsers = parser.add_subparsers(dest="command")

    p = subparsers.add_parser("health", help="check UE connection")
    p.add_argument("--ue-send", help="path to ue_send.py")

    p = subparsers.add_parser("read", help="read blueprint")
    p.add_argument("blueprint_path")
    p.add_argument("--level", default="L2", choices=["L1", "L2", "full"])
    p.add_argument("--ue-send", help="path to ue_send.py")

    p = subparsers.add_parser("build", help="build blueprint from JSON spec")
    p.add_argument("spec_json")
    p.add_argument("--ue-send", help="path to ue_send.py")
    p.add_argument("--bridge-port", type=int, default=8889, help="HTTP bridge port")

    p = subparsers.add_parser("connect", help="connect pins")
    p.add_argument("blueprint_path")
    p.add_argument("src_node")
    p.add_argument("src_pin")
    p.add_argument("dst_node")
    p.add_argument("dst_pin")
    p.add_argument("--ue-send", help="path to ue_send.py")
    p.add_argument("--bridge-port", type=int, default=8889, help="HTTP bridge port")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return

    runner = UE5Runner(
        ue_send_path=args.ue_send,
        http_port=getattr(args, "bridge_port", 8889),
    )

    if args.command == "health":
        print(json.dumps(runner.health(), indent=2, ensure_ascii=False))
    elif args.command == "read":
        print(json.dumps(runner.read(args.blueprint_path, args.level), indent=2, ensure_ascii=False))
    elif args.command == "build":
        with open(args.spec_json, "r", encoding="utf-8") as f:
            spec = json.load(f)
        print(json.dumps(runner.build(spec), indent=2, ensure_ascii=False))
    elif args.command == "connect":
        print(json.dumps(runner.connect(
            args.blueprint_path, args.src_node, args.src_pin,
            args.dst_node, args.dst_pin
        ), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
