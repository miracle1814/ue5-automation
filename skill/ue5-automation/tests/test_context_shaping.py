# -*- coding: utf-8 -*-
"""v6.0：读结果字段裁剪 + 类能力探针。"""
import os
import sys

os.environ["UE5_BRIDGE_TEST_MODE"] = "1"
_SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPTS = os.path.join(_SKILL_ROOT, "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import ue5_bridge as B  # noqa: E402
import fake_unreal as fu  # noqa: E402


def test_shape_fields_filters_nodes_and_connections():
    data = {"success": True, "data": {"l2": {"graphs": [{
        "graph_name": "EventGraph",
        "nodes": [{"title": "Branch", "guid": "A1", "pins": [1, 2]}],
        "connections": [{"from_node": "A", "to_node": "B", "from_pin": "x"}],
    }]}}}
    out = B._shape_fields(data, ["title", "guid", "from_node", "to_node"])
    g = out["data"]["l2"]["graphs"][0]
    assert g["nodes"][0] == {"title": "Branch", "guid": "A1"}
    assert g["connections"][0] == {"from_node": "A", "to_node": "B"}
    assert g["graph_name"] == "EventGraph"      # 元数据保留


def test_shape_fields_empty_is_noop():
    data = {"a": 1}
    assert B._shape_fields(data, None) is data
    assert B._shape_fields(data, []) is data


def test_class_probe_reports_presence():
    r = B._cmd_class_probe(["EditorAssetLibrary", "NoSuchThing"], False)
    d = r["data"]
    assert d["EditorAssetLibrary"]["exists"] is True
    assert d["NoSuchThing"]["exists"] is False


def test_class_probe_members():
    r = B._cmd_class_probe(["EditorAssetLibrary"], True)
    members = r["data"]["EditorAssetLibrary"]["members"]
    assert any("does_asset_exist" in m for m in members)


def test_class_probe_rejects_empty():
    import pytest
    with pytest.raises(B.BridgeError):
        B._cmd_class_probe([])
