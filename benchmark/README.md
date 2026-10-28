# Benchmark / reproducible checks (v6.1)

Two read-only checks, no network writes, no editor mutation.

## Offline (no UE required)

```bash
python benchmark/run_offline.py
```

Runs the full pytest suite and writes `benchmark/last_offline.json`.
Baseline for v6.1: **797 passed / 0 failed / 95 skipped**.

## Live smoke (editor running)

```bash
# token: UE5_BRIDGE_TOKEN or %APPDATA%/ue5_bridge/token
python benchmark/live_smoke.py
```

Checks:
1. `/health` → 200 + engine/project info
2. `/diag` → >50 UFUNCTION methods
3. `/mcp tools/list` → ≥5 tools (default 33)
4. `/ui` → 200 static shell

Exit code 0 only when all pass. Baseline on UE 5.1.1 / 5.8.2:
`34 tools / ~95 methods`.
