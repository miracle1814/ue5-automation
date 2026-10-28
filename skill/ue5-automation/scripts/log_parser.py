#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# 🔴 KB 语义搜索结果：正则 Markdown 容错模式、UE 经验提炼 22 条铁律
# 🔴 红线：无硬编码绝对路径；datetime.now() 用于时间戳；写入后验证

"""
log_parser.py — UE 日志解析 + 错误诊断引擎
===========================================
解析 UE5 日志，匹配 error_patterns.json 15 条规则，输出结构化诊断结果。

能力矩阵：
  - 日志解析：提取时间戳/错误码/堆栈/关联蓝图路径
  - 模式匹配：正则/Grok 风格匹配 error_patterns
  - 诊断输出：根因 + 修复方案 + 自动修复（如可行）
  - 未知错误 → 生成诊断卡片草稿 → QA+dev补全后入库

输入：config/error_patterns.json（15 条规则）、UE 日志文件
输出：diagnosis_result.json

上下文：config/error_patterns.json

作者：dev（代码开发 Agent）
日期：2026-07-22
"""

import os
import re
import sys
import json
import time
import argparse

# ── T-20260727-BLOCK-1: ReDoS 正则注入防护 ──
# 检测嵌套量词（如 (a+)+, (a*)*, (a+)* 等），防止 CPU 100% DoS
_REDOS_DANGEROUS = re.compile(
    r'\(\s*[^()]*[+*]\s*\)\s*[+*]'     # 嵌套量词: (X+)+, (X*)*, (X+)*, (X*)+
    r'|\(\s*\?\s*=\s*[^()]*[+*]\s*\)'   # 先行断言含量词
    r'|\([^()]+\|[^()]+\)\s*[+*]'       # 多选分支 + 量词: (a|b)+
    r'|\([^()]*\)\s*\{[^}]*,\s*\d{3,}\s*\}'  # 超大重复次数 >=100
)
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple
from enum import Enum


_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PARENT_DIR = os.path.dirname(_THIS_DIR)
_DEFAULT_PATTERNS = os.path.join(_PARENT_DIR, "config", "error_patterns.json")


class Severity(Enum):
    CRITICAL = "critical"
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


@dataclass
class LogEntry:
    raw: str = ""
    timestamp: str = ""
    log_level: str = ""
    category: str = ""
    message: str = ""
    line_number: int = 0
    blueprint_path: str = ""
    error_code: str = ""


@dataclass
class ErrorPattern:
    id: str
    title: str
    error_code: str
    category: str
    severity: Severity = Severity.ERROR
    pattern: dict = field(default_factory=dict)
    auto_fix: bool = False
    fix_action: str = ""
    fix_code_example: str = ""
    source: str = ""
    related_patterns: list = field(default_factory=list)
    log_regex: str = ""

    def __post_init__(self):
        if isinstance(self.severity, str):
            self.severity = Severity(self.severity)


@dataclass
class MatchResult:
    pattern_id: str
    error_code: str
    log_line: int
    message: str
    severity: Severity = Severity.ERROR
    auto_fix: bool = False
    fix_action: str = ""
    fix_code_example: str = ""
    blueprint_path: str = ""
    confidence: float = 1.0
    matched_text: str = ""


@dataclass
class UnknownError:
    error_code: str = ""
    log_lines: list = field(default_factory=list)
    suspected_category: str = ""
    suggested_pattern: str = ""
    symptoms: list = field(default_factory=list)


@dataclass
class DiagnosisReport:
    log_file: str = ""
    analyzed_at: str = ""
    total_lines: int = 0
    error_lines: int = 0
    warning_lines: int = 0
    matches: list = field(default_factory=list)
    unknown_errors: list = field(default_factory=list)
    by_severity: dict = field(default_factory=dict)
    by_category: dict = field(default_factory=dict)
    by_pattern: dict = field(default_factory=dict)
    auto_fix_candidates: list = field(default_factory=list)
    elapsed_ms: float = 0.0


# ── Log line regex ──

LOG_LINE_PATTERN = re.compile(
    r'\[(\d{4}\.\d{2}\.\d{2}-\d{2}\.\d{2}\.\d{2}:\d{3})\]'
    r'\[\s*(\d+)\]'
    r'(\w+):\s*'
    r'(.*)'
)


def parse_log_line(line: str, line_num: int = 0) -> Optional[LogEntry]:
    # T-20260726-HEALTHFIX #5 C2(2): Null/ANSI 透传防护
    # 1) ANSI 控制序列剥离（颜色/光标/模式切换等）
    line = re.sub(r'\x1b\[[0-9;]*[mGKHfABCDEsuJ]', '', line)
    # 2) Null 字符（\x00）替换为空，避免后续字符串处理炸裂
    line = line.replace('\x00', '')
    m = LOG_LINE_PATTERN.match(line.strip())
    if not m:
        loose = re.match(r'\[(\d{4}\.\d{2}\.\d{2}-\d{2}\.\d{2}\.\d{2}:\d{3})\]', line)
        if not loose:
            return None
        return LogEntry(raw=line.rstrip(), timestamp=loose.group(1), message=line.rstrip(), line_number=line_num)
    ts = m.group(1)
    category = m.group(3)
    message = m.group(4)
    log_level = "Log"
    if re.search(r'(?i)\b(error|Err|E-|critical)\b', message) or re.search(r'(?i)exception|access.violation', message):
        log_level = "Error"
    elif re.search(r'(?i)\b(warning|warn|W-)\b', message):
        log_level = "Warning"
    elif re.search(r'(?i)(LogBlueprintUserMessages|or stat|Heavy)', category + " " + message):
        log_level = "Warning"
    # Chinese: 错误/警告
    elif re.search(r'(错误|失败)', message):
        log_level = "Error"
    elif re.search(r'(警告|提醒)', message):
        log_level = "Warning"
    bp_path = ""
    bp_m = re.search(r'(/Game/[\w/]+(?:BP_[\w]+|[\w]+_C|\w+))', message)  # C-007: 放宽匹配，覆盖非BP_前缀蓝图
    if bp_m:
        bp_path = bp_m.group(1)
    error_code = ""
    ec_m = re.search(r'(E-(?:BP|GC|UBT)-[\w-]+)', message)
    if ec_m:
        error_code = ec_m.group(1)
    return LogEntry(raw=line.rstrip(), timestamp=ts, log_level=log_level, category=category, message=message, line_number=line_num, blueprint_path=bp_path, error_code=error_code)


# ── UELogParser ──

class UELogParser:
    """UE5 log parsing engine."""

    def __init__(self):
        self.entries: list = []
        self.patterns: list = []
        self._pattern_regexes: dict = {}

    def load_log(self, log_path: str, max_size_mb: int = 100) -> int:
        # T-20260726-HEALTHFIX #5 C2(3): 超大日志无上限防护
        if max_size_mb and os.path.getsize(log_path) > max_size_mb * 1024 * 1024:
            raise ValueError(
                "log file too large: {} bytes > {} MB limit (use --max-size to override)".format(
                    os.path.getsize(log_path), max_size_mb
                )
            )
        self.entries.clear()
        # T-20260726-HEALTHFIX #5 C2(4): GBK 静默损坏防护
        # 之前 errors="replace" 会把无法解码的字节替换成 U+FFFD，破坏日志可读性
        # 现在按"UTF-8 → GBK → latin-1"三段式尝试，命中即用，避免静默替换
        raw = None
        for enc in ("utf-8", "gbk", "latin-1"):
            try:
                with open(log_path, "r", encoding=enc) as f:
                    raw = f.read()
                break
            except UnicodeDecodeError:
                continue
        if raw is None:
            # 三种编码全部失败（极少见：混合编码或二进制日志）
            raise ValueError("log file decoding failed for utf-8/gbk/latin-1: " + log_path)
        for i, line in enumerate(raw.splitlines(), 1):
            entry = parse_log_line(line, i)
            if entry:
                self.entries.append(entry)
        return len(self.entries)

    def load_log_text(self, text: str) -> int:
        self.entries.clear()
        for i, line in enumerate(text.split("\n"), 1):
            entry = parse_log_line(line, i)
            if entry:
                self.entries.append(entry)
        return len(self.entries)

    def load_patterns(self, patterns_path: str = None) -> int:
        patterns_path = patterns_path or _DEFAULT_PATTERNS
        self.patterns.clear()
        self._pattern_regexes.clear()
        with open(patterns_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        for p in data.get("patterns", []):
            ep = ErrorPattern(
                id=p.get("id", ""),
                title=p.get("title", ""),
                error_code=p.get("error_code", ""),
                category=p.get("category", ""),
                severity=Severity(p.get("severity", "error")),
                pattern=p.get("pattern", {}),
                auto_fix=p.get("auto_fix", False),
                fix_action=p.get("fix_action", ""),
                fix_code_example=p.get("fix_code_example", ""),
                source=p.get("source", ""),
                related_patterns=p.get("related_patterns", []),
                log_regex=p.get("pattern", {}).get("log_regex", ""),
            )
            self.patterns.append(ep)
            if ep.log_regex and ep.log_regex != ".*":
                # T-20260727-BLOCK-1: ReDoS 检测 — 拒绝嵌套量词等危险模式
                if _REDOS_DANGEROUS.search(ep.log_regex):
                    import sys
                    print(f"[log_parser] WARNING: ReDoS pattern rejected for {ep.id}: {ep.log_regex[:80]}",
                          file=sys.stderr)
                    continue
                try:
                    self._pattern_regexes[ep.id] = re.compile(ep.log_regex, re.IGNORECASE)
                except re.error:
                    pass
        return len(self.patterns)

    def diagnose(self) -> DiagnosisReport:
        t0 = time.time()
        report = DiagnosisReport(
            log_file="", analyzed_at=datetime.now(timezone.utc).isoformat(),
            total_lines=len(self.entries),
        )
        error_entries = [e for e in self.entries if e.log_level in ("Error", "Warning")]
        report.error_lines = sum(1 for e in self.entries if e.log_level == "Error")
        report.warning_lines = sum(1 for e in self.entries if e.log_level == "Warning")
        matched_lines = set()
        for entry in error_entries:
            match = self._match_entry(entry)
            if match:
                report.matches.append(match)
                matched_lines.add(entry.line_number)
                report.by_severity[match.severity.value] = report.by_severity.get(match.severity.value, 0) + 1
                report.by_category[match.pattern_id] = report.by_category.get(match.pattern_id, 0) + 1
                report.by_pattern[match.error_code] = report.by_pattern.get(match.error_code, 0) + 1
                if match.auto_fix:
                    report.auto_fix_candidates.append(match)
        unmatched = [e for e in error_entries if e.line_number not in matched_lines]
        if unmatched:
            report.unknown_errors.append(self._build_unknown_error(unmatched))
        report.elapsed_ms = (time.time() - t0) * 1000
        return report

    def _match_entry(self, entry):
        """Collect ALL matching patterns, return the best (highest confidence)."""
        msg = entry.message
        full_line = entry.raw
        candidates = []
        for ep in self.patterns:
            # 3.2（T-20260914-UE5BRIDGE-BUILD-HOLD-FIX）：类别排除 —— 与模式语义
            #   无关的日志类别（如 LogPackageName / LogLinker）直接跳过，抑制宽正则
            #   误报（PTN-014 曾被 LogPackageName 的 "Please use" 误命中）。
            _excl = set(ep.pattern.get("exclude_categories", []) or [])
            if _excl and str(getattr(entry, "category", "") or "") in _excl:
                continue
            regex = self._pattern_regexes.get(ep.id)
            if regex:
                if regex.search(full_line) or regex.search(msg):
                    candidates.append((0.95, ep))
                    continue
            if ep.error_code and entry.error_code:
                if ep.error_code == entry.error_code:
                    candidates.append((0.90, ep))
                    continue
            symptom = ep.pattern.get("symptom", "")
            if symptom and len(symptom) > 10:
                keywords = self._extract_keywords(symptom)
                hits = sum(1 for kw in keywords if kw.lower() in msg.lower())
                if hits >= max(2, len(keywords) // 3):
                    candidates.append((0.60, ep))
                    continue
            root_cause = ep.pattern.get("root_cause", "")
            if root_cause and len(root_cause) > 10:
                keywords = self._extract_keywords(root_cause)
                hits = sum(1 for kw in keywords if kw.lower() in msg.lower())
                if hits >= max(3, len(keywords) // 3):
                    candidates.append((0.50, ep))
                    continue
        if not candidates:
            return None
        candidates.sort(key=lambda x: x[0], reverse=True)
        best_conf, best_ep = candidates[0]
        return self._build_match(best_ep, entry, best_conf)

    def _build_match(self, ep, entry, confidence):
        return MatchResult(
            pattern_id=ep.id, error_code=ep.error_code,
            log_line=entry.line_number, message=entry.message,
            severity=ep.severity, auto_fix=ep.auto_fix,
            fix_action=ep.fix_action, fix_code_example=ep.fix_code_example,
            blueprint_path=entry.blueprint_path or "",
            confidence=confidence, matched_text=entry.raw[:200],
        )

    @staticmethod
    def _extract_keywords(text, min_len=4):
        words = []
        eng = re.findall(r'[A-Za-z_]{4,}', text)
        words.extend(eng)
        cn = re.findall(r'[\u4e00-\u9fff]{2,}', text)
        words.extend(cn)
        return words

    def _build_unknown_error(self, entries):
        messages = [e.message for e in entries]
        word_freq = {}
        for msg in messages:
            for w in re.findall(r'[A-Za-z_]{3,}', msg):
                word_freq[w] = word_freq.get(w, 0) + 1
        top_words = sorted(word_freq.items(), key=lambda x: x[1], reverse=True)[:10]
        combined = " ".join(messages).lower()
        cat = "api_usage"
        if "compile" in combined or "blueprint" in combined:
            cat = "compile_error"
        elif "reference" in combined or "null" in combined or "missing" in combined:
            cat = "reference_lost"
        elif "circular" in combined or "dependency" in combined:
            cat = "circular_dependency"
        elif "interface" in combined:
            cat = "interface_mismatch"
        elif "gc" in combined:
            cat = "gc_issue"
        elif "pin" in combined or "connect" in combined:
            cat = "node_error"
        elif "tick" in combined or "performance" in combined:
            cat = "performance_warning"
        elif "access violation" in combined:
            cat = "runtime_error"
        kw = "|".join(w for w, _ in top_words[:5]) if top_words else ".*"
        return UnknownError(
            log_lines=[e.raw[:200] for e in entries[:5]],
            suspected_category=cat,
            suggested_pattern=f"({kw})",
            symptoms=[f"high-freq: {w}({c}x)" for w, c in top_words],
        )


# ── DiagnosisFormatter ──

class DiagnosisFormatter:
    @staticmethod
    def to_dict(report):
        return {
            "analyzed_at": report.analyzed_at,
            "log_file": report.log_file,
            "summary": {
                "total_lines": report.total_lines,
                "error_lines": report.error_lines,
                "warning_lines": report.warning_lines,
                "matches_found": len(report.matches),
                "unknown_errors": len(report.unknown_errors),
                "auto_fix_candidates": len(report.auto_fix_candidates),
                "elapsed_ms": report.elapsed_ms,
            },
            "by_severity": report.by_severity,
            "by_category": report.by_category,
            "matches": [
                {
                    "pattern_id": m.pattern_id, "error_code": m.error_code,
                    "log_line": m.log_line, "severity": m.severity.value,
                    "message": m.message[:300], "auto_fix": m.auto_fix,
                    "fix_action": m.fix_action, "fix_code_example": m.fix_code_example,
                    "blueprint_path": m.blueprint_path, "confidence": m.confidence,
                }
                for m in report.matches
            ],
            "auto_fix_candidates": [
                {
                    "pattern_id": m.pattern_id, "error_code": m.error_code,
                    "log_line": m.log_line, "fix_action": m.fix_action,
                    "fix_code_example": m.fix_code_example, "blueprint_path": m.blueprint_path,
                }
                for m in report.auto_fix_candidates
            ],
            "unknown_errors": [
                {
                    "suspected_category": u.suspected_category,
                    "suggested_pattern": u.suggested_pattern,
                    "symptoms": u.symptoms,
                    "sample_lines": u.log_lines,
                }
                for u in report.unknown_errors
            ],
        }

    @staticmethod
    def to_json(report, indent=2):
        return json.dumps(DiagnosisFormatter.to_dict(report), indent=indent, ensure_ascii=False, default=str)

    @staticmethod
    def to_text(report):
        lines = []
        lines.append("=" * 60)
        lines.append("  UE5 Log Diagnosis Report")
        lines.append("=" * 60)
        lines.append("  Time: " + report.analyzed_at)
        lines.append("  Lines: " + str(report.total_lines) + " (E:" + str(report.error_lines) + " W:" + str(report.warning_lines) + ")")
        lines.append("  Matches: " + str(len(report.matches)))
        lines.append("  Unknown: " + str(len(report.unknown_errors)))
        lines.append("  Auto-fix: " + str(len(report.auto_fix_candidates)))
        lines.append("")
        for sev in [Severity.CRITICAL, Severity.ERROR, Severity.WARNING]:
            sev_matches = [m for m in report.matches if m.severity == sev]
            if not sev_matches:
                continue
            lines.append("--- " + sev.value.upper() + " (" + str(len(sev_matches)) + ") ---")
            for m in sev_matches[:20]:
                lines.append("  [" + m.pattern_id + "] L" + str(m.log_line) + ": " + m.message[:120])
                if m.auto_fix:
                    lines.append("    [FIX] " + m.fix_action[:100])
                if m.blueprint_path:
                    lines.append("    [BP] " + m.blueprint_path)
            if len(sev_matches) > 20:
                lines.append("  ... and " + str(len(sev_matches) - 20) + " more")
        if report.unknown_errors:
            lines.append("")
            lines.append("--- UNKNOWN ERRORS (draft cards needed) ---")
            for ue in report.unknown_errors:
                lines.append("  Category: " + ue.suspected_category)
                lines.append("  Regex: " + ue.suggested_pattern)
                for s in ue.symptoms[:5]:
                    lines.append("    " + s)
        lines.append("")
        lines.append("  Elapsed: " + str(round(report.elapsed_ms)) + "ms")
        lines.append("=" * 60)
        return "\n".join(lines)

    @staticmethod
    def to_draft_card(unknown):
        return {
            "_draft": True,
            "_created_by": "log_parser.py auto-generated",
            "_created_at": datetime.now(timezone.utc).isoformat(),
            "id": "PTN-DRAFT",
            "title": "Unknown error - " + unknown.suspected_category,
            "error_code": "",
            "category": unknown.suspected_category,
            "severity": "error",
            "pattern": {
                "log_regex": unknown.suggested_pattern,
                "symptom": unknown.symptoms[0] if unknown.symptoms else "",
                "root_cause": "TODO: review by human",
                "detection_check": "TODO: verify",
            },
            "auto_fix": False,
            "fix_action": "TODO: fix plan",
            "fix_code_example": "# TODO: fix code",
            "source": "log_parser.py draft",
            "related_patterns": [],
            "sample_lines": unknown.log_lines[:5],
        }


# ── AutoFixExecutor ──

class AutoFixExecutor:
    def __init__(self, bridge=None):
        self.bridge = bridge
        self._fix_log = []

    def execute(self, match):
        entry = {"pattern_id": match.pattern_id, "log_line": match.log_line, "auto_fix": match.auto_fix, "success": False, "detail": ""}
        if not match.auto_fix:
            entry["detail"] = "not auto-fixable"
            self._fix_log.append(entry)
            return entry
        handlers = {
            "PTN-001": self._fix_001, "PTN-003": self._fix_003,
            "PTN-005": self._fix_005, "PTN-008": self._fix_008,
            "PTN-010": self._fix_010, "PTN-011": self._fix_011,
        }
        handler = handlers.get(match.pattern_id)
        if handler:
            try:
                result = handler(match)
                entry.update(result)
            except Exception as e:
                entry["detail"] = "fix exception: " + str(e)
        else:
            entry["detail"] = "no handler for " + match.pattern_id
        self._fix_log.append(entry)
        return entry

    def execute_batch(self, matches):
        return [self.execute(m) for m in matches if m.auto_fix]

    def get_fix_log(self):
        return self._fix_log

    def _fix_001(self, m):
        return {"success": True, "action": "PTN-001: rebuild via FKismetEditorUtilities", "detail": "delete old BP, use create_actor_blueprint", "auto_applied": False}
    def _fix_003(self, m):
        return {"success": True, "action": "PTN-003: fix CDO timing", "detail": "compile_blueprint() before set_editor_property()", "auto_applied": False}
    def _fix_005(self, m):
        return {"success": True, "action": "PTN-005: merge into single ue_send call", "detail": "all create+connect ops in one script", "auto_applied": False}
    def _fix_008(self, m):
        return {"success": True, "action": "PTN-008: cleanup orphan blueprints", "detail": "SafeBatchExecutor.cleanup_orphans()", "auto_applied": False}
    def _fix_010(self, m):
        return {"success": True, "action": "PTN-010: scan and replace ref path", "detail": "AssetRegistry search + auto replace", "auto_applied": False}
    def _fix_011(self, m):
        return {"success": True, "action": "PTN-011: add interface function override", "detail": "add Function Override node in event graph", "auto_applied": False}


# ── convenience ──

# T-20260726-HEALTHFIX #5 C2(1): 路径遍历写入防护
# v2.20 修复：drafts 落盘时对**绝对路径**调用本函数必抛 ValueError（未捕获 →
# 整个诊断进程崩溃，主报告已写但 drafts 全丢）。新增 allow_absolute 开关：
#   · 相对路径：语义不变（cwd/base 包含校验 + 拒 '..' / '~'）
#   · 绝对路径：仅当 allow_absolute=True 时放行（用户显式指定的 --output、
#     以及由已校验 safe_output 派生的 drafts 绝对路径），仍做 realpath 规范化
def sanitize_output_path(path, base=None, allow_absolute=False):
    """校验输出路径：相对路径不得逃逸 base（默认 cwd），禁 '..' / '~'。

    Args:
        path: 用户提供的输出文件路径
        base: 允许的根目录（None = 当前 cwd）；相对路径必须落在此目录（或其父目录）下
        allow_absolute: 允许绝对路径（用于用户显式指定的 --output 与内部派生路径）

    Returns:
        os.path.realpath 后的绝对路径

    Raises:
        ValueError: 路径不安全
    """
    if not path:
        raise ValueError("output_path is empty")
    if os.path.isabs(path):
        if not allow_absolute:
            raise ValueError("output_path must be relative, got absolute: " + path)
        # 绝对路径：用户显式指定 / 已校验调用方派生 → 仅规范化，不再做 base 包含校验
        return os.path.realpath(path)
    if ".." in path.replace("\\", "/").split("/"):
        raise ValueError("output_path must not contain '..': " + path)
    if "~" in path:
        raise ValueError("output_path must not contain '~': " + path)
    abs_path = os.path.abspath(path)
    base_dir = os.path.realpath(base) if base else os.path.realpath(os.getcwd())
    real = os.path.realpath(abs_path)
    # 允许 final path 在 base 同目录或 base 子目录（含 drafts/）
    base_parent = os.path.dirname(base_dir) if base else base_dir
    if not (real == base_dir or real.startswith(base_dir + os.sep) or real.startswith(base_parent + os.sep)):
        raise ValueError("output_path escapes allowed base: " + real)
    return real


def diagnose_log(log_path, patterns_path=None, output_path=None, summary=False, max_size_mb=100):
    # T-20260726-HEALTHFIX #5 C2(1): 输出路径遍历写入防护
    # v2.20：--output 允许绝对路径（用户显式指定），相对路径仍限制在 cwd 内
    safe_output = None
    if output_path:
        safe_output = sanitize_output_path(output_path, allow_absolute=True)
    parser = UELogParser()
    parser.load_log(log_path, max_size_mb=max_size_mb)
    parser.load_patterns(patterns_path)
    report = parser.diagnose()
    report.log_file = log_path
    fmt = DiagnosisFormatter()
    if safe_output:
        with open(safe_output, "w", encoding="utf-8") as f:
            f.write(fmt.to_json(report))
        print("report written: " + safe_output)
    if summary:
        print(fmt.to_text(report))
    else:
        print("diagnosis done: " + str(report.error_lines) + " errors, " + str(len(report.matches)) + " hits, " + str(len(report.unknown_errors)) + " unknown")
    if report.unknown_errors and safe_output:
        # drafts 子目录也走 sanitize（在 safe_output 同目录下）
        # v2.20 修复：drafts_dir / dp 都是由已校验的 safe_output **派生的绝对路径**，
        #   旧实现对绝对路径无条件拒绝 → 未捕获 ValueError → 诊断进程崩溃。
        #   这里 allow_absolute=True 放行（路径来源可信：父目录即已校验的输出目录）。
        drafts_dir = os.path.join(os.path.dirname(safe_output), "drafts")
        # 校验 drafts 路径不逃逸
        sanitized_drafts = sanitize_output_path(drafts_dir, allow_absolute=True)
        os.makedirs(sanitized_drafts, exist_ok=True)
        for i, ue in enumerate(report.unknown_errors):
            draft = fmt.to_draft_card(ue)
            dp = os.path.join(sanitized_drafts, "draft_" + str(i + 1) + ".json")
            # 每个 draft 也校验
            dp = sanitize_output_path(dp, allow_absolute=True)
            with open(dp, "w", encoding="utf-8") as f:
                json.dump(draft, f, indent=2, ensure_ascii=False)
            print("  draft card: " + dp)
    return report


# ── CLI ──

def main():
    parser = argparse.ArgumentParser(description="UE5 log diagnosis engine")
    parser.add_argument("--log", "-l", required=True, help="UE log file path")
    parser.add_argument("--patterns", "-p", default=_DEFAULT_PATTERNS, help="error_patterns.json path")
    parser.add_argument("--output", "-o", default="diagnosis_result.json", help="output JSON path")
    parser.add_argument("--summary", "-s", action="store_true", help="print readable summary")
    parser.add_argument("--auto-fix", action="store_true", help="attempt auto-fix")
    # T-20260726-HEALTHFIX #5 C2(3): CLI 暴露 max-size 参数（默认 100MB）
    parser.add_argument("--max-size", type=int, default=100, help="max log size in MB (default 100, 0 to disable)")
    args = parser.parse_args()
    # v2.20 修复：输入文件缺失时旧实现 print 后 return → 退出码 0，自动化调用方
    #   无法感知失败。改为 stderr 提示 + 退出码 2。
    if not os.path.exists(args.log):
        print("log not found: " + args.log, file=sys.stderr)
        sys.exit(2)
    if not os.path.exists(args.patterns):
        print("patterns not found: " + args.patterns, file=sys.stderr)
        sys.exit(2)
    try:
        report = diagnose_log(args.log, args.patterns, args.output, args.summary, max_size_mb=args.max_size)
    except ValueError as e:
        # v2.20：路径策略违规（'..' / '~' / 逃逸 base）→ 可读错误而非裸 traceback
        print("output path error: " + str(e), file=sys.stderr)
        sys.exit(2)
    if args.auto_fix and report.auto_fix_candidates:
        executor = AutoFixExecutor()
        results = executor.execute_batch(report.auto_fix_candidates)
        fixed = sum(1 for r in results if r.get("auto_applied"))
        suggested = sum(1 for r in results if r.get("success") and not r.get("auto_applied"))
        # v2.20 修复：AutoFixExecutor 此前为无操作桩却上报 auto_applied=True，
        #   CLI 打印「已应用」误导用户以为工程已被修复。如实改为「建议」口径。
        print("auto-fix suggestions: " + str(suggested + fixed) + "/" + str(len(results))
              + " generated (0 auto-applied — fix suggestions require manual/bridge execution)")


if __name__ == "__main__":
    main()
