#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
analyzer_models.py — 项目分析数据模型
=====================================
从 analyzer.py 拆分出的纯数据模型（dataclass/Enum），零业务逻辑依赖。
"""

import json
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple


# ── 常量 ──────────────────────────────────────────────

DEEP_DIVE_NODE_THRESHOLD = 200
DEEP_DIVE_INHERITANCE_DEPTH = 4
DEEP_DIVE_CIRCULAR = True

BLUEPRINT_CLASS_NAMES = [
    "Blueprint",
    "WidgetBlueprint",
    "AnimBlueprint",
    "BlueprintGeneratedClass",
]


# ── 项目级数据模型 ──────────────────────────────────────

@dataclass
class BlueprintSummary:
    """单张蓝图摘要。"""
    path: str
    name: str
    blueprint_type: str = ""
    parent_class: str = ""
    node_count: int = 0
    variable_count: int = 0
    graph_count: int = 0
    connection_count: int = 0
    interface_count: int = 0
    inheritance_depth: int = 0
    referenced_assets_count: int = 0
    package_path: str = ""
    size_bytes: int = 0
    last_modified: str = ""


@dataclass
class CircularDependency:
    """循环依赖。"""
    cycle: List[str] = field(default_factory=list)
    cycle_type: str = ""
    severity: str = "error"


@dataclass
class CouplingMetrics:
    """耦合度指标。"""
    total_references: int = 0
    cross_package_references: int = 0
    cross_package_ratio: float = 0.0
    top_coupled_pairs: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class DistributionStats:
    """类型分布统计。"""
    total: int = 0
    by_type: Dict[str, int] = field(default_factory=dict)
    by_package: Dict[str, int] = field(default_factory=dict)
    by_parent_class: Dict[str, int] = field(default_factory=dict)


@dataclass
class DeepDiveDetail:
    """深挖分析详情（单张蓝图）。"""
    blueprint_path: str = ""
    reason: str = ""
    node_breakdown: Dict[str, int] = field(default_factory=dict)
    circular_path: List[str] = field(default_factory=list)
    unimplemented_interfaces: List[str] = field(default_factory=list)
    suggestions: List[str] = field(default_factory=list)


@dataclass
class InterfaceUsage:
    """接口使用统计。"""
    interface_name: str = ""
    implemented_by_count: int = 0
    blueprints: List[str] = field(default_factory=list)


@dataclass
class AnalysisResult:
    """完整分析结果。"""
    project_name: str = ""
    project_root: str = "/Game/"
    analyzed_at: str = ""
    distribution: DistributionStats = field(default_factory=DistributionStats)
    top_largest: List[BlueprintSummary] = field(default_factory=list)
    deep_inheritance: List[BlueprintSummary] = field(default_factory=list)
    circular_dependencies: List[CircularDependency] = field(default_factory=list)
    coupling: CouplingMetrics = field(default_factory=CouplingMetrics)
    interface_usage: List[InterfaceUsage] = field(default_factory=list)
    deep_dives: List[DeepDiveDetail] = field(default_factory=list)
    total_blueprints: int = 0
    total_nodes: int = 0
    total_connections: int = 0
    total_elapsed_ms: float = 0.0
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


# ── 深度分析数据模型 ──────────────────────────────────────

class ComplexityRating(Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    VERY_HIGH = "VERY_HIGH"


class SummaryGrade(Enum):
    A = "A"
    B = "B"
    C = "C"
    D = "D"
    F = "F"


@dataclass
class NodeTypeStats:
    node_class: str
    category: str = ""
    count: int = 0
    percentage: float = 0.0
    is_pure_count: int = 0
    is_latent_count: int = 0
    avg_pins: float = 0.0
    graphs_present: List[str] = field(default_factory=list)


@dataclass
class GraphComplexity:
    graph_name: str
    graph_type: str = ""
    node_count: int = 0
    connection_count: int = 0
    event_count: int = 0
    branch_count: int = 0
    sequence_count: int = 0
    loop_count: int = 0
    delay_count: int = 0
    nesting_depth: int = 0
    cyclomatic_complexity: int = 0
    density_score: float = 0.0
    complexity_rating: str = ""


@dataclass
class ComplexityScore:
    total_node_count: int = 0
    total_connection_count: int = 0
    total_branch_count: int = 0
    total_loop_count: int = 0
    total_delay_count: int = 0
    max_nesting_depth: int = 0
    avg_cyclomatic_complexity: float = 0.0
    score: float = 0.0
    overall_rating: str = ""
    per_graph: List[GraphComplexity] = field(default_factory=list)


@dataclass
class DependencyNode:
    asset_path: str
    dependency_type: str = ""
    source_node: str = ""
    source_graph: str = ""
    is_external: bool = False
    is_suspicious: bool = False


@dataclass
class DependencyAnalysis:
    parent_class: str = ""
    interfaces: List[str] = field(default_factory=list)
    direct_dependencies: List[DependencyNode] = field(default_factory=list)
    circular_chains: List[List[str]] = field(default_factory=list)
    external_count: int = 0
    internal_count: int = 0
    engine_ref_count: int = 0
    max_dependency_depth: int = 0


@dataclass
class HotPathNode:
    node_title: str
    graph_name: str = ""
    category: str = ""
    execution_estimate: str = ""
    downstream_count: int = 0
    pin_count: int = 0
    cost_note: str = ""


@dataclass
class HotPathAnalysis:
    entry_points: List[HotPathNode] = field(default_factory=list)
    tick_related: List[HotPathNode] = field(default_factory=list)
    timer_nodes: List[HotPathNode] = field(default_factory=list)
    heavy_functions: List[HotPathNode] = field(default_factory=list)
    tight_loops: List[HotPathNode] = field(default_factory=list)
    delay_nodes: List[HotPathNode] = field(default_factory=list)
    estimated_hot_graphs: List[tuple] = field(default_factory=list)
    recommendations: List[str] = field(default_factory=list)


@dataclass
class DeepAnalysisReport:
    blueprint_name: str
    asset_path: str = ""
    analysis_time: str = ""
    degrade_level: str = ""
    node_stats: List[NodeTypeStats] = field(default_factory=list)
    complexity: Optional[ComplexityScore] = None
    dependencies: Optional[DependencyAnalysis] = None
    hot_paths: Optional[HotPathAnalysis] = None
    summary_score: int = 0
    summary_grade: str = ""
    recommendations: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(_report_to_dict(self), indent=indent,
                          ensure_ascii=False, default=str)


def _report_to_dict(report: DeepAnalysisReport) -> Dict[str, Any]:
    return asdict(report)
