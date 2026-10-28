# -*- coding: utf-8 -*-
"""
任务日志（Task Journal）—— 仪表盘 v2 的数据脊柱（T-20261024-UI-V2）
====================================================================

定位：agent 干活时通过白名单命令上报「意图 → 计划步骤 → 进度 → 复盘」，
管理端据此渲染任务脉络/进度定位/统计图表/完成汇总。没有上报就没有脉络
（面板显示引导态，绝不编造）。

与 ui_dashboard 的关系：同为主机侧纯 Python 模块，零 UE 依赖；
本模块被 ue5_bridge import（单向，反向会成环）。

纪律口径：
  · schema 校验 fail-loud（上报错字段立即 BridgeError 级报错 —— 记错账
    比不记更糟）；
  · 持久化 fail-open（JSONL 写失败只告警，不拖垮命令主路径）；
  · 单任务在跑（同一时刻一个 active task —— agent 是串行干活的）。
"""
import json
import os
import threading
from datetime import datetime

_lock = threading.Lock()

_active = None      # 正在跑的任务 dict
_history = []       # 已结束任务（新→旧）
_seq = 0            # task_id 序号（当日）

# 持久化：APPDATA/ue5_bridge/tasks/YYYYMMDD.jsonl（逐条生命周期事件，重启可回放）
# v6.0.3：TEST_MODE 默认写临时目录 —— 测试绝不能污染真实账本（曾被 pytest
# 在每次跑测试时写入 "g"/"检查门…" 噪声任务，面板里反复出现）。
if os.environ.get("UE5_BRIDGE_TEST_MODE") == "1":
    import tempfile
    _dir = os.path.join(tempfile.gettempdir(), "ue5_bridge_tasks_test")
else:
    _dir = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")),
                        "ue5_bridge", "tasks")


def _day_stamp():
    return datetime.now().strftime("%Y%m%d")


def _file_path():
    return os.path.join(_dir, _day_stamp() + ".jsonl")


def _persist(record):
    """追加一条生命周期记录（fail-open）。"""
    try:
        os.makedirs(_dir, exist_ok=True)
        with open(_file_path(), "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:  # noqa: BLE001 —— 持久化不许拖垮主路径
        print(f"[task_journal] [WARN] 落盘失败: {e}")


def _now():
    return datetime.now().isoformat(timespec="seconds")


def _req_str(params, key, cap=600, required=False):
    val = params.get(key)
    if val is None:
        if required:
            raise ValueError(f"{key} 必填")
        return ""
    s = str(val).strip()
    if not s:
        if required:
            raise ValueError(f"{key} 不能为空字符串")
        return ""
    return s[:cap]


def _norm_steps(raw):
    """计划步骤归一化：[{name, hint?}] 或 ["a","b"] → [{id,name,hint,status}]。"""
    if raw is None:
        return []
    if not isinstance(raw, list) or not raw:
        raise ValueError("steps 必须是非空数组（[{name,hint}] 或 [\"a\",\"b\"]）")
    steps = []
    for i, item in enumerate(raw, 1):
        if isinstance(item, str):
            name, hint = item.strip(), ""
        elif isinstance(item, dict):
            name = str(item.get("name") or "").strip()
            hint = str(item.get("hint") or "").strip()
        else:
            raise ValueError(f"steps[{i}] 必须是字符串或对象")
        if not name:
            raise ValueError(f"steps[{i}].name 不能为空")
        steps.append({"id": i, "name": name[:200], "hint": hint[:200],
                      "status": "pending", "note": "", "evidence": {}})
    if len(steps) > 30:
        raise ValueError("steps 最多 30 步（计划要拆到可执行粒度）")
    return steps


#: 视为「验证证据」的关键工具（T-20261025-TRUST：success 声明需证据）
EVIDENCE_TOOLS = {
    "ue5_compile_and_diagnose": "compile",
    "ue5_compile_blueprint": "compile",
    "ue5_apply_blueprint_graph": "apply",
    "ue5_screenshot": "shot",
}
#: 自动进度注记用的动作中文名（面板显示，不需要 agent 手动 task_step）
_TOOL_LABELS = {
    "ue5_compile_and_diagnose": "编译诊断",
    "ue5_compile_blueprint": "编译",
    "ue5_apply_blueprint_graph": "整图写入",
    "ue5_screenshot": "截图",
    "ue5_build_blueprint": "建蓝图",
    "ue5_search_blueprint_nodes": "搜索节点",
    "ue5_disconnect_pin": "断开连线",
}


def note_rollback(undone: int, titles=None, reason: str = "") -> None:
    """记录一次任务级回滚（T-20261027-TASK-UNDO）。

    面板显示「已回滚 N 步」的数据源；无任务在跑时静默跳过（回滚不必依附任务）。
    """
    undone = int(undone or 0)
    reason = str(reason or "")[:400]
    if undone <= 0 and not reason:
        return
    try:
        with _lock:
            if _active is None:
                return
            _active.setdefault("rollbacks", []).append({
                "ts": _now(),
                "undone": undone,
                "titles": [str(t)[:160] for t in (titles or [])][:50],
                "stopped_reason": reason,
            })
            label = ("已回滚 %d 步" % undone) if undone else "回滚未执行"
            if reason:
                label += "（%s）" % reason[:120]
            _active.setdefault("notes", []).append({"ts": _now(), "note": label})
    except Exception:
        pass


def active_task_id() -> str:
    """当前进行中任务的 id（无任务 → ""）。供快照目录命名。"""
    with _lock:
        return str((_active or {}).get("task_id") or "")


def note_created_asset(path: str) -> None:
    """记录本任务新建的资产（rollback_task_assets 用它做删除）。"""
    path = str(path or "").strip()
    if not path.startswith("/Game/"):
        return
    try:
        with _lock:
            if _active is None:
                return
            bucket = _active.setdefault("created_assets", [])
            if path not in bucket:
                bucket.append(path)
    except Exception:
        pass


def note_deleted_asset(path: str, snapshot: dict = None) -> None:
    """记录本任务删除的资产 + 删除前快照。"""
    path = str(path or "").strip()
    if not path.startswith("/Game/"):
        return
    try:
        with _lock:
            if _active is None:
                return
            bucket = _active.setdefault("deleted_assets", [])
            if path not in bucket:
                bucket.append(path)
            if isinstance(snapshot, dict) and snapshot.get("snapshot"):
                _active.setdefault("asset_snapshots", []).append({
                    "asset": str(snapshot.get("asset") or path),
                    "snapshot": str(snapshot["snapshot"]),
                    "bytes": int(snapshot.get("bytes") or 0),
                    "ts": _now(),
                })
    except Exception:
        pass


def note_receipt(receipt: dict) -> None:
    """记录一条变更回执（上限 100；无任务时静默跳过）。"""
    if not isinstance(receipt, dict):
        return
    try:
        with _lock:
            if _active is None:
                return
            bucket = _active.setdefault("receipts", [])
            bucket.append(receipt)
            del bucket[:-100]
    except Exception:
        pass


def task_asset_ledger(task_id: str = "") -> dict:
    """读任务资产账本（created/deleted/snapshots）；无此任务 → 抛 ValueError。"""
    task_id = str(task_id or "").strip()
    with _lock:
        candidates = []
        if _active is not None:
            candidates.append(_active)
        candidates.extend(_history)
        for t in candidates:
            if task_id and t.get("task_id") != task_id:
                continue
            return {
                "task_id": t.get("task_id"),
                "status": t.get("status"),
                "goal": t.get("goal"),
                "created_assets": list(t.get("created_assets") or []),
                "deleted_assets": list(t.get("deleted_assets") or []),
                "asset_snapshots": list(t.get("asset_snapshots") or []),
                "touched_assets": (
                    list(t.get("created_assets") or [])
                    + [p for p in (t.get("deleted_assets") or [])
                       if p not in (t.get("created_assets") or [])]
                ),
            }
    raise ValueError("找不到任务：%s（先 task_begin / task_list）" % (task_id or "<当前>"))


def note_tool_use(tool: str, ok: bool = True, latency_ms: float = 0.0) -> None:
    """中间件挂点（fail-open）：把关键工具调用自动记到当前任务。

    解决两个用户体验痛点（T-20261025-UX）：
      ① **自动进度**：agent 不必为每一步手动 task_step —— 编译/写入/截图等
         关键动作自动落到当前 running 步骤的过程日志里（auto=True，面板可辨识）。
      ② **证据计数**：success 声明需要证据（compile/apply/shot 计数），
         task_end 时校验（防「自述成功、实则未验证」）。
    任何异常吞掉（观测面纪律，同 record_event）。
    """
    try:
        with _lock:
            if _active is None:
                return
            tool = str(tool or "")
            ev = _active.setdefault("evidence_counts", {})
            kind = EVIDENCE_TOOLS.get(tool)
            if kind and ok:
                ev[kind] = ev.get(kind, 0) + 1
            label = _TOOL_LABELS.get(tool)
            if not label:
                return
            steps = _active.get("steps", [])
            # 先按计划步骤的 hint 归位（T-20261025-UX②）：hint 与命令名互相包含
            # 即视为该步骤的执行动作 —— agent 不手动推进时，命令也能落到正确步骤。
            short = tool.lower().replace("ue5_", "")
            step = None
            for x in steps:
                hint = str(x.get("hint") or "").lower()
                if hint and (hint in short or short in hint):
                    step = x
                    break
            if step is None:
                step = next((x for x in steps if x.get("status") == "running"), None)
            if step is None and steps and label:
                # 自动起步（T-20261025-UX）：agent 完全没上报过进度时，
                # 把第一个 pending 步骤推进为 running —— 让「只上报 begin/end」
                # 的 agent 也能在面板上看到进度，而不是永远全 pending。
                if not any(x.get("status") != "pending" for x in steps):
                    step = steps[0]
                    step["status"] = "running"
                    step["auto_started"] = True
            target = step if step is not None else None
            entry = {"ts": _now(), "status": "auto",
                     "note": "%s%s（%.0fms）" % (label, "✓" if ok else "失败",
                                                float(latency_ms or 0)),
                     "auto": True}
            if target is not None:
                log = target.setdefault("log", [])
                log.append(entry)
                del log[:-20]
    except Exception:
        pass  # fail-open：观测面永不阻塞命令主路径


#: 命令解释表（T-20261025-UX）：命令 → (人话, UE 内实际动作)。
#: 面板每条命令流水都会带上这两句 —— 用户不需要懂 API 也能看懂在干什么。
COMMAND_EXPLAIN = {
    "ue5_task_begin": ("声明任务：回显需求要点与计划", "仅记账，不触达编辑器"),
    "ue5_task_step": ("上报进度/结论", "仅记账"),
    "ue5_task_end": ("收尾复盘", "仅记账"),
    "ue5_build_blueprint": ("新建蓝图资产（含变量/事件/函数）",
                            "创建 Blueprint 资产并写入变量、事件与函数图节点，随后编译"),
    "ue5_search_blueprint_nodes": ("搜索可用函数/事件/变量及引脚签名",
                                   "只读遍历图内节点、蓝图函数、变量与引擎函数目录"),
    "ue5_apply_blueprint_graph": ("整图写入（一次事务）",
                                  "在一个事务内建节点+连线+设默认值，读回校验，失败整体回滚"),
    "ue5_compile_and_diagnose": ("编译并提取结构化错误",
                                 "触发蓝图编译，读取编辑器日志并解析为资产/节点级错误"),
    "ue5_compile_blueprint": ("编译蓝图", "触发蓝图编译"),
    "ue5_disconnect_pin": ("断开某引脚上的连线", "定位节点引脚并断开连接，回读确认已断开"),
    "ue5_connect_pins": ("连接两个引脚（三重验证）", "建线后做引脚级与拓扑级回读确认"),
    "ue5_delete_node": ("删除节点", "从图中移除节点并检查引用"),
    "ue5_screenshot": ("截图（agent 的眼睛）",
                       "按机位拍编辑器视口/游戏画面并落盘，返回尺寸与机位溯源"),
    "ue5_read_blueprint": ("读取蓝图全量信息", "只读遍历资产"),
    "ue5_execute_command": ("透传一条白名单命令", "按命令名执行对应编辑器操作"),
    "ue5_read_logs": ("读取编辑器日志", "只读日志文件"),
    "ue5_task_list": ("查看任务日志", "仅记账"),
}

#: 参数摘要白名单（只留关键字段，避免把整份 graph_spec 塞进面板）
_PARAM_KEYS = ("bp_path", "blueprint_path", "asset_path", "graph_name", "query",
               "step_id", "status", "camera_mode", "source", "actor_names",
               "actor_name", "replace", "annotate", "max_dimension", "command")


def _summarize_params(name: str, args: dict) -> dict:
    """参数摘要（白名单 + 计数 + 路径取末段，脱敏且不刷屏）。"""
    out = {}
    if not isinstance(args, dict):
        return out
    for k in _PARAM_KEYS:
        v = args.get(k)
        if v in (None, "", [], {}):
            continue
        if k.endswith("_path") or k == "asset_path":
            out[k] = str(v).rsplit("/", 1)[-1]
        else:
            out[k] = v if not isinstance(v, (list, dict)) else str(v)[:60]
    spec = args.get("graph_spec")
    if isinstance(spec, dict):
        out["nodes"] = len(spec.get("nodes") or [])
        out["edges"] = len(spec.get("edges") or [])
        out["graph"] = spec.get("graph") or "EventGraph"
    conns = args.get("connections")
    if isinstance(conns, list):
        out["connections"] = len(conns)
    names = args.get("actor_names")
    if isinstance(names, list):
        out["actors"] = len(names)
    cam = args.get("camera")
    if isinstance(cam, dict):
        out["camera"] = "pitch%s/yaw%s" % (cam.get("pitch"), cam.get("yaw"))
    return out


def _summarize_result(result) -> dict:
    """结果摘要（只挑可验证的关键字段）。"""
    if not isinstance(result, dict):
        return {}
    keys = ("success", "verified", "compiled", "compile_status", "nodes_applied",
            "edges_applied", "visible_count", "width", "height", "bytes_on_disk",
            "annotated", "source", "count", "total_matches", "rollback")
    out = {k: result[k] for k in keys if k in result}
    # 单条失败原因（守卫拒绝/参数错误等走 error；与 errors 数组不同 ——
    # 面板命令流水必须显示它，否则「verified=false 但 errors=[]」是判读黑洞）
    if result.get("error"):
        out["error"] = str(result["error"])[:160]
    errs = result.get("errors")
    if isinstance(errs, list) and errs:
        out["errors"] = len(errs)
        first = errs[0]
        if isinstance(first, dict) and first.get("message"):
            out["first_error"] = str(first["message"])[:80]
    data = result.get("data")
    if isinstance(data, dict):
        for k in ("steps", "task", "is_playing", "count", "total_scanned"):
            if k in data:
                out[k] = data[k] if not isinstance(data[k], list) else len(data[k])
    return out


def note_command(tool: str, args, result, ok: bool = True,
                 latency_ms: float = 0.0, explain_key: str = None) -> None:
    """中间件/MCP 端点挂点（fail-open）：把「下发给工具的命令」记到当前任务。

    这是用户视角最重要的一层（T-20261025-UX）：**看得见下发了什么命令、
    参数是什么、UE 返回什么、以及在 UE 里实际做了什么**。落在当前进行中
    步骤的 `commands` 列表（上限 30）；没有步骤时落任务级 `commands`。
    """
    try:
        with _lock:
            if _active is None:
                return
            # 元命令不入流水（task_begin/step/end/list 只是记账，不操作 UE）
            if str(tool or "").startswith("ue5_task_"):
                return
            explain = COMMAND_EXPLAIN.get(explain_key or tool) or ("", "")
            entry = {
                "ts": _now(), "tool": tool, "ok": bool(ok),
                "ms": round(float(latency_ms or 0), 0),
                "params": _summarize_params(tool, args),
                "result": _summarize_result(result),
                # 交付物推断用（完整路径，面板显示时取末段）
                "path": str(args.get("bp_path") or args.get("blueprint_path")
                            or args.get("asset_path") or ""),
                "shot_path": str((result or {}).get("saved_path") or ""),
                "human": explain[0], "ue_action": explain[1],
            }
            steps = _active.get("steps", [])
            # ① 按计划步骤的 hint 归位：hint 与命令名互相包含即视为该步骤的动作
            #    （agent 不手动推进时，命令也能落到"理论上应该做这件事"的步骤）
            short = tool.lower().replace("ue5_", "")
            step = None
            for _x in steps:
                _hint = str(_x.get("hint") or "").lower()
                if _hint and (_hint in short or short in _hint):
                    step = _x
                    break
            # ② 兜底：当前进行中的步骤
            if step is None:
                step = next((x for x in steps if x.get("status") == "running"), None)
            # ③ 再兜底：agent 完全没上报过进度 → 自动起步第一步
            if step is None and steps and not any(
                    x.get("status") != "pending" for x in steps):
                step = steps[0]
                step["status"] = "running"
                step["auto_started"] = True
            bucket = step if step is not None else _active
            cmds = bucket.setdefault("commands", [])
            cmds.append(entry)
            del cmds[:-30]
    except Exception:
        pass  # fail-open


#: 会产生交付物的命令 → 默认动作说明
_DELIVERABLE_TOOLS = {
    "ue5_build_blueprint": ("新建蓝图资产", "created"),
    "ue5_apply_blueprint_graph": ("写入/替换蓝图逻辑图", "modified"),
    "ue5_create_node_by_class": ("新增节点", "modified"),
    "ue5_compile_and_diagnose": ("编译验证", "verified"),
    "ue5_screenshot": ("截图证据", "evidence"),
}


def _infer_deliverables(task: dict) -> list:
    """从命令流水自动推断交付物（agent 未填 deliverables 时的兜底）。

    产出 [{kind, path, action, note}]：资产按路径去重、截图按文件列出、
    编译结果作为验证手段。绝不编造 —— 只汇总真实发生过的调用。
    """
    items, seen = [], set()
    # 命令可能挂在任务级或步骤级，统一收集
    buckets = list(task.get("commands") or [])
    for st in (task.get("steps") or []):
        buckets.extend(st.get("commands") or [])
    for c in buckets:
        tool = str(c.get("tool") or "")
        meta = _DELIVERABLE_TOOLS.get(tool)
        if not meta:
            continue
        label, action = meta
        path = str(c.get("path") or "")
        shot = str(c.get("shot_path") or "")
        if shot:
            key = ("shot", shot)
            if key in seen:
                continue
            seen.add(key)
            items.append({"kind": "截图", "path": shot, "action": "evidence",
                          "note": label, "ts": c.get("ts")})
        elif path:
            key = ("asset", path)
            if key in seen:
                continue
            seen.add(key)
            res = c.get("result") or {}
            note = label
            if res.get("verified") is True:
                note += "（读回验证通过）"
            if res.get("compiled") is True:
                note += "（编译通过）"
            items.append({"kind": "蓝图资产", "path": path, "action": action,
                          "note": note, "ts": c.get("ts")})
    return items


def task_touched_assets() -> list:
    """本任务触碰过的资产路径（命令流水的 path + deliverables 的资产段）。

    T-20261027-SAVE：task_end 前用它做「保存闭环检查」——改了却没存，
    关编辑器就白干（真机踩过：BP_Door 重启后消失，工具一声不吭）。
    """
    try:
        with _lock:
            if _active is None:
                return []
            seen, out = set(), []

            def _add(p):
                p = str(p or "").strip().split(" :: ")[0]
                if p.startswith("/Game/") and p not in seen:
                    seen.add(p)
                    out.append(p)

            for c in (_active.get("commands") or []):
                _add(c.get("path"))
            for st in (_active.get("steps") or []):
                for c in (st.get("commands") or []):
                    _add(c.get("path"))
            for d in (_active.get("deliverables") or []):
                _add(d.get("path"))
            return out[:40]
    except Exception:
        return []


def _close_hanging_steps(task: dict, result_status: str) -> int:
    """收尾封口：把仍为 running/pending 的步骤按结果落定（返回封口数）。"""
    closed = 0
    for s in task.get("steps") or []:
        if s.get("status") in ("running", "pending"):
            s["status"] = "done" if result_status == "success" else "skipped"
            s["auto_closed"] = True
            closed += 1
    if closed:
        task["auto_closed_steps"] = closed
    return closed


def _normalize_finished(task: dict) -> dict:
    """惰性迁移（T-20261025-UX-FIX）：旧的已结束任务补做封口。

    背景：v3.9.2 之前 task_end 不做封口，导致「状态成功但步骤 0/N」的
    悬挂数据；读取时统一修正（幂等），避免面板显示自相矛盾的进度。
    """
    try:
        if isinstance(task, dict) and task.get("status") == "finished":
            _close_hanging_steps(task, str(task.get("result_status") or ""))
    except Exception:
        pass
    return task


def task_begin(params: dict) -> dict:
    """开始一个任务：goal 必填；understanding/approach/steps 建议带。"""
    global _active, _seq
    if not isinstance(params, dict):
        raise ValueError("params 必须是对象")
    with _lock:
        if _active is not None:
            raise ValueError(
                f"已有任务在跑（{_active['task_id']}：{_active['goal'][:40]}）—— "
                f"先 task_end 再开新任务（串行纪律，账不能并行记）")
        _seq += 1
        goal = _req_str(params, "goal", 300, required=True)
        steps = _norm_steps(params.get("steps"))
        # 唯一性守卫（v6.0.1）：历史里已有同 ID（旧版本同日重启重复分配）→ 顺延
        used_ids = {t.get("task_id") for t in _history}
        if _active:
            used_ids.add(_active.get("task_id"))
        task_id = "%s-%02d" % (_day_stamp(), _seq)
        while task_id in used_ids:
            _seq += 1
            task_id = "%s-%02d" % (_day_stamp(), _seq)
        _active = {
            "task_id": task_id,
            "goal": goal,
            "asset": _req_str(params, "asset", 300) if params.get("asset") else "",
            "user_request": _req_str(params, "user_request", 1500)
            if params.get("user_request") else "",
            "understanding": _req_str(params, "understanding", 2000)
            if params.get("understanding") else "",
            "approach": _req_str(params, "approach", 2000)
            if params.get("approach") else "",
            "steps": steps,
            "status": "running",
            "started_at": _now(),
            "ended_at": "",
            "notes": [],       # 自由进度注记 [{ts, note}]
            "rollbacks": [],   # 任务级回滚记录 [{ts, undone, titles, stopped_reason}]
            # v5.0 资产账本（任务级资产回滚的数据源）
            "created_assets": [],   # 本任务新建的资产路径
            "deleted_assets": [],   # 本任务删除的资产路径
            "asset_snapshots": [],  # 删除前快照 [{asset, snapshot, bytes, ts}]
            "receipts": [],         # 变更回执 [{id, ts, command, asset, before, after, ...}]
            "summary": "",
            "lessons": "",
            "result_status": "",
        }
        _persist({"kind": "begin", "task": dict(_active)})
        return {"task": dict(_active)}


def _get_active_locked(task_id: str):
    if _active is None:
        raise ValueError("没有在跑的任务 —— 先 task_begin")
    if task_id and task_id != _active["task_id"]:
        raise ValueError(f"task_id 不匹配：当前是 {_active['task_id']}，收到 {task_id}")
    return _active


def task_step(params: dict) -> dict:
    """上报进度：step_id（有计划时）标记步骤状态；无计划时自由注记 note。

    同一 step_id 的多次上报会**追加**到 step["log"]（中间过程时间线，
    上限 20 条）—— 执行脉络面板把逻辑步骤的展开过程逐条展示给用户。
    """
    if not isinstance(params, dict):
        raise ValueError("params 必须是对象")
    with _lock:
        task = _get_active_locked(str(params.get("task_id") or ""))
        step_id = params.get("step_id")
        note = str(params.get("note") or "").strip()[:1000]
        evidence = params.get("evidence") if isinstance(params.get("evidence"), dict) else {}
        evidence = {k: evidence[k] for k in list(evidence)[:8]}  # 截断防灌爆

        if step_id is not None:
            try:
                step_id = int(step_id)
            except (TypeError, ValueError):
                raise ValueError("step_id 必须是整数")
            step = next((s for s in task["steps"] if s["id"] == step_id), None)
            if step is None:
                raise ValueError(
                    f"step_id={step_id} 不在计划里（合法: "
                    f"{[s['id'] for s in task['steps']]}）—— 步骤要按计划定位")
            status = str(params.get("status") or "running").strip().lower()
            if status not in ("running", "done", "failed", "skipped"):
                raise ValueError('status 可选 "running" | "done" | "failed" | "skipped"')
            step["status"] = status
            if note:
                step["note"] = note          # 摘要 = 最新一条
            if evidence:
                step["evidence"] = evidence
            log = step.setdefault("log", [])
            log.append({"ts": _now(), "status": status,
                        "note": note or f"（{status}）"})
            del log[:-20]
            out = {"task_id": task["task_id"], "step_id": step_id,
                   "step_status": status}
        else:
            if not note:
                raise ValueError("task_step 需要 step_id（按计划定位）或 note（自由注记）")
            task["notes"].append({"ts": _now(), "note": note})
            del task["notes"][:-50]  # 注记上限 50 条
            out = {"task_id": task["task_id"], "noted": True}

        _persist({"kind": "step", "task_id": task["task_id"], "params": {
            "step_id": step_id, "note": note, "evidence": evidence,
            "status": str(params.get("status") or "")}})
        out["task"] = dict(task)
        return out


def task_end(params: dict) -> dict:
    """收尾复盘：status 必填；summary/lessons 建议带；耗时/重复自动计算。"""
    if not isinstance(params, dict):
        raise ValueError("params 必须是对象")
    global _active, _history
    with _lock:
        task = _get_active_locked(str(params.get("task_id") or ""))
        status = str(params.get("status") or "").strip().lower()
        if status not in ("success", "failed", "aborted"):
            raise ValueError('status 可选 "success" | "failed" | "aborted"')
        task["status"] = "finished"
        task["result_status"] = status
        task["summary"] = str(params.get("summary") or "").strip()[:2000]
        task["lessons"] = str(params.get("lessons") or "").strip()[:2000]
        task["ended_at"] = _now()
        # 收尾封口（T-20261025-UX）：任务已结束，未上报完成度的步骤不该永远挂在
        # running —— 自动封口（auto_closed，面板可辨识），绝不留悬挂状态。
        _close_hanging_steps(task, status)
        # T-20261025-TRUST：success 声明需证据 —— 全程没有编译/写入/截图记录时
        # 打 unverified 标记并附 warnings（面板显示「未验证」徽章；绝不阻止收尾，
        # 但让人一眼看出「这是 agent 自述」）
        counts = task.get("evidence_counts") or {}
        evidence_total = sum(int(v or 0) for v in counts.values())
        task["evidence_total"] = evidence_total
        end_warnings = []
        if status == "success" and evidence_total == 0:
            task["unverified"] = True
            end_warnings.append(
                "success 未检测到验证证据（本轮无编译诊断/整图写入/截图记录）——"
                "结论仅为 agent 自述，请人工复核")
        elif status == "success" and not (counts.get("compile") or counts.get("apply")):
            end_warnings.append(
                "success 缺少编译/写入证据（仅有截图）——建议补一次 compile_and_diagnose")
        try:
            t0 = datetime.fromisoformat(task["started_at"])
            t1 = datetime.fromisoformat(task["ended_at"])
            task["duration_s"] = round((t1 - t0).total_seconds(), 1)
        except Exception:
            task["duration_s"] = None
        # 交付物清单（T-20261026-DELIVERABLE）：agent 填优先，否则从命令流水推断
        raw_items = params.get("deliverables")
        items = []
        if isinstance(raw_items, list):
            for it in raw_items[:30]:
                if not isinstance(it, dict):
                    continue
                path = str(it.get("path") or "").strip()
                if not path:
                    continue
                items.append({
                    "kind": str(it.get("kind") or "文件")[:20],
                    "path": path[:400],
                    "action": str(it.get("action") or "modified")[:20],
                    "note": str(it.get("note") or "")[:300],
                })
        if not items:
            items = _infer_deliverables(task)
            task["deliverables_inferred"] = True
        task["deliverables"] = items
        # 保存闭环结果（由桥侧 GT 保存后回填；缺省不出现该字段）
        if isinstance(params.get("_auto_save"), dict):
            task["auto_save"] = params["_auto_save"]
        # how_to_verify 允许字符串或步骤数组（数组按行拼接，面板渲染为有序列表）
        _hv = params.get("how_to_verify")
        if isinstance(_hv, (list, tuple)):
            _hv = chr(10).join(str(x).strip() for x in _hv if str(x).strip())
        task["how_to_verify"] = str(_hv or "").strip()[:1500]
        # 复盘统计：failed 步骤数（返工信号）；failed 步骤名列表给面板直接展示
        failed_steps = [s["name"] for s in task["steps"] if s.get("status") == "failed"]
        task["failed_steps"] = failed_steps
        _history.insert(0, task)
        del _history[50:]
        _active = None
        _persist({"kind": "end", "task": dict(task)})
        out = {"task": dict(task)}
        if end_warnings:
            out["warnings"] = end_warnings
        return out


def task_list(params: dict = None) -> dict:
    """全量任务（active + history；任务量小，不做增量游标）。

    读取时对已结束任务做惰性封口（_normalize_finished，幂等）——兼容
    v3.9.2 之前产生的「成功但步骤悬挂」历史数据。
    """
    with _lock:
        active = dict(_active) if _active else None
        history = [_normalize_finished(dict(t)) for t in _history]
        return {"active": active, "history": history}


def task_error_nudge(params: dict = None) -> dict:
    """把一条错误注记挂到当前任务（agent 撞错时顺手记经验素材）。"""
    if not isinstance(params, dict):
        raise ValueError("params 必须是对象")
    with _lock:
        task = _get_active_locked(str(params.get("task_id") or ""))
        note = _req_str(params, "note", 500, required=True)
        task["notes"].append({"ts": _now(), "note": "⚠️ " + note,
                              "error": True})
        del task["notes"][:-50]
        _persist({"kind": "error_note", "task_id": task["task_id"], "note": note})
        return {"task_id": task["task_id"], "noted": True}


def reset_for_tests() -> None:
    """测试辅助（生产代码不调用）：清空内存态。"""
    global _active, _history, _seq
    with _lock:
        _active = None
        _history = []
        _seq = 0


def _replay_from_disk() -> None:
    """启动回放：从当日 JSONL 恢复任务板（编辑器重启不丢当日进度）。

    逐条重放 begin/step/end 生命周期记录（step/end 按 task_id 落到对应
    任务）；损坏行跳过（fail-open）。在 import 末尾调用一次。
    """
    global _active, _history, _seq
    try:
        # 跨日回放（T-20261026-FIX）：只读当天会让"昨天 23 点的任务今天打开就没了"。
        # 回放最近 3 天的会话文件（按文件名=日期排序），历史按时间新→旧保留。
        paths = []
        try:
            names = sorted(f for f in os.listdir(_dir) if f.endswith(".jsonl"))
            paths = [os.path.join(_dir, n) for n in names[-3:]]
        except OSError:
            paths = []
        if not paths:
            path = _file_path()
            paths = [path] if os.path.isfile(path) else []
        lines_all = []
        for path in paths:
            if not os.path.isfile(path):
                continue
            with open(path, encoding="utf-8") as f:
                lines_all.extend(f.readlines())
        for line in lines_all:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            task = rec.get("task") or {}
            tid = task.get("task_id") or rec.get("task_id") or ""
            try:
                seq_no = int(tid.rsplit("-", 1)[-1])
            except (ValueError, AttributeError):
                continue
            _seq = max(_seq, seq_no)
            kind = rec.get("kind")
            if kind == "begin":
                _active = dict(task)
            elif kind == "step" and _active and _active["task_id"] == tid:
                params = rec.get("params") or {}
                sid = params.get("step_id")
                step = next((s for s in _active.get("steps", [])
                             if s["id"] == sid), None)
                if step is not None:
                    if params.get("status"):
                        step["status"] = params["status"]
                    if params.get("note"):
                        step["note"] = params["note"]
                    if params.get("evidence"):
                        step["evidence"] = params["evidence"]
                elif params.get("note"):
                    _active.setdefault("notes", []).append(
                        {"ts": _now(), "note": params["note"]})
            elif kind == "end" and task:
                _active = None
                _history.insert(0, _normalize_finished(dict(task)))
        # v6.0.2：回放去重 + 只保留最近 50 条（面板/内存不被历史灌爆）。
        # 旧版本同日重启会重写 begin，文件里可能存在同 (id, started_at) 的重复。
        _seen, _dedup = set(), []
        for _t in _history:
            _k = (_t.get("task_id"), _t.get("started_at"))
            if _k in _seen:
                continue
            _seen.add(_k)
            _dedup.append(_t)
        _history = _dedup[:50]

        # 结束态修正：文件里最后的 active 若没有对应 end（编辑器中途被杀）
        # → 标 aborted 落历史（绝不留给用户一个永久"进行中"）
        if _active is not None:
            _active["status"] = "finished"
            _active["result_status"] = "aborted"
            _active["ended_at"] = _now()
            _active["summary"] = "（bridge 重启，任务被中断收尾）"
            _close_hanging_steps(_active, "aborted")
            _history.insert(0, _active)
            _active = None
    except Exception as e:  # noqa: BLE001 —— 回放失败从空白板开始（fail-open）
        print(f"[task_journal] [WARN] 启动回放失败（从空白板开始）: {e}")


_replay_from_disk()
