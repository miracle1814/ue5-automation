#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""任务账本清理（v6.0.2）—— 默认 dry-run，--apply 时先备份再重写。

用法：
    python task_cleanup.py                 # 只预览：会丢弃/保留哪些任务
    python task_cleanup.py --apply         # 备份后重写（备份在 tasks-backup-<时间>/）
    python task_cleanup.py --tasks-dir DIR # 指定账本目录（默认 %APPDATA%/ue5_bridge/tasks）

规则（保守，可审计）：
  · 丢弃纯噪声：goal 为 "g" / "检查门蓝图能不能编译"，且无原话/步骤/交付物；
  · 丢弃显式测试/演示任务（关键词 + 已知测试 goal）；
  · 同一需求（原话或 goal）只保留最近一次成功 + 最近一次失败；
  · 任务按开始时间排序，ID 重新编号为 <日期>-NN（消除历史重复 ID）。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import time
from typing import List, Optional

NOISE_GOALS = {"g", "检查门蓝图能不能编译"}
TEST_WORDS = ("测一下", "测试回滚", "我想看看任务能不能撤销", "审计用临时",
              "演示：", "试做门的开关动画")
TEST_GOALS = {"验证资产回滚", "验证任务级撤销", "验证保存闭环",
              "验证变更回执与资产锁", "审计用临时任务", "试做门的开关动画"}


def default_tasks_dir() -> str:
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, "ue5_bridge", "tasks")


def load_tasks(tasks_dir: str) -> List[dict]:
    """从 JSONL 收集已结束任务（end 记录含完整 task），按 (id, started_at) 去重。"""
    out, seen = [], set()
    if not os.path.isdir(tasks_dir):
        return out
    for fn in sorted(f for f in os.listdir(tasks_dir) if f.endswith(".jsonl")):
        with open(os.path.join(tasks_dir, fn), encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("kind") != "end":
                    continue
                task = rec.get("task") or {}
                key = (task.get("task_id"), task.get("started_at"))
                if key in seen:
                    continue
                seen.add(key)
                out.append(task)
    return out


def is_test(task: dict) -> bool:
    ur = str(task.get("user_request") or "").strip()
    goal = str(task.get("goal") or "").strip()
    if (goal in NOISE_GOALS and not ur
            and not (task.get("steps") or [])
            and not (task.get("deliverables") or [])):
        return True
    blob = ur + " " + goal
    if any(w in blob for w in TEST_WORDS):
        return True
    if goal in TEST_GOALS:
        return True
    return False


def select_tasks(tasks: List[dict]) -> List[dict]:
    """过滤噪声，并按需求去重（最近成功 + 最近失败各一条）。"""
    pool = [t for t in tasks if not is_test(t)]
    groups = {}
    for t in pool:
        key = (str(t.get("user_request") or t.get("goal") or "").strip())[:80]
        groups.setdefault(key, []).append(t)
    kept = []
    for items in groups.values():
        items.sort(key=lambda x: x.get("started_at") or "")
        ok = [x for x in items if x.get("result_status") == "success"]
        bad = [x for x in items if x.get("result_status") != "success"]
        if ok:
            kept.append(ok[-1])
        if bad:
            kept.append(bad[-1])
    kept.sort(key=lambda x: x.get("started_at") or "")
    return kept


def backup(tasks_dir: str) -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dst = tasks_dir.rstrip("\\/") + "-backup-" + stamp
    shutil.copytree(tasks_dir, dst)
    return dst


def write_tasks(tasks_dir: str, tasks: List[dict]) -> str:
    """清掉旧 JSONL，按日期重新编号并写 begin/end 两条记录。"""
    for fn in os.listdir(tasks_dir):
        if fn.endswith(".jsonl"):
            os.remove(os.path.join(tasks_dir, fn))
    counters = {}
    for t in tasks:
        day = str(t.get("started_at") or time.strftime("%Y-%m-%d"))[:10]
        day = day.replace("-", "") or time.strftime("%Y%m%d")
        counters[day] = counters.get(day, 0) + 1
        t = dict(t)
        t["task_id"] = "%s-%02d" % (day, counters[day])
        with open(os.path.join(tasks_dir, day + ".jsonl"), "a",
                  encoding="utf-8") as f:
            f.write(json.dumps({"kind": "begin", "task": t},
                               ensure_ascii=False) + "\n")
            f.write(json.dumps({"kind": "end", "task": t},
                               ensure_ascii=False) + "\n")
    return tasks_dir


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="清理任务账本（默认 dry-run）")
    ap.add_argument("--apply", action="store_true", help="备份后真正重写")
    ap.add_argument("--tasks-dir", default=None, help="账本目录（默认 %%APPDATA%%/ue5_bridge/tasks）")
    args = ap.parse_args(argv)

    tasks_dir = args.tasks_dir or default_tasks_dir()
    if not os.path.isdir(tasks_dir):
        print("账本目录不存在:", tasks_dir)
        return 2
    tasks = load_tasks(tasks_dir)
    kept = select_tasks(tasks)
    print("账本: %s" % tasks_dir)
    print("任务: %d → 保留 %d（丢弃 %d）" % (len(tasks), len(kept),
                                          len(tasks) - len(kept)))
    for t in kept:
        print("  保留 %-12s %s | %s" % (t.get("task_id"),
                                       t.get("started_at"),
                                       str(t.get("goal") or "")[:44]))
    if not args.apply:
        print("\n[dry-run] 未改动。加 --apply 执行（会先备份）。")
        return 0

    bak = backup(tasks_dir)
    print("已备份 →", bak)
    write_tasks(tasks_dir, kept)
    print("已重写 %d 条任务（ID 已重新编号）" % len(kept))
    print("重启编辑器后生效（账本在启动时回放）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
