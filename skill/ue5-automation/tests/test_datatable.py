# -*- coding: utf-8 -*-
"""v6.0 DataTable 命令契约（离线）。"""
import os
import sys

os.environ["UE5_BRIDGE_TEST_MODE"] = "1"
_SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPTS = os.path.join(_SKILL_ROOT, "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import pytest  # noqa: E402
import ue5_bridge as B  # noqa: E402


class FakeName:
    def __init__(self, v):
        self.v = v

    def __str__(self):
        return self.v


class FakeTable:
    def __init__(self):
        self.rows = [FakeName("Row1"), FakeName("Row2")]

    def get_row_names(self):
        return self.rows

    def export_to_csv_string(self):
        return "---,Tag\nRow1,x\nRow2,y\n"

    def get_row_struct(self):
        return None

    def fill_from_csv_string(self, csv):
        self.rows = [FakeName("Row1")]
        return True


@pytest.fixture
def table(monkeypatch):
    t = FakeTable()
    monkeypatch.setattr(B, "_dt_require_table", lambda p: t)
    return t


def test_read_serializes_fname_rows(table):
    r = B._cmd_datatable_read("/Game/T/DT_X")
    d = r["data"]
    assert d["row_names"] == ["Row1", "Row2"]
    assert all(isinstance(x, str) for x in d["row_names"])
    assert d["row_count"] == 2 and "Row1" in d["csv"]


def test_write_requires_confirm(table):
    with pytest.raises(B.BridgeError) as ei:
        B._cmd_datatable_write_csv("/Game/T/DT_X", "---,Tag\nRow1,x\n")
    assert ei.value.category == "destructive_guard"


def test_write_serializes_rows_and_saves(table):
    r = B._cmd_datatable_write_csv("/Game/T/DT_X", "---,Tag\nRow1,x\n",
                                   confirm=True)
    assert r["success"] is True
    assert r["data"]["row_names"] == ["Row1"]
    assert r["data"]["saved"] is True


def test_read_rejects_non_table(monkeypatch):
    class NotTable:
        pass
    monkeypatch.setattr(B.unreal.EditorAssetLibrary, "load_asset",
                        lambda p: NotTable())
    with pytest.raises(B.BridgeError) as ei:
        B._cmd_datatable_read("/Game/T/BP_X")
    assert ei.value.category == "datatable"
