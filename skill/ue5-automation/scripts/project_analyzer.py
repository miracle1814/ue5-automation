#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
project_analyzer.py — 向后兼容导出模块（A-008 修复）
==================================================
原为 analyzer.py 的 1914 行重复代码。
修复后改为从 analyzer.py 导入核心类，消除代码重复。

用法（向后兼容）：
    from project_analyzer import ProjectAnalyzer, DeepAnalysisReport, ...

作者：dev（代码开发 Agent）
日期：2026-07-22 / 修复 2026-07-23
"""

# 从 analyzer.py 复用所有公共 API
from analyzer import (
    ProjectAnalyzer,
    BlueprintDeepAnalyzer,
    DeepAnalysisReport,
    NodeTypeStats,
    GraphComplexity,
    ComplexityScore,
    ComplexityRating,
    DependencyAnalysis,
    DependencyNode,
    HotPathAnalysis,
    HotPathNode,
    SummaryGrade,
    BlueprintSummary,
    CircularDependency,
    CouplingMetrics,
    DistributionStats,
    DeepDiveDetail,
    InterfaceUsage,
    AnalysisResult,
    analyze_from_reader_output,
    analyze_live,
    deep_analyze,
    generate_markdown_report,
)

# 向后兼容别名
__all__ = [
    "ProjectAnalyzer",
    "BlueprintDeepAnalyzer",
    "DeepAnalysisReport",
    "NodeTypeStats",
    "GraphComplexity",
    "ComplexityScore",
    "ComplexityRating",
    "DependencyAnalysis",
    "DependencyNode",
    "HotPathAnalysis",
    "HotPathNode",
    "SummaryGrade",
    "BlueprintSummary",
    "CircularDependency",
    "CouplingMetrics",
    "DistributionStats",
    "DeepDiveDetail",
    "InterfaceUsage",
    "AnalysisResult",
    "analyze_from_reader_output",
    "analyze_live",
    "deep_analyze",
    "generate_markdown_report",
]
