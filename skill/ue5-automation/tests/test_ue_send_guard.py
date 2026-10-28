# -*- coding: utf-8 -*-
"""v6.1 读通道安全默认：ue_send 必须显式 opt-in（源码级契约）。"""
import io
import os

import pytest

_SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "scripts")


@pytest.mark.ue_offline
def test_remote_exec_default_denied():
    src = io.open(os.path.join(_SCRIPTS, "ue_send.py"), encoding="utf-8").read()
    assert "UE5_ALLOW_REMOTE_EXEC" in src
    assert "读通道默认关闭" in src
    assert "return 4" in src
