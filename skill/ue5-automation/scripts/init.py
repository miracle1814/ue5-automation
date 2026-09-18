# -*- coding: utf-8 -*-
"""
init.py — UE5 automation skill 初始化执行器（薄执行器 + 人工确认门）
================================================================
面向"skill 通用化"场景：新电脑 / 新宿主机首次使用 ue5-automation 时，
运行本脚本完成一次全流程适配：

    phase0  参数解析
    phase1  能力探测（三级链）：list_agents API → agent.json → 系统提示词语义
    phase2  UE 环境探测（import deploy 复用 find_engine_path / find_project_path）
    phase3  一键配置（默认 dry-run 只读预览；--apply 才落盘）
    phase4  health 自检（分级 L0~L3）
    phase5  产出 role_assignments.yaml（含 status）+ init_report.md

🔴 收敛语义（T-20260909-SKILL-GEN-C-INIT-CONVERGE 定稿 · QA PATH-REVIEW 评审①采纳）:
    init.py 是「薄执行器」而非「编排器」——探测/部署/health 等确定性工程由代码
    执行；**语义角色自动分配 = 预加载建议稿，不作为生效配置**。产出
    role_assignments.yaml 顶层含:
        status: proposed | confirmed
            proposed  = 自动语义建议（预加载），未经人工确认，不生效
            confirmed = 经用户/主控 Agent 显式确认（--assign / --reconcile）
                       或确定性指派（single_agent all_in_one）
        role_sources: 每个 role_map 角色的来源 auto | manual | deterministic

    人工确认门（生效三通道）:
        a) --assign <id>=<role>          显式指定（orchestrator+ue_executor 均
                                          manual → confirmed）
        b) --reconcile --apply           读回手编 role_assignments.yaml → manual
        c) apply 时提示确认              存在 status=confirmed 旧配置且本次无确认
                                          通道 → 拒绝覆盖（禁止静默自动覆盖）

用法:
    python init.py                          # dry-run 安全预览（默认，不写任何文件）
    python init.py --apply                  # 落盘【建议稿】role_assignments.yaml（status: proposed）
    python init.py --apply --assign <id>=<role> [--assign <id2>=<role2> ...]   # → confirmed
    python init.py --reconcile --apply      # 读回(手编) yaml → 合并为 manual → confirmed
    python init.py --deploy-script <path>   # 显式指定 deploy.py（三级定位链最后防线）

红线（T-20260909-SKILL-GEN-C-INIT + CONVERGE 定稿）:
    - 非破坏：默认只读，写操作需 --apply；不删除任何既有文件
    - 人工确认门：自动语义分配只产出 proposed 建议稿；生效须显式确认通道；
      禁止静默自动覆盖 status=confirmed 的既有配置
    - 无自启/定时/常驻：本脚本为手动一次性命令，生命周期 ⊆ 用户显式执行
    - 路径全部由 __file__ / 环境变量推导，不硬编码绝对路径
    - 产物默认落 <skill 运行根>/output/（publish 黑名单，杜绝漂移入发布包）

角色模型（role_assignments.yaml schema v1.1）:
    role_map: {orchestrator, ue_executor[, ue_executor_backup], screenshot_ocr,
               report_writer, archivist} -> agent_id
    agents:   每 agent 的 capabilities / ue_capability / role / role_source /
              boundaries / exec_commands

版本: v1.1 (T-20260909-SKILL-GEN-C-INIT-CONVERGE)
"""

import argparse
import contextlib
import datetime
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import urllib.request

# Windows GBK 控制台兜底：UTF-8 输出 + errors=replace（防 emoji/中文崩）
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# ── 常量 ──────────────────────────────────────────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))       # <skill>/scripts
SKILL_DIR = os.path.dirname(SCRIPT_DIR)                        # <skill> 运行根
OUT_DIR = os.path.join(SKILL_DIR, "output")                    # 生成物目录
DEFAULT_API_BASE = "http://127.0.0.1:8088"                     # QwenPaw/CoPaw agent API
API_AGENTS_PATH = "/api/agents"
API_TIMEOUT = 3.0                                              # L1 超时 <=3s

SCHEMA_VERSION = "1.1"
GENERATOR_TAG = "ue5-automation/scripts/init.py"

# 角色分配来源（role_source / role_sources 取值）
SRC_AUTO = "auto"               # 语义自动建议（预加载，未经人工确认）
SRC_MANUAL = "manual"           # 人工确认（--assign / --reconcile 读回）
SRC_DETERMINISTIC = "deterministic"  # 确定性指派（single_agent all_in_one，无选择空间）
SRC_UNSPECIFIED = "unspecified"

STATUS_PROPOSED = "proposed"    # 建议稿：存在 auto 来源，未经人工确认，不生效
STATUS_CONFIRMED = "confirmed"  # 生效稿：关键角色均 manual / deterministic 或手动通道
ROLE_ASSIGN_FILE = "role_assignments.yaml"
REPORT_FILE = "init_report.md"

# UE 能力软判据词表（v1：身份文本 description/name 命中即 high；system_prompt 仅增强）
UE_SOFT_WORDS = [
    "ue", "unreal", "蓝图", "bridge", "引擎", "python", "代码开发",
    "自动化", "cad", "脚本", "运行",
]

# 角色词表（(role, 关键词, 特异性分)；特异分用于同 agent 多命中时取最长词）
ROLE_KEYWORDS = [
    # orchestrator —— 总调度 > 调度（总管也含"调度"，特异性低）
    ("orchestrator", "总调度", 3),
    ("orchestrator", "maintainer", 2),
    ("orchestrator", "调度", 1),
    # ue_executor —— 不靠词表自动抢，靠 UE 软判据
    # screenshot_ocr
    ("screenshot_ocr", "截图", 3),
    ("screenshot_ocr", "图像", 2),
    ("screenshot_ocr", "视觉", 2),
    ("screenshot_ocr", "ocr", 3),
    # report_writer
    ("report_writer", "文案", 2),
    ("report_writer", "写作", 2),
    ("report_writer", "报告", 2),
    ("report_writer", "调研", 1),
    # archivist
    ("archivist", "资料员", 3),
    ("archivist", "知识库", 2),
    ("archivist", "复盘", 2),
    ("archivist", "归档", 2),
    ("archivist", "索引", 1),
]

# 硬判据工具名
TOOL_SHELL = "execute_shell_command"
TOOL_READ = "read_file"
TOOL_WRITE = "write_file"
TOOL_CHAT = "chat_with_agent"
TOOL_SUBMIT = "submit_to_agent"
TOOL_SCREENSHOT = "desktop_screenshot"

ALLOWED_ROLES = {"orchestrator", "ue_executor", "ue_executor_backup",
                 "screenshot_ocr", "report_writer", "archivist",
                 "all_in_one", "unspecified"}

# 角色 → 默认能力边界 / 可执行命令（生成文案，允许手编）
ROLE_BOUNDARIES = {
    "orchestrator": [
        "负责任务接收/拆解/分派与结果汇总",
        "不直接执行 UE 蓝图级写操作（转 ue_executor）",
    ],
    "ue_executor": [
        "负责 UE 脚本/HTTP bridge/蓝图自动化执行（ue5_runner / blueprint_editor / analyzer 等）",
        "不负责 UE 界面级图像识别（转 screenshot_ocr）",
        "不负责最终用户报告润色（转 report_writer）",
    ],
    "screenshot_ocr": [
        "负责 UE 界面/图表截图与 OCR 识别兜底",
    ],
    "report_writer": [
        "负责按模板输出用户报告与文档润色",
    ],
    "archivist": [
        "负责复盘归档、知识库维护、索引管理",
    ],
    "all_in_one": [
        "单 Agent 环境：orchestrator + ue_executor + report_writer 合一",
    ],
    "unspecified": [
        "未分配专职角色；仅在 orchestrator 分派时按需协作",
    ],
}

ROLE_EXEC_COMMANDS = {
    "orchestrator": [],
    "ue_executor": [
        "python scripts/ue5_runner.py health",
        "python scripts/ue5_runner.py read <bp_path>",
        "python scripts/ue5_runner.py build <spec.json>",
        "python scripts/blueprint_editor.py <op> ...",
        "python scripts/analyzer.py <project_root>",
    ],
    "screenshot_ocr": [],
    "report_writer": [],
    "archivist": [],
    "all_in_one": [],
    "unspecified": [],
}

ROLE_DEFAULT_BEHAVIOR = {
    "orchestrator": "dispatch_only",
    "ue_executor": "execute_and_report",
    "screenshot_ocr": "execute_and_report",
    "report_writer": "execute_and_report",
    "archivist": "read_only",
    "all_in_one": "execute_and_report",
    "unspecified": "ask_first",
}


# ── 小工具 ────────────────────────────────────────────────────────────

def utc_now_iso():
    """带本地时区的 ISO 时间（YYYY-MM-DDTHH:MM:SS+08:00）。"""
    now = datetime.datetime.now().astimezone()
    return now.isoformat(timespec="seconds")


def norm_ws(path):
    """归一化 workspace_dir（list_agents 可能返回混合分隔符）。"""
    if not path:
        return ""
    return os.path.normpath(str(path).replace("/", os.sep))


def detect_python_available():
    """本机 python 可用性（硬判据④：init 自身进程即证明）。"""
    exe = sys.executable
    return bool(exe) and os.path.isfile(exe)


def _py_safe(v):
    """YAML 标量渲染：需要引号时加双引号。"""
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v)
    # 空、含特殊字符、首尾空格、纯数字/布尔形态 → 引号
    numeric = s.replace(".", "", 1).replace("-", "", 1).isdigit() \
        and any(ch.isdigit() for ch in s)
    if (s == "" or s != s.strip() or any(ch in s for ch in ":#{}\"',&*?|<>%@`")
            or s.lower() in ("null", "true", "false", "yes", "no", "on", "off")
            or numeric):
        return json.dumps(s, ensure_ascii=False)
    return s


def yaml_dump(doc, header=None):
    """最小 YAML 序列化器（覆盖本 schema：dict/list/scalar）。"""
    lines = []
    if header:
        for h in header.splitlines():
            lines.append(h)
    lines.append("")

    def emit(obj, indent, prefix=None):
        pad = "  " * indent
        if isinstance(obj, dict):
            for k, v in obj.items():
                if isinstance(v, (dict, list)):
                    lines.append(f"{pad}{k}:")
                    emit(v, indent + 1)
                else:
                    lines.append(f"{pad}{k}: {_py_safe(v)}")
        elif isinstance(obj, list):
            for item in obj:
                if isinstance(item, (dict, list)):
                    lines.append(f"{pad}-")
                    emit(item, indent + 1)
                else:
                    lines.append(f"{pad}- {_py_safe(item)}")
        else:
            lines.append(f"{pad}{_py_safe(obj)}")

    emit(doc, 0)
    return "\n".join(lines) + "\n"

# ── YAML 最小读取（仅用于 --reconcile 读回本 schema；PyYAML 存在则优先）────
def _yaml_scalar(text):
    t = text.strip()
    if t in ("null", "~", ""):
        return None
    if t in ("true", "True", "TRUE"):
        return True
    if t in ("false", "False", "FALSE"):
        return False
    if len(t) >= 2 and t[0] == '"' and t[-1] == '"':
        try:
            return json.loads(t)
        except Exception:
            return t[1:-1]
    if len(t) >= 2 and t[0] == "'" and t[-1] == "'":
        return t[1:-1]
    # 纯数字/浮点
    try:
        if t.replace(".", "", 1).isdigit() and any(ch.isdigit() for ch in t):
            return float(t) if "." in t else int(t)
    except Exception:
        pass
    return t


class _MiniYaml:
    """解析 ue5-automation init 产出的 YAML 子集。

    支持的形态（缩进 = 2 空格倍数，与 yaml_dump 输出严格一致）：
      key: scalar
      key:
        subkey: scalar
      key:
        - scalar
      key:
        - subkey1: scalar
          subkey2: scalar
    注释/空行忽略；行内 # 不处理。
    """

    def __init__(self, text):
        self.lines = []
        for raw in text.splitlines():
            s = raw.rstrip()
            if not s.strip() or s.lstrip().startswith("#"):
                continue
            indent = len(s) - len(s.lstrip(" "))
            self.lines.append((indent, s.strip()))
        self.pos = 0

    def _peek_indent(self):
        if self.pos < len(self.lines):
            return self.lines[self.pos][0]
        return -1

    def _parse_block(self, indent):
        """解析缩进 == indent 的连续条目，返回 dict。"""
        node = {}
        while self.pos < len(self.lines):
            cur_indent, text = self.lines[self.pos]
            if cur_indent < indent:
                break
            if cur_indent > indent:
                raise ValueError(f"unexpected indent {cur_indent}>{indent}: {text}")
            # 条目必须 key: ...
            if ":" not in text:
                raise ValueError(f"expected 'key: value', got: {text}")
            key, _, rest = text.partition(":")
            key = key.strip()
            rest = rest.strip()
            self.pos += 1
            if rest == "":
                # 子块：dict 或 list
                nxt = self._peek_indent()
                if nxt <= cur_indent:
                    node[key] = None
                else:
                    if (self.pos < len(self.lines)
                            and self.lines[self.pos][1].startswith("- ")):
                        node[key] = self._parse_list(nxt)
                    else:
                        node[key] = self._parse_block(nxt)
            else:
                node[key] = _yaml_scalar(rest)
        return node

    def _parse_list(self, indent):
        """解析缩进 == indent 的 '- item' 列表。"""
        lst = []
        while self.pos < len(self.lines):
            cur_indent, text = self.lines[self.pos]
            if cur_indent < indent:
                break
            if cur_indent > indent:
                raise ValueError(f"unexpected indent {cur_indent}>{indent}: {text}")
            if not text.startswith("- "):
                break
            body = text[2:].strip()
            self.pos += 1
            if body == "":
                # 多行 dict 项
                nxt = self._peek_indent()
                lst.append(self._parse_block(nxt))
            elif ":" in body:
                # 单行起头 + 后续子键（yaml_dump 对 dict 项总是多行，安全起见兼容）
                k, _, v = body.partition(":")
                item = {k.strip(): _yaml_scalar(v.strip())}
                nxt = self._peek_indent()
                if nxt > indent:
                    sub = self._parse_block(nxt)
                    item.update(sub)
                lst.append(item)
            else:
                lst.append(_yaml_scalar(body))
        return lst

    def parse(self):
        if not self.lines:
            return {}
        return self._parse_block(0)


def load_yaml_file(path):
    """读 role_assignments.yaml；优先 PyYAML（若已安装），否则内置解析。"""
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except Exception:
        return None
    try:
        import yaml  # type: ignore
        return yaml.safe_load(text)
    except Exception:
        try:
            return _MiniYaml(text).parse()
        except Exception as e:
            print(f"[WARN] role_assignments.yaml 解析失败: {e}")
            return None


# ── deploy.py 三级定位链 ─────────────────────────────────────────────
def find_deploy_py(explicit=None):
    """定位 bridge 真源 deploy.py（不硬编码绝对路径）。

    定位链（设计规格 §三）:
      ① 相对 __file__ 逐级上溯，找 sibling <根>/bridge/BlueprintPythonBridge/deploy.py
         （发布包内天然命中：<包根>/skill/ue5-automation/scripts/init.py
           → 上溯到 <包根>/bridge/...）；同时尝试
         <根>/ue5-automation-skill-package/bridge/...（真源旁的发布包）
      ② 环境变量 UE5_SKILL_ROOT / COPAW_WORKSPACE 推导
      ③ --deploy-script <path> 显式覆盖（最后防线）
    """
    if explicit and os.path.isfile(explicit):
        return os.path.abspath(explicit)

    cur = SCRIPT_DIR
    for _ in range(8):
        for cand in (
                os.path.join(cur, "bridge", "BlueprintPythonBridge", "deploy.py"),
                os.path.join(cur, "ue5-automation-skill-package",
                             "bridge", "BlueprintPythonBridge", "deploy.py")):
            if os.path.isfile(cand):
                return cand
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent

    for env in ("UE5_SKILL_ROOT", "COPAW_WORKSPACE"):
        base = os.environ.get(env, "").strip()
        if not base:
            continue
        for cand in (
                os.path.join(base, "bridge", "BlueprintPythonBridge", "deploy.py"),
                os.path.join(base, "ue5-automation-skill-package",
                             "bridge", "BlueprintPythonBridge", "deploy.py")):
            if os.path.isfile(cand):
                return cand

    return None


def import_deploy(deploy_py):
    """把 deploy.py 所在目录加入 sys.path 并 import，返回模块或 None。"""
    d = os.path.dirname(deploy_py)
    if d not in sys.path:
        sys.path.insert(0, d)
    try:
        import deploy  # type: ignore
        return deploy
    except Exception as e:
        print(f"[WARN] import deploy 失败: {e}")
        return None


# ── phase1 · L1：list_agents API ─────────────────────────────────────
def api_list_agents(base_url=None, timeout=API_TIMEOUT):
    """GET {base}/api/agents → agents 列表；失败返回 None。"""
    base = (base_url or os.environ.get("QWENPAW_API_BASE")
            or DEFAULT_API_BASE).rstrip("/")
    url = base + API_AGENTS_PATH
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        agents = data.get("agents")
        return agents if isinstance(agents, list) else None
    except Exception:
        return None

# ── phase1 · L2：agent.json 深度读取 ─────────────────────────────────
def _agent_json_path(ws_dir):
    if not ws_dir or not os.path.isdir(ws_dir):
        return None
    p = os.path.join(ws_dir, "agent.json")
    return p if os.path.isfile(p) else None


def read_agent_json(ws_dir):
    """读 workspace_dir/agent.json → dict；不存在/损坏返回 {}。"""
    p = _agent_json_path(ws_dir)
    if not p:
        return {}
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def extract_tools(agent_json):
    """从 agent.json 提取硬能力布尔（缺失默认 False）。"""
    tools = agent_json.get("tools", {}) if isinstance(agent_json, dict) else {}
    bt = tools.get("builtin_tools", {}) if isinstance(tools, dict) else {}
    if not isinstance(bt, dict):
        bt = {}

    def en(name):
        node = bt.get(name, {})
        if not isinstance(node, dict):
            return False
        return bool(node.get("enabled", False))

    return {
        "shell": en(TOOL_SHELL),
        "file_io": en(TOOL_READ) and en(TOOL_WRITE),
        "inter_agent": en(TOOL_CHAT) or en(TOOL_SUBMIT),
        "screenshot": en(TOOL_SCREENSHOT),
    }


def _safe_read_text(p, limit=20000):
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            return f.read(limit)
    except Exception:
        return ""


def collect_sys_prompt_text(ws_dir, agent_json):
    """读 system_prompt_files（AGENTS.md/SOUL.md/PROFILE.md）全文拼接。"""
    if not ws_dir or not os.path.isdir(ws_dir):
        return ""
    if not isinstance(agent_json, dict):
        return ""
    files = agent_json.get("system_prompt_files", [])
    if isinstance(files, str):
        files = [files]
    if not isinstance(files, list):
        return ""
    parts = []
    for rel in files:
        if not isinstance(rel, str) or not rel:
            continue
        # 防目录穿越
        base = os.path.basename(rel)
        p = os.path.join(ws_dir, base)
        if os.path.isfile(p):
            parts.append(_safe_read_text(p))
    return "\n".join(parts)


# ── phase1 · L3：语义标注 ─────────────────────────────────────────────
def _lower_hits(text, words):
    tl = text.lower()
    hits = []
    for w in words:
        if w.lower() in tl:
            hits.append(w)
    return hits


def desc_tags_for(agent):
    """从 description + name 提取 UE 语义标签（身份文本，软判据主源）。"""
    name = str(agent.get("name") or "")
    desc = str(agent.get("description") or "")
    text = f"{name} {desc}"
    return _lower_hits(text, UE_SOFT_WORDS)


def sys_tags_for(sys_text):
    """从系统提示词提取 UE 语义标签（仅增强 confidence，不单独翻 capable）。"""
    if not sys_text:
        return []
    return _lower_hits(sys_text, UE_SOFT_WORDS)


def role_hits_for(agent):
    """角色词表命中 → [(role, score, word), ...]，从身份文本。"""
    name = str(agent.get("name") or "")
    desc = str(agent.get("description") or "")
    text = f"{name} {desc}".lower()
    hits = []
    for role, word, score in ROLE_KEYWORDS:
        if word.lower() in text:
            hits.append((role, score, word))
    return hits


def build_capability_record(agent, capabilities, local_py, tags, sys_tags,
                            manual_role=None, probe_error=None):
    """组装单 agent 能力/UE 判定记录（schema §二）。"""
    agent_id = str(agent.get("id") or "")
    name = str(agent.get("name") or "")
    ws = norm_ws(agent.get("workspace_dir"))
    enabled = bool(agent.get("enabled", True))
    startup = str(agent.get("startup_status") or "")
    model = ""
    am = agent.get("active_model")
    if isinstance(am, dict):
        model = f"{am.get('provider_id') or ''}/{am.get('model') or ''}"
        model = model.strip("/")
    backend = str(agent.get("backend") or "")

    # 硬判据
    hard_ok = enabled and capabilities["shell"] and capabilities["file_io"] \
        and local_py
    hard_evidence = []
    if not enabled:
        hard_evidence.append("enabled")
    if not capabilities["shell"]:
        hard_evidence.append("shell")
    if not capabilities["file_io"]:
        hard_evidence.append("file_io")
    if not local_py:
        hard_evidence.append("python")

    # 软判据（规格 §1.1）：
    #   主源 = 身份文本 desc_tags（API description + name 命中 UE_SOFT_WORDS）
    #   增强 = system_prompt sys_tags（不单独翻转 capable——团队 prompt 普遍含 UE
    #          字样，若计入会让非 UE 职责 agent 全判 capable）
    #   manual = --assign / --reconcile 显式指派
    semantic_hit = bool(tags)
    manual = bool(manual_role)
    soft_ok = semantic_hit or manual

    if hard_ok and semantic_hit:
        verdict = "capable"
        confidence = "high"
    elif hard_ok and manual:
        verdict = "manual"
        confidence = "manual"
    elif hard_ok:
        verdict = "incapable"       # 硬项全过但身份未命中 → 待人工确认
        confidence = "low"
    else:
        verdict = "incapable"
        confidence = "low"

    evidence = {
        "hard": {
            "enabled": enabled,
            "shell": capabilities["shell"],
            "file_io": capabilities["file_io"],
            "python": local_py,
        },
        "tags": list(dict.fromkeys(tags)),
        "sys_tags": list(dict.fromkeys(sys_tags))[:8],
        "manual": manual,
    }
    if hard_evidence:
        evidence["missing"] = hard_evidence

    rec = {
        "agent_id": agent_id,
        "name": name,
        "description": str(agent.get("description") or ""),
        "workspace_dir": ws,
        "enabled": enabled,
        "startup_status": startup,
        "backend": backend,
        "model": model,
        "capabilities": {
            "shell": capabilities["shell"],
            "file_io": capabilities["file_io"],
            "inter_agent": capabilities["inter_agent"],
            "screenshot": capabilities["screenshot"],
        },
        "ue_capability": {
            "verdict": verdict,
            "confidence": confidence,
            "evidence": evidence,
            "probe_error": probe_error,
        },
        "role": "unspecified",
        "role_source": SRC_UNSPECIFIED,
        "boundaries": list(ROLE_BOUNDARIES.get("unspecified", [])),
        "exec_commands": [],
        "default_behavior": ROLE_DEFAULT_BEHAVIOR["unspecified"],
    }
    return rec


# ── 角色判定与 role_map ──────────────────────────────────────────────
def assign_roles(records, manual_map=None, mode="auto"):
    """分配角色并产出 role_map。

    manual_map: {"agent_id": "role"} 来自 --assign 或 --reconcile 手编。
    分配优先级:
      1. manual_map 指定（orchestrator/ue_executor/.../unspecified）
      2. orchestrator: 身份命中 orchestrator 词（取分最高）
      3. ue_executor: ue_capable(capable/manual) 中 confidence 排序，
         primary + backup 两级（③ 拍板：多候选 = primary+backup）
      4. screenshot_ocr / report_writer / archivist: 身份命中对应词表
      5. 其余 unspecified
    单 agent 环境（mode=single_agent）: 全部指宿主 all_in_one。
    """
    recs = [r for r in records if r["enabled"]]
    role_map = {}

    if mode == "single_agent" and recs:
        host = recs[0]
        host["role"] = "all_in_one"
        host["role_source"] = SRC_DETERMINISTIC
        host["boundaries"] = list(ROLE_BOUNDARIES["all_in_one"])
        host["exec_commands"] = list(ROLE_EXEC_COMMANDS["ue_executor"])
        host["default_behavior"] = ROLE_DEFAULT_BEHAVIOR["all_in_one"]
        for r in records:
            if r is not host:
                r["role"] = "unspecified"
                r["role_source"] = SRC_UNSPECIFIED
        role_map = {"orchestrator": host["agent_id"],
                    "ue_executor": host["agent_id"],
                    "screenshot_ocr": host["agent_id"],
                    "report_writer": host["agent_id"],
                    "archivist": host["agent_id"]}
        return role_map, records

    manual_map = manual_map or {}
    used = set()

    def apply_role(rec, role, source=SRC_AUTO):
        rec["role"] = role
        rec["role_source"] = source
        rec["boundaries"] = list(ROLE_BOUNDARIES.get(role, []))
        rec["exec_commands"] = list(ROLE_EXEC_COMMANDS.get(role, []))
        rec["default_behavior"] = ROLE_DEFAULT_BEHAVIOR.get(role, "ask_first")

    # 1. 手动指派（--assign / --reconcile 读回）→ manual
    for aid, role in manual_map.items():
        if role not in ALLOWED_ROLES:
            print(f"[WARN] --assign 忽略未知角色 {aid}={role}")
            continue
        for r in recs:
            if r["agent_id"] == aid:
                apply_role(r, role, source=SRC_MANUAL)
                # 手动指派 UE 相关角色 → 同步 ue_capability verdict（一致性）
                if role in ("ue_executor", "ue_executor_backup"):
                    r["ue_capability"]["verdict"] = "manual"
                    r["ue_capability"]["confidence"] = "manual"
                    r["ue_capability"]["evidence"]["manual"] = True
                used.add(aid)
                if role == "orchestrator":
                    role_map["orchestrator"] = aid
                elif role == "ue_executor":
                    role_map["ue_executor"] = aid
                elif role == "ue_executor_backup":
                    role_map["ue_executor_backup"] = aid
                elif role == "screenshot_ocr":
                    role_map["screenshot_ocr"] = aid
                elif role == "report_writer":
                    role_map["report_writer"] = aid
                elif role == "archivist":
                    role_map["archivist"] = aid
                elif role == "all_in_one":
                    role_map.setdefault("orchestrator", aid)
                    role_map.setdefault("ue_executor", aid)
                break

    # 2. orchestrator（身份文本命中，取特异性分最高）→ auto 建议
    if "orchestrator" not in role_map:
        best = None
        for r in recs:
            if r["agent_id"] in used:
                continue
            for role, score, _w in role_hits_for(
                    {"name": r["name"], "description": r["description"]}):
                if role == "orchestrator":
                    if best is None or score > best[0]:
                        best = (score, r)
                    break
        if best:
            apply_role(best[1], "orchestrator", source=SRC_AUTO)
            role_map["orchestrator"] = best[1]["agent_id"]
            used.add(best[1]["agent_id"])

    # 3. ue_executor primary + backup → auto 建议
    capable = []
    for r in recs:
        if r["agent_id"] in used:
            continue
        v = r["ue_capability"]["verdict"]
        if v in ("capable", "manual"):
            conf = r["ue_capability"]["confidence"]
            capable.append((conf, r))
    # 排序：manual > high > medium > low；同 conf 保序
    conf_order = {"manual": 3, "high": 2, "medium": 1, "low": 0}
    capable.sort(key=lambda x: -conf_order.get(x[0], 0))

    # 手动已指派 ue_executor 时，自动候选只补 backup（不覆盖 primary）
    if "ue_executor" not in role_map and capable:
        _c, primary = capable[0]
        apply_role(primary, "ue_executor", source=SRC_AUTO)
        role_map["ue_executor"] = primary["agent_id"]
        used.add(primary["agent_id"])
        capable = capable[1:]

    # backup：无论 primary 来自手动还是自动，剩余 capable 首位补 backup
    if "ue_executor_backup" not in role_map and capable:
        _c, backup = capable[0]
        apply_role(backup, "ue_executor_backup", source=SRC_AUTO)
        role_map["ue_executor_backup"] = backup["agent_id"]
        used.add(backup["agent_id"])

    # 4. 辅助角色（screenshot_ocr / report_writer / archivist）→ auto 建议
    for role_key in ("screenshot_ocr", "report_writer", "archivist"):
        if role_key in role_map:
            continue
        best = None
        for r in recs:
            if r["agent_id"] in used:
                continue
            for role, score, _w in role_hits_for(
                    {"name": r["name"], "description": r["description"]}):
                if role == role_key and (best is None or score > best[0]):
                    best = (score, r)
                    break
        if best:
            apply_role(best[1], role_key, source=SRC_AUTO)
            role_map[role_key] = best[1]["agent_id"]
            used.add(best[1]["agent_id"])

    return role_map, records

# ── phase1 · 编排：完整三级探测 ─────────────────────────────────────
def detect_agents(base_url=None, assign_list=None):
    """三级探测 → (detection_meta, records)。永不抛异常。"""
    manual_map = {}
    for item in assign_list or []:
        if "=" in item:
            aid, role = item.split("=", 1)
            manual_map[aid.strip()] = role.strip()

    agents = api_list_agents(base_url)
    probe_sources = []
    mode = "auto"
    disable_reason = None
    records = []

    if agents is None:
        # L1 失败 → 降级：尝试从本机 workspaces 扫描 agent.json
        probe_sources.append("agent_json_scan")
        disable_reason = "no_api"
        ws_root = os.environ.get("COPAW_WORKSPACE", "")
        if ws_root:
            scan_roots = [ws_root]
        else:
            # 由 __file__ 推导 .copaw/workspaces（兼容 coPaw/QwenPaw 布局）
            home = os.path.expanduser("~")
            scan_roots = [os.path.join(home, ".copaw", "workspaces"),
                          os.path.join(home, ".qwenpaw", "workspaces")]
        seen_ids = set()
        scanned = []
        for root in scan_roots:
            if not os.path.isdir(root):
                continue
            try:
                ws_names = sorted(os.listdir(root))
            except Exception:
                continue
            for wsn in ws_names:
                ws = os.path.join(root, wsn)
                if not os.path.isdir(ws):
                    continue
                aj = read_agent_json(ws)
                if not aj or not aj.get("id"):
                    continue
                aid = aj["id"]
                if aid in seen_ids:
                    continue
                seen_ids.add(aid)
                scanned.append({
                    "id": aid,
                    "name": aj.get("name") or wsn,
                    "description": aj.get("description") or "",
                    "workspace_dir": ws,
                    "enabled": bool(aj.get("enabled", True)),
                    "startup_status": aj.get("startup_status") or "unknown",
                    "backend": aj.get("backend") or "",
                    "active_model": aj.get("active_model") or {},
                })
        agents = scanned or None

    if agents:
        probe_sources.append("list_agents_api")
        for agent in agents:
            if not isinstance(agent, dict):
                continue
            aid = str(agent.get("id") or "")
            if not aid:
                continue
            ws = norm_ws(agent.get("workspace_dir"))
            aj = read_agent_json(ws)
            if aj:
                probe_sources.append("agent_json")
            caps = extract_tools(aj)
            # L1 字段优先，agent.json 补 description/name
            if not agent.get("description") and aj.get("description"):
                agent["description"] = aj["description"]
            if not agent.get("name") and aj.get("name"):
                agent["name"] = aj["name"]
            if aj.get("enabled") is not None:
                agent.setdefault("enabled", aj["enabled"])
            sys_text = collect_sys_prompt_text(ws, aj)
            if sys_text:
                probe_sources.append("system_prompt_files")
            tags = desc_tags_for(agent)
            sys_tags = sys_tags_for(sys_text)
            manual_role = manual_map.get(aid)
            probe_error = None
            if not aj and ws and not os.path.isdir(ws):
                probe_error = "workspace_dir_unreachable"
            rec = build_capability_record(
                agent, caps, detect_python_available(), tags, sys_tags,
                manual_role=manual_role, probe_error=probe_error)
            records.append(rec)

    if not records:
        # 全部探测失败 → 用宿主自身兜底（运行 init 的 agent 工作区）
        mode = "single_agent"
        disable_reason = disable_reason or "no_agents"
        ws = norm_ws(os.environ.get("QWENPAW_AGENT_WORKSPACE")
                     or SKILL_DIR)
        aj = read_agent_json(ws)
        host = {
            "id": aj.get("id") or "local",
            "name": aj.get("name") or "host",
            "description": aj.get("description") or "",
            "workspace_dir": ws,
            "enabled": True,
            "startup_status": "running",
            "backend": aj.get("backend") or "",
            "active_model": aj.get("active_model") or {},
        }
        caps = extract_tools(aj)
        sys_text = collect_sys_prompt_text(ws, aj)
        tags = desc_tags_for(host)
        rec = build_capability_record(host, caps, detect_python_available(),
                                      tags, sys_tags_for(sys_text))
        records.append(rec)

    # 单 agent 判定：enabled 且有效记录 ≤1 个 → single_agent（规格 §1.3）
    enabled_recs = [r for r in records if r["enabled"]]
    if len(enabled_recs) <= 1:
        mode = "single_agent"

    # 多 agents 判定修正：记录>1 但 enabled==1 仍按 single_agent（上面已处理）
    return {
        "mode": mode,
        "agents_found": len(records),
        "agents_enabled": len(enabled_recs),
        "probe_sources": list(dict.fromkeys(probe_sources)),
        "disable_reason": disable_reason,
    }, records


# ── phase2 · UE 环境探测 ────────────────────────────────────────────
def probe_ue_environment(deploy_mod, engine_override=None, project_override=None):
    """复用 deploy.find_engine_path / find_project_path。

    返回 dict: engine_path / project_path / engine_plugin_dir / source_dir / verified
    找不到返回 None 字段（不视为失败）。
    """
    if deploy_mod is None:
        return {"engine_path": None, "project_path": None,
                "engine_plugin_dir": None, "source_dir": None,
                "verified": False, "verify_detail": "",
                "deploy_py": None}
    try:
        engine = engine_override or deploy_mod.find_engine_path()
    except Exception as e:
        print(f"[WARN] find_engine_path 异常: {e}")
        engine = None
    try:
        project = project_override or deploy_mod.find_project_path()
    except Exception as e:
        print(f"[WARN] find_project_path 异常: {e}")
        project = None

    source_dir = None
    verified = False
    verify_detail = ""
    try:
        source_dir = deploy_mod.resolve_source_dir(deploy_mod.SCRIPT_DIR)
        # verify_source 内部有 print（缺失清单），重定向捕获避免噪声
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            verified = bool(deploy_mod.verify_source(source_dir))
        verify_detail = buf.getvalue().strip()
    except Exception as e:
        verify_detail = f"verify_source 异常: {e}"

    engine_plugin_dir = None
    if engine:
        try:
            engine_plugin_dir = deploy_mod.get_engine_plugins_dir(engine)
        except Exception:
            pass

    return {
        "engine_path": engine,
        "project_path": project,
        "engine_plugin_dir": engine_plugin_dir,
        "source_dir": source_dir,
        "verified": verified,
        "verify_detail": verify_detail,
        "deploy_py": getattr(deploy_mod, "__file__", None),
    }


# ── phase4 · health 自检 ─────────────────────────────────────────────
def health_check(ue_env, deploy_mod):
    """分级自检 L0~L3，返回分级 dict。

    L0 进程（python 可跑）→ L1 依赖（import requests）
    L2 UE 层（引擎/项目/插件源 verify）→ L3 在线（可选，不做阻塞式连接）
    """
    res = {"L0_python": False, "L1_requests": False, "L2_ue_layer": None,
           "L3_online": None}

    # L0
    try:
        import sys as _s
        res["L0_python"] = bool(_s.executable) and os.path.isfile(_s.executable)
    except Exception:
        pass

    # L1
    try:
        import requests  # noqa: F401
        res["L1_requests"] = True
    except Exception:
        res["L1_requests"] = False

    # L2：UE 层 = 引擎/项目任一路径就绪 且 插件源文件 verify 通过
    engine_ok = bool(ue_env.get("engine_path"))
    project_ok = bool(ue_env.get("project_path"))
    if engine_ok or project_ok:
        if ue_env.get("verified"):
            res["L2_ue_layer"] = "ready"
        else:
            res["L2_ue_layer"] = "source_missing"
    else:
        res["L2_ue_layer"] = "not_ready"

    # L3：仅在 L2 ready 时给提示（不自动连 UE，避免阻塞）
    if res["L2_ue_layer"] == "ready":
        res["L3_online"] = "deferred"   # 需 UE 编辑器开启后手动验证
    else:
        res["L3_online"] = "skipped"

    return res


# ── phase5 · 报告 ────────────────────────────────────────────────────
def render_detection_summary(meta, role_map):
    lines = [
        f"mode={meta['mode']}",
        f"agents_found={meta['agents_found']}",
        f"agents_enabled={meta['agents_enabled']}",
        f"probe_sources={json.dumps(meta['probe_sources'], ensure_ascii=False)}",
        f"role_map={json.dumps(role_map, ensure_ascii=False)}",
    ]
    return " | ".join(lines)


def render_report(meta, records, role_map, ue_env, health, out_assign_path,
                  out_report_path, dry_run, status=STATUS_PROPOSED,
                  status_reason="", role_sources=None):
    """生成 init_report.md 文本。"""
    now = utc_now_iso()
    host = socket.gethostname()
    py = sys.version.split()[0]
    ue_state = ("ready" if (ue_env.get("engine_path") or ue_env.get("project_path"))
                else "not_ready")
    role_sources = role_sources or {}
    L = []
    L.append("# ue5-automation · init 报告")
    L.append("")
    L.append(f"> 生成时间: {now}  ")
    L.append(f"> host: {host} · python: {py}  ")
    L.append(f"> 模式: dry-run" if dry_run else f"> 模式: apply")
    L.append("")
    L.append("## 1. 探测摘要")
    L.append("")
    L.append(f"- detection.mode: **{meta['mode']}**")
    L.append(f"- agents_found: {meta['agents_found']} · agents_enabled: {meta['agents_enabled']}")
    L.append(f"- probe_sources: {', '.join(meta['probe_sources']) or '-'}")
    if meta.get("disable_reason"):
        L.append(f"- disable_reason: {meta['disable_reason']}")
    L.append("")
    L.append("## 1.1 分配状态（人工确认门）")
    L.append("")
    L.append(f"- status: **{status}**")
    L.append(f"- 说明: {status_reason or '-'}")
    if status == STATUS_PROPOSED:
        L.append("- ⚠️ 本 role_map 为**建议稿（proposed）**，未经人工确认，"
                 "**不作为生效配置**。")
        L.append("  生效方式：`--assign <id>=<role>` 显式确认核心角色，"
                 "或手编 role_assignments.yaml 后 `--reconcile --apply`。")
    else:
        L.append("- ✅ 本 role_map 已经确认/确定性指派，可作为生效配置。")
    L.append("")
    L.append("## 2. UE 环境")
    L.append("")
    L.append(f"- 状态: **{ue_state}**")
    L.append(f"- engine_path: {ue_env.get('engine_path') or '-'}")
    L.append(f"- project_path: {ue_env.get('project_path') or '-'}")
    L.append(f"- 插件源 verify: {'通过' if ue_env.get('verified') else '未通过/跳过'}")
    L.append("")
    L.append("## 3. health 自检")
    L.append("")
    L.append(f"- L0 python 进程: {'✅' if health['L0_python'] else '❌'}")
    L.append(f"- L1 依赖 requests: {'✅' if health['L1_requests'] else '❌ 需 pip install -r requirements.txt'}")
    L.append(f"- L2 UE 层: {health['L2_ue_layer']}")
    L.append(f"- L3 在线: {health['L3_online']}")
    L.append("")
    L.append("## 4. role_map")
    L.append("")
    L.append("```yaml")
    L.append(yaml_dump(role_map).rstrip())
    L.append("```")
    L.append("")
    L.append("## 4.1 role_sources（人工确认门）")
    L.append("")
    L.append("```yaml")
    L.append(yaml_dump(role_sources or {}).rstrip())
    L.append("```")
    L.append("")
    L.append("## 5. agents 明细")
    L.append("")
    for r in records:
        ue = r["ue_capability"]
        L.append(f"- **{r['agent_id']}** ({r['name']}) role=`{r['role']}` "
                 f"role_source=`{r.get('role_source', '-')}` "
                 f"ue_verdict={ue['verdict']}({ue['confidence']}) "
                 f"caps={json.dumps(r['capabilities'], ensure_ascii=False)}")
        if ue["evidence"].get("tags"):
            L.append(f"  - tags: {', '.join(ue['evidence']['tags'])}")
        if ue["evidence"].get("sys_tags"):
            L.append(f"  - sys_tags: {', '.join(ue['evidence']['sys_tags'])}")
        if ue.get("probe_error"):
            L.append(f"  - probe_error: {ue['probe_error']}")
    L.append("")
    L.append("## 6. 产物")
    L.append("")
    if dry_run:
        L.append("- ⚠️ dry-run：未写入任何文件。加 `--apply` 落盘：")
    L.append(f"- role_assignments.yaml → `{out_assign_path}`")
    L.append(f"- init_report.md → `{out_report_path}`")
    L.append("")
    L.append("## 7. 下一步建议")
    L.append("")
    if ue_state == "not_ready":
        L.append("- 未检测到 UE 引擎/项目：可离线使用 log_parser / analyzer 离线部分；")
        L.append("  UE 就绪后重跑 `python scripts/init.py --apply` 安装 Bridge 插件。")
        L.append("- 安装指引见 DEPLOY.md（用户侧手动安装 UE + Python 插件 + Remote Execution）。")
    else:
        L.append("- UE 已就绪：确认插件部署后打开 UE 编辑器，运行 "
                 "`python scripts/ue5_runner.py health` 做 L3 在线验证。")
    if meta["mode"] == "single_agent":
        L.append("- 当前为单 Agent 环境：宿主承担全部角色（all_in_one），SOP 自执行。")
    else:
        missing = [k for k in ("orchestrator", "ue_executor") if k not in role_map]
        if missing:
            L.append(f"- ⚠️ 关键角色缺失（{', '.join(missing)}）：可手编 "
                     "role_assignments.yaml 后重跑 `init.py --reconcile --apply`，"
                     "或用 `--assign <agent_id>=<role>` 显式指派。")
    if status == STATUS_PROPOSED and meta["mode"] != "single_agent":
        L.append("- ⚠️ 本文件为建议稿（status: proposed）。确认生效：")
        L.append("  1) 手编 role_assignments.yaml（角色/role_source）后，")
        L.append("     重跑 `python scripts/init.py --reconcile --apply`；")
        L.append("  2) 或 `python scripts/init.py --apply "
                 "--assign <agent_id>=<role> ...` 显式指派核心角色。")
    L.append("")
    return "\n".join(L)


# ── main ─────────────────────────────────────────────────────────────
def _decide_status(mode, role_sources, manual_channels, confirm_desc=""):
    """人工确认门：根据角色来源判定 role_assignments.yaml 的 status。

    - single_agent：确定性指派（all_in_one），无选择空间 → confirmed
    - 无 auto 残留且存在人工通道（--assign / --reconcile）→ confirmed
    - 有 auto 建议残留（含部分人工 + 部分自动）→ proposed
    - 纯自动建议（无人工通道）→ proposed
    返回 (status, status_reason)。
    """
    auto_roles = sorted(role for role, src in (role_sources or {}).items()
                        if src == SRC_AUTO)
    if mode == "single_agent":
        return STATUS_CONFIRMED, "single_agent 确定性指派（宿主 all_in_one，无选择空间）"
    if manual_channels and not auto_roles:
        who = confirm_desc or "--assign / --reconcile 显式指派"
        return STATUS_CONFIRMED, f"经人工确认通道（{who}），role_map 无自动建议残留"
    if auto_roles:
        detail = ", ".join(auto_roles)
        if manual_channels:
            return (STATUS_PROPOSED,
                    f"部分人工确认 + 仍含自动建议角色（{detail}），不作为生效配置")
        return (STATUS_PROPOSED,
                f"自动语义建议稿（预加载）：{detail} 未经人工确认，不作为生效配置")
    return STATUS_PROPOSED, "role_map 为空或无需确认（无生效配置）"


def _resolve_out_paths():
    """产物路径：默认 <skill>/output/（publish 黑名单，防漂移入包）。"""
    return os.path.join(OUT_DIR, ROLE_ASSIGN_FILE), os.path.join(OUT_DIR, REPORT_FILE)


def run(args):
    dry_run = not args.apply
    print("=" * 64)
    print(" init.py · ue5-automation skill 初始化/适配")
    print(" " + ("dry-run（只读，不落盘）" if dry_run else "apply（落盘）"))
    print("=" * 64)

    # phase0/1：探测
    meta, records = detect_agents(args.api_base, args.assign)
    print(f"\n[phase1] 能力探测 -> {render_detection_summary(meta, {})}")

    manual_map = {}
    for item in args.assign or []:
        if "=" in item:
            aid, role = item.split("=", 1)
            manual_map[aid.strip()] = role.strip()

    # --reconcile：读回既有 role_assignments.yaml 的手编角色
    out_assign, out_report = _resolve_out_paths()
    prev_manual = {}
    if args.reconcile and os.path.isfile(out_assign):
        prev = load_yaml_file(out_assign)
        if prev:
            agents_sec = prev.get("agents")
            if isinstance(agents_sec, list):
                for a in agents_sec:
                    if isinstance(a, dict) and a.get("role") not in (None, "unspecified"):
                        prev_manual[str(a["agent_id"])] = a["role"]
            # 兼容旧结构：role_map 键是角色 → 反推 agent 承担角色
            rm = prev.get("role_map")
            if isinstance(rm, dict):
                for role, aid in rm.items():
                    if aid and str(aid) not in prev_manual:
                        prev_manual[str(aid)] = role
        # --assign 优先于 reconcile 手编
        prev_manual.update(manual_map)
    elif args.assign:
        prev_manual = manual_map

    role_map, records = assign_roles(records, prev_manual, mode=meta["mode"])

    # ── 人工确认门：role_sources / status 基线 ──
    by_id = {r["agent_id"]: r for r in records}
    role_sources = {}
    for role, aid in role_map.items():
        rec = by_id.get(aid)
        role_sources[role] = rec.get("role_source", SRC_AUTO) if rec else SRC_AUTO
    manual_channels = bool(args.assign) or bool(args.reconcile)
    confirm_desc = []
    if args.reconcile:
        confirm_desc.append("--reconcile 读回手编 yaml")
    if args.assign:
        confirm_desc.append("--assign 显式指派")
    status, status_reason = _decide_status(
        meta["mode"], role_sources, manual_channels, " + ".join(confirm_desc))

    # phase2：UE 环境
    deploy_py = find_deploy_py(args.deploy_script)
    deploy_mod = import_deploy(deploy_py) if deploy_py else None
    ue_env = probe_ue_environment(deploy_mod)
    print(f"\n[phase2] deploy.py -> {deploy_py or '未找到'}")
    print(f"         UE engine={ue_env['engine_path'] or '-'}")
    print(f"         UE project={ue_env['project_path'] or '-'}")
    if ue_env.get("verify_detail"):
        # 只展示 verify 结论摘要（详细缺失清单入报告）
        first = ue_env["verify_detail"].splitlines()[:1]
        print(f"         verify_source: {'通过' if ue_env['verified'] else '未通过'} "
              f"({first[0] if first else ''})")

    # phase3：一键配置计划（默认 dry-run）
    print("\n[phase3] 配置计划：")
    deploy_action = []
    if ue_env.get("engine_path") or ue_env.get("project_path"):
        if ue_env.get("verified"):
            target = (f"engine={ue_env['engine_path']}" if ue_env["engine_path"]
                      else f"project={ue_env['project_path']}")
            deploy_action.append(f"部署 Bridge 插件 -> {target}")
            if args.apply:
                try:
                    if ue_env["engine_path"]:
                        deploy_mod.deploy_to_engine_plugins(
                            ue_env["source_dir"], ue_env["engine_path"])
                    else:
                        deploy_mod.deploy_to_target(
                            ue_env["source_dir"], ue_env["project_path"])
                    print("  [OK] Bridge 插件已部署")
                except Exception as e:
                    print(f"  [ERROR] 插件部署失败: {e}")
            else:
                print(f"  [计划] 插件部署 -> {target}（--apply 时执行）")
        else:
            deploy_action.append("插件源 verify 未通过，跳过部署")
            print("  [跳过] 插件源文件不完整（verify_source 失败），不执行空部署")
    else:
        print("  [跳过] 未检测到 UE 引擎/项目；离线可用功能不受影响")
    if not dry_run:
        pass

    # phase4：health
    health = health_check(ue_env, deploy_mod)
    print(f"\n[phase4] health: L0={health['L0_python']} "
          f"L1={health['L1_requests']} L2={health['L2_ue_layer']} "
          f"L3={health['L3_online']}")

    # phase5：产出（人工确认门）
    print("\n[phase5] 角色分配结果：")
    for k, v in role_map.items():
        src = role_sources.get(k, SRC_AUTO)
        print(f"         {k:20s} -> {v:30s} [{src}]")
    print(f"         status: {status}")
    print(f"         说明: {status_reason}")
    print(f"         role_assignments.yaml -> {out_assign}")
    print(f"         init_report.md        -> {out_report}")

    report_text = render_report(meta, records, role_map, ue_env, health,
                                out_assign, out_report, dry_run,
                                status=status, status_reason=status_reason,
                                role_sources=role_sources)

    if dry_run:
        print("\n" + "=" * 64)
        print(" DRY-RUN 完成：未写入任何文件。")
        if status == STATUS_PROPOSED:
            print(" 当前为自动建议稿（status: proposed），不写入生效配置。")
            print(" 生效方式：--assign <id>=<role> 显式指派核心角色，")
            print("          或手编 role_assignments.yaml 后 --reconcile --apply。")
        print(" 如需落盘角色分配与报告，执行: python init.py --apply")
        print("=" * 64)
        # dry-run 也打印报告到 stdout 供预览
        print("\n" + report_text)
        return 0

    # apply：人工确认门 gate（禁止静默自动覆盖）
    existing_status = None
    existing_role_map = None
    if os.path.isfile(out_assign):
        prev_doc = load_yaml_file(out_assign)
        if isinstance(prev_doc, dict):
            existing_status = prev_doc.get("status")
            existing_role_map = prev_doc.get("role_map")

    changed = existing_role_map != role_map

    # apply 时提示确认：TTY 下允许将 auto 建议升级为 manual（确认通道 c）
    # 放在 gate 之前，使交互确认能作为合法的覆盖确认通道
    if sys.stdin.isatty() and status == STATUS_PROPOSED:
        print("\n[提示] 当前 role_assignments.yaml 为建议稿（status: proposed），"
              "不作为生效配置。")
        if existing_status == STATUS_CONFIRMED and changed:
            print("       检测到既有人工确认配置（status=confirmed）将被本次"
                  "自动建议覆盖。")
        try:
            ans = input("       是否确认该建议稿为生效配置？[y/N]: ").strip().lower()
            if ans in ("y", "yes"):
                for role, aid in role_map.items():
                    rec = by_id.get(aid)
                    if rec and rec.get("role_source") == SRC_AUTO:
                        rec["role_source"] = SRC_MANUAL
                        role_sources[role] = SRC_MANUAL
                status = STATUS_CONFIRMED
                status_reason = "apply 时交互确认（用户 y）"
                manual_channels = True
                if "交互确认" not in confirm_desc:
                    confirm_desc.append("交互确认")
                print("        ✔ 已确认，将写入 status=confirmed")
        except EOFError:
            pass
    elif status == STATUS_PROPOSED:
        print("\n[提示] 本次写入的 role_assignments.yaml 为建议稿"
              "（status: proposed），不作为生效配置。")
        print("       确认生效（status: confirmed）方式：")
        print("         a) python scripts/init.py --apply "
              "--assign <id>=<role> ...（覆盖核心角色）")
        print("         b) 手编 role_assignments.yaml 后："
              "python scripts/init.py --reconcile --apply")
        print("       （非交互环境：未自动确认；如需生效请用 a/b 通道）")

    # gate（禁止静默自动覆盖）：既有 confirmed + 无确认通道 + role_map 变化 → 拒绝
    refuse = False
    if existing_status == STATUS_CONFIRMED and not manual_channels and changed:
        refuse = True
        reason_tail = (f"本次 role_map 将变化，但无人工确认通道"
                       f"（status 基线={status}）")
    print(f"\n[GATE] existing_status={existing_status} · new_status={status} "
          f"· manual_channels={manual_channels} · role_map_changed={changed}")
    if refuse:
        print("\n[GATE] 拒绝覆盖：output/role_assignments.yaml 已是人工确认配置"
              f"(status=confirmed)，{reason_tail}。")
        print("       禁止静默自动覆盖。请显式确认后重试：")
        print("         python scripts/init.py --apply "
              "--assign <id>=<role> [--assign <id2>=<role2> ...]")
        print("         或手编 role_assignments.yaml 后：")
        print("         python scripts/init.py --reconcile --apply")
        return 3

    # 既有 confirmed 且本次自动结果与之一致 → 不降级，保留确认态
    if (existing_status == STATUS_CONFIRMED and status == STATUS_PROPOSED
            and not changed):
        status = STATUS_CONFIRMED
        status_reason = "与既有 confirmed 配置一致，保留确认态（未静默改变）"
        for role, aid in role_map.items():
            rec = by_id.get(aid)
            if rec and rec.get("role_source") == SRC_AUTO:
                rec["role_source"] = SRC_MANUAL
            role_sources[role] = SRC_MANUAL
        if "保留确认态（与既有 confirmed 一致）" not in confirm_desc:
            confirm_desc.append("保留既有确认态")

    # apply：落盘（非破坏原则：仅写 output/，不删除既有文件）
    os.makedirs(OUT_DIR, exist_ok=True)
    confirmed_by = confirm_desc if status == STATUS_CONFIRMED else []
    assign_doc = {
        "schema_version": SCHEMA_VERSION,
        "generated_by": GENERATOR_TAG,
        "generated_at": utc_now_iso(),
        "host": {"machine": socket.gethostname(),
                 "python": sys.version.split()[0]},
        "status": status,
        "status_reason": status_reason,
        "confirmed_by": confirmed_by,
        "role_sources": role_sources,
        "detection": {
            "mode": meta["mode"],
            "agents_found": meta["agents_found"],
            "agents_enabled": meta["agents_enabled"],
            "ue_capable_count": sum(
                1 for r in records if r["ue_capability"]["verdict"]
                in ("capable", "manual")),
            "probe_sources": meta["probe_sources"],
            "disable_reason": meta.get("disable_reason"),
        },
        "role_map": role_map,
        "agents": records,
    }
    header = "# role_assignments.yaml · 由 ue5-automation/scripts/init.py 生成\n" \
             "# status: proposed=建议稿(未生效) / confirmed=已人工确认(生效)\n" \
             "# 可手编 role/role_source 后重跑 `init.py --reconcile --apply` 确认"
    try:
        with open(out_assign, "w", encoding="utf-8") as f:
            f.write(yaml_dump(assign_doc, header=header))
        with open(out_report, "w", encoding="utf-8") as f:
            f.write(report_text)
    except Exception as e:
        print(f"\n[ERROR] 写盘失败: {e}")
        return 2

    # 回读验证（P0：写入后回读）
    try:
        back = load_yaml_file(out_assign)
        ok = bool(back and back.get("schema_version") == SCHEMA_VERSION)
        rep_ok = os.path.isfile(out_report) and os.path.getsize(out_report) > 100
        print(f"\n[VERIFY] role_assignments.yaml 回读: "
              f"{'PASS' if ok else 'FAIL'} | init_report.md: "
              f"{'PASS' if rep_ok else 'FAIL'}")
        if not ok or not rep_ok:
            return 2
    except Exception as e:
        print(f"[VERIFY] 回读异常: {e}")
        return 2

    print("\n" + "=" * 64)
    print(" INIT 完成：")
    print(f"  - {out_assign}  (status: {status})")
    print(f"  - {out_report}")
    if status == STATUS_PROPOSED:
        print("  ⚠️ 当前配置为建议稿（status: proposed），未生效。")
        print("     生效：手编 role_assignments.yaml 后 `--reconcile --apply`，")
        print("           或用 `--assign` 显式指派核心角色。")
    else:
        print("  ✅ 配置已确认/确定性指派，可作为生效配置使用。")
    print("=" * 64)
    return 0


def main():
    ap = argparse.ArgumentParser(
        description="ue5-automation skill 初始化执行器（薄执行器 v1.1 · "
                    "自动语义分配=建议稿，生效须人工确认）")
    ap.add_argument("--apply", action="store_true",
                    help="落盘 role_assignments.yaml + init_report.md。"
                         "无人工确认通道(--assign/--reconcile)时写建议稿"
                         "(status: proposed)；禁止静默覆盖既有 confirmed 配置")
    ap.add_argument("--dry-run", dest="apply", action="store_false",
                    help="显式 dry-run（默认）")
    ap.set_defaults(apply=False)
    ap.add_argument("--assign", action="append", metavar="AGENT_ID=ROLE",
                    help="手动指派角色，可多次（如 mWNCXY=ue_executor）")
    ap.add_argument("--reconcile", action="store_true",
                    help="读回既有 role_assignments.yaml，合并手编角色")
    ap.add_argument("--deploy-script", default=None,
                    help="显式指定 deploy.py 路径（三级定位链最后防线）")
    ap.add_argument("--api-base", default=None,
                    help="agent API base（默认环境 QWENPAW_API_BASE 或 "
                         "http://127.0.0.1:8088）")
    args = ap.parse_args()
    sys.exit(run(args))


if __name__ == "__main__":
    main()
