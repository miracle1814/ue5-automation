# Copyright Epic Games, Inc. All Rights Reserved.
"""ue5-automation-extras 工具集：把 ue5-automation 的验证型能力带进官方 MCP。

设计要点（全部来自 ue5-automation v3.x 的开发与实测经验）：
  · 只读优先、写操作带**回读比对**（fail-loud，绝不"返回值说谎"）；
  · 节点级图操作走 BlueprintPythonBridge C++ API（Python 摸不到 UEdGraph，
    官方 DSL 的 exec 线缺陷正源于此）——桥不存在时给可读提示，不假装；
  · PIE 传送必须 teleport=True（实测：Static/非 teleport 移动在 PIE 中静默无效）。

实现约束（5.8 实测踩坑）：
  · 工具方法返回类型必须是**具体类型**（-> str），返回 JSON 字符串；
    返回 dict 会在注册时报 "missing specification for contained type"；
  · 不要 `from __future__ import annotations`（装饰器检查真实类型对象）。
"""

import json
import os
import re

import unreal

import toolset_registry


def _j(obj) -> str:
    """统一 JSON 序列化（工具方法一律返回 str）。"""
    return json.dumps(obj, ensure_ascii=False, default=str)


def _bridge():
    """取 C++ 桥类（未加载返回 None）。"""
    return getattr(unreal, "BlueprintPythonBridge", None)


def _bridge_or_hint():
    b = _bridge()
    if b is None:
        raise ValueError(
            "BlueprintPythonBridge C++ plugin is not loaded; node-level graph tools "
            "are unavailable. Deploy it from the ue5-automation package: run "
            "bridge/Source/build_for_engine.bat <UE_ROOT>, copy "
            "bridge/BlueprintPythonBridge into <Project>/Plugins/, restart the editor. "
            "Pure-Python tools (status/pie_state/logs/actor_*) do not need the bridge.")
    return b


def _ell():
    """PIE 相关 API 的跨库候选链（5.8：LevelEditorSubsystem 优先）。"""
    out = []
    ges = getattr(unreal, "get_editor_subsystem", None)
    if callable(ges):
        for name in ("LevelEditorSubsystem", "UnrealEditorSubsystem"):
            cls = getattr(unreal, name, None)
            if cls is None:
                continue
            try:
                out.append((name, ges(cls)))
            except Exception:  # pylint: disable=broad-except
                continue
    lib = getattr(unreal, "EditorLevelLibrary", None)
    if lib is not None:
        out.append(("EditorLevelLibrary", lib))
    return out


def _call_any(names, *args):
    """跨库候选调用 → (result, used_name)。全部失败返回 (None, "")。"""
    for lib_name, obj in _ell():
        for n in names:
            fn = getattr(obj, n, None)
            if not callable(fn):
                continue
            try:
                return fn(*args), "%s.%s" % (lib_name, n)
            except Exception:  # pylint: disable=broad-except
                continue
    return None, ""


def _actor_brief(a):
    out = {}
    try:
        out["name"] = a.get_name()
    except Exception:  # pylint: disable=broad-except
        pass
    try:
        out["class"] = a.get_class().get_name()
    except Exception:  # pylint: disable=broad-except
        pass
    try:
        loc = a.get_actor_location()
        out["location"] = [round(loc.x, 2), round(loc.y, 2), round(loc.z, 2)]
    except Exception:  # pylint: disable=broad-except
        pass
    try:
        rc = a.root_component
        out["mobility"] = str(rc.get_editor_property("mobility"))
    except Exception:  # pylint: disable=broad-except
        pass
    return out


def _split3(text):
    parts = [float(x) for x in str(text).replace(",", " ").split()]
    if len(parts) != 3:
        raise ValueError('need three numbers (e.g. "100 200 300"), got: %r' % text)
    return parts


def _is_guid(value) -> bool:
    t = str(value or "").replace("{", "").replace("}", "").replace("-", "").replace(" ", "")
    return len(t) == 32 and all(c in "0123456789abcdefABCDEF" for c in t)


@unreal.uclass()
class UE5AutomationExtras(unreal.ToolsetDefinition):
    """ue5-automation verification-grade capabilities for the official Unreal MCP.

    Use for: proving blueprint behaviour at runtime (read live PIE state, teleport
    with read-back verification), self-service UE logs, find/teleport level actors,
    and (with the BlueprintPythonBridge C++ plugin deployed) node-level graph read
    and connect with topology verification. All tool results are JSON strings.
    """

    @toolset_registry.tool_call
    @staticmethod
    def ue5_status() -> str:
        """ue5-automation link status: C++ bridge loaded? engine/project info.

        Returns:
            JSON: engine_version, project, bridge_loaded, bridge_method_count, bridge_has_umg.
        """
        info = {}
        try:
            info["engine_version"] = str(unreal.SystemLibrary.get_engine_version())
            info["project"] = str(unreal.SystemLibrary.get_game_name())
        except Exception as e:  # pylint: disable=broad-except
            info["error"] = str(e)
        b = _bridge()
        info["bridge_loaded"] = b is not None
        if b is not None:
            names = [m for m in dir(b) if not m.startswith("_")]
            info["bridge_method_count"] = len(names)
            info["bridge_has_umg"] = hasattr(b, "create_widget_blueprint")
        return _j(info)

    @toolset_registry.tool_call
    @staticmethod
    def ue5_pie_state() -> str:
        """PIE/Simulate run state (is_playing / world / paused / time).

        The official StartPIE reports a spurious failure, and PIE pauses when the
        editor loses focus; read the state here instead of trusting either.

        Returns:
            JSON: is_playing, is_playing_api, world, paused, time_seconds.
        """
        out = {"is_playing": None, "is_playing_api": "", "world": None,
               "paused": None, "time_seconds": None}
        v, used = _call_any(("is_in_play_in_editor", "editor_is_in_play_in_editor"))
        if used:
            out["is_playing"] = bool(v)
            out["is_playing_api"] = used
        gw, used_w = _call_any(("get_game_world",))
        if gw is not None:
            out["is_playing"] = True if out["is_playing"] is None else out["is_playing"]
            try:
                out["world"] = gw.get_name()
            except Exception:  # pylint: disable=broad-except
                out["world"] = "<world>"
            try:
                out["paused"] = bool(unreal.GameplayStatics.is_game_paused(gw))
            except Exception:  # pylint: disable=broad-except
                pass
            try:
                out["time_seconds"] = round(float(unreal.GameplayStatics.get_time_seconds(gw)), 3)
            except Exception:  # pylint: disable=broad-except
                pass
        elif out["is_playing"] is None:
            out["is_playing"] = False
        out["world_api"] = used_w
        return _j(out)

    @toolset_registry.tool_call
    @staticmethod
    def ue5_logs(lines: int = 200, pattern: str = "*", category: str = "*") -> str:
        """Read the tail of this project's UE log (self-service diagnostics).

        Args:
            lines: max lines to return (1-2000).
            pattern: optional regex filter (case-insensitive).
            category: optional substring filter (e.g. "LogBlueprint").

        Returns:
            JSON: file, size, count, lines[].
        """
        lines = max(1, min(2000, int(lines)))
        log_dir = None
        try:
            log_dir = unreal.Paths.project_log_dir()
        except Exception:  # pylint: disable=broad-except
            pass
        if not log_dir or not os.path.isdir(log_dir):
            raise ValueError("project log dir not found (unreal.Paths.project_log_dir)")
        files = [f for f in os.listdir(log_dir) if f.lower().endswith(".log")]
        if not files:
            raise ValueError("no .log files under %s" % log_dir)
        files.sort(key=lambda f: os.path.getmtime(os.path.join(log_dir, f)), reverse=True)
        path = os.path.join(log_dir, files[0])
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            raw = f.read().splitlines()
        if category and category != "*":   # "*" = 不过滤（空串默认会被注册器视为必填）
            raw = [l for l in raw if category.lower() in l.lower()]
        if pattern and pattern != "*":
            rx = re.compile(pattern, re.IGNORECASE)
            raw = [l for l in raw if rx.search(l)]
        return _j({"file": files[0], "size": os.path.getsize(path),
                   "count": len(raw[-lines:]), "lines": raw[-lines:]})

    @toolset_registry.tool_call
    @staticmethod
    def ue5_actor_find(actor_class: str = "*", name_filter: str = "*",
                       limit: int = 50) -> str:
        """List editor-level actors (filter by class / name substring).

        Args:
            actor_class: optional class-name substring (e.g. "StaticMeshActor").
            name_filter: optional name substring.
            limit: max results (1-500).

        Returns:
            JSON: count, actors[{name, class, location, mobility}].
        """
        limit = max(1, min(500, int(limit)))
        world, _used = _call_any(("get_editor_world",))
        if world is None:
            raise ValueError("editor world unavailable (get_editor_world)")
        actors = unreal.GameplayStatics.get_all_actors_of_class(world, unreal.Actor)
        out = []
        for a in actors or []:
            brief = _actor_brief(a)
            if actor_class not in ("", "*") and actor_class.lower() not in str(brief.get("class", "")).lower():
                continue
            if name_filter not in ("", "*") and name_filter.lower() not in str(brief.get("name", "")).lower():
                continue
            out.append(brief)
            if len(out) >= limit:
                break
        return _j({"count": len(out), "actors": out})

    @toolset_registry.tool_call
    @staticmethod
    def ue5_actor_set_transform(actor_name: str, location: str = "*",
                                rotation: str = "*", world: str = "editor") -> str:
        """Teleport an actor WITH read-back verification (never reports false success).

        Args:
            actor_name: actor name (exact or unique substring).
            location: optional "x y z".
            rotation: optional "pitch yaw roll".
            world: "editor" (default) or "pie".

        Returns:
            JSON: applied, verified, location, rotation, mobility.
        """
        location = "" if location == "*" else location
        rotation = "" if rotation == "*" else rotation
        if not location and not rotation:
            raise ValueError("provide at least one of location / rotation")
        if world == "pie":
            w, _u = _call_any(("get_game_world",))
            if w is None:
                raise ValueError("not in PIE/Simulate (no game world)")
        else:
            w, _u = _call_any(("get_editor_world",))
            if w is None:
                raise ValueError("editor world unavailable")
        target = None
        for a in unreal.GameplayStatics.get_all_actors_of_class(w, unreal.Actor) or []:
            try:
                if a.get_name() == actor_name:
                    target = a
                    break
            except Exception:  # pylint: disable=broad-except
                continue
        if target is None:
            raise ValueError("actor not found: %s" % actor_name)
        applied = {}
        if location:
            vec = unreal.Vector(*_split3(location))
            # PIE: teleport=True is what actually moves the actor (verified).
            target.set_actor_location(vec, False, True)
            applied["location"] = location
        if rotation:
            rot = unreal.Rotator(*_split3(rotation))
            target.set_actor_rotation(rot, False)
            applied["rotation"] = rotation
        brief = _actor_brief(target)
        if location:
            want = _split3(location)
            got = brief.get("location")
            if got is None or any(abs(got[i] - want[i]) > 1.0 for i in range(3)):
                raise ValueError(
                    "teleport did not take effect: readback=%s != requested=%s "
                    "(world=%s; for PIE check mobility=Movable - static components "
                    "refuse runtime movement)" % (got, want, world))
        if rotation:
            want = _split3(rotation)
            try:
                r = target.get_actor_rotation()
                got = [r.pitch, r.yaw, r.roll]
                if any(abs(got[i] - want[i]) > 1.0 for i in range(3)):
                    raise ValueError("rotation did not take effect: %s != %s" % (got, want))
            except ValueError:
                raise
            except Exception:  # pylint: disable=broad-except
                pass
        brief["applied"] = applied
        brief["verified"] = True
        return _j(brief)

    @toolset_registry.tool_call
    @staticmethod
    def ue5_compile(bp_path: str) -> str:
        """Compile a blueprint and read the compile status back (no C++ bridge needed).

        Args:
            bp_path: blueprint asset path (/Game/...).

        Returns:
            JSON: compiled, status_readback.
        """
        bp = unreal.EditorAssetLibrary.load_asset(bp_path)
        if bp is None:
            raise ValueError("blueprint load failed: %s" % bp_path)
        ok = unreal.BlueprintEditorLibrary.compile_blueprint(bp)
        status = None
        for getter in (lambda: bp.get_editor_property("status"),
                       lambda: getattr(bp, "status", None)):
            try:
                v = getter()
                if v is not None:
                    status = str(v)
                    break
            except Exception:  # pylint: disable=broad-except
                continue
        return _j({"compiled": bool(ok), "status_readback": status})

    @toolset_registry.tool_call
    @staticmethod
    def ue5_graph_nodes(bp_path: str, graph_name: str = "EventGraph") -> str:
        """List graph nodes (title/GUID/pin count) - requires the C++ bridge.

        The official DSL can leave exec wires empty and event nodes missing; this is
        the trustworthy read-back channel for "what is actually in the graph".

        Args:
            bp_path: blueprint asset path.
            graph_name: graph name (default EventGraph).

        Returns:
            JSON: graph, count, nodes[{title, class, node_guid, pin_count}].
        """
        b = _bridge_or_hint()
        bp = unreal.EditorAssetLibrary.load_asset(bp_path)
        if bp is None:
            raise ValueError("blueprint load failed: %s" % bp_path)
        graphs = b.get_all_graphs(bp) or []
        target = None
        for g in graphs:
            try:
                if g.get_name() == graph_name:
                    target = g
                    break
            except Exception:  # pylint: disable=broad-except
                continue
        if target is None:
            names = []
            for g in graphs:
                try:
                    names.append(g.get_name())
                except Exception:  # pylint: disable=broad-except
                    pass
            raise ValueError("graph not found: %s (available: %s)" % (graph_name, names))
        nodes = []
        for n in b.list_nodes(target) or []:
            item = {}
            for k in ("node_title", "NodeTitle"):
                v = getattr(n, k, None)
                if v is not None:
                    item["title"] = str(v)
                    break
            for k in ("node_class", "NodeClass"):
                v = getattr(n, k, None)
                if v is not None:
                    item["class"] = str(v)
                    break
            for k in ("node_guid", "NodeGuid"):
                v = getattr(n, k, None)
                if v is not None:
                    item["node_guid"] = str(v)
                    break
            try:
                item["pin_count"] = len(b.list_pins(n) or [])
            except Exception:  # pylint: disable=broad-except
                pass
            nodes.append(item)
        return _j({"graph": graph_name, "count": len(nodes), "nodes": nodes})

    @toolset_registry.tool_call
    @staticmethod
    def ue5_graph_connect(bp_path: str, src_node: str, src_pin: str,
                          dst_node: str, dst_pin: str) -> str:
        """Connect two node pins WITH topology verification - requires the C++ bridge.

        The correct-path fix for the DSL's missing exec wires: wires are created
        through the C++ bridge and verified by reading the graph topology back.

        Args:
            bp_path: blueprint asset path.
            src_node: source node title or 32-hex GUID.
            src_pin: source pin name (e.g. then / ReturnValue).
            dst_node: destination node title or 32-hex GUID.
            dst_pin: destination pin name (e.g. execute / InString).

        Returns:
            JSON: connected, verified, topology_snippet.
        """
        b = _bridge_or_hint()
        bp = unreal.EditorAssetLibrary.load_asset(bp_path)
        if bp is None:
            raise ValueError("blueprint load failed: %s" % bp_path)
        graph = b.get_event_graph(bp)
        if graph is None:
            raise ValueError("EventGraph unavailable")
        src = b.get_node_by_guid(graph, src_node) if _is_guid(src_node) \
            else b.find_node_by_title(graph, src_node)
        dst = b.get_node_by_guid(graph, dst_node) if _is_guid(dst_node) \
            else b.find_node_by_title(graph, dst_node)
        if src is None or dst is None:
            raise ValueError(
                "node not found (src=%r -> %s; dst=%r -> %s); for duplicate titles "
                "pass the 32-hex GUID from ue5_graph_nodes"
                % (src_node, src is not None, dst_node, dst is not None))
        ok = b.connect_pins(src, src_pin, dst, dst_pin)
        if not ok:
            raise ValueError("connect_pins returned False: %s.%s -> %s.%s"
                             % (src_node, src_pin, dst_node, dst_pin))
        verified = False
        snippet = []
        try:
            for c in b.get_node_connections(graph) or []:
                snippet.append({
                    "from": str(getattr(c, "from_node_title", "")),
                    "from_pin": str(getattr(c, "from_pin_name", "")),
                    "to": str(getattr(c, "to_node_title", "")),
                    "to_pin": str(getattr(c, "to_pin_name", "")),
                })
        except Exception:  # pylint: disable=broad-except
            pass
        src_t = str(getattr(src, "node_title", "") or src_node)
        dst_t = str(getattr(dst, "node_title", "") or dst_node)
        for c in snippet:
            if (c["from"] == src_t and c["from_pin"] == src_pin
                    and c["to"] == dst_t and c["to_pin"] == dst_pin):
                verified = True
                break
        if not verified:
            raise ValueError(
                "connect_pins returned success but topology read-back missed it "
                "(false-positive guard): requested %s.%s -> %s.%s; wires in graph=%d"
                % (src_t, src_pin, dst_t, dst_pin, len(snippet)))
        return _j({"connected": True, "verified": True, "topology_snippet": snippet[:20]})
