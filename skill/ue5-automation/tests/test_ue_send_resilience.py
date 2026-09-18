# -*- coding: utf-8 -*-
"""
test_ue_send_resilience.py — ue_send 断连兜底测试（P1-1 · T-20260805-UE-FULL-CLOSURE）
========================================================================================
不需要真 UE 即可运行（全部标 @pytest.mark.ue_offline）。

背景（T-20260805-UE-FULL-CLOSURE P1-1）：
  ue_send.py 原先在"未发现 UE 节点 / 远程执行异常 / UE 端执行失败"时只打印
  [错误] 信息并静默 return（CLI exit code 恒为 0）→ 调用方无法感知失败。
  本次为 send() 引入明确错误码：
      0 = 成功
      1 = 未发现 UE 节点（UE 不在线）
      2 = UE 端执行失败（run_command success=False）
      3 = remote_execution 调用异常（不吐 raw traceback）
  CLI 入口 main() 改为 sys.exit(send(...))。

覆盖测试：
  1. test_ue_send_ping_no_ue — UE 不在线 → send() 返回 1 + 明确错误信息 + 无 Traceback
  2. test_ue_send_retry — UE 在线执行成功 → UE 掉线 → 再连失败返回 1（缓存失效重试）
  3. test_ue_send_cli_no_ue — CLI 层：python ue_send.py "code" 在 UE 不在线时
     exit code = 1（subprocess 真跑脚本，注入 fake remote_execution）

方法：monkeypatch sys.modules['remote_execution'] 注入 fake 模块，
      fake RemoteExecution.remote_nodes 可控（节点存在/消失），
      time.sleep 打桩为 no-op（避免 15s×3 的发现等待）。

作者：dev（代码开发 Agent）
日期：2026-08-05
"""

import os
import sys
import time
import types
import subprocess
import pytest

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS_DIR = os.path.join(os.path.dirname(_THIS_DIR), "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)


# ══════════════════════════════════════════════════════════════
# Fake remote_execution（可控节点列表）
# ══════════════════════════════════════════════════════════════

_fake_nodes = []  # 模块级可变列表：测试通过增删模拟 UE 上线/掉线


class _FakeRemoteExecution:
    """模拟 remote_execution.RemoteExecution。"""

    MODE_EXEC_FILE = "exec_file"

    def __init__(self):
        self.remote_nodes = list(_fake_nodes)

    def start(self):
        pass

    def stop(self):
        pass

    def open_command_connection(self, node_id):
        pass

    def run_command(self, code, **kwargs):
        return {"success": True, "output": [{"output": "PROBE_OK\n"}]}


# 注入 fake remote_execution 到 sys.modules，再 import ue_send（避免真导入 exit 1）
_fake_rex_mod = types.ModuleType("remote_execution")
_fake_rex_mod.RemoteExecution = _FakeRemoteExecution
_fake_rex_mod.MODE_EXEC_FILE = "exec_file"
sys.modules.setdefault("remote_execution", _fake_rex_mod)

import ue_send  # noqa: E402  （conftest 已注入 scripts/ 到 sys.path）


@pytest.fixture(autouse=True)
def _clean_state():
    """每个测试前清空 fake 节点 + 缓存，保证隔离。"""
    _fake_nodes.clear()
    ue_send._CACHED_NODE = None
    yield
    _fake_nodes.clear()
    ue_send._CACHED_NODE = None


@pytest.fixture()
def _no_sleep(monkeypatch):
    """打桩 time.sleep 为 no-op（避免 15s×3 发现等待）。"""
    monkeypatch.setattr(time, "sleep", lambda s: None)


# ══════════════════════════════════════════════════════════════
# 测试 1：UE 不在线 → 明确错误码 1，无 raw traceback
# ══════════════════════════════════════════════════════════════

@pytest.mark.ue_offline
def test_ue_send_ping_no_ue(_no_sleep, capsys):
    """UE 不在线：send() 返回错误码 1，输出明确 [错误]，不吐 Traceback。"""
    _fake_nodes.clear()  # 无 UE 节点

    rc = ue_send.send("print('hello')")

    out = capsys.readouterr().out
    assert rc == 1, f"UE 不在线应返回错误码 1，实际 {rc}"
    assert "[错误] 未发现 UE 节点" in out, f"应输出明确错误信息，实际: {out!r}"
    assert "Traceback" not in out, f"不应吐 raw traceback，实际: {out!r}"
    assert "remote_execution" not in out.lower() or "[错误]" in out, (
        "错误信息应为人类可读，而非模块级 Traceback"
    )


# ══════════════════════════════════════════════════════════════
# 测试 2：先连后断 → 缓存失效 → 再连失败返回 1
# ══════════════════════════════════════════════════════════════

@pytest.mark.ue_offline
def test_ue_send_retry(_no_sleep, capsys):
    """UE 在线成功 → UE 掉线 → 再连失败返回错误码 1（缓存失效重试路径）。"""
    # ── 第一轮：UE 在线 → 成功 ──
    _fake_nodes.append({"node_id": "fake-ue-1", "project_name": "FakeProj"})
    rc1 = ue_send.send("print('ok')")
    out1 = capsys.readouterr().out

    assert rc1 == 0, f"UE 在线应返回 0，实际 {rc1}"
    assert "PROBE_OK" in out1, f"应透传 UE 输出，实际: {out1!r}"
    assert ue_send._CACHED_NODE == "fake-ue-1", "成功后应缓存节点 id"

    # ── 第二轮：UE 掉线（fake 节点消失）→ 缓存失效 → 重新发现失败 ──
    _fake_nodes.clear()
    rc2 = ue_send.send("print('retry')")
    out2 = capsys.readouterr().out

    assert rc2 == 1, f"UE 掉线后应返回错误码 1，实际 {rc2}"
    assert "[错误] 未发现 UE 节点" in out2, f"应输出明确错误信息，实际: {out2!r}"
    assert "Traceback" not in out2, f"不应吐 raw traceback，实际: {out2!r}"
    assert ue_send._CACHED_NODE is None, "失败后缓存应失效"


# ══════════════════════════════════════════════════════════════
# 测试 3：CLI 层 exit code = 1（真实 subprocess 跑 ue_send.py）
# ══════════════════════════════════════════════════════════════

@pytest.mark.ue_offline
def test_ue_send_cli_no_ue(_no_sleep, tmp_path):
    """CLI：python ue_send.py 'code' 在 UE 不在线时 exit code = 1，无 Traceback。"""
    runner = tmp_path / "run_cli_ue_send.py"
    runner.write_text(
        "\n".join([
            "# -*- coding: utf-8 -*-",
            "import sys, types",
            "import os",
            "_mod = types.ModuleType('remote_execution')",
            "class _FakeREX:",
            "    MODE_EXEC_FILE = 'exec_file'",
            "    def __init__(self):",
            "        self.remote_nodes = []",
            "    def start(self): pass",
            "    def stop(self): pass",
            "    def open_command_connection(self, n): pass",
            "    def run_command(self, code, **kw):",
            "        return {'success': True, 'output': []}",
            "_mod.RemoteExecution = _FakeREX",
            "_mod.MODE_EXEC_FILE = 'exec_file'",
            "sys.modules['remote_execution'] = _mod",
            f"sys.path.insert(0, {_SCRIPTS_DIR!r})",
            "import ue_send",
            "rc = ue_send.send('print(1)')",
            "print(f'RC={rc}')",
            "sys.exit(rc)",
        ]),
        encoding="utf-8",
    )

    # T-20260805-UE-FULL-CLOSURE Bug4: 必须显式 utf-8 解码，否则 cmd 默认 GBK
    # 解码失败时 r.stdout=None → "unsupported operand type(s) for +: 'NoneType' and 'str'"
    # 且必须 env 注入 PYTHONIOENCODING，否则子进程 stdout 仍是 GBK → utf-8 解码乱码
    _env = os.environ.copy()
    _env["PYTHONIOENCODING"] = "utf-8"
    r = subprocess.run(
        [sys.executable, str(runner)],
        capture_output=True, text=True, timeout=60,
        cwd=_SCRIPTS_DIR,
        encoding="utf-8", errors="replace",
        env=_env,
    )
    raw = r.stdout + r.stderr

    assert r.returncode == 1, f"UE 不在线 CLI exit code 应为 1，实际 {r.returncode}"
    assert "[错误] 未发现 UE 节点" in raw, f"应输出明确错误信息，实际: {raw[-300:]}"
    assert "Traceback" not in raw, f"不应吐 raw traceback，实际: {raw[-300:]}"
    assert "RC=1" in r.stdout, f"runner 应透传 send() 返回码，实际: {r.stdout!r}"
