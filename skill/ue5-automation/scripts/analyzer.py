#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
analyzer.py — 项目架构分析引擎
==============================
全量蓝图遍历 → 指标计算 → 深度分析 → JSON 输出供docs-writer消费。

能力矩阵：
  - 全量蓝图遍历（EditorAssetLibrary.list_assets → 过滤 Blueprint 类型）
  - 指标：蓝图数量/类型分布、TOP 20 大蓝图（节点数排序）、继承深度 > 4
  - 循环依赖检测（DFS 引用图 → 拓扑排序 → 检测环）
  - 耦合度：跨目录引用比例
  - 接口使用率
  - 深挖触发：>200节点 / 循环依赖 / 引用异常 / 接口未实现 → 自动 depth=2 分析
  - 输出 JSON → 供docs-writer模板消费

输入：blueprint_reader.py（提供 BlueprintReadResult 数据源）
输出：analyzer_result.json（结构化分析报告）

用法：
  python analyzer.py
  python analyzer.py --project-root "/Game/MyProject" --depth 2
  python analyzer.py --output analysis_result.json

上下文：blueprint_reader.py

作者：dev（代码开发 Agent）
日期：2026-07-22
"""

import os
import sys
import json
import time
import argparse
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from collections import defaultdict, deque
from typing import Any, Dict, List, Optional, Set, Tuple

from analyzer_models import (
    DEEP_DIVE_NODE_THRESHOLD, DEEP_DIVE_INHERITANCE_DEPTH, DEEP_DIVE_CIRCULAR,
    BLUEPRINT_CLASS_NAMES,
    BlueprintSummary, CircularDependency, CouplingMetrics, DistributionStats,
    DeepDiveDetail, InterfaceUsage, AnalysisResult,
    ComplexityRating, SummaryGrade, NodeTypeStats, GraphComplexity,
    ComplexityScore, DependencyNode, DependencyAnalysis,
    HotPathNode, HotPathAnalysis, DeepAnalysisReport,
)


# ── ProjectAnalyzer ──────────────────────────────────

class ProjectAnalyzer:
    """项目架构分析引擎。

    两种运行模式：
      1. 离线分析 — 传入 blueprint_reader 的数据源（BlueprintReadResult 列表）
      2. UE 环境直连 — 运行时通过 unreal + bridge 扫描

    使用方式：
      analyzer = ProjectAnalyzer()
      analyzer.feed(blueprint_results)          # 喂入 reader 数据
      result = analyzer.analyze(project_root)   # 执行分析
    """

    def __init__(self, unreal_module=None, bridge_module=None):
        self.unreal = unreal_module
        self.bridge = bridge_module

        # 输入数据
        self._blueprint_results: List[Dict[str, Any]] = []

        # 分析中间状态
        self._blueprint_summaries: List[BlueprintSummary] = []
        self._ref_graph: Dict[str, Set[str]] = {}        # bp_path → {referenced_bp_paths}
        self._inheritance_chain: Dict[str, int] = {}      # bp_path → depth
        self._package_index: Dict[str, List[str]] = {}    # package → [bp_paths]

        # 结果
        self.result: Optional[AnalysisResult] = None

    # ── 数据输入 ─────────────────────────────────────

    def feed(self, blueprint_results: List[Dict[str, Any]]):
        """喂入 blueprint_reader 的输出数据，自动适配嵌套格式。

        blueprint_reader.to_dict() 输出为嵌套结构（basic_info / graphs dict /
        related_blueprints），本方法通过 _normalize_reader_output() 将其转为
        内部统一使用的扁平格式（blueprint_path / graphs list / dependent_blueprints 等）。

        Args:
            blueprint_results: BlueprintReader.to_dict() 返回的 dict 列表。
        """
        normalized = [self._normalize_reader_output(d) for d in blueprint_results]
        self._blueprint_results.extend(normalized)

    @staticmethod
    def _normalize_reader_output(bp_data: Dict[str, Any]) -> Dict[str, Any]:
        """将 blueprint_reader.to_dict() 的嵌套输出转为 analyzer 期望的扁平格式。

        reader 输出结构::
            basic_info: {name, asset_path, parent_class, blueprint_type, interfaces, …}
            graphs:     dict[str, GraphAnalysis]    ← key=graph_name
            related_blueprints: list[str]
            variables / referenced_assets / connections / …

        analyzer 期望::
            blueprint_path      ← basic_info.asset_path
            blueprint_name      ← basic_info.name
            blueprint_type      ← basic_info.blueprint_type
            parent_class        ← basic_info.parent_class
            parent_class_path   ← basic_info.parent_class（同源）
            interfaces          ← basic_info.interfaces
            graphs              ← dict.values → list[dict]，每项注入 graph_name
            dependent_blueprints ← related_blueprints
        """
        basic = bp_data.get("basic_info", {})
        # T-20260805-UE-FULL-CLOSURE P0-4：畸形输入容错——basic_info 非 dict（如 null）
        # 时兜底为空 dict，避免 feed 阶段 AttributeError（L3 降级语义）
        if not isinstance(basic, dict):
            basic = {}

        # ── graphs: dict → list ──
        graphs_raw = bp_data.get("graphs", {})
        # T-20260805-UE-FULL-CLOSURE P0-4：graphs 非 dict/list（如 null）时兜底为空 dict
        if not isinstance(graphs_raw, (dict, list)):
            graphs_raw = {}
        if isinstance(graphs_raw, dict):
            graphs_list = []
            for gname, gdata in graphs_raw.items():
                gdata["graph_name"] = gname
                graphs_list.append(gdata)
        else:
            graphs_list = list(graphs_raw)  # 已是列表，兜底拷贝

        return {
            "blueprint_path":       basic.get("asset_path", ""),
            "blueprint_name":       basic.get("name", ""),
            "blueprint_type":       basic.get("blueprint_type", ""),
            "parent_class":         basic.get("parent_class", ""),
            "parent_class_path":    basic.get("parent_class", ""),
            "variables":            bp_data.get("variables", []),
            "graphs":               graphs_list,
            "interfaces":           basic.get("interfaces", []),
            "dependent_blueprints": bp_data.get("related_blueprints", []),
            "referenced_assets":    bp_data.get("referenced_assets", []),
        }

    def feed_from_json(self, json_path: str):
        """从 JSON 文件加载 reader 输出。"""
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            self.feed(data)
        elif isinstance(data, dict) and "blueprints" in data:
            self.feed(data["blueprints"])
        else:
            self.feed([data])

    async def scan_unreal(self, project_root: str = "/Game/") -> List[Dict[str, Any]]:
        """在 UE 运行时扫描全量蓝图（需要 unreal 模块）。

        使用 EditorAssetLibrary.list_assets 遍历，过滤 Blueprint 类型。
        然后通过 blueprint_reader 读取每张蓝图。

        Args:
            project_root: 项目根路径

        Returns:
            blueprint_reader 输出列表
        """
        if not self.unreal:
            return []

        results = []
        asset_registry = self.unreal.AssetRegistryHelpers.get_asset_registry()
        editor_lib = self.unreal.EditorAssetLibrary

        # 列出所有资产
        all_assets = editor_lib.list_assets(project_root, recursive=True)

        bp_assets = []
        for asset_path in all_assets:
            try:
                asset_data = asset_registry.get_asset_by_object_path(asset_path)
                if not asset_data:
                    continue
                asset_class = str(asset_data.asset_class_path.asset_name)
                if any(bc in asset_class for bc in BLUEPRINT_CLASS_NAMES):
                    bp_assets.append(asset_path)
            except Exception:
                continue

        # 使用 blueprint_reader 读取每张蓝图
        # v2.20 修复（P1 · analyze_live 直连路径三重坏）：
        #   ① BlueprintReader.__init__ 签名只有 mode，旧调用传 unreal_module=/
        #      bridge_module= → TypeError；
        #   ② BlueprintReader 无 to_dict 方法（to_dict 属 BlueprintAnalysis）；
        #   ③ self.result 仅在 analyze() 内初始化，scan 失败路径 AttributeError。
        from blueprint_reader import BlueprintReader
        reader = BlueprintReader()
        _scan_errors: List[str] = []

        for bp_path in bp_assets:
            try:
                bp_result = reader.analyze(bp_path)
                results.append(bp_result.to_dict())
            except Exception as e:
                _scan_errors.append(f"扫描失败 {bp_path}: {e}")

        if _scan_errors:
            if getattr(self, "result", None) is None:
                self.result = AnalysisResult(
                    project_root=project_root,
                    analyzed_at=datetime.now(timezone.utc).isoformat(),
                )
            self.result.errors.extend(_scan_errors)

        self.feed(results)
        return results

    # ── 主分析入口 ────────────────────────────────────

    def analyze(self, project_root: str = "/Game/",
                depth: int = 1) -> AnalysisResult:
        """执行全量分析。

        Args:
            project_root: 项目根路径
            depth: 分析深度
                   1 = 基础指标 + TOP N + 分布
                   2 = + 循环依赖 + 耦合度 + 接口 + 深挖

        Returns:
            AnalysisResult
        """
        t0 = time.time()
        self.result = AnalysisResult(
            project_root=project_root,
            analyzed_at=datetime.now(timezone.utc).isoformat(),
        )

        # ── Phase 1: 构建摘要 ──
        self._build_summaries()

        # ── Phase 2: 基础指标 ──
        self._compute_distribution()
        self._compute_top_largest(20)
        self._compute_inheritance_depth()

        self.result.total_blueprints = len(self._blueprint_summaries)
        self.result.total_nodes = sum(s.node_count for s in self._blueprint_summaries)
        self.result.total_connections = sum(s.connection_count
                                            for s in self._blueprint_summaries)

        # ── Phase 3: 深度分析 ──
        if depth >= 2:
            self._detect_circular_dependencies()
            self._compute_coupling()
            self._compute_interface_usage()
            self._run_deep_dives()

        self.result.total_elapsed_ms = (time.time() - t0) * 1000
        return self.result


    # ════════════════ Phase 1: 构建摘要 ═══════════════

    def _build_summaries(self):
        """从 blueprint_reader 输出构建 BlueprintSummary 列表。"""
        self._blueprint_summaries.clear()
        self._ref_graph.clear()

        for bp_data in self._blueprint_results:
            try:
                summary = self._extract_summary(bp_data)
                self._blueprint_summaries.append(summary)

                # 构建引用图
                bp_path = summary.path
                refs = self._extract_references(bp_data)
                self._ref_graph[bp_path] = refs

                # 包索引
                pkg = summary.package_path
                if pkg not in self._package_index:
                    self._package_index[pkg] = []
                self._package_index[pkg].append(bp_path)

                # 继承链
                self._inheritance_chain[bp_path] = summary.inheritance_depth

            except Exception as e:
                self.result.warnings.append(
                    f"构建摘要失败 {bp_data.get('blueprint_path', 'unknown')}: {e}"
                )

    def _extract_summary(self, bp_data: Dict[str, Any]) -> BlueprintSummary:
        """从 reader 输出中提取摘要。"""
        graphs = bp_data.get("graphs", [])
        total_nodes = sum(len(g.get("nodes", [])) for g in graphs)
        total_connections = sum(len(g.get("connections", [])) for g in graphs)
        graph_count = len(graphs)
        variable_count = len(bp_data.get("variables", []))
        interface_count = len(bp_data.get("interfaces", []))

        bp_type = self._classify_blueprint_type(
            bp_data.get("blueprint_type", ""),
            bp_data.get("parent_class", "")
        )

        return BlueprintSummary(
            path=bp_data.get("blueprint_path", ""),
            name=bp_data.get("blueprint_name", ""),
            blueprint_type=bp_type,
            parent_class=bp_data.get("parent_class", ""),
            node_count=total_nodes,
            variable_count=variable_count,
            graph_count=graph_count,
            connection_count=total_connections,
            interface_count=interface_count,
            inheritance_depth=self._compute_inheritance_depth_single(bp_data),
            referenced_assets_count=len(bp_data.get("referenced_assets", [])),
            package_path=self._extract_package(bp_data.get("blueprint_path", "")),
        )

    def _extract_references(self, bp_data: Dict[str, Any]) -> Set[str]:
        """从 reader 输出提取引用的其他蓝图路径。

        规则：
          - Cast 节点的 target 类（以 /Game/ 开头）
          - dependent_blueprints 字段
          - referenced_assets 中以 .BP_ 结尾的路径
        """
        refs: Set[str] = set()

        # 显式依赖
        for bp in bp_data.get("dependent_blueprints", []):
            refs.add(str(bp))

        # 引用资产中的蓝图
        for asset in bp_data.get("referenced_assets", []):
            path = asset.get("path", "")
            if path.startswith("/Game/") and ("BP_" in path or "_C" in path):
                refs.add(path.removesuffix("_C"))

        # 从 graphs 中的 Cast 节点提取
        for graph in bp_data.get("graphs", []):
            for node in graph.get("nodes", []):
                if node.get("category") == "cast":
                    for pin in node.get("pins", []):
                        default = pin.get("default_value", "")
                        if default and default.startswith("/Game/"):
                            refs.add(default.removesuffix("_C"))

        return refs

    @staticmethod
    def _classify_blueprint_type(bp_type: str, parent_class: str) -> str:
        """分类蓝图类型。"""
        bp_type_lower = bp_type.lower()
        parent_lower = parent_class.lower()

        if "widget" in bp_type_lower or "widget" in parent_lower or "userwidget" in parent_lower:
            return "Widget"
        if "anim" in bp_type_lower or "anim" in parent_lower:
            return "AnimBP"
        if "actor" in parent_lower or "pawn" in parent_lower or "character" in parent_lower:
            return "Actor"
        if "component" in parent_lower:
            return "ActorComponent"
        if "functionlibrary" in parent_lower:
            return "FunctionLibrary"
        if "interface" in parent_lower:
            return "Interface"
        if "gameinstance" in parent_lower:
            return "GameInstance"
        if "gamemode" in parent_lower:
            return "GameMode"
        if "gamesession" in parent_lower:
            return "GameSession"
        if "playercontroller" in parent_lower:
            return "PlayerController"
        if "hud" in parent_lower:
            return "HUD"
        return "Other"

    @staticmethod
    def _extract_package(bp_path: str) -> str:
        """提取蓝图所在包路径。"""
        if "/" in bp_path:
            return "/".join(bp_path.split("/")[:-1])
        return "/Game/"

    def _compute_inheritance_depth_single(self, bp_data: Dict[str, Any]) -> int:
        """计算单张蓝图的继承深度。

        简化方案：基于 parent_class_path 估算。
        未来可接入 UE 反射系统获取准确深度。
        """
        parent_path = bp_data.get("parent_class_path", "")

        # 估算：Actor/Object 深度为 1，其子类逐层 +1
        if not parent_path:
            return 1

        # 如果父类不在 /Game/ 中（引擎类），视为基础深度
        if not parent_path.startswith("/Game/"):
            base_depths = {
                "actor": 1, "pawn": 2, "character": 3,
                "userwidget": 1, "blueprintfunctionlibrary": 1,
                "object": 0, "actorcomponent": 1,
                "gameinstance": 0, "gamemode": 0,
            }
            parent_lower = parent_path.lower()
            for key, val in base_depths.items():
                if key in parent_lower:
                    return val + 1
            return 1

        # 父类在项目中 → 尝试从已扫描蓝图中获取
        parent_clean = parent_path.removesuffix("_C")
        for summary in self._blueprint_summaries:
            if summary.path == parent_clean:
                return summary.inheritance_depth + 1

        return 2  # 默认：用户蓝图 → 引擎类


    # ════════════════ Phase 2: 基础指标 ═══════════════

    def _compute_distribution(self):
        """计算蓝图类型和包分布。"""
        dist = DistributionStats()
        dist.total = len(self._blueprint_summaries)

        for s in self._blueprint_summaries:
            dist.by_type[s.blueprint_type] = dist.by_type.get(s.blueprint_type, 0) + 1
            pkg = s.package_path
            dist.by_package[pkg] = dist.by_package.get(pkg, 0) + 1
            dist.by_parent_class[s.parent_class] = \
                dist.by_parent_class.get(s.parent_class, 0) + 1

        self.result.distribution = dist

    def _compute_top_largest(self, n: int = 20):
        """TOP N 大蓝图（按节点数排序）。"""
        sorted_bps = sorted(self._blueprint_summaries,
                           key=lambda s: s.node_count, reverse=True)
        self.result.top_largest = sorted_bps[:n]

    def _compute_inheritance_depth(self):
        """找出继承深度 > 4 的蓝图。"""
        self.result.deep_inheritance = [
            s for s in self._blueprint_summaries
            if s.inheritance_depth > DEEP_DIVE_INHERITANCE_DEPTH
        ]


    # ════════════════ Phase 3: 深度分析 ═══════════════

    def _detect_circular_dependencies(self):
        """DFS 拓扑排序检测循环依赖。

        构建完整引用图（bp_path → referenced bp_paths），
        从每个节点出发做 DFS，检测已访问节点上的环。
        """
        # 收集所有已知蓝图路径
        all_paths = {s.path for s in self._blueprint_summaries}

        # 构建完整邻接表（只保留项目内引用）
        adj: Dict[str, List[str]] = {}
        for src, refs in self._ref_graph.items():
            adj[src] = [r for r in refs if r in all_paths and r != src]

        # DFS 检测环
        cycles_found: List[List[str]] = []

        def find_cycles():
            """Tarjan 简化版：记录 DFS 栈中的路径。"""
            visited_all: Set[str] = set()

            for node in adj:
                if node in visited_all:
                    continue
                path_stack: List[str] = []
                path_set: Set[str] = set()

                def dfs(v: str) -> Optional[List[str]]:
                    if v in path_set:
                        # 找到环 → 从 v 开始截取
                        idx = path_stack.index(v) if v in path_stack else 0
                        cycle = path_stack[idx:] + [v]
                        return cycle
                    if v in visited_all:
                        return None
                    visited_all.add(v)
                    path_set.add(v)
                    path_stack.append(v)
                    for neighbor in adj.get(v, []):
                        result = dfs(neighbor)
                        if result:
                            return result
                    path_stack.pop()
                    path_set.discard(v)
                    return None

                cycle = dfs(node)
                if cycle:
                    cycles_found.append(cycle)

        find_cycles()

        # 去重：同一个环只保留一份
        seen_cycles: Set[Tuple[str, ...]] = set()
        for cycle in cycles_found:
            # 规范化（v2.20 修复）：旧实现只去掉末元素，不做旋转 → 同一环从不同
            # 起点 DFS 会得到 (A,B,C) 与 (B,C,A) 被判为不同环重复上报。
            # 现取全部旋转形态的最小字典序作规范键。
            rotated_base = tuple(cycle[:-1])  # 去掉最后一个（重复的）
            rotations = [tuple(rotated_base[i:] + rotated_base[:i])
                         for i in range(len(rotated_base))] or (rotated_base,)
            rotated = min(rotations)
            if rotated not in seen_cycles:
                seen_cycles.add(rotated)
                self.result.circular_dependencies.append(
                    CircularDependency(
                        cycle=list(rotated),
                        cycle_type="hard_ref",
                        severity="error" if len(rotated) <= 3 else "warning",
                    )
                )

    def _compute_coupling(self):
        """计算耦合度：跨目录引用比例。

        统计所有引用对中，src 和 dst 不在同一 package 的占比。
        """
        coupling = CouplingMetrics()
        total_pairs = 0
        cross_pairs = 0

        # 统计每对引用的耦合关系
        pair_counts: Dict[Tuple[str, str], int] = defaultdict(int)

        for src, refs in self._ref_graph.items():
            src_pkg = self._extract_package(src)
            for dst in refs:
                dst_pkg = self._extract_package(dst)
                total_pairs += 1
                if src_pkg != dst_pkg:
                    cross_pairs += 1
                pair_counts[(src, dst)] += 1

        coupling.total_references = total_pairs
        coupling.cross_package_references = cross_pairs
        coupling.cross_package_ratio = (
            cross_pairs / total_pairs if total_pairs > 0 else 0.0
        )

        # TOP 耦合对
        sorted_pairs = sorted(pair_counts.items(), key=lambda x: x[1], reverse=True)
        coupling.top_coupled_pairs = [
            {"src": p[0], "dst": p[1], "ref_count": c}
            for (p, c) in sorted_pairs[:10]
        ]

        self.result.coupling = coupling

    def _compute_interface_usage(self):
        """计算接口使用率。

        统计每个接口被多少蓝图实现。
        """
        interface_map: Dict[str, InterfaceUsage] = {}

        for bp_data in self._blueprint_results:
            bp_path = bp_data.get("blueprint_path", "")
            for iface in bp_data.get("interfaces", []):
                # blueprint_reader 返回的 interfaces 是字符串列表（接口名称），
                # 但部分旧数据可能是 dict 格式（{"name": ..., "path": ...}）
                if isinstance(iface, str):
                    iface_name = iface
                elif isinstance(iface, dict):
                    iface_name = iface.get("name", iface.get("path", str(iface)))
                else:
                    iface_name = str(iface)
                if not iface_name:
                    continue
                if iface_name not in interface_map:
                    interface_map[iface_name] = InterfaceUsage(
                        interface_name=iface_name,
                        implemented_by_count=0,
                    )
                interface_map[iface_name].implemented_by_count += 1
                interface_map[iface_name].blueprints.append(bp_path)

        self.result.interface_usage = sorted(
            interface_map.values(),
            key=lambda u: u.implemented_by_count,
            reverse=True,
        )

    def _run_deep_dives(self):
        """执行深挖分析。

        触发条件（每张蓝图）：
          1. 节点数 > 200
          2. 存在循环依赖
          3. 引用路径不匹配（Cast 目标类不存在）
          4. 接口未实现
        """
        # 构建快速查找索引
        all_bp_paths = {s.path for s in self._blueprint_summaries}
        bp_data_map = {d.get("blueprint_path", ""): d for d in self._blueprint_results}

        for summary in self._blueprint_summaries:
            bp_path = summary.path
            bp_data = bp_data_map.get(bp_path, {})

            reasons = []
            node_breakdown = {}
            circular_path = []
            unimplemented_ifaces = []
            suggestions = []

            # 触发 1: >200 节点
            if summary.node_count > DEEP_DIVE_NODE_THRESHOLD:
                reasons.append(f"大蓝图：{summary.node_count} 节点 > {DEEP_DIVE_NODE_THRESHOLD}")
                # 节点分类统计
                for graph in bp_data.get("graphs", []):
                    for node in graph.get("nodes", []):
                        cat = node.get("category", "other")
                        node_breakdown[cat] = node_breakdown.get(cat, 0) + 1
                suggestions.append(
                    f"建议拆分为多个子蓝图（当前 {summary.node_count} 节点）"
                )

            # 触发 2: 循环依赖
            in_cycle = False
            for cd in self.result.circular_dependencies:
                if bp_path in cd.cycle:
                    in_cycle = True
                    circular_path = cd.cycle
                    reasons.append(f"循环依赖：{cd.cycle_type}")
                    suggestions.append(
                        f"打破循环：用 Interface/EventDispatcher 替代 {cd.cycle_type}"
                    )
                    break

            # 触发 3: 引用路径不匹配
            mismatch_assets = self._check_reference_mismatch(bp_data, all_bp_paths)
            if mismatch_assets:
                reasons.append(f"引用路径不匹配：{len(mismatch_assets)} 个")
                suggestions.append(f"检查以下引用目标是否存在：{mismatch_assets[:5]}")

            # 触发 4: 接口未实现
            if self._check_unimplemented_interfaces(bp_data):
                reasons.append("接口未实现")
                unimplemented_ifaces = [
                    (i.get("name", "") if isinstance(i, dict) else str(i))
                    for i in bp_data.get("interfaces", [])
                ]
                suggestions.append("实现缺失的接口函数")

            if reasons:
                self.result.deep_dives.append(DeepDiveDetail(
                    blueprint_path=bp_path,
                    reason="; ".join(reasons),
                    node_breakdown=node_breakdown,
                    circular_path=circular_path,
                    unimplemented_interfaces=unimplemented_ifaces,
                    suggestions=suggestions,
                ))

    def _check_reference_mismatch(self, bp_data: Dict[str, Any],
                                  all_bp_paths: Set[str]) -> List[str]:
        """检查 Cast 节点中的目标类是否存在于项目蓝图列表中。

        宽松匹配：检查引用路径是否以已知蓝图路径开头。
        """
        mismatches = []
        for graph in bp_data.get("graphs", []):
            for node in graph.get("nodes", []):
                if node.get("category") != "cast":
                    continue
                for pin in node.get("pins", []):
                    default = pin.get("default_value", "")
                    if not default or not default.startswith("/Game/"):
                        continue
                    clean_path = default.removesuffix("_C")
                    # 宽松匹配：检查是否有任何蓝图以此路径开头
                    found = any(bp.startswith(clean_path) or
                               clean_path.startswith(bp)
                               for bp in all_bp_paths)
                    if not found and clean_path not in all_bp_paths:
                        mismatches.append(clean_path)
        return mismatches

    def _check_unimplemented_interfaces(self, bp_data: Dict[str, Any]) -> bool:
        """检查蓝图接口是否缺少实现。

        简化策略：
          - 蓝图有接口
          - EventGraph 中没有对应 Interface 的 Event 节点
          → 标记为可疑
        """
        interfaces = bp_data.get("interfaces", [])
        if not interfaces:
            return False

        # 收集已有的事件名
        event_names: Set[str] = set()
        for graph in bp_data.get("graphs", []):
            for node in graph.get("nodes", []):
                if node.get("category") == "event":
                    event_names.add(node.get("function_name", ""))
                    event_names.add(node.get("title", ""))

        # 检查是否有 interface 相关事件
        has_interface_event = any(
            "interface" in e.lower() or "bndevt" in e.lower()
            for e in event_names
        )

        return bool(interfaces) and not has_interface_event


    # ════════════════ 序列化 ══════════════════════════

    def to_dict(self) -> Dict[str, Any]:
        """将 AnalysisResult 序列化为 JSON 兼容 dict。"""
        r = self.result
        if not r:
            return {}

        return {
            "project_name": r.project_name,
            "project_root": r.project_root,
            "analyzed_at": r.analyzed_at,
            "summary": {
                "total_blueprints": r.total_blueprints,
                "total_nodes": r.total_nodes,
                "total_connections": r.total_connections,
                "total_elapsed_ms": r.total_elapsed_ms,
            },
            "distribution": {
                "by_type": r.distribution.by_type,
                "by_package": dict(sorted(
                    r.distribution.by_package.items(),
                    key=lambda x: x[1], reverse=True
                )),
                "by_parent_class": dict(sorted(
                    r.distribution.by_parent_class.items(),
                    key=lambda x: x[1], reverse=True
                )),
            },
            "top_largest": [
                {
                    "path": s.path,
                    "name": s.name,
                    "type": s.blueprint_type,
                    "node_count": s.node_count,
                    "variable_count": s.variable_count,
                    "connection_count": s.connection_count,
                    "inheritance_depth": s.inheritance_depth,
                    "package": s.package_path,
                }
                for s in r.top_largest
            ],
            "deep_inheritance": [
                {
                    "path": s.path,
                    "name": s.name,
                    "inheritance_depth": s.inheritance_depth,
                    "parent_class": s.parent_class,
                    "type": s.blueprint_type,
                }
                for s in r.deep_inheritance
            ],
            "circular_dependencies": [
                {
                    "cycle": cd.cycle,
                    "cycle_type": cd.cycle_type,
                    "severity": cd.severity,
                }
                for cd in r.circular_dependencies
            ],
            "coupling": {
                "total_references": r.coupling.total_references,
                "cross_package_references": r.coupling.cross_package_references,
                "cross_package_ratio": round(r.coupling.cross_package_ratio, 4),
                "top_coupled_pairs": r.coupling.top_coupled_pairs,
            },
            "interface_usage": [
                {
                    "interface_name": u.interface_name,
                    "implemented_by": u.implemented_by_count,
                }
                for u in r.interface_usage
            ],
            "deep_dives": [
                {
                    "blueprint_path": d.blueprint_path,
                    "reason": d.reason,
                    "node_breakdown": d.node_breakdown,
                    "circular_path": d.circular_path,
                    "unimplemented_interfaces": d.unimplemented_interfaces,
                    "suggestions": d.suggestions,
                }
                for d in r.deep_dives
            ],
            "errors": r.errors,
            "warnings": r.warnings,
        }

    def to_json(self, indent: int = 2) -> str:
        """序列化为 JSON 字符串。"""
        return json.dumps(self.to_dict(), indent=indent,
                          ensure_ascii=False, default=str)


# ── 便捷函数 ─────────────────────────────────────────

def analyze_from_reader_output(reader_output_path: str,
                                project_root: str = "/Game/",
                                depth: int = 2) -> AnalysisResult:
    """从 blueprint_reader JSON 输出直接分析。"""
    analyzer = ProjectAnalyzer()
    analyzer.feed_from_json(reader_output_path)
    return analyzer.analyze(project_root, depth)


async def analyze_live(project_root: str = "/Game/",
                       depth: int = 2) -> AnalysisResult:
    """在 UE 环境中实时扫描分析。"""
    analyzer = ProjectAnalyzer(
        unreal_module=None,  # 运行时注入
        bridge_module=None,
    )

    # 尝试获取 unreal 模块
    try:
        import unreal
        analyzer.unreal = unreal
        analyzer.bridge = getattr(unreal, "BlueprintPythonBridge", None)
    except ImportError:
        pass

    await analyzer.scan_unreal(project_root)
    return analyzer.analyze(project_root, depth)


# ── CLI 入口 ─────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="项目架构分析引擎 — 全量蓝图分析")
    parser.add_argument("--input", "-i",
                        help="blueprint_reader JSON 输出文件路径")
    parser.add_argument("--output", "-o", default="analyzer_result.json",
                        help="输出 JSON 文件路径")
    parser.add_argument("--project-root", default="/Game/",
                        help="项目根路径")
    parser.add_argument("--depth", type=int, default=2,
                        choices=[1, 2],
                        help="分析深度：1=基础 2=完整")
    parser.add_argument("--summary", action="store_true",
                        help="仅输出摘要")

    args = parser.parse_args()

    # v2.20 修复（P2）：中文 Windows 控制台默认 GBK，摘要含 ⚠️/🔍 emoji 时
    #   UnicodeEncodeError 崩溃（blueprint_reader.py 已有同款修复，此处对齐）。
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    analyzer = ProjectAnalyzer()

    if args.input:
        analyzer.feed_from_json(args.input)

    result = analyzer.analyze(args.project_root, args.depth)

    if args.summary:
        print(f"\n{'='*50}")
        print(f"  项目: {result.project_root}")
        print(f"  蓝图总数: {result.total_blueprints}")
        print(f"  节点总数: {result.total_nodes}")
        print(f"  连线总数: {result.total_connections}")
        print(f"\n  类型分布:")
        for t, c in result.distribution.by_type.items():
            print(f"    {t}: {c}")
        print(f"\n  TOP 5 大蓝图:")
        for s in result.top_largest[:5]:
            print(f"    {s.name}: {s.node_count} 节点 ({s.blueprint_type})")
        if result.circular_dependencies:
            print(f"\n  ⚠️  循环依赖: {len(result.circular_dependencies)} 个")
            for cd in result.circular_dependencies[:3]:
                print(f"    → {' → '.join(cd.cycle)}")
        if result.deep_dives:
            print(f"\n  🔍 深挖: {len(result.deep_dives)} 张蓝图")
            for d in result.deep_dives[:3]:
                print(f"    {d.blueprint_path}: {d.reason[:80]}")
        print(f"\n  耗时: {result.total_elapsed_ms:.0f}ms")
        print(f"{'='*50}\n")
        return

    json_str = analyzer.to_json()
    with open(args.output, "w", encoding="utf-8") as f:
        f.write(json_str)
    print(f"分析结果已写入: {args.output}")
    print(f"蓝图 {result.total_blueprints} 张, "
          f"节点 {result.total_nodes} 个, "
          f"循环依赖 {len(result.circular_dependencies)} 个, "
          f"深挖 {len(result.deep_dives)} 项")


if __name__ == "__main__":
    main()


def _dep_to_dict(dep) -> dict:
    """DependencyAnalysis 转 dict"""
    return {
        "parent_class": dep.parent_class,
        "interfaces": dep.interfaces,
        "direct_dependencies": [asdict(d) for d in dep.direct_dependencies],
        "circular_chains": dep.circular_chains,
        "external_count": dep.external_count,
        "internal_count": dep.internal_count,
        "engine_ref_count": dep.engine_ref_count,
        "max_dependency_depth": dep.max_dependency_depth,
    }


def _hp_to_dict(hp) -> dict:
    """HotPathAnalysis 转 dict"""
    return {
        "entry_points": [asdict(e) for e in hp.entry_points],
        "tick_related": [asdict(t) for t in hp.tick_related],
        "timer_nodes": [asdict(t) for t in hp.timer_nodes],
        "heavy_functions": [asdict(h) for h in hp.heavy_functions],
        "tight_loops": [asdict(tl) for tl in hp.tight_loops],
        "delay_nodes": [asdict(d) for d in hp.delay_nodes],
        "estimated_hot_graphs": [list(p) for p in hp.estimated_hot_graphs],
        "recommendations": hp.recommendations,
    }


# =============================================================
# 节点分类器
# =============================================================

# 节点类名 → 归类映射
_NODE_CLASS_CATEGORIES = {
    "K2Node_Event": "Event",
    "K2Node_CustomEvent": "Event",
    "K2Node_InputAction": "Event",
    "K2Node_InputAxisEvent": "Event",
    "K2Node_ComponentBoundEvent": "Event",
    "K2Node_ActorBoundEvent": "Event",
    "K2Node_CallFunction": "Function",
    "K2Node_CallParentFunction": "Function",
    "K2Node_CallArrayFunction": "Function",
    "K2Node_CallMaterialParameterCollectionFunction": "Function",
    "K2Node_IfThenElse": "FlowControl",
    "K2Node_Branch": "FlowControl",
    "K2Node_Switch": "FlowControl",
    "K2Node_SwitchEnum": "FlowControl",
    "K2Node_ExecutionSequence": "FlowControl",
    "K2Node_ForEachLoop": "FlowControl",
    "K2Node_ForLoop": "FlowControl",
    "K2Node_WhileLoop": "FlowControl",
    "K2Node_DoOnce": "FlowControl",
    "K2Node_Delay": "FlowControl",
    "K2Node_Timeline": "FlowControl",
    "K2Node_VariableGet": "Variable",
    "K2Node_VariableSet": "Variable",
    "K2Node_Self": "Variable",
    "K2Node_DynamicCast": "Cast",
    "K2Node_ClassDynamicCast": "Cast",
    "K2Node_SpawnActor": "Spawn",
    "K2Node_SpawnActorFromClass": "Spawn",
    "K2Node_CreateWidget": "Widget",
    "K2Node_CallDelegate": "Delegate",
    "K2Node_AddDelegate": "Delegate",
    "K2Node_RemoveDelegate": "Delegate",
    "K2Node_Message": "Other",
    "K2Node_Comment": "Other",
    "K2Node_Tunnel": "Other",
}

# 性能相关的节点模式
_PERF_SENSITIVE_PATTERNS = {
    "Tick": ["EventTick", "ReceiveTick", "Tick"],
    "Timer": ["SetTimer", "SetTimerByEvent", "SetTimerByFunctionName"],
    "Delay": ["Delay", "RetriggerableDelay"],
    "Loop": ["ForLoop", "ForEachLoop", "WhileLoop"],
    "Heavy": ["SpawnActor", "CreateWidget", "LoadObject", "AsyncLoadAsset"],
}


def _classify_node(node) -> str:
    """将节点归类到顶层类别"""
    if node.node_class in _NODE_CLASS_CATEGORIES:
        return _NODE_CLASS_CATEGORIES[node.node_class]
    nc = node.node_class
    if nc.startswith("K2Node_Event") or nc.startswith("K2Node_Input"):
        return "Event"
    if nc.startswith("K2Node_Call") or nc.startswith("K2Node_Get"):
        return "Function"
    if nc.startswith("K2Node_If") or nc.startswith("K2Node_Switch") or nc.startswith("K2Node_Branch"):
        return "FlowControl"
    if nc.startswith("K2Node_For") or nc.startswith("K2Node_While") or nc.startswith("K2Node_Do"):
        return "FlowControl"
    if nc.startswith("K2Node_Variable") or nc.startswith("K2Node_Self"):
        return "Variable"
    if nc.startswith("K2Node_DynamicCast") or nc.startswith("K2Node_ClassDynamicCast"):
        return "Cast"
    if nc.startswith("K2Node_Spawn"):
        return "Spawn"
    if nc.startswith("K2Node_CreateWidget"):
        return "Widget"
    if nc.startswith("K2Node_Timeline") or nc.startswith("K2Node_Delay"):
        return "FlowControl"
    return "Other"


# =============================================================
# BlueprintDeepAnalyzer 核心分析引擎
# =============================================================


class BlueprintDeepAnalyzer:
    """蓝图深度分析引擎。

    用法:
        from blueprint_reader import BlueprintReader
        from analyzer import BlueprintDeepAnalyzer  # 在 analyzer.py 内使用时用本模块名引用

        reader = BlueprintReader()
        ba = reader.analyze("/Game/Blueprints/BP_HelloMirror")
        analyzer = BlueprintDeepAnalyzer(ba)
        report = analyzer.analyze()
        print(report.to_json())
    """

    def __init__(self, blueprint_analysis):
        self.ba = blueprint_analysis
        self.report = DeepAnalysisReport(
            blueprint_name=self.ba.basic_info.name or "Unknown",
            asset_path=self.ba.basic_info.asset_path,
            analysis_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            degrade_level=self.ba.degrade_level.name,
        )

    # ── 主入口 ──

    def analyze(self):
        """执行全维度分析"""
        self._analyze_node_stats()
        self._analyze_complexity()
        self._analyze_dependencies()
        self._analyze_hot_paths()
        self._compute_summary()
        self.report.analysis_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return self.report

    # ── 维度 1：节点统计 ──

    def _analyze_node_stats(self):
        """统计所有节点的类型分布、图级分组、引脚密度"""
        all_nodes = []  # (graph_name, node)
        for gname, graph in self.ba.graphs.items():
            for node in graph.nodes:
                all_nodes.append((gname, node))

        total = len(all_nodes)
        if total == 0:
            self.report.warnings.append("没有找到任何节点——可能处于 L3 降级")
            return

        # 按节点类名分组
        class_groups = {}
        for gname, node in all_nodes:
            nc = node.node_class
            if nc not in class_groups:
                class_groups[nc] = []
            class_groups[nc].append((gname, node))

        stats_list = []
        for nclass, items in sorted(class_groups.items(), key=lambda x: -len(x[1])):
            cnt = len(items)
            category = _classify_node(items[0][1])
            pure_cnt = sum(1 for _, n in items if n.is_pure)
            latent_cnt = sum(1 for _, n in items if n.is_latent)
            total_pins = sum(len(n.pins) for _, n in items)
            avg_pins = total_pins / cnt if cnt > 0 else 0
            graphs_seen = list(set(gname for gname, _ in items))

            stats_list.append(NodeTypeStats(
                node_class=nclass,
                category=category,
                count=cnt,
                percentage=round(cnt / total * 100, 1),
                is_pure_count=pure_cnt,
                is_latent_count=latent_cnt,
                avg_pins=round(avg_pins, 1),
                graphs_present=graphs_seen,
            ))

        self.report.node_stats = stats_list

        # 按类别汇总检查
        cat_counts = {}
        for s in stats_list:
            cat_counts[s.category] = cat_counts.get(s.category, 0) + s.count
        cat_counts = {k: v for k, v in sorted(cat_counts.items(), key=lambda x: -x[1])}
        if cat_counts.get("Event", 0) == 0:
            self.report.recommendations.append("⚠️ 未检测到事件入口节点，蓝图可能无法自动触发逻辑")

    # ── 维度 2：复杂度评分 ──

    def _analyze_complexity(self):
        """分析每个 Graph 的复杂度并计算综合评分"""
        per_graph = []

        # 构建每个图内的连线索引
        conn_by_graph = {}
        for conn in self.ba.connections:
            for gname, graph in self.ba.graphs.items():
                node_titles = {n.title for n in graph.nodes}
                if conn.from_node in node_titles or conn.to_node in node_titles:
                    if gname not in conn_by_graph:
                        conn_by_graph[gname] = []
                    conn_by_graph[gname].append(conn)
                    break

        for gname, graph in self.ba.graphs.items():
            gc = GraphComplexity(
                graph_name=gname,
                graph_type=graph.graph_type,
                node_count=len(graph.nodes),
                connection_count=len(conn_by_graph.get(gname, [])),
            )

            for node in graph.nodes:
                nc = node.node_class
                nt = node.title
                if "K2Node_Event" in nc:
                    gc.event_count += 1
                if any(kw in nc for kw in ("IfThenElse", "Branch", "Switch")):
                    gc.branch_count += 1
                if "K2Node_ExecutionSequence" in nc:
                    gc.sequence_count += 1
                if any(kw in nc for kw in ("ForLoop", "ForEachLoop", "WhileLoop")):
                    gc.loop_count += 1
                if any(kw in nc or kw in nt for kw in ("Delay", "Timeline")):
                    gc.delay_count += 1

            # 嵌套深度
            gc.nesting_depth = self._compute_nesting_depth(gname, graph)

            # 圈复杂度简化公式
            gc.cyclomatic_complexity = gc.branch_count + 1

            # 连接密度
            if gc.node_count > 0:
                gc.density_score = round(gc.connection_count / gc.node_count, 2)

            # 评级
            gc.complexity_rating = self._rate_graph_complexity(gc)
            per_graph.append(gc)

        # 汇总
        cs = ComplexityScore(
            total_node_count=sum(g.node_count for g in per_graph),
            total_connection_count=sum(g.connection_count for g in per_graph),
            total_branch_count=sum(g.branch_count for g in per_graph),
            total_loop_count=sum(g.loop_count for g in per_graph),
            total_delay_count=sum(g.delay_count for g in per_graph),
            max_nesting_depth=max((g.nesting_depth for g in per_graph), default=0),
            avg_cyclomatic_complexity=round(
                sum(g.cyclomatic_complexity for g in per_graph) / max(len(per_graph), 1), 1
            ),
            per_graph=per_graph,
        )

        # 综合评分 0-100
        factors = [
            min(cs.total_node_count / 200 * 25, 25),
            min(cs.total_branch_count / 30 * 20, 20),
            min(cs.total_loop_count / 10 * 15, 15),
            min(cs.max_nesting_depth / 5 * 20, 20),
            min(cs.total_delay_count / 8 * 10, 10),
            min(cs.avg_cyclomatic_complexity / 15 * 10, 10),
        ]
        cs.score = round(sum(factors), 1)

        if cs.score < 20:
            cs.overall_rating = ComplexityRating.LOW.value
        elif cs.score < 45:
            cs.overall_rating = ComplexityRating.MEDIUM.value
        elif cs.score < 70:
            cs.overall_rating = ComplexityRating.HIGH.value
        else:
            cs.overall_rating = ComplexityRating.VERY_HIGH.value

        self.report.complexity = cs

    def _compute_nesting_depth(self, gname, graph):
        """通过 exec 连线 BFS 计算最大嵌套深度"""
        adj = {}
        node_set = {n.title for n in graph.nodes}
        for conn in self.ba.connections:
            if conn.from_node in node_set and conn.to_node in node_set:
                from_is_exec = any(kw in conn.from_pin.lower() for kw in ("then", "exec", "output", "completed"))
                to_is_exec = any(kw in conn.to_pin.lower() for kw in ("execute", "exec", "enter"))
                if from_is_exec and to_is_exec:
                    if conn.from_node not in adj:
                        adj[conn.from_node] = []
                    adj[conn.from_node].append(conn.to_node)

        max_depth = 0
        entry_nodes = [n.title for n in graph.nodes if n.is_event or "K2Node_Event" in n.node_class]
        if not entry_nodes:
            entry_nodes = list(node_set)[:1] if node_set else []

        visited_depths = {}
        q = deque()
        for entry in entry_nodes:
            q.append((entry, 0))

        while q:
            node, depth = q.popleft()
            if depth > max_depth:
                max_depth = depth
            for neighbor in adj.get(node, []):
                new_depth = depth + 1
                if neighbor not in visited_depths or visited_depths[neighbor] < new_depth:
                    visited_depths[neighbor] = new_depth
                    q.append((neighbor, new_depth))

        return max_depth

    @staticmethod
    def _rate_graph_complexity(gc):
        """评级单个 Graph 的复杂度"""
        score = 0
        if gc.node_count > 50:
            score += 3
        elif gc.node_count > 20:
            score += 1
        if gc.branch_count > 10:
            score += 3
        elif gc.branch_count > 5:
            score += 2
        elif gc.branch_count > 2:
            score += 1
        if gc.loop_count > 3:
            score += 2
        elif gc.loop_count > 0:
            score += 1
        if gc.nesting_depth > 4:
            score += 2
        elif gc.nesting_depth > 2:
            score += 1
        if score >= 6:
            return ComplexityRating.VERY_HIGH.value
        if score >= 4:
            return ComplexityRating.HIGH.value
        if score >= 2:
            return ComplexityRating.MEDIUM.value
        return ComplexityRating.LOW.value


    # ── 维度 3：依赖分析 ──

    def _analyze_dependencies(self):
        """分析蓝图的外部依赖"""
        dep = DependencyAnalysis(
            parent_class=self.ba.basic_info.parent_class,
            interfaces=self.ba.basic_info.interfaces,
        )

        # 从 referenced_assets 提取
        for ref in self.ba.referenced_assets:
            dep.direct_dependencies.append(DependencyNode(
                asset_path=ref.asset_path,
                dependency_type=ref.asset_type,
                source_node=ref.source_node,
                is_external=not ref.asset_path.startswith("/Game"),
                is_suspicious=ref.is_suspicious,
            ))

        # 从节点中提取运行时引用
        for gname, graph in self.ba.graphs.items():
            for node in graph.nodes:
                if "K2Node_DynamicCast" in node.node_class:
                    for pin in node.pins:
                        if pin.default_value and pin.default_value.startswith("/"):
                            dep.direct_dependencies.append(DependencyNode(
                                asset_path=pin.default_value,
                                dependency_type="CastTo",
                                source_node=node.title,
                                source_graph=gname,
                                is_external=not pin.default_value.startswith("/Game"),
                            ))
                if "CreateWidget" in node.title:
                    for pin in node.pins:
                        if "Class" in pin.name and pin.default_value and pin.default_value.startswith("/"):
                            dep.direct_dependencies.append(DependencyNode(
                                asset_path=pin.default_value,
                                dependency_type="CreateWidget",
                                source_node=node.title,
                                source_graph=gname,
                                is_external=not pin.default_value.startswith("/Game"),
                            ))

        # 去重
        seen = set()
        unique_deps = []
        for dnode in dep.direct_dependencies:
            key = f"{dnode.asset_path}|{dnode.dependency_type}|{dnode.source_graph}"
            if key not in seen:
                seen.add(key)
                unique_deps.append(dnode)
        dep.direct_dependencies = unique_deps

        # 统计
        dep.external_count = sum(1 for d in dep.direct_dependencies if d.is_external)
        dep.internal_count = sum(1 for d in dep.direct_dependencies if not d.is_external)
        dep.engine_ref_count = sum(1 for d in dep.direct_dependencies if d.asset_path.startswith("/Engine"))

        # 父类依赖
        if dep.parent_class and dep.parent_class != "Actor" and dep.parent_class != "Unknown":
            if not any(d.asset_path == dep.parent_class for d in dep.direct_dependencies):
                dep.direct_dependencies.append(DependencyNode(
                    asset_path=dep.parent_class,
                    dependency_type="ParentClass",
                ))

        # 接口依赖
        for iface in dep.interfaces:
            if not any(d.asset_path == iface for d in dep.direct_dependencies):
                dep.direct_dependencies.append(DependencyNode(
                    asset_path=iface,
                    dependency_type="Interface",
                ))

        if dep.external_count > dep.internal_count and dep.internal_count > 0:
            self.report.recommendations.append(
                f"⚠️ 外部引用 ({dep.external_count}) 超过内部引用 ({dep.internal_count})，检查是否有不必要的跨模块依赖"
            )

        if dep.engine_ref_count > 3:
            self.report.recommendations.append(
                f"⚡ 引擎引用 ({dep.engine_ref_count}) 偏高，考虑是否可替换为项目内资源"
            )

        self.report.dependencies = dep

    # ── 维度 4：性能热路径 ──

    def _analyze_hot_paths(self):
        """识别性能热路径"""
        hp = HotPathAnalysis()

        for gname, graph in self.ba.graphs.items():
            for node in graph.nodes:
                nc = node.node_class
                nt = node.title

                # 入口事件
                if "K2Node_Event" in nc:
                    cat = "EventEntry"
                    if "Tick" in nt or "Tick" in nc:
                        cat = "Tick"
                    hp.entry_points.append(HotPathNode(
                        node_title=nt, graph_name=gname, category=cat,
                        execution_estimate="EveryFrame" if cat == "Tick" else "OnEvent",
                        downstream_count=len(graph.nodes) if graph.nodes else 0,
                        pin_count=len(node.pins),
                    ))

                # Tick 相关
                if any(kw in nt for kw in _PERF_SENSITIVE_PATTERNS["Tick"]):
                    hp.tick_related.append(HotPathNode(
                        node_title=nt, graph_name=gname, category="Tick",
                        execution_estimate="EveryFrame", pin_count=len(node.pins),
                        cost_note="每帧执行，检查是否有非必要的 Tick 计算",
                    ))

                # 定时器
                if any(kw in nt for kw in _PERF_SENSITIVE_PATTERNS["Timer"]):
                    hp.timer_nodes.append(HotPathNode(
                        node_title=nt, graph_name=gname, category="Timer",
                        execution_estimate="Periodic", pin_count=len(node.pins),
                    ))

                # 延迟
                if any(kw in nt for kw in _PERF_SENSITIVE_PATTERNS["Delay"]):
                    hp.delay_nodes.append(HotPathNode(
                        node_title=nt, graph_name=gname, category="Delay",
                        execution_estimate="Once", pin_count=len(node.pins),
                    ))

                # 循环
                if any(kw in nt for kw in _PERF_SENSITIVE_PATTERNS["Loop"]):
                    hp.tight_loops.append(HotPathNode(
                        node_title=nt, graph_name=gname, category="TightLoop",
                        execution_estimate="Periodic", pin_count=len(node.pins),
                        cost_note=f"循环节点，检查{nt}内部操作是否有不必要的每帧计算",
                    ))

                # 重函数
                if any(kw in nt for kw in _PERF_SENSITIVE_PATTERNS["Heavy"]):
                    hp.heavy_functions.append(HotPathNode(
                        node_title=nt, graph_name=gname, category="HeavyFunction",
                        execution_estimate="OnEvent", pin_count=len(node.pins),
                        cost_note=f"{nt} 可能触发资产加载/实例化，建议异步处理",
                    ))

        # 热图排序：按节点数 + Tick*5 + Loop*3 降序
        hot_graphs = []
        for gname, graph in self.ba.graphs.items():
            cost = (
                len(graph.nodes)
                + sum(1 for n in graph.nodes if "Tick" in n.title) * 5
                + sum(1 for n in graph.nodes if any(kw in n.title for kw in ("ForLoop", "WhileLoop"))) * 3
            )
            hot_graphs.append((gname, cost))
        hot_graphs.sort(key=lambda x: -x[1])
        hp.estimated_hot_graphs = hot_graphs

        # 建议生成
        if hp.tick_related:
            hp.recommendations.append(
                f"⚡ 检测到 {len(hp.tick_related)} 个 Tick 相关节点 —— 评估是否可用 Timer/Event 替代轮询"
            )
        if hp.tight_loops:
            hp.recommendations.append(
                f"🔄 检测到 {len(hp.tight_loops)} 个循环节点 —— 检查循环体内是否有资产加载或复杂计算"
            )
        if hp.heavy_functions:
            hp.recommendations.append(
                f"🐌 检测到 {len(hp.heavy_functions)} 个重函数节点 —— 建议使用异步加载避免卡顿"
            )
        if len(hp.delay_nodes) > 5:
            hp.recommendations.append(
                f"⏱ 延迟节点较多 ({len(hp.delay_nodes)})，检查是否有不必要的等待"
            )

        self.report.hot_paths = hp

    # ── 综合评分 ──

    def _compute_summary(self):
        """计算综合评级和总分"""
        score = 0.0

        # 复杂度贡献：0-40 分
        if self.report.complexity:
            score += self.report.complexity.score * 0.4

        # 节点统计贡献：0-20 分
        if self.report.node_stats:
            total_nodes = sum(s.count for s in self.report.node_stats)
            event_pct = sum(s.count for s in self.report.node_stats if s.category == "Event") / max(total_nodes, 1) * 100
            if event_pct < 5:
                score += 10
            cast_pct = sum(s.count for s in self.report.node_stats if s.category == "Cast") / max(total_nodes, 1) * 100
            if cast_pct > 15:
                score += 10

        # 依赖贡献：0-20 分
        if self.report.dependencies:
            dep = self.report.dependencies
            if dep.external_count > 5:
                score += 10
            if dep.engine_ref_count > 3:
                score += 10

        # 热路径贡献：0-20 分
        if self.report.hot_paths:
            hp = self.report.hot_paths
            if hp.tick_related:
                score += min(len(hp.tick_related) * 3, 10)
            if hp.tight_loops:
                score += min(len(hp.tight_loops) * 3, 10)

        self.report.summary_score = min(int(score), 100)

        # 评级
        if self.report.summary_score < 20:
            self.report.summary_grade = SummaryGrade.A.value
        elif self.report.summary_score < 40:
            self.report.summary_grade = SummaryGrade.B.value
        elif self.report.summary_score < 60:
            self.report.summary_grade = SummaryGrade.C.value
        elif self.report.summary_score < 80:
            self.report.summary_grade = SummaryGrade.D.value
        else:
            self.report.summary_grade = SummaryGrade.F.value

        # 总体建议
        if self.report.summary_grade in ("D", "F"):
            self.report.recommendations.append(
                f"🏗️ 综合评分 {self.report.summary_grade} ({self.report.summary_score}/100)，"
                f"建议重构蓝图。优先处理复杂度高和依赖多的图。"
            )


# =============================================================
# Markdown 报告生成
# =============================================================


def generate_markdown_report(report: DeepAnalysisReport) -> str:
    """将 DeepAnalysisReport 生成为 Markdown 报告。

    Args:
        report: 深度分析报告

    Returns:
        Markdown 格式的字符串
    """
    lines = []
    lines.append(f"# 🔬 蓝图深度分析报告：{report.blueprint_name}")
    lines.append("")
    lines.append(f"**分析时间：** {report.analysis_time}")
    lines.append(f"**资产路径：** `{report.asset_path}`")
    lines.append(f"**降级层级：** {report.degrade_level}")
    lines.append("")

    # 综合评级
    grade = report.summary_grade
    score = report.summary_score
    grade_emoji = {"A": "🟢", "B": "🟢", "C": "🟡", "D": "🟠", "F": "🔴"}.get(grade, "⚪")
    lines.append(f"## 综合评级：{grade_emoji} {grade} ({score}/100)")
    lines.append("")
    lines.append("> 分数越低越健康。0-20: A（健康）/ 20-40: B（良好）/ 40-60: C（一般）/ 60-80: D（需改进）/ 80-100: F（严重）")
    lines.append("")

    # 建议
    if report.recommendations:
        lines.append("## 📋 优化建议")
        lines.append("")
        for rec in report.recommendations:
            lines.append(f"- {rec}")
        lines.append("")

    # 维度 1：节点统计
    lines.append("---")
    lines.append("## 1. 📊 节点类型统计")
    lines.append("")
    if report.node_stats:
        total_nodes = sum(s.count for s in report.node_stats)
        lines.append(f"**总节点数：** {total_nodes}")
        lines.append("")
        # 按类别汇总
        cat_map = {}
        for s in report.node_stats:
            cat_map[s.category] = cat_map.get(s.category, 0) + s.count
        lines.append("### 类别分布")
        lines.append("")
        lines.append("| 类别 | 数量 | 占比 |")
        lines.append("|:---|:---:|:---:|")
        for cat, cnt in sorted(cat_map.items(), key=lambda x: -x[1]):
            pct = round(cnt / total_nodes * 100, 1)
            bar = "█" * int(pct / 5) + "░" * (20 - int(pct / 5))
            lines.append(f"| {cat} | {cnt} | {bar} {pct}% |")
        lines.append("")

        # TOP 10 节点类
        lines.append(f"### TOP 10 节点类（共 {len(report.node_stats)} 种）")
        lines.append("")
        lines.append("| 节点类 | 类别 | 数量 | 占比 | 纯函数 | 引脚均值 |")
        lines.append("|:---|:---|:---:|:---:|:---:|:---:|")
        for ns in report.node_stats[:10]:
            pure = f"🟢 {ns.is_pure_count}" if ns.is_pure_count else "-"
            lines.append(
                f"| {ns.node_class[:40]} | {ns.category} | {ns.count} | "
                f"{ns.percentage}% | {pure} | {ns.avg_pins} |"
            )
        lines.append("")
        if len(report.node_stats) > 10:
            lines.append(f"*(省略 {len(report.node_stats) - 10} 种节点类型)*")
            lines.append("")
    else:
        lines.append("*(无节点数据)*")
        lines.append("")

    # 维度 2：复杂度
    lines.append("---")
    lines.append("## 2. 🧩 复杂度评分")
    lines.append("")
    if report.complexity:
        cs = report.complexity
        lines.append(f"| 维度 | 值 |")
        lines.append(f"|:---|:---|")
        lines.append(f"| 总节点数 | {cs.total_node_count} |")
        lines.append(f"| 总连线数 | {cs.total_connection_count} |")
        lines.append(f"| 分支节点 | {cs.total_branch_count} |")
        lines.append(f"| 循环节点 | {cs.total_loop_count} |")
        lines.append(f"| 延迟节点 | {cs.total_delay_count} |")
        lines.append(f"| 最大嵌套深度 | {cs.max_nesting_depth} |")
        lines.append(f"| 平均圈复杂度 | {cs.avg_cyclomatic_complexity} |")
        lines.append(f"| **复杂度评分** | **{cs.score}/100** |")
        lines.append(f"| **复杂度评级** | **{cs.overall_rating}** |")
        lines.append("")

        # 逐图详情
        if cs.per_graph:
            lines.append("### 各 Graph 复杂度明细")
            lines.append("")
            lines.append("| Graph | 节点 | 连线 | 分支 | 循环 | 嵌套深 | 圈复杂度 | 评级 |")
            lines.append("|:---|:---:|:---:|:---:|:---:|:---:|:---:|:---:|")
            for gc in cs.per_graph:
                lines.append(
                    f"| {gc.graph_name[:30]} | {gc.node_count} | {gc.connection_count} | "
                    f"{gc.branch_count} | {gc.loop_count} | {gc.nesting_depth} | "
                    f"{gc.cyclomatic_complexity} | {gc.complexity_rating} |"
                )
            lines.append("")
    else:
        lines.append("*(无复杂度数据)*")
        lines.append("")

    # 维度 3：依赖
    lines.append("---")
    lines.append("## 3. 🔗 依赖分析")
    lines.append("")
    if report.dependencies:
        dep = report.dependencies
        lines.append(f"| 属性 | 值 |")
        lines.append(f"|:---|:---|")
        lines.append(f"| 父类 | {dep.parent_class} |")
        lines.append(f"| 接口 | {', '.join(dep.interfaces) if dep.interfaces else '(无)'} |")
        lines.append(f"| 外部引用 | {dep.external_count} |")
        lines.append(f"| 内部引用 | {dep.internal_count} |")
        lines.append(f"| 引擎引用 | {dep.engine_ref_count} |")
        lines.append("")

        if dep.direct_dependencies:
            lines.append("### 直接依赖清单")
            lines.append("")
            lines.append("| 资产路径 | 类型 | 来源节点 | 外部 | 可疑 |")
            lines.append("|:---|:---|:---|:---:|:---:|")
            for d in dep.direct_dependencies:
                ext_flag = "✅" if d.is_external else ""
                susp_flag = "⚠️" if d.is_suspicious else ""
                lines.append(
                    f"| {d.asset_path[:50]} | {d.dependency_type} | "
                    f"{d.source_node[:25]} | {ext_flag} | {susp_flag} |"
                )
            lines.append("")
    else:
        lines.append("*(无依赖数据)*")
        lines.append("")

    # 维度 4：热路径
    lines.append("---")
    lines.append("## 4. ⚡ 性能热路径")
    lines.append("")
    if report.hot_paths:
        hp = report.hot_paths

        if hp.entry_points:
            lines.append("### 入口事件")
            lines.append("")
            lines.append("| 节点 | Graph | 类型 | 频率 |")
            lines.append("|:---|:---|:---|:---|")
            for ep in hp.entry_points:
                lines.append(f"| {ep.node_title[:30]} | {ep.graph_name[:20]} | {ep.category} | {ep.execution_estimate} |")
            lines.append("")

        if hp.tick_related:
            lines.append("### ⚡ Tick 相关")
            for t in hp.tick_related:
                lines.append(f"- **{t.node_title}** ({t.graph_name}) — {t.cost_note}")
            lines.append("")

        if hp.timer_nodes:
            lines.append("### ⏱ 定时器")
            for t in hp.timer_nodes:
                lines.append(f"- **{t.node_title}** ({t.graph_name}) — {t.execution_estimate}")
            lines.append("")

        if hp.tight_loops:
            lines.append("### 🔄 紧循环")
            for tl in hp.tight_loops:
                lines.append(f"- **{tl.node_title}** ({tl.graph_name}) — {tl.cost_note}")
            lines.append("")

        if hp.heavy_functions:
            lines.append("### 🐌 重函数")
            for hf in hp.heavy_functions:
                lines.append(f"- **{hf.node_title}** ({hf.graph_name}) — {hf.cost_note}")
            lines.append("")

        if hp.estimated_hot_graphs:
            lines.append("### 热图排行")
            lines.append("")
            lines.append("| Graph | 热度评分 |")
            lines.append("|:---|:---:|")
            for gname, cost in hp.estimated_hot_graphs[:5]:
                lines.append(f"| {gname[:50]} | {cost} |")
            lines.append("")

        if hp.recommendations:
            lines.append("### 性能建议")
            for rec in hp.recommendations:
                lines.append(f"- {rec}")
            lines.append("")
    else:
        lines.append("*(无热路径数据)*")
        lines.append("")

    # 警告
    if report.warnings:
        lines.append("---")
        lines.append("## ⚠️ 警告")
        lines.append("")
        for w in report.warnings:
            lines.append(f"- {w}")
        lines.append("")

    lines.append("---")
    lines.append(f"*报告由 analyzer.py 自动生成 · {report.analysis_time}*")
    lines.append("")

    return "\n".join(lines)


# =============================================================
# 便捷函数
# =============================================================


def deep_analyze(blueprint_analysis: DeepAnalysisReport,
                 output_dir: Optional[str] = None) -> Dict[str, str]:
    """一键深度分析 + 生成 JSON/MD 双格式报告。

    Args:
        blueprint_analysis: 来自 blueprint_reader 的 BlueprintAnalysis
        output_dir: 输出目录，默认输出到蓝图自动化 output/

    Returns:
        {"json": path, "md": path, "report": DeepAnalysisReport}
    """
    analyzer = BlueprintDeepAnalyzer(blueprint_analysis)
    report = analyzer.analyze()

    # 默认输出目录
    if output_dir is None:
        output_dir = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "..", "output"
        )
    output_dir = os.path.abspath(output_dir)
    os.makedirs(output_dir, exist_ok=True)

    # 生成文件名
    safe_name = report.blueprint_name.replace("/", "_").replace("\\", "_").replace(" ", "_")
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = os.path.join(output_dir, f"{safe_name}_deep_{timestamp}.json")
    md_path = os.path.join(output_dir, f"{safe_name}_deep_{timestamp}.md")

    # 写入 JSON
    with open(json_path, "w", encoding="utf-8") as f:
        f.write(report.to_json())

    # 写入 Markdown
    md_content = generate_markdown_report(report)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md_content)

    return {"json": json_path, "md": md_path, "report": report}
