# 整图声明式写入（GraphSpec 完整说明）

> 从 SKILL.md §3.7 迁出（v4.6 分层）。速查与硬规则仍在 SKILL.md。

## 整图声明式写入 — 写图逻辑的主路径（agent 必须遵守）

**v0.22 新增。** 设计原则（UEBox 源码透析最大发现）：**"Never build a graph one
node at a time"** —— 逐节点改图是上下文与轮次黑洞（模型每次调用都要重读图状态）。
**写图逻辑默认用 `apply_graph` 整图声明式写入；逐节点工具（`add_function_node` /
`connect_pins` / `set_pin_default_value` / `delete_node`）只用于修单点**
（改一个默认值 / 接一根线 / 删一个节点 —— 手术式微调）。

**写图 SOP（search_nodes → spec → apply_graph → verified → 编译失败走 §3.6）：**

1. **查签名**：调 `ue5_search_blueprint_nodes`（只读）拿到 target 与完整引脚签名
   —— 引擎库候选如实不带引脚（纯 Python 通道读不到 UFunction 签名），节点落图后
   用 `read_nodes` 看实际引脚；**不要凭记忆猜引脚名**
2. **构造 GraphSpec**（声明最终图长什么样，不是动作序列）：

   ```json
   {
     "graph": "EventGraph",
     "nodes": [
       {"id": "n1", "kind": "event",    "target": "BeginPlay"},
       {"id": "n2", "kind": "flow",     "target": "Branch"},
       {"id": "n3", "kind": "function", "target": "PrintString", "class": "/Script/Engine.KismetSystemLibrary"}
     ],
     "edges": [
       {"from": "n1.then", "to": "n2.execute"},
       {"from": "n2.then", "to": "n3.execute"}
     ],
     "pin_defaults": {"n3.InString": "Hello"}
   }
   ```

   kind ∈ `event`（标准事件 ReceiveBeginPlay…；非标准名 = 自定义事件）/
   `function`（引擎库需带 `class`，蓝图自有函数可不带）/ `flow`（Branch /
   Sequence / ForEachLoop / WhileLoop / DoOnce / Gate / FlipFlop / MultiGate /
   IsValid）/ `variable_get` / `variable_set`（变量必须已存在）。edge 用
   `"节点id.引脚名"` 点语法；id 唯一且不含点号。
3. **应用**：调 `ue5_apply_blueprint_graph` —— 一次事务内建节点 + 连线 +
   设默认值；守卫：蓝图编译中 → **409 语义拒绝**（等编译完成再写）；图非空且
   未显式 `replace=true` → 拒绝并返回图摘要（**先 read 再决定**，整图写入是
   替换语义不是合并）
4. **看 verified**：返回体 `verified=true` = 读回比对执行且通过（节点/连线与
   spec 一致；引擎多插的隐式转换节点只记 warning 不算失败 —— R1 口径）；
   `verified=false` = 图与 spec 不一致，绝不假成功 —— 读 `errors` 与
   `node_guids` 处置
5. **编译失败走 §3.6**：apply_graph 在事务外编译，编译失败时返回体带结构化错误
   （图已落好，以 `node_guids` 为定位键进入编译修复循环），不要重复整图写入

**硬规则：**

- **写图逻辑禁止逐节点搭**：超过 2 个节点的新图一律走 `apply_graph`
  （逐节点搭 3 节点 2 边 = 至少 5 次往返 + 每次重读状态）
- **先 search 后写 spec**：引脚名来自 search 返回（可给签名的源给全），
  引擎库候选引脚未知时，先应用再看读回，不要编造引脚名
- **非空图不静默覆盖**：`replace=true` 只在你已 read 现图并确认替换后使用；
  函数图的 FunctionEntry/FunctionResult 结构节点会被保留
- **事务边界**：应用失败自动回滚（图恢复原样，返回 `rollback` 详情）；
  布局为确定性拓扑序网格（相同 spec → 相同坐标），显式 `pos` 优先于自动布局
- **编译失败 ≠ 重写**：图内容正确但编译不过时，走 §3.6 定位修复，不要
  推倒重来

**`ue5_apply_blueprint_graph` 返回体速览：**

```json
{
  "success": true,            // 事务成功 + 编译通过（MCP isError 据此置位）
  "compiled": true,
  "compile_status": "BS_UpToDate",
  "verified": true,           // 读回比对：节点/连线与 spec 一致
  "nodes_applied": 3,
  "edges_applied": 2,
  "node_guids": {"n1": "A1B2...", "n2": "..."},   // 修单点时的定位键
  "errors": [], "warnings": []
}
```

**已知边界：** 引擎函数候选的引脚签名需 C++ 枚举通道（UEBox 用 C++
GetAllFunctions，本版零 C++）→ 候选名照给、签名如实缺省并附说明；被外部引用
的节点在 replace 清空时由引擎拒绝删除 → 命令失败并列出节点（fail-loud）。

---


