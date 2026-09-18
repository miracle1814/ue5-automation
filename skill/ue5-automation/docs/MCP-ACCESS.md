# MCP 接入指南（5.1–5.5 编辑器直连 · v3.0）

> **一句话**：`ue5_bridge` 自 v3.0 起在 `/mcp` 端点提供 **MCP 协议（JSON-RPC 2.0 / Streamable HTTP）** ——
> 任何 MCP 客户端（ZCode / Claude Desktop / Cursor / 自研）都能像连官方 UE 5.8 MCP 那样，
> 直接驱动 **UE 5.1–5.5** 编辑器做蓝图读写、PIE 验证、日志查询与批量操作。
>
> 官方 `ModelContextProtocol` 插件只随 UE 5.8 提供；本端点是**同一协议的自建实现**，
> 零额外依赖（UE5.1 内嵌 Python 3.9 装不了官方 mcp SDK，故手写 ~300 行）。

---

## 一、前置条件

1. UE 编辑器已打开目标项目（5.1–5.5），且已按 DEPLOY.md 完成部署（Bridge DLL + 远程执行）；
2. Bridge HTTP 服务在线（`python scripts\ue5_runner.py health` 通过；未启动时 runner 会自动远程拉起）；
3. 取 token：`%APPDATA%\ue5_bridge\token` 文件内容（bridge 首次启动自动生成）。

```cmd
:: 快速取 token（cmd）
type %APPDATA%\ue5_bridge\token
```

## 二、客户端配置

### ZCode（`%USERPROFILE%\.zcode\cli\config.json`）

在顶层 `mcp.servers` 下加一项（与官方 `unreal`（8000 端口）**并存**，互不冲突）：

```json
{
  "mcp": {
    "servers": {
      "unreal":       { "type": "http", "url": "http://127.0.0.1:8000/mcp", "timeoutMs": 60000 },
      "ue5-automation": {
        "type": "http",
        "url": "http://127.0.0.1:8889/mcp",
        "timeoutMs": 60000,
        "headers": { "X-Skill-Token": "<token 文件内容>" }
      }
    }
  }
}
```

> 若你的客户端版本不支持自定义 headers：改用 URL 查询参数
> `"url": "http://127.0.0.1:8889/mcp?token=<token>"`（⚠️ token 会进 URL，仅限本机回环）。

### Claude Desktop / 其他 `mcpServers` 风格

```json
{
  "mcpServers": {
    "ue5-automation": {
      "type": "http",
      "url": "http://127.0.0.1:8889/mcp",
      "headers": { "X-Skill-Token": "<token>" }
    }
  }
}
```

## 三、协议与冒烟测试

**支持**：`initialize`（协议版本协商 2025-06-18 / 2025-03-26 / 2024-11-05）、
`notifications/*`（202 空体）、`ping`、`tools/list`、`tools/call`、`resources/list`、`prompts/list`。
响应为单 JSON（Streamable HTTP 允许）；`initialize` 返回 `Mcp-Session-Id` 头（无状态兼容，可不回传）。

```bash
:: curl 冒烟（Windows cmd 用 ^ 换行；bash 用 \）
curl -s -X POST http://127.0.0.1:8889/mcp ^
  -H "Content-Type: application/json" ^
  -H "X-Skill-Token: <token>" ^
  -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/list\"}"
```

```python
# python 冒烟（任意 3.8+ 环境）
import json, urllib.request
TOKEN = open(r"%APPDATA%\ue5_bridge\token").read().strip()  # 注意展开
def rpc(method, params=None, _id=1):
    body = {"jsonrpc": "2.0", "id": _id, "method": method}
    if params: body["params"] = params
    req = urllib.request.Request("http://127.0.0.1:8889/mcp",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Skill-Token": TOKEN})
    return json.loads(urllib.request.urlopen(req, timeout=60).read())
print([t["name"] for t in rpc("tools/list")["result"]["tools"]])
```

## 四、工具清单（23 个）

| 分组 | 工具 |
|:--|:--|
| 诊断 | `ue5_health` · `ue5_diag` |
| 只读 | `ue5_list_assets` · `ue5_read_blueprint` · `ue5_read_nodes` · `ue5_read_connections` |
| 蓝图写 | `ue5_build_blueprint` · `ue5_build_batch` · `ue5_compile` · `ue5_connect_pins` · `ue5_disconnect_pin` · `ue5_delete_node` |
| 资产 | `ue5_save_asset` · `ue5_delete_asset` |
| PIE 验证 | `ue5_pie_start` · `ue5_pie_stop` · `ue5_pie_state` · `ue5_pie_actors` · `ue5_pie_get_property` · `ue5_pie_set_transform` |
| 日志/批量 | `ue5_logs` · `ue5_batch` |
| 材质（v3.0） | `ue5_command` 内：`material_create` / `material_add_expression` / `material_connect_expressions` / `material_connect_property` / `material_set_expression_property` / `material_compile` / `material_read` / `material_instance_create` / `material_instance_set_parameter` |
| UMG（v3.0，需 v3.0 DLL） | `ue5_command` 内：`widget_create` / `widget_add_child` / `widget_set_property` / `widget_read_tree` |
| 透传 | `ue5_command`（**72 条**白名单命令全量入口；描述中列全命令名） |

**为何不平铺 72 条命令**：`tools/list` 体积直接吃客户端 token 预算（官方 MCP 的
`describe_toolset` 就因超预算被截断）。这里 23 个高频工具 + 1 个透传入口，
覆盖全量能力的同时保持清单精简。

## 五、典型工作流（PIE 运行时验证）

```
ue5_build_blueprint(spec)                 # 建蓝图（事件+函数节点+连线+编译）
ue5_pie_start(mode="simulate")            # 启动 Simulate
ue5_pie_state()                           # 轮询 is_playing == true
ue5_pie_actors(name_filter="BP_MyActor")  # 找运行时对象（路径含 UEDPIE_0_）
ue5_pie_get_property(actor, "Health")     # 读运行时变量 → 证明逻辑真的跑了
ue5_pie_set_transform(actor, location=[...])  # 传送后复读，验证响应
ue5_logs(pattern="BP_MyActor")            # 自取日志交叉验证
ue5_pie_stop()
```

## 六、安全边界（与 REST 端点同级）

- `/mcp` 与其余 9 个端点**同一 token 鉴权**（X-Skill-Token / Bearer / ?token= 三通道）；
- 仅监听 `127.0.0.1:8889`，不对外暴露；
- `tools/call` 只路由到**白名单实现**（23 工具 + 72 命令），无任意代码执行入口；
- 通知类消息不消耗任何 UE 操作；所有 UE 对象访问仍走 GameThread 队列。
