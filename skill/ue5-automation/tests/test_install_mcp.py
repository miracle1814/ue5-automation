# -*- coding: utf-8 -*-
"""install_mcp 一键接入契约测试（v4.9 · 离线）

锁定：
  ① ZCode 配置形态（~/.zcode/cli/config.json → mcp.servers）与保留其他键；
  ② 只写“检测到”的客户端（--all 才强制），默认不制造垃圾配置；
  ③ --dry-run 绝不落盘；
  ④ 幂等：重复写入不产生重复条目。
"""
import importlib.util
import json
import os
import sys

import pytest

_SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "scripts")


def _load():
    spec = importlib.util.spec_from_file_location(
        "install_mcp_mod", os.path.join(_SCRIPTS, "install_mcp.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


im = _load()


def test_upsert_zcode_preserves_other_servers():
    entry = {"plugins": {"x": 1},
             "mcp": {"servers": {"unreal": {"type": "http",
                                            "url": "http://127.0.0.1:8000/mcp"}}}}
    im.upsert_zcode(entry, False)
    servers = entry["mcp"]["servers"]
    assert servers["unreal"]["type"] == "http"
    assert servers["ue5-automation"]["command"]
    assert servers["ue5-automation"]["args"]
    # 幂等
    im.upsert_zcode(entry, False)
    assert list(servers).count("ue5-automation") == 1
    # 移除只删自己
    im.upsert_zcode(entry, True)
    assert "ue5-automation" not in servers and "unreal" in servers
    assert entry["plugins"] == {"x": 1}


def test_target_exists_detects_parent_dir(tmp_path):
    target = tmp_path / ".cursor" / "mcp.json"
    assert im._target_exists("cursor", target) is False
    target.parent.mkdir()
    assert im._target_exists("cursor", target) is True


def test_default_writes_only_detected(monkeypatch, tmp_path, capsys):
    cfg = tmp_path / ".client" / "mcp.json"
    monkeypatch.setattr(im, "user_clients",
                        lambda: {"tmp": (cfg, im.upsert, "Tmp Client")})
    monkeypatch.setattr(sys, "argv", ["install_mcp.py"])
    rc = im.main()
    assert rc == 2  # 没检测到任何客户端 → 不写
    assert not cfg.exists()
    assert "未检测到" in capsys.readouterr().out


def test_dry_run_does_not_write(monkeypatch, tmp_path):
    cfg = tmp_path / ".client" / "mcp.json"
    cfg.parent.mkdir()
    monkeypatch.setattr(im, "user_clients",
                        lambda: {"tmp": (cfg, im.upsert, "Tmp Client")})
    monkeypatch.setattr(sys, "argv", ["install_mcp.py", "--dry-run"])
    rc = im.main()
    assert rc == 0
    assert not cfg.exists()


def test_write_is_idempotent(monkeypatch, tmp_path):
    cfg = tmp_path / ".client" / "mcp.json"
    cfg.parent.mkdir()
    cfg.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}),
                   encoding="utf-8")
    monkeypatch.setattr(im, "user_clients",
                        lambda: {"tmp": (cfg, im.upsert, "Tmp Client")})
    monkeypatch.setattr(sys, "argv", ["install_mcp.py"])
    assert im.main() == 0
    first = json.loads(cfg.read_text(encoding="utf-8"))
    assert first["mcpServers"]["other"]["command"] == "x"
    assert "ue5-automation" in first["mcpServers"]
    assert im.main() == 0
    second = json.loads(cfg.read_text(encoding="utf-8"))
    assert list(second["mcpServers"]).count("ue5-automation") == 1
    # 备份文件存在（首次写入前）
    assert any(p.name.startswith("mcp.json.bak-") for p in cfg.parent.iterdir())
