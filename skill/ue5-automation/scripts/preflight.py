#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
preflight.py — UE 蓝图批量生成前预检脚本
==========================================
基于 error_patterns.json 的已知陷阱，逐项检查蓝图 JSON 描述。
输出：PASS（全部通过）/ FAIL（列出不通过项+位置）/ WARN（有风险但可继续）
不通过 → 不进入生成阶段。

用法：
    python preflight.py <blueprint.json>
    python preflight.py --batch <manifest.json>
    python preflight.py <blueprint.json> --json --exit-code

依赖：error_patterns.json (skills/ue5-automation/config/error_patterns.json)
      源自 pitfalls.md 8 条已知陷阱 + 7 条 UE5 日志错误码
作者：dev（代码开发 Agent）
日期：2026-07-22（从 output/preflight.py 迁移至 skills/ue5-automation/scripts/）
"""

import json
import os
import sys
import argparse
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Dict, Any, Optional, Tuple
from datetime import datetime

# ── 路径常量（A-001 修复：消除硬编码绝对路径）───────────

_DEFAULT_WORKSPACE = os.environ.get(
    "COPAW_WORKSPACE",
    os.path.join(os.path.expanduser("~"), ".copaw", "workspaces", "default")
)

# T-20260915-UE5PKG-STANDALONE：优先按 __file__ 上溯定位。
# 发布包/独立部署布局 = <pkg>\skill\ue5-automation\{scripts,config}\，
# 陌生机器上没有 ~/.copaw/workspaces/... 层级，旧逻辑会指向不存在的路径导致
# error_patterns.json 加载失败。config/ 不存在时才回退到工作区布局。
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))   # <skill>/scripts
_SKILL_LOCAL = os.path.dirname(_SCRIPT_DIR)                # <skill>
_WORKSPACE_SKILL = os.path.join(_DEFAULT_WORKSPACE, "skills", "ue5-automation")
SKILL_DIR = (_SKILL_LOCAL
             if os.path.isdir(os.path.join(_SKILL_LOCAL, "config"))
             else _WORKSPACE_SKILL)
CONFIG_DIR = os.path.join(SKILL_DIR, "config")
SCRIPTS_DIR = os.path.join(SKILL_DIR, "scripts")
ERROR_PATTERNS_PATH = os.path.join(CONFIG_DIR, "error_patterns.json")

# ── 常量 ──────────────────────────────────────────────

MEMORY_WARN_THRESHOLD_MB = 1024
MEMORY_CRITICAL_THRESHOLD_MB = 2048

PITFALL_MAP = {
    "P01": {"name": "ConnectPins 假阳性", "severity": "🔴"},
    "P02": {"name": "CONNECTPINS 静默失败", "severity": "🔴"},
    "P03": {"name": "CDO 编译冲突", "severity": "🔴"},
    "P04": {"name": "CDO_BEFORE_COMPILE", "severity": "🔴"},
    "P05": {"name": "bOverrideFunction 未设", "severity": "🔴"},
    "P06": {"name": "UBT 998 内存崩溃", "severity": "🔴"},
    "P07": {"name": "ASSETTOOLS_INSTEADOF_KISMET", "severity": "🟡"},
    "P08": {"name": "CROSS_CALL_REF_LOSS", "severity": "🟡"},
}


class Result(Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    WARN = "WARN"


@dataclass
class CheckResult:
    pitfall_id: str
    pitfall_name: str
    result: Result
    detail: str = ""
    location: str = ""
    severity: str = ""


@dataclass
class PreflightReport:
    blueprint_name: str
    results: List[CheckResult] = field(default_factory=list)

    @property
    def overall(self) -> Result:
        if any(r.result == Result.FAIL for r in self.results):
            return Result.FAIL
        if any(r.result == Result.WARN for r in self.results):
            return Result.WARN
        return Result.PASS

    @property
    def failures(self) -> List[CheckResult]:
        return [r for r in self.results if r.result == Result.FAIL]

    @property
    def warnings(self) -> List[CheckResult]:
        return [r for r in self.results if r.result == Result.WARN]


# ── 辅助函数 ──────────────────────────────────────────

def _get_operations(blueprint: dict) -> list:
    """兼容两种 JSON 结构：顶级 operations 或 scripts[*].operations"""
    ops = list(blueprint.get('operations', []))
    if not ops:
        for script in blueprint.get('scripts', []):
            ops.extend(script.get('operations', []))
    return ops


def _find_ops_by_type(operations: list, op_type: str) -> List[Tuple[int, dict]]:
    return [(i, op) for i, op in enumerate(operations) if op.get("type") == op_type]


def _find_nodes_by_type(nodes: list, node_type: str) -> List[dict]:
    return [n for n in nodes if n.get("type") == node_type]


# ── P01: ConnectPins 假阳性 ───────────────────────────

def check_p01_connect_pins_false_positive(blueprint: dict) -> CheckResult:
    operations = _get_operations(blueprint)
    connect_ops = _find_ops_by_type(operations, 'connect_pins')
    for idx, op in connect_ops:
        follow_up = op.get('verify_linked', False)
        fallback = op.get('use_makelinkto_fallback', False)
        if not follow_up or not fallback:
            return CheckResult(
                pitfall_id='P01',
                pitfall_name=PITFALL_MAP['P01']['name'],
                result=Result.FAIL,
                detail=f'connect_pins #[{idx}] 缺少 LinkedTo.Contains 验证或 MakeLinkTo 兜底',
                location=f'operations[{idx}]',
                severity=PITFALL_MAP['P01']['severity']
            )
    return CheckResult(
        pitfall_id='P01', pitfall_name=PITFALL_MAP['P01']['name'],
        result=Result.PASS, detail='所有 connect_pins 均有验证 + 兜底'
    )


# ── P02: CONNECTPINS 静默失败 ─────────────────────────

def check_p02_connect_pins_silent_fail(blueprint: dict) -> CheckResult:
    operations = _get_operations(blueprint)
    connect_ops = _find_ops_by_type(operations, 'connect_pins')
    for idx, op in connect_ops:
        src_pin = op.get('src_pin', '')
        dst_pin = op.get('dst_pin', '')
        pre_check = op.get('verify_pins_exist', False)
        if not pre_check:
            return CheckResult(
                pitfall_id='P02',
                pitfall_name=PITFALL_MAP['P02']['name'],
                result=Result.FAIL,
                detail=f'connect_pins #[{idx}] ({src_pin} -> {dst_pin}) 缺少引脚存在性预检',
                location=f'operations[{idx}]',
                severity=PITFALL_MAP['P02']['severity']
            )
    return CheckResult(
        pitfall_id='P02', pitfall_name=PITFALL_MAP['P02']['name'],
        result=Result.PASS, detail='所有 connect_pins 均有引脚存在性预检'
    )


# ── P03: CDO 编译冲突 ─────────────────────────────────

def check_p03_cdo_compile_conflict(blueprint: dict) -> CheckResult:
    operations = _get_operations(blueprint)
    compiled = False
    for idx, op in enumerate(operations):
        op_type = op.get('type', '')
        if op_type in ('compile_blueprint',):
            compiled = True
        if op_type in ('set_cdo_property', 'modify_default_object') and not compiled:
            return CheckResult(
                pitfall_id='P03',
                pitfall_name=PITFALL_MAP['P03']['name'],
                result=Result.FAIL,
                detail=f'CDO 修改 #[{idx}] 在 compile_blueprint 之前执行',
                location=f'operations[{idx}]',
                severity=PITFALL_MAP['P03']['severity']
            )
    return CheckResult(
        pitfall_id='P03', pitfall_name=PITFALL_MAP['P03']['name'],
        result=Result.PASS, detail='CDO 修改在编译之后执行（时序正确）'
    )


# ── P04: CDO_BEFORE_COMPILE ───────────────────────────

def check_p04_cdo_before_compile(blueprint: dict) -> CheckResult:
    operations = _get_operations(blueprint)
    cdo_ops = [(i, op) for i, op in enumerate(operations)
               if op.get('type') in ('set_cdo_property', 'modify_default_object')]
    for idx, op in cdo_ops:
        if not op.get('check_generated_class', False):
            return CheckResult(
                pitfall_id='P04',
                pitfall_name=PITFALL_MAP['P04']['name'],
                result=Result.FAIL,
                detail=f'CDO 操作 #[{idx}] 缺少 GeneratedClass 存在性 guard',
                location=f'operations[{idx}]',
                severity=PITFALL_MAP['P04']['severity']
            )
    return CheckResult(
        pitfall_id='P04', pitfall_name=PITFALL_MAP['P04']['name'],
        result=Result.PASS, detail='CDO 操作均有 GeneratedClass guard'
    )


# ── P05: bOverrideFunction 未设 ───────────────────────

def check_p05_b_override_function(blueprint: dict) -> CheckResult:
    nodes = blueprint.get('nodes', [])
    event_nodes = _find_nodes_by_type(nodes, 'event')
    for node in event_nodes:
        if not node.get('bOverrideFunction', False):
            node_title = node.get('title', 'UnknownEvent')
            return CheckResult(
                pitfall_id='P05',
                pitfall_name=PITFALL_MAP['P05']['name'],
                result=Result.FAIL,
                detail=f'事件节点 "{node_title}" 的 bOverrideFunction 未设为 True',
                location=f'nodes.{node_title}',
                severity=PITFALL_MAP['P05']['severity']
            )
    if not event_nodes:
        return CheckResult(
            pitfall_id='P05', pitfall_name=PITFALL_MAP['P05']['name'],
            result=Result.PASS, detail='无事件节点，无需检查'
        )
    return CheckResult(
        pitfall_id='P05', pitfall_name=PITFALL_MAP['P05']['name'],
        result=Result.PASS,
        detail=f'所有 {len(event_nodes)} 个事件节点 bOverrideFunction = True'
    )


# ── P06: UBT 998 内存崩溃 ─────────────────────────────

def check_p06_ubt_998_memory(blueprint: dict) -> CheckResult:
    metadata = blueprint.get('metadata', {})
    estimated_mem_mb = metadata.get('estimated_memory_mb', 0)
    if estimated_mem_mb > MEMORY_CRITICAL_THRESHOLD_MB:
        return CheckResult(
            pitfall_id='P06',
            pitfall_name=PITFALL_MAP['P06']['name'],
            result=Result.FAIL,
            detail=f'预估内存 {estimated_mem_mb}MB > {MEMORY_CRITICAL_THRESHOLD_MB}MB 严重阈值',
            location='metadata.estimated_memory_mb',
            severity=PITFALL_MAP['P06']['severity']
        )
    if estimated_mem_mb > MEMORY_WARN_THRESHOLD_MB:
        return CheckResult(
            pitfall_id='P06',
            pitfall_name=PITFALL_MAP['P06']['name'],
            result=Result.WARN,
            detail=f'预估内存 {estimated_mem_mb}MB > {MEMORY_WARN_THRESHOLD_MB}MB 警告阈值，建议启用手动编译方案',
            location='metadata.estimated_memory_mb',
            severity=PITFALL_MAP['P06']['severity']
        )
    return CheckResult(
        pitfall_id='P06', pitfall_name=PITFALL_MAP['P06']['name'],
        result=Result.PASS,
        detail=f'预估内存 {estimated_mem_mb}MB 在安全范围内'
    )


# ── P07: ASSETTOOLS_INSTEADOF_KISMET ──────────────────

def check_p07_assettools_insteadof_kismet(blueprint: dict) -> CheckResult:
    operations = _get_operations(blueprint)
    create_ops = _find_ops_by_type(operations, 'create_blueprint')
    for idx, op in create_ops:
        method = op.get('method', '').lower()
        if 'assettools' in method:
            return CheckResult(
                pitfall_id='P07',
                pitfall_name=PITFALL_MAP['P07']['name'],
                result=Result.FAIL,
                detail=f'create_blueprint #[{idx}] 使用 AssetTools 方法，应改用 FKismetEditorUtilities',
                location=f'operations[{idx}]',
                severity=PITFALL_MAP['P07']['severity']
            )
    return CheckResult(
        pitfall_id='P07', pitfall_name=PITFALL_MAP['P07']['name'],
        result=Result.PASS, detail='创建蓝图使用 Kismet 路径'
    )


# ── P08: CROSS_CALL_REF_LOSS ──────────────────────────

def check_p08_cross_call_ref_loss(blueprint: dict) -> CheckResult:
    operations = _get_operations(blueprint)
    scripts = blueprint.get('scripts', [])
    if len(scripts) > 1:
        return CheckResult(
            pitfall_id='P08',
            pitfall_name=PITFALL_MAP['P08']['name'],
            result=Result.FAIL,
            detail=f'蓝图分 {len(scripts)} 个脚本块，存在跨调用引用丢失风险。应合并为单个脚本块。',
            location='scripts',
            severity=PITFALL_MAP['P08']['severity']
        )
    ref_ops = _find_ops_by_type(operations, 'use_cached_reference')
    for idx, op in ref_ops:
        if op.get('cross_session', False):
            return CheckResult(
                pitfall_id='P08',
                pitfall_name=PITFALL_MAP['P08']['name'],
                result=Result.FAIL,
                detail=f'use_cached_reference #[{idx}] 标记为跨会话引用，有引用丢失风险',
                location=f'operations[{idx}]',
                severity=PITFALL_MAP['P08']['severity']
            )
    return CheckResult(
        pitfall_id='P08', pitfall_name=PITFALL_MAP['P08']['name'],
        result=Result.PASS, detail='所有操作在同一脚本块内，无跨调用引用'
    )


# ── 检查注册表 ────────────────────────────────────────

ALL_CHECKS = [
    check_p01_connect_pins_false_positive,
    check_p02_connect_pins_silent_fail,
    check_p03_cdo_compile_conflict,
    check_p04_cdo_before_compile,
    check_p05_b_override_function,
    check_p06_ubt_998_memory,
    check_p07_assettools_insteadof_kismet,
    check_p08_cross_call_ref_loss,
]


# ── 运行引擎 ───────────────────────────────────────────

def run_preflight(blueprint: dict) -> PreflightReport:
    bp_name = blueprint.get('name', blueprint.get('blueprint_name', 'Unknown'))
    report = PreflightReport(blueprint_name=bp_name)
    for check_fn in ALL_CHECKS:
        try:
            result = check_fn(blueprint)
            report.results.append(result)
        except Exception as e:
            fn_name = check_fn.__name__
            pitfall_id = fn_name.split('_')[1].upper()
            report.results.append(CheckResult(
                pitfall_id=pitfall_id,
                pitfall_name=PITFALL_MAP.get(pitfall_id, {}).get('name', fn_name),
                result=Result.FAIL,
                detail=f'检查异常: {e}',
                location=fn_name,
                severity='🔴'
            ))
    return report


def run_batch(manifest) -> Dict[str, PreflightReport]:
    """批量预检。

    v2.20 修复（P2）：manifest 顶层为 JSON 数组（常见批量形态）时，
    旧实现 `manifest.get('blueprints', [manifest])` 对 list 调 .get()
    → AttributeError 裸 traceback。现在两种顶层形态都支持。
    """
    if isinstance(manifest, list):
        blueprints = manifest
    elif isinstance(manifest, dict):
        blueprints = manifest.get('blueprints', [manifest])
    else:
        raise ValueError("manifest 必须是 JSON 对象或数组")
    reports = {}
    for bp in blueprints:
        report = run_preflight(bp)
        reports[report.blueprint_name] = report
    return reports


# ── 输出格式化 ─────────────────────────────────────────

def format_report(report: PreflightReport, use_color: bool = True) -> str:
    RED = '\033[91m' if use_color else ''
    YELLOW = '\033[93m' if use_color else ''
    GREEN = '\033[92m' if use_color else ''
    RESET = '\033[0m' if use_color else ''
    BOLD = '\033[1m' if use_color else ''

    lines = []
    lines.append(f"{'='*60}")
    lines.append(f"  Preflight Report: {report.blueprint_name}")
    lines.append(f"  Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"  Checks: {len(report.results)} total")

    overall = report.overall
    if overall == Result.PASS:
        lines.append(f"  Result: {GREEN}{BOLD}PASS{RESET}")
    elif overall == Result.WARN:
        lines.append(f"  Result: {YELLOW}{BOLD}WARN{RESET}")
    else:
        lines.append(f"  Result: {RED}{BOLD}FAIL{RESET}")
    lines.append(f"{'='*60}")

    for r in report.results:
        icon = {Result.PASS: f'{GREEN}PASS{RESET}',
                Result.FAIL: f'{RED}FAIL{RESET}',
                Result.WARN: f'{YELLOW}WARN{RESET}'}[r.result]
        lines.append(f"  {icon} [{r.pitfall_id}] {r.pitfall_name}")
        if r.result != Result.PASS:
            lines.append(f"      Detail : {r.detail}")
            if r.location:
                lines.append(f"      Location: {r.location}")

    if report.failures:
        lines.append(f"\n{RED}{BOLD}FAIL: {len(report.failures)}/{len(report.results)} checks failed.{RESET}")
        lines.append(f"  蓝图 {report.blueprint_name} 不通过预检，禁止进入生成阶段。")
    elif report.warnings:
        lines.append(f"\n{YELLOW}WARN: {len(report.warnings)}/{len(report.results)} checks with warnings.{RESET}")
        lines.append(f"  蓝图 {report.blueprint_name} 可继续，但请注意风险项。")
    else:
        lines.append(f"\n{GREEN}All {len(report.results)} checks passed.{RESET}")

    return '\n'.join(lines)


def report_to_json(report: PreflightReport) -> dict:
    return {
        'blueprint_name': report.blueprint_name,
        'timestamp': datetime.now().isoformat(),
        'overall': report.overall.value,
        'total_checks': len(report.results),
        'passed': sum(1 for r in report.results if r.result == Result.PASS),
        'failed': len(report.failures),
        'warnings': len(report.warnings),
        'results': [
            {
                'pitfall_id': r.pitfall_id,
                'pitfall_name': r.pitfall_name,
                'result': r.result.value,
                'detail': r.detail,
                'location': r.location,
                'severity': 'CRITICAL' if r.severity == '🔴' else 'MEDIUM',
            }
            for r in report.results
        ]
    }


# ── CLI 入口 ───────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='UE 蓝图批量生成前预检脚本（基于 error_patterns.json 8 个已知陷阱）'
    )
    parser.add_argument('input', nargs='?', help='蓝图 JSON 描述文件路径')
    parser.add_argument('--batch', help='批量 manifest JSON 文件路径')
    parser.add_argument('--json', action='store_true', help='以 JSON 格式输出结果')
    parser.add_argument('--exit-code', action='store_true', help='FAIL 时返回非零退出码')
    parser.add_argument('--no-color', action='store_true', help='禁用终端颜色输出')

    args = parser.parse_args()

    # v2.20 修复（P2）：默认仅在交互终端启用 ANSI 颜色 —— 重定向/CI/旧版 conhost
    #   下原样打印 \x1b[... 垃圾字符。
    _use_color = (not args.no_color) and sys.stdout.isatty()
    if _use_color and os.name == "nt":
        os.system("")  # 在 Win10+ conhost 上启用 VT 处理

    if not args.input and not args.batch:
        parser.print_help()
        sys.exit(2)

    try:
        if args.batch:
            with open(args.batch, 'r', encoding='utf-8') as f:
                manifest = json.load(f)
            reports = run_batch(manifest)
            if args.json:
                output = {
                    'batch_file': args.batch,
                    'blueprints': {
                        name: report_to_json(r) for name, r in reports.items()
                    }
                }
                print(json.dumps(output, indent=2, ensure_ascii=False))
            else:
                for name, report in reports.items():
                    print(format_report(report, use_color=_use_color))
                    print()

            if args.exit_code:
                for name, r in reports.items():
                    if r.overall == Result.FAIL:
                        sys.exit(1)
            sys.exit(0)
        else:
            with open(args.input, 'r', encoding='utf-8') as f:
                blueprint = json.load(f)
            report = run_preflight(blueprint)
            if args.json:
                print(json.dumps(report_to_json(report), indent=2, ensure_ascii=False))
            else:
                print(format_report(report, use_color=_use_color))

            if args.exit_code and report.overall == Result.FAIL:
                sys.exit(1)
            sys.exit(0)

    except FileNotFoundError as e:
        print(f'Error: File not found - {e}', file=sys.stderr)
        sys.exit(2)
    except json.JSONDecodeError as e:
        print(f'Error: Invalid JSON - {e}', file=sys.stderr)
        sys.exit(2)
    except (OSError, ValueError) as e:
        print(f'Error: {e}', file=sys.stderr)
        sys.exit(2)


if __name__ == '__main__':
    main()
