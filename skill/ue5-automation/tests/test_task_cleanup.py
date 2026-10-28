# -*- coding: utf-8 -*-
"""任务账本清理工具（v6.0.2）契约。"""
import importlib.util
import json
import os

import pytest

_SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "scripts")


def _load():
    spec = importlib.util.spec_from_file_location(
        "task_cleanup_mod", os.path.join(_SCRIPTS, "task_cleanup.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


tc = _load()


def _t(tid, goal, ur="", status="success", ts="2026-10-28T00:00:0%d"):
    return {"task_id": tid, "goal": goal, "user_request": ur,
            "result_status": status, "started_at": (ts % len(goal)) if "%" in ts else ts,
            "steps": [], "deliverables": []}


def test_is_test_rules():
    assert tc.is_test(_t("1", "g"))
    assert tc.is_test(_t("2", "测一下批量资产与回滚", "测一下批量资产与回滚"))
    assert tc.is_test(_t("3", "验证保存闭环"))
    assert not tc.is_test(_t("4", "做一个碰到就开的门",
                             "我想做一扇门：玩家走过去碰到它"))


def test_select_keeps_latest_success_and_failure():
    tasks = [
        _t("1", "做一个门", "我想做一扇门", "success", "2026-10-26T00:00:00"),
        _t("2", "做一个门", "我想做一扇门", "success", "2026-10-27T00:00:00"),
        _t("3", "做一个门", "我想做一扇门", "failed", "2026-10-27T01:00:00"),
        _t("4", "g"),
        _t("5", "验证资产回滚", "测一下批量资产与回滚"),
        _t("6", "做个传送门", "我想做一个传送门"),
    ]
    kept = tc.select_tasks(tasks)
    goals = {(k["task_id"], k["result_status"]) for k in kept}
    assert ("2", "success") in goals
    assert ("3", "failed") in goals
    assert ("1", "success") not in goals      # 旧的同需求成功被去重
    assert all(k["task_id"] not in ("4", "5") for k in kept)
    assert len(kept) == 3


def test_write_renumbers_and_roundtrips(tmp_path):
    tasks = [
        _t("20261028-01", "做一个门", "我想做一扇门", "success",
           "2026-10-27T00:00:00"),
        _t("20261028-01", "做个传送门", "我想做传送门", "success",
           "2026-10-28T00:00:00"),
    ]
    tc.write_tasks(str(tmp_path), tasks)
    loaded = tc.load_tasks(str(tmp_path))
    ids = [t["task_id"] for t in loaded]
    assert ids == ["20261027-01", "20261028-01"], ids
    assert len(set(ids)) == len(ids)


def test_dry_run_does_not_modify(tmp_path):
    _t1 = _t("1", "做一个门", "我想做一扇门")
    with open(os.path.join(str(tmp_path), "20261028.jsonl"), "w",
              encoding="utf-8") as f:
        f.write(json.dumps({"kind": "end", "task": _t1}) + "\n")
    before = open(os.path.join(str(tmp_path), "20261028.jsonl"),
                  encoding="utf-8").read()
    assert tc.main(["--tasks-dir", str(tmp_path)]) == 0
    after = open(os.path.join(str(tmp_path), "20261028.jsonl"),
                 encoding="utf-8").read()
    assert before == after
