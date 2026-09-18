# -*- coding: utf-8 -*-
"""
UE5 自动化 Skill · 环境适配器
=============================
让任意 QwenPaw 环境一键适配此 skill 包。
自动检测环境 → 交互分配 Agent 角色 → 改写配置 → 部署 Bridge。

用法:
    python setup.py

纯标准库（Python >= 3.10），无第三方依赖。
"""

import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
from datetime import datetime
from pathlib import Path

# ── 常量 ────────────────────────────────────────────────────

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR / "skill" / "ue5-automation"
BRIDGE_DIR = SCRIPT_DIR / "bridge" / "BlueprintPythonBridge"
REQUIREMENTS_FILE = SCRIPT_DIR / "requirements.txt"
DEPLOY_CONFIG_FILE = SCRIPT_DIR / "deploy_config.json"

# 需要改写的目标文件
REWRITE_FILES = {
    "SKILL.md": SKILL_DIR / "SKILL.md",
    "workflow.md": SKILL_DIR / "workflow.md",
}

# Skill 需要的 5 个角色
REQUIRED_ROLES = {
    "scheduler": {
        "label": "总调度",
        "ability": "决策、任务分派、汇总交付",
        "agent_name_hint": "maintainer",
    },
    "executor": {
        "label": "Python/JS 执行",
        "ability": "代码编写、脚本自动化、UE Bridge 交互",
        "agent_name_hint": "dev",
    },
    "reporter": {
        "label": "报告撰写",
        "ability": "调研、文档生成、模板填充",
        "agent_name_hint": "docs-writer",
    },
    "auditor": {
        "label": "代码审核",
        "ability": "安全检查、合规验证、质量把控",
        "agent_name_hint": "QA",
    },
    "archivist": {
        "label": "复盘入库",
        "ability": "知识库维护、经验入库、模式管理",
        "agent_name_hint": "资料员",
    },
}

# UE5 引擎常见安装路径
KNOWN_UE5_DIRS = [
    r"C:\Program Files\Epic Games\UE_5.5",
    r"C:\Program Files\Epic Games\UE_5.4",
    r"C:\Program Files\Epic Games\UE_5.3",
    r"C:\Program Files\Epic Games\UE_5.2",
    r"C:\Program Files\Epic Games\UE_5.1",
    r"D:\Epic Games\UE_5.5",
    r"D:\Epic Games\UE_5.4",
    r"D:\Epic Games\UE_5.3",
    r"D:\Epic Games\UE_5.2",
    r"D:\Epic Games\UE_5.1",
    r"E:\Epic Games\UE_5.1",
]

# ── 工具函数 ────────────────────────────────────────────────


def _print_header():
    """打印顶部标题栏。"""
    print()
    print("=" * 60)
    print("    UE5 自动化 Skill · 环境适配器 v1.0")
    print("=" * 60)
    print()


def _print_step(step, total, title):
    """打印步骤标题。"""
    print(f"\n[{step}/{total}] {title}...")


def _ok(msg):
    """打印成功标记。"""
    print(f"  ✅ {msg}")


def _warn(msg):
    """打印警告标记。"""
    print(f"  ⚠️  {msg}")


def _fail(msg):
    """打印错误标记。"""
    print(f"  ❌ {msg}")


def _info(msg):
    """打印信息。"""
    print(f"  ℹ️  {msg}")


def _error_exit(msg):
    """打印错误并退出。"""
    print(f"\n❌ {msg}")
    sys.exit(1)


def _read_text(path):
    """读取 UTF-8 文本文件。"""
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _write_text(path, content):
    """写入 UTF-8 文本文件。"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _backup_file(path):
    """创建备份文件（路径.bak.{timestamp}）。"""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = path.with_suffix(path.suffix + f".bak.{ts}")
    shutil.copy2(path, backup)
    return backup


# ── 第 1 步：环境检查 ────────────────────────────────────────


def _check_python():
    """检查 Python 版本。"""
    ver = sys.version_info
    if ver < (3, 10):
        return False, f"Python {ver.major}.{ver.minor}.{ver.micro}（需要 >= 3.10）"
    return True, f"Python {ver.major}.{ver.minor}.{ver.micro}"


def _check_pip_deps():
    """检查并安装 pip 依赖。"""
    if not REQUIREMENTS_FILE.exists():
        return True, "requirements.txt 未找到，跳过 pip 检查"

    # 尝试安装
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-r",
             str(REQUIREMENTS_FILE), "--quiet"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            stderr = result.stderr.strip()
            return False, f"pip install 失败:\n{stderr[:200]}"
        return True, "pip 依赖已安装 (requests)"
    except FileNotFoundError:
        return False, "找不到 pip 命令"
    except subprocess.TimeoutExpired:
        return False, "pip install 超时"


def _find_ue5_from_registry():
    """从 Windows 注册表查找 UE5 引擎路径。"""
    if sys.platform != "win32":
        return None
    try:
        import winreg
        # v2.20：与 deploy.py 对齐 —— 5.1 优先（随包 DLL 仅适用 5.1），先查
        #   HKCU（Epic 非管理员安装写在此处）再 HKLM，加 WOW64_64KEY 防 32 位重定向。
        for version_key in ["5.1", "5.2", "5.3", "5.4", "5.5"]:
            for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
                try:
                    reg_path = (r"SOFTWARE\EpicGames\Unreal Engine"
                                + "\\" + version_key)
                    with winreg.OpenKey(root, reg_path, 0,
                                        winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
                        install_dir, _ = winreg.QueryValueEx(
                            key, "InstalledDirectory")
                        if install_dir and os.path.isdir(install_dir):
                            return install_dir
                except OSError:
                    continue
    except Exception as e:
        _warn(f"注册表查询异常: {e}")
    return None


def _find_ue5_from_known_dirs():
    """遍历常见安装目录。"""
    for candidate in KNOWN_UE5_DIRS:
        engine_exe = os.path.join(candidate, "Engine", "Binaries",
                                  "Win64", "UnrealEditor.exe")
        plugins_dir = os.path.join(candidate, "Engine", "Plugins")
        if os.path.isfile(engine_exe) and os.path.isdir(plugins_dir):
            return candidate
    return None


def _check_ue5_engine():
    """检测 UE5 引擎路径。"""
    # ① 环境变量
    env_path = os.environ.get("UE5_ENGINE_PATH", "")
    if env_path and os.path.isdir(env_path):
        return True, env_path.rstrip("\\/")

    # ② 注册表
    reg = _find_ue5_from_registry()
    if reg:
        return True, reg

    # ③ 常见目录
    known = _find_ue5_from_known_dirs()
    if known:
        return True, known

    return False, None


def _check_qwenpaw():
    """检查 QwenPaw 是否可用。"""
    try:
        # 尝试多种方式
        result = subprocess.run(
            ["qwenpaw", "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            version = result.stdout.strip() or result.stderr.strip()
            return True, f"QwenPaw 可用 ({version})"
    except FileNotFoundError:
        pass
    except subprocess.TimeoutExpired:
        pass

    # 尝试 copaw 作为别名
    try:
        result = subprocess.run(
            ["copaw", "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            version = result.stdout.strip() or result.stderr.strip()
            return True, f"QwenPaw (copaw) 可用 ({version})"
    except FileNotFoundError:
        pass
    except subprocess.TimeoutExpired:
        pass

    return False, "未检测到 qwenpaw 或 copaw CLI"


# ── 第 2 步：扫描 Agent ──────────────────────────────────────


def _list_agents():
    """获取目标环境的 Agent 列表。返回 [(name, id), ...] 或空列表。"""
    agents = []

    # 方式一：qwenpaw agents list
    for cmd_name in ["qwenpaw", "copaw"]:
        try:
            result = subprocess.run(
                [cmd_name, "agents", "list"],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode == 0 and result.stdout.strip():
                parsed = _parse_agent_list_output(result.stdout)
                if parsed:
                    return parsed
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue

    # 方式二：尝试 API
    try:
        import urllib.request
        import urllib.error
        req = urllib.request.Request(
            # v2.20：8889 是 ue5_bridge HTTP 端口，Agent API 在 8088（与 init.py 同源）
            "http://127.0.0.1:8088/api/agents",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if isinstance(data, list):
                for a in data:
                    name = a.get("name", a.get("id", "unknown"))
                    aid = a.get("id", a.get("agent_id", "unknown"))
                    agents.append((name, aid))
                return agents
    except Exception as e:
        _warn(f"Agent API 查询失败: {e}")

    # 方式三：尝试读 agent.json 文件
    try:
        workspace = os.environ.get(
            "QWENPAW_WORKSPACE",
            os.path.join(os.path.expanduser("~"), ".copaw", "workspaces",
                         "default"),
        )
        agent_json = os.path.join(workspace, "agent.json")
        if os.path.isfile(agent_json):
            with open(agent_json, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                aid = data.get("id", "unknown")
                name = data.get("name", data.get("profile", {}).get("name",
                                                                   aid))
                if isinstance(name, dict):
                    name = name.get("name", aid)
                agents.append((name, aid))
                return agents
    except Exception as e:
        _warn(f"agent.json 读取失败: {e}")

    return agents


def _parse_agent_list_output(output):
    """解析 qwenpaw agents list 输出。
    支持表格格式：
        ID       名称      能力
        default  maintainer      总调度
        mWNCXY   dev      Python/JS
    """
    agents = []
    lines = output.strip().split("\n")
    data_start = False
    for line in lines:
        line = line.strip()
        if not line:
            continue
        # 跳过表头和分隔线
        if re.match(r'^[-\s]+$', line):
            data_start = True
            continue
        if not data_start:
            # 检查是否表头行
            if re.search(r'\b(ID|标识|Agent)', line, re.IGNORECASE):
                data_start = False  # 这是表头
                continue
            # 也可能直接就是数据行
        # 尝试解析：按空白分割，第一列是 ID，后续是名称和能力
        parts = line.split(None, 2)
        if len(parts) >= 2:
            aid = parts[0].strip()
            name = parts[1].strip()
            ability = parts[2].strip() if len(parts) > 2 else ""
            agents.append((name, aid))
    return agents


# ── 第 3 步：交互式角色分配 ──────────────────────────────────


def _print_role_assignment_table(assignments, agents, shared_roles):
    """打印角色分配表格。"""
    print(f"\n  此 skill 需要以下角色：\n")
    print(f"  角色{'':16s}所需能力{'':18s}分配")
    print("  " + "─" * 62)

    for role_key, role_info in REQUIRED_ROLES.items():
        label = role_info["label"]
        ability = role_info["ability"]
        if role_key in assignments:
            assigned = assignments[role_key]
            shared_tag = " ⚠️兼任" if role_key in shared_roles else ""
            display = f"{assigned[0]} ({assigned[1]}){shared_tag}"
        else:
            display = "未分配 ⚠️"
        print(f"  {label:14s}  {ability:28s}  {display}")

    # 兼任提示
    if shared_roles:
        unique_agents = set(
            assignments[r][1] for r in assignments if r in assignments)
        print(f"\n  ⚠️ 目标环境有 {len(agents)} 个 Agent，"
              f"{len(REQUIRED_ROLES)} 个角色需由 {len(unique_agents)} 个 Agent "
              f"兼任。")


def _auto_suggest_assignments(agents):
    """自动建议角色分配。"""
    suggestions = {}
    agent_map = {name.lower(): (name, aid) for name, aid in agents}
    # 也按 ID 建索引
    agent_by_id = {aid: (name, aid) for name, aid in agents}

    # 按 hint 匹配
    for role_key, role_info in REQUIRED_ROLES.items():
        hint = role_info["agent_name_hint"].lower()
        # 精确匹配名称
        if hint in agent_map:
            suggestions[role_key] = agent_map[hint]
            continue
        # 模糊匹配：名称包含 hint 或 hint 包含名称
        for name, aid in agents:
            if hint in name.lower() or name.lower() in hint:
                suggestions[role_key] = (name, aid)
                break
        else:
            # 没匹配到，按角色特性分配
            if role_key == "scheduler":
                # 总调度给第一个 Agent（通常是 default）
                suggestions[role_key] = agents[0]
            elif role_key == "executor":
                # Python/JS 执行给有编程能力的
                for name, aid in agents:
                    nl = name.lower()
                    if any(kw in nl for kw in ["python", "js", "code",
                                               "代码", "开发", "engineer"]):
                        suggestions[role_key] = (name, aid)
                        break
                else:
                    suggestions[role_key] = agents[0]  # fallback
            elif role_key == "reporter":
                suggestions[role_key] = agents[-1] if len(
                    agents) > 1 else agents[0]
            elif role_key == "auditor":
                # 审核复用 executor
                suggestions[role_key] = suggestions.get(
                    "executor", agents[0])
            elif role_key == "archivist":
                suggestions[role_key] = agents[-1] if len(
                    agents) > 2 else agents[0]

    return suggestions


def _interactive_role_assignment(agents):
    """交互式角色分配。返回 {(name, id): role_key, ...}。"""
    if not agents:
        _error_exit("未检测到 Agent，无法继续。请确认 QwenPaw 环境已正确配置。")

    suggestions = _auto_suggest_assignments(agents)

    # 检测兼任
    unique_ids = set(s[1] for s in suggestions.values())
    shared_roles = set()
    id_count = {}
    for role_key, (name, aid) in suggestions.items():
        id_count[aid] = id_count.get(aid, 0) + 1
        if id_count[aid] > 1:
            shared_roles.add(role_key)

    _print_role_assignment_table(suggestions, agents, shared_roles)

    print()
    choice = input("  是否接受建议分配？[y] 接受 / [n] 手动调整\n  > ").strip(
    ).lower()

    if choice in ("y", "yes", ""):
        return suggestions

    # 手动调整
    print("\n  手动分配（输入 Agent 序号或 ID）：\n")
    for i, (name, aid) in enumerate(agents, 1):
        print(f"    {i}. {name} ({aid})")
    print()

    assignments = {}
    assigned_ids = {}
    for role_key, role_info in REQUIRED_ROLES.items():
        default = suggestions[role_key]
        default_idx = None
        for i, (name, aid) in enumerate(agents, 1):
            if aid == default[1]:
                default_idx = i
                break

        prompt = (f"  [{role_info['label']}] {role_info['ability']}\n"
                  f"  选择 Agent (1-{len(agents)})"
                  + (f" [默认 {default_idx}]" if default_idx else "")
                  + " > ")
        while True:
            user_in = input(prompt).strip()
            if not user_in and default:
                sel_agent = default
                break
            # 按序号
            try:
                idx = int(user_in)
                if 1 <= idx <= len(agents):
                    sel_agent = agents[idx - 1]
                    break
            except ValueError:
                pass
            # 按 ID 匹配
            for name, aid in agents:
                if user_in.lower() == aid.lower():
                    sel_agent = (name, aid)
                    break
                if user_in.lower() in name.lower():
                    sel_agent = (name, aid)
                    break
            else:
                print(f"    无效输入，请重新选择 (1-{len(agents)})")
                continue
            break

        assignments[role_key] = sel_agent
        assigned_ids[sel_agent[1]] = assigned_ids.get(sel_agent[1], 0) + 1

    # 显示确认表
    shared_roles_final = set()
    for role_key, (name, aid) in assignments.items():
        if assigned_ids.get(aid, 0) > 1:
            shared_roles_final.add(role_key)

    print()
    _print_role_assignment_table(assignments, agents, shared_roles_final)
    print()
    confirm = input("  确认以上分配？[y] / [n 重新分配]\n  > ").strip().lower()
    if confirm not in ("y", "yes", ""):
        return _interactive_role_assignment(agents)

    return assignments


# ── 第 4 步：改写配置文件 ────────────────────────────────────


def _rewrite_file(path, assignments):
    """改写单个配置文件中的 Agent 引用。
    策略：在文件头部插入部署映射块；对文中明显的 Agent 角色引用
    （如"dev（引擎交互）"）追加 ID 信息。
    """
    content = _read_text(path)

    # 检查是否已有部署映射（重复运行处理）
    if "<!-- DEPLOY_MAPPING_START -->" in content:
        content = re.sub(
            r'<!-- DEPLOY_MAPPING_START -->.*?<!-- DEPLOY_MAPPING_END -->\n*',
            '',
            content,
            flags=re.DOTALL,
        )

    # 构建角色映射块的文本
    role_lines = []
    for role_key, (name, aid) in assignments.items():
        role_info = REQUIRED_ROLES[role_key]
        role_lines.append(
            f"| {role_info['label']} | {name} | `{aid}` |")

    deploy_block = textwrap.dedent(f"""\
    <!-- DEPLOY_MAPPING_START -->
    > **📋 部署映射**（由 setup.py {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} 生成）
    >
    > | 角色 | Agent | ID |
    > |:---|---:|:---|
    > {chr(10).join('> ' + line for line in role_lines)}
    <!-- DEPLOY_MAPPING_END -->

    """)

    # 插入到第一个标题前或文件开头
    first_heading = re.search(r'^#', content, re.MULTILINE)
    if first_heading:
        insert_pos = first_heading.start()
        new_content = content[:insert_pos] + deploy_block + content[
            insert_pos:]
    else:
        new_content = deploy_block + content

    return new_content


def _generate_deploy_config(ue5_path, assignments, python_version):
    """生成 deploy_config.json。"""
    roles = {}
    for role_key, (name, aid) in assignments.items():
        role_info = REQUIRED_ROLES[role_key]
        entry = {"name": name, "id": aid, "label": role_info["label"]}
        # 标记兼任
        all_ids = [a[1] for a in assignments.values()]
        if all_ids.count(aid) > 1:
            entry["shared"] = True
        roles[role_key] = entry

    config = {
        "deployed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source_package": "ue5-automation-skill-package v2.0",
        "python_version": python_version,
        "ue5_engine_path": ue5_path,
        "roles": roles,
        "files_rewritten": ["skill/ue5-automation/SKILL.md",
                            "skill/ue5-automation/workflow.md"],
        "bridge_deployed": False,  # 第 5 步更新
    }

    return config


# ── 第 5 步：部署 Bridge ─────────────────────────────────────


def _deploy_bridge(ue5_path):
    """部署 C++ Bridge DLL 到 UE5 引擎目录。"""
    # v2.20：发布包布局 DLL 在 bridge/BlueprintPythonBridge/Binaries/Win64/ 下
    #   （旧路径指向插件根目录 → 恒不存在 → 自动部署为死代码）；兼容两种布局。
    dll_src = BRIDGE_DIR / "Binaries" / "Win64" / "UnrealEditor-BlueprintPythonBridge.dll"
    if not dll_src.exists():
        dll_src = BRIDGE_DIR / "UnrealEditor-BlueprintPythonBridge.dll"
    pdb_src = BRIDGE_DIR / "Binaries" / "Win64" / "UnrealEditor-BlueprintPythonBridge.pdb"

    if not dll_src.exists():
        _warn(f"Bridge DLL 未找到: {dll_src}")
        _info("请手动将 bridge/BlueprintPythonBridge/ 下的文件部署到 UE5 引擎")
        return False

    target_plugins = os.path.join(ue5_path, "Engine", "Plugins",
                                  "BlueprintPythonBridge")

    try:
        # 创建目标目录
        os.makedirs(os.path.join(target_plugins, "Binaries", "Win64"),
                    exist_ok=True)

        # 复制 DLL
        dll_dst = os.path.join(target_plugins, "Binaries", "Win64",
                               dll_src.name)
        shutil.copy2(dll_src, dll_dst)
        _ok(f"DLL 已部署到 {dll_dst}")

        # 复制 PDB（可选）
        if pdb_src.exists():
            pdb_dst = os.path.join(target_plugins, "Binaries", "Win64",
                                   pdb_src.name)
            shutil.copy2(pdb_src, pdb_dst)

        # 复制 uplugin（如果存在）
        uplugin_src = BRIDGE_DIR / "BlueprintPythonBridge.uplugin"
        if uplugin_src.exists():
            shutil.copy2(uplugin_src, os.path.join(target_plugins,
                                                   uplugin_src.name))

        return True

    except PermissionError:
        _fail("没有写入权限。请以管理员身份运行或手动复制文件。")
        _info(f"DLL 源路径: {dll_src}")
        return False
    except Exception as e:
        _fail(f"部署 Bridge 时出错: {e}")
        return False


# ── 主流程 ────────────────────────────────────────────────────


def main():
    # ── 重复运行检测 ──
    if DEPLOY_CONFIG_FILE.exists():
        print("\n⚠️  检测到已存在的配置文件: deploy_config.json")
        print("  这意味着之前已经运行过适配。")
        choice = input("\n  是否重新适配？[y]/[n 退出]\n  > ").strip().lower()
        if choice not in ("y", "yes"):
            print("  已取消。")
            return

    _print_header()

    # ═══════════════════════════════════════════════════════════
    # 第 1 步：环境检查
    # ═══════════════════════════════════════════════════════════
    _print_step(1, 5, "检查运行环境")

    # Python 版本
    py_ok, py_msg = _check_python()
    if py_ok:
        _ok(py_msg)
    else:
        _error_exit(py_msg)

    # pip 依赖
    pip_ok, pip_msg = _check_pip_deps()
    if pip_ok:
        _ok(pip_msg)
    else:
        _error_exit(pip_msg)

    # UE5 引擎
    ue5_ok, ue5_path = _check_ue5_engine()
    if ue5_ok:
        _ok(f"UE5 引擎: {ue5_path}")
    else:
        _warn("未检测到 UE5 引擎")
        ue5_path = input(
            "  请手动输入 UE5 引擎根目录（如 D:\\UE_5.1）：\n  > "
        ).strip()
        if not ue5_path or not os.path.isdir(ue5_path):
            _warn("未指定有效的 UE5 路径，Bridge 部署将跳过。")
            ue5_path = "未指定"
        else:
            _ok(f"手动指定: {ue5_path}")

    # QwenPaw
    qp_ok, qp_msg = _check_qwenpaw()
    if qp_ok:
        _ok(qp_msg)
    else:
        _warn(qp_msg + "（将尝试其他方式获取 Agent 列表）")

    # ═══════════════════════════════════════════════════════════
    # 第 2 步：扫描 Agent
    # ═══════════════════════════════════════════════════════════
    _print_step(2, 5, "扫描可用 Agent")
    agents = _list_agents()

    if agents:
        print(f"  检测到 {len(agents)} 个 Agent：")
        for i, (name, aid) in enumerate(agents, 1):
            print(f"    {i}. {name:14s} ({aid})")
    else:
        _warn("无法自动获取 Agent 列表。")
        print("\n  请手动输入 Agent 信息。")
        while True:
            name = input("  Agent 名称（如 maintainer，输入空行结束）: ").strip()
            if not name:
                break
            aid = input(f"  {name} 的 ID: ").strip()
            if aid:
                agents.append((name, aid))
            print()

        if not agents:
            _error_exit("未添加任何 Agent，无法继续。")

    # ═══════════════════════════════════════════════════════════
    # 第 3 步：交互式角色分配
    # ═══════════════════════════════════════════════════════════
    _print_step(3, 5, "角色分配")
    assignments = _interactive_role_assignment(agents)

    # ═══════════════════════════════════════════════════════════
    # 第 4 步：改写配置文件
    # ═══════════════════════════════════════════════════════════
    _print_step(4, 5, "改写配置文件")

    rewritten = []
    for label, filepath in REWRITE_FILES.items():
        if not filepath.exists():
            _warn(f"{label} 未找到，跳过: {filepath}")
            continue
        # 备份
        backup = _backup_file(filepath)
        _info(f"已备份: {backup.name}")

        # 改写
        new_content = _rewrite_file(filepath, assignments)
        _write_text(filepath, new_content)
        _ok(f"{label} → Agent 引用已更新")
        rewritten.append(str(filepath.relative_to(SCRIPT_DIR)))

    # 生成 deploy_config.json
    deploy_config = _generate_deploy_config(ue5_path, assignments, py_msg)
    _write_text(DEPLOY_CONFIG_FILE, json.dumps(deploy_config,
                                               ensure_ascii=False,
                                               indent=2))
    _ok("已生成映射文件: deploy_config.json")

    # ═══════════════════════════════════════════════════════════
    # 第 5 步：部署 Bridge
    # ═══════════════════════════════════════════════════════════
    _print_step(5, 5, "部署 Bridge")

    bridge_ok = False
    if ue5_path and ue5_path != "未指定":
        print(f"\n  检测到 UE5 引擎: {ue5_path}")
        print("  正在部署 BlueprintPythonBridge...")
        bridge_ok = _deploy_bridge(ue5_path)

    if not bridge_ok:
        _warn("Bridge 部署未完成，请手动部署。")

    # 更新 deploy_config
    deploy_config["bridge_deployed"] = bridge_ok
    _write_text(DEPLOY_CONFIG_FILE, json.dumps(deploy_config,
                                               ensure_ascii=False,
                                               indent=2))

    # ═══════════════════════════════════════════════════════════
    # 完成
    # ═══════════════════════════════════════════════════════════
    print()
    print("  " + "─" * 48)
    print("  适配完成！下一步：")
    print()

    if bridge_ok:
        print("    1. 重启 UE5 编辑器")
        print("    2. Edit → Plugins → 搜索 BlueprintPythonBridge → 启用")
        print("    3. 确认 Remote Execution 已启用:")
        print("       Edit → Project Settings → Plugins → Python → ☑ Enable Remote Execution")
        print("    4. 测试连通性:")
        print("       python skill/ue5-automation/scripts/ue5_runner.py health")
        print("    5. 回到总调度 Agent，说\"分析 BP_xxx\"即可使用")
    else:
        print("    1. 手动将 bridge/BlueprintPythonBridge/ 文件复制到 UE5 引擎")
        print("    2. 重启 UE5 编辑器")
        print("    3. Edit → Plugins → 搜索 BlueprintPythonBridge → 启用")
        print("    4. 确认 Remote Execution 已启用:")
        print("       Edit → Project Settings → Plugins → Python → ☑ Enable Remote Execution")
        print("    5. 测试连通性:")
        print("       python skill/ue5-automation/scripts/ue5_runner.py health")
        print("    6. 回到总调度 Agent，说\"分析 BP_xxx\"即可使用")

    print("  " + "─" * 48)
    print(f"\n  配置文件: {DEPLOY_CONFIG_FILE.name}")
    print(f"  已改写: {', '.join(rewritten)}")
    print()
    print("=" * 60)
    print()


# ── 入口 ──────────────────────────────────────────────────────

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n⚠️  已取消。未保存的更改已回滚（备份文件保留）。")
        print("  下次运行将检测 deploy_config.json 并提示是否重新适配。")
        sys.exit(0)
    except Exception as e:
        print(f"\n\n❌ 未预期的错误: {e}")
        print("  请将以上错误信息提交给开发者。")
        sys.exit(1)
