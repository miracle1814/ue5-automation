#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Offline benchmark runner — reproduces the test baseline and writes JSON.

Usage (from repo root):
    python benchmark/run_offline.py
Output:
    benchmark/last_offline.json  {version, total, passed, failed, skipped, duration_s}
Exit code 0 only when failed == 0.
"""
import json
import os
import re
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILL = os.path.join(REPO, "skill", "ue5-automation")
OUT = os.path.join(REPO, "benchmark", "last_offline.json")


def _version():
    try:
        m = json.load(open(os.path.join(REPO, "manifest.json"), encoding="utf-8"))
        return m.get("package_version"), m.get("skill_version")
    except Exception:
        return None, None


def main():
    t0 = time.time()
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "-q", "--timeout=180"],
        cwd=SKILL, capture_output=True, text=True, encoding="utf-8",
        errors="replace")
    text = (proc.stdout or "") + "\n" + (proc.stderr or "")
    m = re.search(r"(\d+) passed(?:, (\d+) skipped)?(?:, (\d+) failed)?", text)
    passed = int(m.group(1)) if m else 0
    skipped = int(m.group(2) or 0) if m else 0
    failed = int(m.group(3) or 0) if m else 0
    if not m:
        failed = 1  # 摘要都解析不到 = 环境/收集失败，不能当成功
    pkg, skill = _version()
    result = {
        "package_version": pkg,
        "skill_version": skill,
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "duration_s": round(time.time() - t0, 1),
        "exit_code": proc.returncode,
        "tail": text.strip().splitlines()[-3:],
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if (failed == 0 and proc.returncode == 0) else 1


if __name__ == "__main__":
    raise SystemExit(main())
