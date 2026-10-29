# UE5 Automation Skill — AI-driven Blueprint Automation for Unreal Engine

**English** | [简体中文](README.zh-CN.md)

**Let AI agents actually operate the Unreal Editor** — node-level blueprint authoring,
materials, UMG, and PIE runtime verification, with every write read back and verified.

**Works on UE 5.1 – 5.8.** Epic's built-in MCP server ships with 5.8+ only — if your
project (or your team's mobile line) is on 5.1–5.7, this is the toolkit that covers it.
No compilation for 5.1 & 5.8 (prebuilt bridge DLLs included).

[![GitHub release](https://img.shields.io/github/v/release/miracle1814/ue5-automation?display_name=tag)](https://github.com/miracle1814/ue5-automation/releases/latest)
[![Tests](https://github.com/miracle1814/ue5-automation/actions/workflows/tests.yml/badge.svg)](https://github.com/miracle1814/ue5-automation/actions/workflows/tests.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)
[![SkillHub](https://img.shields.io/badge/SkillHub-UE5%20Blueprint%20Automation-2ea44f)](https://skillhub.cn/skills/user_9249b15b/ue5-automation)
[![miracle1814/ue5-automation MCP server](https://glama.ai/mcp/servers/miracle1814/ue5-automation/badges/score.svg)](https://glama.ai/mcp/servers/miracle1814/ue5-automation)

**Download v6.5.4:** [portable package](https://github.com/miracle1814/ue5-automation/releases/download/v6.5.4/ue5-automation-v6.5.4-portable.zip) · [release notes](https://github.com/miracle1814/ue5-automation/releases/tag/v6.5.4) · [SkillHub listing](https://skillhub.cn/skills/user_9249b15b/ue5-automation)

> The SkillHub skill package is at **6.5.9** (docs-only; runtime remains **6.5.4**).

![UE5 Blueprint Automation workflow](docs/assets/skillhub-overview.png)
*Read → write → compile → verify → save/undo. `verified=true` is the gate.*

> ⭐ **If this saves you time, star the repo** — it helps other Unreal developers find it.

> 🎬 **Demo GIF** — coming soon: a 60–120s capture of "one sentence → agent builds a
> blueprint → PIE runtime verification" will be embedded here.

---

## Why this exists

Most "AI controls Unreal" attempts die at the same wall: **Python cannot reach `UEdGraph`**.
You can demo asset creation in a day, but node-level graph authoring, reliable connections,
and "the operation actually did what it claimed" are where projects stall.

This toolkit solves that with:

- **A C++ bridge plugin** (editor-level UFUNCTIONs): node creation, pin/connection editing with
  GUID-based targeting, widget-tree authoring, material expression enumeration — the things
  Python physically cannot do in-engine.
- **A verification discipline**: every write operation is **read back and compared**
  (fail-loud, never fake success), connections are verified against live topology,
  compile results are read back with a log-pattern scan.
- **Runtime behavior checks**: start PIE, read live actor state, teleport with read-back —
  prove the logic *runs*, not just that it compiles. `verify_scenario` packages
  wait/assert/log/screenshot steps into a JUnit-producing acceptance run.
- **Multi-agent safety (v6.5)**: idempotency keys (retries never double-write),
  advisory session locks plus task locks, and write receipts with before/after
  fingerprints.
- **A security model you can defend to a studio**: localhost-only, token auth,
  a 101-command whitelist (no arbitrary code execution), transaction logs, rollback.

## Feature overview

| Domain | What it does |
|:--|:--|
| 🔍 Blueprint analysis | classes / variables / CDO / node graph / connections → Markdown + Mermaid reports |
| 🏗️ Blueprint creation | asset + variables + components + interfaces + events + function nodes + wiring + compile (atomic, rolls back on failure) |
| ✏️ Incremental editing | pin default values, connect/disconnect (topology-verified), delete with reference checks, transaction rollback |
| 🎨 Materials | create → expressions (params, math, samplers) → connect to properties → compile → instances |
| 🧩 UMG widget blueprints | create → widget tree (panels/text/buttons…) → set properties → graph logic reuses blueprint commands |
| ▶️ PIE runtime verification | start/state/live actors/read properties/teleport with read-back/stop |
| 🖥️ Actors & levels | query / spawn / transform / read properties |
| 🩺 Log diagnostics | parse engine logs + 19-rule error pattern library + incremental log reader |
| 📐 Project audit | whole-project scan: counts, complexity, coupling, circular deps, hot spots |
| ⚡ Batch execution | multi-step operations in a single request |

**Security model**: localhost bind, token auth, command whitelist, read-back verification on
writes, transaction logs, scoped saves only (never saves the whole project).

## Relation to Epic's official Unreal MCP (5.8+)

| | Official MCP (5.8+) | ue5-automation |
|:--|:--|:--|
| Engine versions | 5.8+ only | **5.1 – 5.8** |
| Blueprint graph authoring | object-level tools | **node-level (create/wire/verify, GUID-targeted)** |
| Write verification | returns success | **read-back & compare, fail-loud** |
| Transaction rollback | ✗ | **✓ atomic build, rollback on failure** |
| Auth | none (loopback only) | **token auth + 101-command whitelist + audit log** |
| Works offline of editor | ✗ | partial (analyzer/log tools) |
| On 5.8 | — | **integrates with it** (ue58-extras adds 8 node-level tools on top) |

On 5.8 the two compose: keep Epic's toolset for scene/asset work, add this bridge for
node-level graph authoring and verified writes. On 5.1–5.7, this is the only option.

## Requirements

| Engine | Read/audit/diagnose | Node-level blueprint writes | Materials/UMG | MCP access |
|:--|:--|:--|:--|:--|
| **5.1.x** | ✅ out of the box | ✅ prebuilt DLL included | ✅ | ✅ built-in `/mcp` |
| 5.2–5.5 | ✅ | ⚠️ compile the bridge once (`bridge/Source/build_for_engine.bat`) | ⚠️ same | ✅ |
| 5.6 / 5.7 | ✅ | ❔ unverified (structurally compatible) | ❔ | ✅ |
| **5.8** | ✅ | ✅ **prebuilt DLL included** (`bridge/prebuilt-5.8/`) | ✅ | ✅ official MCP + `bridge/ue58-extras/` plugin |

Also needs: Python 3.10+ on the host, the **Python Editor Script Plugin** enabled in the
editor, and **Remote Execution** turned on.

## Install (3 steps)

```cmd
:: 1) dependencies + attempt automatic bridge deployment
install_dependencies.bat

:: 2) manual fallback (project-level, no admin needed):
cd bridge\BlueprintPythonBridge
python deploy.py --target "C:\path\to\YourProject"

:: 3) in the editor: enable "Python Editor Script Plugin",
::    enable Remote Execution (Project Settings → Plugins → Python), restart.
:: Verify:
cd ..\..\skill\ue5-automation\scripts
python ue5_runner.py health
```

Then one command registers the MCP proxy into your AI clients (auto-detects
Claude Desktop / Cursor / Claude Code / VS Code / Windsurf / Codex; every config
is backed up before writing):

```cmd
python skill\ue5-automation\scripts\install_mcp.py
```

Full guide: [README.zh-CN.md](README.zh-CN.md) (中文) · `skill/ue5-automation/DEPLOY.md`.

## Use it with AI (MCP)

The toolkit **is** an MCP server. Point any MCP client at it:

- **UE 5.1–5.5**: `http://127.0.0.1:8889/mcp` (this repo ships the endpoint; the bridge
  auto-starts on first write, or see `skill/ue5-automation/docs/MCP-ACCESS.md`)
- **UE 5.8**: the engine's official MCP server at `http://127.0.0.1:8000/mcp` — enable it in
  project settings (`bAutoStartServer=True`) and install `bridge/ue58-extras/` (8 tools; with
  the prebuilt bridge, node-level graph tools work too)
- **stdio clients (Claude Desktop & co.)**: launch the bundled stdio proxy — it serves the
  full tool catalog locally and relays tool calls to the in-editor bridge (token read
  automatically), so clients that can't set HTTP headers work out of the box:

  ```json
  {
    "mcpServers": {
      "ue5-automation": {
        "command": "python",
        "args": ["C:/path/to/ue5-automation/skill/ue5-automation/scripts/ue5_mcp_stdio.py"]
      }
    }
  }
  ```

Then just talk to your agent:

> "Create a pickup item blueprint with a collision sphere, then verify the pickup logic in PIE."

See [MCP-ACCESS.md](skill/ue5-automation/docs/MCP-ACCESS.md) for client configuration (ZCode / Claude Desktop /
Cursor / raw JSON) and the full tool catalog.

### Observability: admin dashboard (read-only)

While the agent works, open `http://127.0.0.1:8889/ui?token=<contents of the
%APPDATA%\ue5_bridge\token file>` in a browser to watch what it does — read-only:
a task stream (every tool call with latency and status), a blueprint graph view
(nodes/edges, breathing highlight on recent writes, compile errors pinned to nodes),
a verification wall (verified/warnings cards per write + compile-round counter),
and one-click audit export (zip). Everything is GET-only and can never block the
agent's main path; events are also appended to `%APPDATA%\ue5_bridge\sessions\`
(JSONL) for after-the-fact audits.

## Use it from the command line

```cmd
cd skill\ue5-automation\scripts
python ue5_runner.py health
python ue5_runner.py read /Game/BP_Test --level L2
python ue5_runner.py build spec.json
python blueprint_editor.py update  /Game/BP_Test PrintString InString "Hello"
python blueprint_editor.py connect /Game/BP_Test BeginPlay then PrintString execute
python log_parser.py --log "%PROJECT%\Saved\Logs\YourProject.log" --output diag.json
python analyzer.py --input reader_out.json --summary
```

## The execution chain (what the agent actually does)

```
requirements
  → preflight        (engine/version/bridge checks)
  → read state       (nodes, GUIDs, variables)
  → apply changes    (build / edit / wire — every step read-back verified)
  → verify behavior  (PIE start → live actor state → teleport w/ read-back → stop)
  → deliver          (Markdown + Mermaid reports, transaction logs)
  → clean up         (scoped saves only — never saves the whole project)
```

Every write is **read back and compared**; mismatches raise loud errors. We consider
"returns true but did nothing" the cardinal sin of editor automation.

## Documentation

| Doc | Content |
|:--|:--|
| [START-HERE (中文快速开始)](START-HERE.zh-CN.md) | 部署 / 接入 / SOP / 能力 / 排障 |
| [README.zh-CN.md](README.zh-CN.md) | 中文说明与部署指南 |
| [skill/ue5-automation/SKILL.md](skill/ue5-automation/SKILL.md) | full manual (v0.20) |
| [MCP-ACCESS.md](skill/ue5-automation/docs/MCP-ACCESS.md) | connecting MCP clients to UE 5.1–5.5 |
| [skill/ue5-automation/docs/pitfalls.md](skill/ue5-automation/docs/pitfalls.md) | 19 known pitfalls (and fixes) |
| [bridge/BlueprintPythonBridge/Source/BUILD.md](bridge/BlueprintPythonBridge/Source/BUILD.md) | compiling the bridge for 5.2–5.8 |

## Repository layout

```
├── bridge/                     engine-side "hands"
│   ├── BlueprintPythonBridge/  C++ bridge plugin (editor UFUNCTIONs)
│   │   ├── Binaries/Win64/     prebuilt 5.1 DLL
│   │   └── Source/             full source + build_for_engine.bat (5.2–5.8)
│   ├── prebuilt-5.8/           prebuilt 5.8 DLL (+ UnrealEditor.modules)
│   └── ue58-extras/            official-MCP toolset plugin for 5.8 (8 tools)
├── skill/ue5-automation/       host-side brain: bridge client, MCP endpoint,
│                               runner, blueprint editor/reader/builder, analyzer,
│                               log parser, preflight, docs, report templates
└── CHANGELOG.md                per-release summaries
```

## Quick success path

1. Download the portable package from [Releases](https://github.com/miracle1814/ue5-automation/releases/latest), unzip, run `install.bat`, and drag in your `.uproject`.
2. Restart the editor and confirm `health` returns `bridge_loaded: true`.
3. Build a smoke Blueprint, write `BeginPlay → PrintString` with `apply_graph`, compile it with `compile_and_diagnose`, and read the pin back with `get_pin_default_value`.
4. Save the asset and confirm the result. That is the whole loop: **write → compile → read back → save**.

Full command contract: [`SKILL.md`](skill/ue5-automation/SKILL.md); SkillHub package: [`packaging/skillhub/ue5-automation`](packaging/skillhub/ue5-automation/SKILL.md); troubleshooting: [`TROUBLESHOOTING.md`](packaging/skillhub/ue5-automation/references/TROUBLESHOOTING.md); pitfalls: [`PITFALLS.md`](packaging/skillhub/ue5-automation/references/PITFALLS.md); examples: [`EXAMPLES.md`](packaging/skillhub/ue5-automation/references/EXAMPLES.md).

## Commercial / Enterprise

The core is Apache-2.0. Paid work is available for enterprise integration, UE 5.2–5.7 bridge adaptation, CI/asset-governance pipelines, training, and custom tooling. Open a GitHub issue titled `[Commercial]` or contact [@miracle1814](https://github.com/miracle1814).

## Status & tested-against

- Actively maintained — monthly releases, per-release changelog, CI on every push
- Offline regression: **820 passed / 0 failed** (95 skipped = online-only tests that need a live editor)
- Live end-to-end verified on **UE 5.1.1** and **UE 5.8.2** (editor → official MCP →
  toolset → C++ bridge → live PIE state)
- See [CHANGELOG.md](CHANGELOG.md) for per-release summaries

## License

Apache-2.0 — see [LICENSE](LICENSE) and [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

> Unreal® Engine is a trademark of Epic Games, Inc. This project is an independent
> developer tool and is not affiliated with or endorsed by Epic Games. Engine headers
> and binaries remain subject to Epic's EULA; the Apache-2.0 license covers original
> source code only.

## Acknowledgements

Built on top of the UE editor's Python & remote-execution surfaces, and integrating with
the official [ModelContextProtocol plugin](https://github.com/EpicGames/UnrealEngine) (5.8+).
