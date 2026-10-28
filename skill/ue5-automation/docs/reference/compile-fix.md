# 编译修复 SOP（完整版）

> 从 SKILL.md §3.6 迁出（v4.6 分层）。核心循环与判据仍在 SKILL.md。

## 编译修复 SOP — 编译报错自动修复闭环（agent 必须遵守）

**v0.21 新增。** 设计原则（P0 规格书）：**工具提供「结构化的错误 + 可定位的锚点」，修复决策交给模型** —— 不搞启发式自动修（错误修复的空间太大，启发式会制造假成功，与 fail-loud 纪律冲突）。反馈侧由新命令 `compile_and_diagnose`（MCP 工具名 `ue5_compile_and_diagnose`）承担：编译 + 从日志尾部抽取结构化错误，每条挂资产/函数/节点定位与规则库映射。

**循环协议：**

1. 修改蓝图后必须编译；编译失败时进入本 SOP
2. 调用 `ue5_compile_and_diagnose`，读结构化错误列表
3. 对每条错误：按 `node_guid` 定位（GUID 是增量编辑工具的定位键）→ 用增量编辑工具修（见 3.5）→ 重新编译
4. 硬规则：
   - **最多连续 5 轮修复**；第 5 轮仍失败 → 停止，向用户报告已尝试的方向（禁止死循环）
   - **同参数重复失败 2 次提前熔断**（UEBox `loopBreaker` 经验）：同一工具 + 同一参数连续失败 2 次 → 拦截，换方向或向用户报告，禁止第三次原样重试；任何**成功**的写操作清零计数（"改 → 编 → 再改 → 再编"是正常循环，不计入连续失败）
   - **每轮修改保持事务回滚点**（增量修改模式的 freeze/rollback，见 3.5）
   - **禁止在修复循环中保存资产**（不调 `save_asset`）—— 修复确认前一切改动只在内存里
   - **`fix_hint` 与你的判断冲突时，以你对连线/语义的分析为准** —— fix_hint 来自已知坑规则库（`error_patterns.json` 的 fix_action），**仅供参考，不代替模型判断**
   - **修复成功后必须做一次 read-back**（`ue5_read_blueprint` / `ue5_read_nodes`）确认图结构与意图一致 —— 编译通过 ≠ 图是对的
   - **"Unknown ≠ 失败"措辞纪律**：编译结果 Unknown（Live Coding 等无结果 API 的场景）必须如实说"不知道成没成"，**绝不报成失败** —— 否则会引导模型去改本来没问题的代码；保存失败时第一句就说"改动只在内存里，关编辑器就没了"
   - **解析不出就明说**：`node_guid: null`、空 `errors` 列表 ≠ 没有错误 —— 结合 `warnings`（日志不可读 / GUID 通道不可用 / 同名歧义）与原始 `message` 判断，绝不编造定位

**`ue5_compile_and_diagnose` 返回体速览：**

```json
{
  "success": false,                 // 编译成功与否（与 compile_blueprint 同语义；MCP isError 据此置位）
  "compiled": false,
  "compile_status": "BS_Error",
  "errors": [
    {
      "code": "E-BP-REF-NULL-ASSET",     // 命中 error_patterns → 其 error_code；未命中 → CompileError.Unclassified
      "message": "Missing Asset /Game/Mesh/Cube",   // 原始编译器报错文本
      "asset": "/Game/Dir/BP_Pickup",    // 带路径则归一化；中文引擎短名原样（不拼造路径）
      "function": "OnOverlap",           // 函数/图上下文；缺省为 null
      "node_guid": "A1B2...",            // 尽力解析；解析不到 = null，绝不编造
      "node_name": "Branch",
      "pin_hint": "Condition",           // 能定位到 pin 就给（引号包裹形态），否则缺省
      "pitfall_ref": "PTN-010",          // 命中已知坑 → pattern id，否则 null
      "fix_hint": "扫描 Content Browser …"  // 规则库生成的提示，仅供参考
    }
  ],
  "warnings": ["编译日志不可读 …"],   // 诊断过程告警：errors 为空 ≠ 没有错误
  "fix_hint_note": "fix_hint 仅供参考，不代替模型判断"
}
```

**已知边界：** v4.6 起桥侧 `compile_blueprint_structured` 直接读
`FCompilerResultsLog` 的 token（`FEdGraphToken` / `FUObjectToken`）—— `node_guid`
/ 图名 / 节点标题是权威值，不靠标题反查；旧 DLL 或结构化通道异常时自动回退
「正则解析报错行」（中文/英文引擎两套格式），行为与 v4.5 一致。

**返回体判读（v4.6）：**

| 字段 | 含义 |
|---|---|
| `structured: true` | 本次走 C++ 结构化通道；`false` = 回退日志正则（不是错误） |
| `errors_scope` | `compiler_results_log`（本次编译）或 `log_tail_window`（可能含历史行） |
| `may_be_stale` | 结构化路径恒 `false`；日志路径在编译成功时为 `true`（历史行） |
| `num_errors` / `num_warnings` | 编译器原生计数（仅结构化路径） |
| 每条 error 的 `nodes[]` | 该消息关联的全部节点（顶层 `node_guid` 只是第一个） |

**收敛只看 `compiled`**，`errors` 非空不等于没修好（日志回退路径尤其如此）。


