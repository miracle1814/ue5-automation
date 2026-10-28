# ue5-automation (English)

> UE 5.1–5.8 blueprint surgery desk for AI agents: **write it right, verify it,
> undo it, audit it.** Apache-2.0. Windows. Chinese docs remain the primary
> entry (`README.zh-CN.md`).

**Download v6.5.4:** [portable package](https://github.com/miracle1814/ue5-automation/releases/download/v6.5.4/ue5-automation-v6.5.4-portable.zip) ·
[release notes](https://github.com/miracle1814/ue5-automation/releases/tag/v6.5.4) ·
[SkillHub listing](https://skillhub.cn/skills/user_9249b15b/ue5-automation)

## What it is

An MCP server + skill + read-only dashboard that runs inside the Unreal Editor:

- **Blueprint deep-write**: one-shot declarative `apply_graph` (nodes + edges +
  pin defaults in a single UE transaction), read-back `verified`, rollback on
  failure.
- **Structured compile diagnostics**: `FCompilerResultsLog` tokens → node GUID,
  graph, title (no log regex, no log-flush race).
- **Behavior verification**: `verify_behavior` reads a live property in PIE and
  asserts it (exact / numeric tolerance / contains), optionally attaching a
  screenshot as evidence.
- **Task-level undo**: every write command leaves one `[agent]` transaction step;
  undo stops immediately at a user edit instead of skipping over it.
- **Asset governance**: batch rename/move with dry-run, task asset ledger,
  delete-created / restore-deleted rollback.
- **DataTable**: read rows/CSV, overwrite from CSV, create with a row struct.
- **Scenario acceptance**: `verify_scenario` runs wait/assert/log/screenshot
  steps in PIE and emits JUnit evidence.
- **Multi-agent safety**: idempotency keys, advisory session locks, task locks,
  and write receipts with before/after fingerprints.
- **Safety**: localhost + token, 101-command whitelist, 8MB request cap,
  confirm for destructive ops, fail-loud semantics, read-only dashboard.

## Quick start

```bash
# portable package (recommended)
install.bat            # deploy plugin + host scripts + dashboard
# or manual: see docs/QUICKSTART.md
```

Then connect an MCP client:

- HTTP (Streamable): `http://127.0.0.1:8889/mcp` with `X-Skill-Token`
- stdio: `python Content/Python/ue5_mcp_stdio.py` (session survives editor restarts)

## Reproducible checks

```bash
python benchmark/run_offline.py   # tests, no UE required
python benchmark/live_smoke.py    # read-only checks against a running editor
```

Baseline v6.5: **820 passed / 95 skipped / 0 failed** offline;
**34 MCP tools / 101 whitelisted commands** on UE 5.1.1 and 5.8.2.

## Engines

- Prebuilt Win64 DLLs: **UE 5.1** and **UE 5.8**.
- 5.2–5.7: compile from `bridge/BlueprintPythonBridge/Source` (see `BUILD.md`).

## Context modes

- Default: all 33 tools (discoverability first).
- `UE5_MCP_CORE_TOOLS=1`: 18 core tools.
- `UE5_MCP_GATEWAY=1`: 5 entry points (`tool_search`, `execute_command`,
  `apply_blueprint_graph`, `verify_behavior`, `task_begin`).

## Security notes

- HTTP bridge binds `127.0.0.1` only and requires a per-machine random token.
- The optional UDP Remote Execution read channel is **disabled by default**
  (same-subnet multicast is unauthenticated). Enable explicitly with
  `UE5_ALLOW_REMOTE_EXEC=1`, or use HTTP `read_*` commands.
- Destructive operations (`delete_asset`, `delete_node`, `batch_asset_ops` real
  run, `rollback_task_assets`, `datatable_write_csv`, `apply_graph replace`)
  require `confirm=true`.

## Explicit non-goals

AnimBP state-machine writing, Niagara/PCG/GAS/StateTree/Sequencer, C++ hot
reload, Electron shell, 3D generation. The product stays narrow and deep on
blueprints instead of chasing breadth.

## Docs

- `skill/ue5-automation/SKILL.md` — agent SOP
- `skill/ue5-automation/docs/QUICKSTART.md` — 10-minute human onboarding
- `skill/ue5-automation/docs/pitfalls.md` — known traps (P01–P19, B1–B19)
- `packaging/skillhub/ue5-automation/references/TROUBLESHOOTING.md` — top 10 operational failures
- `benchmark/README.md` — reproducible checks

## Quick success path

1. Download the portable package from Releases, unzip, run `install.bat`, and drag in your `.uproject`.
2. Restart the editor and confirm `health` returns `bridge_loaded: true`.
3. Build a smoke Blueprint, write `BeginPlay → PrintString` with `apply_graph`, compile with `compile_and_diagnose`, and read the pin back with `get_pin_default_value`.
4. Save the asset. The loop is **write → compile → read back → save**.

## Commercial / Enterprise

The core is Apache-2.0. Paid work is available for enterprise integration, UE 5.2–5.7 bridge adaptation, CI/asset-governance pipelines, training, and custom tooling. Open a GitHub issue titled `[Commercial]` or contact [@miracle1814](https://github.com/miracle1814).
