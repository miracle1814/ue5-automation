# Preflight — UE5 自动化 Skill 启动前检查清单

> **版本：v0.15 · 2026-09-18** | 随 SKILL.md v0.15 口径同步（/health 鉴权自检、B3 命令更正）
>
> 每次执行 Skill 操作前，按本清单逐项检查。全绿才能开始。

---

## 快速检查（30 秒）

```bash
# 1. KB 引擎在线？
curl -s -o /dev/null -w "%{http_code}" --max-time 5 "http://127.0.0.1:8890/v1/search?q=ping&top_k=1&kb=agent_knowledge"
# 期望：200

# 2. 最近一次 UE5 操作距今多久？
# 若 > 4 小时，UE 可能已关闭，需确认

# 3. 上次执行的 DLL 版本？
# 检查 ue5-automation-skill-package/bridge/ 下的 DLL 大小
```

---

## 完整检查清单

### A · 环境检查

| # | 检查项 | 命令/方法 | 期望 | 不通过 |
|:--:|:---|:---|:---|:---|
| A1 | Python 版本 ≥ 3.9 | `python --version` | 3.9+ | 安装 Python 3.9+ |
| A2 | pip 依赖 | `pip list \| grep requests` | requests 已安装 | `pip install -r requirements.txt` |
| A3 | UE5 引擎路径 | 检查环境变量 `UE_ENGINE_PATH` 或默认路径 | 存在 `Engine/Binaries/Win64/` | 设置环境变量 |
| A4 | Bridge DLL 存在 | `dir bridge\BlueprintPythonBridge\*.dll` | 大小 = 828,416B · MD5 E63B033E857D59DE3AD6C5EEC47E5406（UE5.1 预编译 DLL · 2026-09-10 编译） | 重新部署 Bridge |
| A5 | KB 引擎在线 | `curl localhost:8890/v1/search?...` | HTTP 200 | 启动 KB 引擎 |

### B · UE5 状态检查

| # | 检查项 | 命令/方法 | 期望 | 不通过 |
|:--:|:---|:---|:---|:---|
| B1 | UE5 编辑器运行中 | `python ue_send.py health` | `success: true` | 启动 UE5 编辑器 |
| B2 | Remote Execution 已启用 | 检查 `Config/DefaultEngine.ini` | `bRemoteExecution=true` | 在 Project Settings 中勾选 |
| B3 | HTTP Bridge 运行中 | `python scripts/ue5_runner.py health`（/health 需 `X-Skill-Token`，裸 curl/浏览器只会 403） | 返回 ok:true + bridge_apis | 在 UE5 Python Console 启动 Bridge |
| B4 | 项目路径正确 | `ue_send.py` 返回节点信息含项目名 | 项目名匹配 | 切换到正确项目 |

### C · 功能就绪检查

| # | 检查项 | 命令/方法 | 期望 | 不通过 |
|:--:|:---|:---|:---|:---|
| C1 | 蓝图读取（L1） | `python ue5_runner.py read /Game/TestTarget --level L1` | 返回蓝图基本信息 | 见诊断卡片 READ_L1_FAIL |
| C2 | 蓝图读取（L2） | `python ue5_runner.py read /Game/TestTarget --level L2` | 返回节点清单 | 见诊断卡片 READ_L2_FAIL |
| C3 | 蓝图创建 | `python ue5_runner.py build test_spec.json` | 编译成功 | 见诊断卡片 BUILD_FAIL |
| C4 | ConnectPins 验证 | 创建后逐条验证 `is_connected` | 全部通过 | 见 pitfalls.md #1 #2 |

### D · 铁律遵守检查

| # | 检查项 | 判定 | 不通过后果 |
|:--:|:---|:---|:---|
| D1 | GameThread 安全 | 所有写操作走 C++ Bridge（非直接 Python 调 UE API） | 🔴 禁止执行 |
| D2 | ConnectPins 验证 | 每次连线后执行双向 `is_connected` | 🟡 警告 |
| D3 | 编译前 CDO | 编译前不在 CDO 上设值 | 🟡 警告 |
| D4 | Kismet 创建 | 蓝图创建用 `FKismetEditorUtilities`，非 `AssetTools` | 🔴 禁止执行 |

---

## 常见不通过场景与修复

| 症状 | 可能原因 | 修复 |
|:---|:---|:---|
| `ue_send.py health` 超时 | Remote Execution 未启用 | 检查 `DefaultEngine.ini`，确认 `bRemoteExecution=true`（6766 为 UE Remote Execution 默认端口，ue_send.py 使用 UDP 多播自动发现，不依赖固定端口） |
| `curl 8889/health` 无响应 | Bridge 未启动 | UE5 Python Console: `import ue5_bridge; ue5_bridge.start()` |
| `ue5_runner.py read` 返回"蓝图不存在" | 路径错误 / GameThread 未调度 | 检查路径格式、确认 DLL 版本 = 828,416B |
| DLL 大小 ≠ 828,416B（如 437,248B / 345,600B） | 旧版 DLL（缺 `GetNodeByGuid` / `AddTimelineNode` 等 API） | 重新部署 bridge/Binaries/Win64/ |
| KB 引擎 8890 不通 | KB 进程未启动 | `python kb-engine/start_kb.py`（外部依赖，非部署包内文件） |

---

## 检查结果记录

```
Preflight · YYYY-MM-DD HH:MM
A1-A5: [✅/❌] ...
B1-B4: [✅/❌] ...
C1-C4: [✅/❌] ...
D1-D4: [✅/❌] ...

结论：[全绿通过 / N 项不通过]
不通过项：[列出]
```

---

*关联：`pitfalls.md` · `docs/SOP.md` · `docs/SOP-UE-HTTP-BRIDGE.md`（`quality_gates.md` / `diagnostic_card_template.md` 仅源端 skill 目录，不随发布包分发）*
