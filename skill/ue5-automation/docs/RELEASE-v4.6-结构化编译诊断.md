# v4.6 · C++ 结构化编译诊断（CompilerResultsLog）实施与验证说明

> 日期：2026-10-27 · 对应计划：`PLAN-AUDIT-v4.4.md` 的 **F 项**
> 结论：**已完成**。5.1 / 5.8 双引擎真机通过，离线 12 项契约测试通过。

---

## 1. 要解决的问题（日志正则路线的三个先天缺陷）

| 缺陷 | 现象 | 后果 |
|---|---|---|
| 日志异步刷盘 | 编译刚失败就 diagnose，tail 里还没有错误行 | 只能 `sleep 2s` 重读一次，仍可能扑空 |
| 节点名靠日志文本 | 同名节点 / 本地化标题（"分支" vs "Branch"） | GUID 反查不可靠（PTN-018） |
| tail 窗口含历史 | 修复后 errors 仍显示上一轮的行 | 必须反复解释"看 compiled 不看 errors" |

## 2. 做法：一次编译，直接读 token 化消息

```
FKismetEditorUtilities::CompileBlueprint(BP, None, &Results)
  → Results.Messages: TArray<TSharedRef<FTokenizedMessage>>
      → Token type EdGraph → FEdGraphToken::GetGraphObject()/GetPin()
          → UEdGraphNode（NodeGuid / 图名 / 标题权威）
      → Token type Object → FUObjectToken::GetObject()
          → UEdGraphNode / UBlueprint / UFunction（按类型分派）
```

产出 JSON（`CompileBlueprintStructured`）：

```json
{ "ok": true, "compiled": false, "status": "BS_Error", "status_code": 2,
  "asset": "/Game/Blueprints/BP_X", "num_errors": 2, "num_warnings": 0,
  "errors": [ { "severity": "error", "message": "...",
                "node_guid": "…", "node_name": "...", "graph": "EventGraph",
                "asset": "/Game/…", "nodes": [ … ] } ],
  "warnings": [ … ], "messages": [ … ] }
```

关键点：

- **资产字段用包路径**（`/Game/X/BP_Y`，与日志 `[AssetLog]` 同口径）；
- NodeGuid 用 `EGuidFormats::Digits`，与 Python `node_guid.export_text()` 一致；
- 一条消息可关联多个节点：顶层给第一个，`nodes[]` 给全部；
- 编译仍走 `FKismetEditorUtilities::CompileBlueprint`，**输出日志照常写**——
  人类排查与 `log_matches` 不受影响；但 Python 侧诊断优先用结构化结果。

## 3. Python 接入（自动回退，零硬依赖）

`_compile_bp_and_scan_log` 的编译段：

1. bridge 有 `compile_blueprint_structured` → 调它，解析 JSON；
2. 成功（`ok:true`）→ `compile_ok = compiled`，`core["structured"] = …`；
3. 方法缺失 / 抛异常 / `ok:false` / JSON 损坏 → 回退 `compile_blueprint` + 日志正则
   （行为与 v4.5 完全一致）。

`compile_and_diagnose` 的返回差异（有结构化时）：

| 字段 | v4.5（日志） | v4.6（结构化） |
|---|---|---|
| `errors_scope` | `log_tail_window` | `compiler_results_log` |
| `structured` | — | `true` |
| `may_be_stale` | 编译成功时 true（历史行） | **恒 false**（本次编译） |
| `node_guid` | 标题反查（可空/歧义） | token 直取（权威） |
| `compile_status` | Python 读 BP.Status | C++ 回传同一枚举 |
| `num_errors/num_warnings` | 无 | 有（编译器原生计数） |

`apply_graph` 的编译失败分支同样优先用结构化 errors（回滚/修复循环的定位更准）。

## 4. 真机验证（2026-10-27）

构造确定性编译错误：新建 Actor 蓝图 → `add_math_expression_node` 表达式 `"1 +"`
→ 编译必然报两条错误（一条全局"未处理令牌"，一条绑定节点的"参数#2 生成失败"）。

| 断言 | 5.1 | 5.8 |
|---|---|---|
| `structured=true`、`errors_scope=compiler_results_log` | ✅ | ✅ |
| `compiled=false` / `success=false` | ✅ | ✅ |
| `num_errors=2` | ✅ | ✅ |
| 其中 ≥1 条带 `node_guid` | ✅（1/2） | ✅（1/2） |
| `graph=EventGraph` / `node_name="1 +"` | ✅ | ✅ |
| `asset` 为包路径（无 `.BP_` 后缀） | ✅ | ✅ |
| `may_be_stale=false` / `source=compiler_results_log` | ✅ | ✅ |
| 旧路回退不触发（调用顺序只出现 structured） | ✅（单测） | ✅（单测） |

构建：5.1 BuildId `23058290`、5.8 BuildId `55116800`。

## 5. 已知边界

1. **无节点归属的消息仍是全局的**（如"表达式中有未处理令牌"），`node_guid=null` ——
   这是编译器本身的信息量，不是解析漏了。
2. **同一消息多节点**：顶层只给第一个；修复按 `nodes[]` 全部定位（避免只改一个）。
3. **规则库映射仍基于消息文本**（`pitfall_ref/fix_hint`）；命中率取决于
   `error_patterns.json` 的覆盖面，未命中如实标 `Unclassified`。
4. 旧 DLL（v4.6 之前）无此入口 → 自动走日志正则；`structured:false` 是回退标志，
   不是错误。
