# UE5 Automation Skill — AI-driven Blueprint Automation for Unreal Engine

**Let AI agents (or scripts) actually operate the Unreal Editor** — read blueprints, build them from scratch, edit them incrementally, author materials and UMG widget blueprints, verify behavior in PIE at runtime, self-service logs, and audit whole projects.

**Engine support: UE 5.1 – 5.8** (5.1 & 5.8 ship with prebuilt bridges — zero compilation).

> 🎬 **Demo GIF** — coming soon: a 60–120s capture of "one sentence → agent builds a
> blueprint → PIE runtime verification" will be embedded here.

---

## Why this exists

Most "AI controls Unreal" attempts die at the same wall: **Python cannot reach `UEdGraph`**.
You can demo asset creation in a day, but node-level graph authoring, reliable connections,
and "the operation actually did what it claimed" are where projects stall.

This toolkit solves that with:

- **A C++ bridge plugin** (59 editor UFUNCTIONs): node creation, pin/connection editing with
  GUID-based targeting, widget-tree authoring, material expression enumeration — the things
  Python physically cannot do in-engine.
- **A verification discipline**: every write operation is **read back and compared**
  (fail-loud, never fake success), connections are verified against live topology,
  compile results are read back with a log-pattern scan.
- **Runtime behavior checks**: start PIE, read live actor state, teleport with read-back —
  prove the logic *runs*, not just that it compiles.
- **A security model you can defend to a studio**: localhost-only, token auth,
  a 72-command whitelist (no arbitrary code execution), transaction logs, rollback.

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

Full guide: [README.zh-CN.md](README.zh-CN.md) (中文) · `skill/ue5-automation/DEPLOY.md`.

## Use it with AI (MCP)

The toolkit **is** an MCP server. Point any MCP client at it:

- **UE 5.1–5.5**: `http://127.0.0.1:8889/mcp` (this repo ships the endpoint; the bridge
  auto-starts on first write, or see `skill/ue5-automation/docs/MCP-ACCESS.md`)
- **UE 5.8**: the engine's official MCP server at `http://127.0.0.1:8000/mcp` — enable it in
  project settings (`bAutoStartServer=True`) and install `bridge/ue58-extras/` (8 tools; with
  the prebuilt bridge, node-level graph tools work too)

Then just talk to your agent:

> "Create a pickup item blueprint with a collision sphere, then verify the pickup logic in PIE."

See [MCP-ACCESS.md](skill/ue5-automation/docs/MCP-ACCESS.md) for client configuration (ZCode / Claude Desktop /
Cursor / raw JSON) and the full tool catalog.

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
| [skill/ue5-automation/SKILL.md](skill/ue5-automation/SKILL.md) | full manual (v0.18) |
| [MCP-ACCESS.md](skill/ue5-automation/docs/MCP-ACCESS.md) | connecting MCP clients to UE 5.1–5.5 |
| [skill/ue5-automation/docs/pitfalls.md](skill/ue5-automation/docs/pitfalls.md) | 19 known pitfalls (and fixes) |
| [bridge/BlueprintPythonBridge/Source/BUILD.md](bridge/BlueprintPythonBridge/Source/BUILD.md) | compiling the bridge for 5.2–5.8 |

## Repository layout

```
├── bridge/                     engine-side "hands"
│   ├── BlueprintPythonBridge/  C++ bridge plugin (59 editor UFUNCTIONs)
│   │   ├── Binaries/Win64/     prebuilt 5.1 DLL
│   │   └── Source/             full source + build_for_engine.bat (5.2–5.8)
│   ├── prebuilt-5.8/           prebuilt 5.8 DLL (+ UnrealEditor.modules)
│   └── ue58-extras/            official-MCP toolset plugin for 5.8 (8 tools)
├── skill/ue5-automation/       host-side brain: bridge client, MCP endpoint,
│                               runner, blueprint editor/reader/builder, analyzer,
│                               log parser, preflight, docs, report templates
└── CHANGELOG.md                per-release summaries
```

## Status & tested-against

- Offline regression: **473 passed / 0 failed** (98 skipped = online-only tests that need a live editor)
- Live end-to-end verified on **UE 5.1.1** and **UE 5.8.2** (editor → official MCP →
  toolset → C++ bridge → live PIE state)
- See [CHANGELOG.md](CHANGELOG.md) for per-release summaries

## License

Apache-2.0 — see [LICENSE](LICENSE).

## Acknowledgements

Built on top of the UE editor's Python & remote-execution surfaces, and integrating with
the official [ModelContextProtocol plugin](https://github.com/EpicGames/UnrealEngine) (5.8+).
