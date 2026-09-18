# SOP · UE5 HTTP Bridge 启动与排查

> **作者**：maintainer + dev（实测）
> **日期**：2026-08-05
> **事故**：pytest 3 个 FAIL（Bug 1/3/4）反复测不通 → 根因非代码，而是 **HTTP Bridge server 未启动**
> **等级**：🔴 P1（每次 UE 重启都会触发，所有写操作类测试/Agent 任务全部受影响）

---

## 🚨 一句话总结

**UE5 编辑器重启后，HTTP Bridge 不会自动启动**——必须**显式调用一次** `ue5_bridge.start(port=8889)`。
所有写操作（pytest 端到端 / `runner.build()` / `runner.connect()` / 直接 `urllib POST /command`）之前必须先确认 Bridge 存活。

---

## ✅ 启动方式（三种，任选一种）

> ⛔ **勿用 `-ExecutePythonScript` 拉起**（缺陷 D-4 · 2026-09-14）：启动脚本跑完 ~0.5s 会触发 `QUIT_EDITOR` 致编辑器自杀 + token 每次重置 → 一律**干净启动 UE + 本页方式 A/B/C 注入**。详见 `SOP.md §4.2`。

### 方式 A：通过 ue5_runner 自动启动（**推荐**）

写操作前调用：
```python
from ue5_runner import UE5Runner
runner = UE5Runner(http_port=8889)  # 端口 = UE 那边启动的端口
runner.ensure_http_bridge()         # 自动检测 + 启动 + 等待就绪
# 之后 runner.build() / runner.connect() 都会先 ensure
```

**原理**（ue5_runner.py L268-295）：
1. 检测 `_http_bridge_alive()`：TCP 连 8889，能连 = 已活
2. 不活 → 通过 `run_python()` 走 ue_send（UDP）发到 UE 进程
3. 在 UE 进程里 `import ue5_bridge; ue5_bridge.start(host="127.0.0.1", port=8889)`
4. 后台线程拉起 uvicorn server
5. 等待 30s（每 0.5s 重试）直到 `_http_bridge_alive()` 返回 True

### 方式 B：在 UE Python Console 里手动启动

UE 编辑器 → Output Log → Python 输入框：
```python
import sys
sys.path.insert(0, r"<skill 根目录>\scripts")   # 替换为 ue5-automation 在本机的实际根目录
import ue5_bridge
ue5_bridge.start(port=8889)
# 输出：[ue5_bridge] [OK] 服务已在后台启动 → http://127.0.0.1:8889/docs
```

### 方式 C：ue_send 远程发送启动代码

Agent 侧（无需 UE 编辑器焦点）：
```bash
python ue_send.py "
import sys
sys.path.insert(0, r'<skill 根目录>\scripts')   # 替换为 ue5-automation 在本机的实际根目录
import ue5_bridge
ue5_bridge.start(port=8889)
"
```

---

## 🔍 排查清单（HTTP 操作失败时按顺序检查）

| # | 检查项 | 命令 / 方式 | 失败处置 |
|:--:|:---|:---|:---|
| 1 | UE 编辑器在跑？ | 看屏幕 | 没开 → 打开 |
| 2 | Remote Execution 开启？ | `Config/DefaultEngine.ini` → `[PythonScriptPlugin]` → `bRemoteExecution=true` | 没开 → 编辑 ini + 重启 UE |
| 3 | **HTTP Bridge 在跑？** | `python -c "import socket; s=socket.socket(); s.settimeout(2); s.connect(('127.0.0.1', 8889)); print('OK')"` | ConnectionRefused → 启动（方式 A/B/C 任一） |
| 4 | Token 文件存在？ | `dir "%APPDATA%\ue5_bridge\token"` → 应有内容 | 不存在 → 启动一次 Bridge 会自动生成 |
| 5 | Token 头正确？ | `X-Skill-Token: <token>`（不是 `Authorization`） | 改 header |
| 6 | payload 格式正确？ | `/command` = `{"command": "X", "params": {...}}`（参数嵌在 params 子对象） | 修正 payload |

---

## 📋 关键事实（写代码前必读）

| 项 | 值 | 来源 |
|:---|:---|:---|
| **HTTP 端口** | **8889**（环境变量 `UE5_BRIDGE_PORT` 可覆盖） | ue5_bridge.py 默认值（现役） |
| **Token 文件位置** | `%APPDATA%\ue5_bridge\token`（如 `C:\Users\<用户名>\AppData\Roaming\ue5_bridge\token`） | ue5_bridge.py `_token_file` |
| **Token header 名** | `X-Skill-Token` | ue5_bridge.py `_verify_token` |
| **`/command` 端点格式** | `{command: str, params: dict}` | ue5_bridge.py CommandRequest |
| **`set_pin_default_value` 参数** | `params: {bp_path, node_id, pin_name, new_value}`（v0.13 起 `node_id` **兼收节点标题与 32 位 GUID**，亦可显式 `node_guid`；`get_pin_default_value` 同） | ue5_bridge.py _cmd_set_pin_default_value |
| **`unpin_console_refs` 参数** | `params: {asset_path}`（**必填语义**：只解绑 `globals()` 中指向**该资产**的包装器 + `collect_garbage()`；⛔ 空参 / 无参 → `success=false`，**不执行任何删除**，本命令**无全量清理语义**） | ue5_bridge.py _cmd_unpin_console_refs |
| **`build_blueprint` 参数** | `params: {bp_name, pkg?, parent?, vars?, comps?, interfaces?, compile_after?}` —— ⚠️ 本命令为**兼容旧调用**，**无 events/functions/connections 参数**（多传会被静默忽略）；带节点/连线的构建必须走 `POST /build` | ue5_bridge.py _cmd_build_blueprint |
| **uvicorn host** | 必须 `127.0.0.1`（外网绑定被禁止，SEC 限制） | ue5_bridge.py L2944 |
| **GameThread worker** | start() 会自动拉起 GameThread tick worker | ue5_bridge.py `_gt_start_worker` |

---

## ❌ 常见踩坑（maintainer实测）

### 踩坑 1：用 `urllib` 直接 POST，跳过 `ensure_http_bridge()`

```python
# ❌ 错误：直接 urllib 打 HTTP
import urllib.request
req = urllib.request.Request("http://127.0.0.1:8889/command", data=...)
urllib.request.urlopen(req)
# 报错：ConnectionRefused（如果 Bridge 没启）
# 或：403 Forbidden（如果 Token 不对）
```

**正确**：
```python
# ✅ 用 ue5_runner
from ue5_runner import UE5Runner
runner = UE5Runner(http_port=8889)
runner.ensure_http_bridge()  # 关键！会自动拉起
resp = runner.connect("/Game/BP_X.BP_X", "EventBeginPlay", "then", "PrintString", "execute")
```

### 踩坑 2：Token header 写成 `Authorization: Bearer ...`

```python
# ❌ 错误
headers={"Authorization": f"Bearer {token}"}
# 报错：403 Unauthorized: invalid or missing X-Skill-Token
```

**正确**：
```python
# ✅
headers={"X-Skill-Token": token}
```

### 踩坑 3：`/command` payload 字段名错位

```python
# ❌ 错误：字段平铺
payload = {"command": "set_pin_default_value", "bp_path": "...", "node_id": "...", ...}
# 报错：TypeError: lambda() missing required positional argument
```

**正确**：
```python
# ✅ 参数嵌 params
payload = {"command": "set_pin_default_value", "params": {"bp_path": "...", "node_id": "...", ...}}
```

### 踩坑 4：bp_path 传成图路径（含 `:EventGraph`）

```python
# ❌ 错误
{"bp_path": "/Game/BP_HelloMirror.BP_HelloMirror:EventGraph"}
# 报错：BridgeError: 蓝图加载失败
```

**正确**：
```python
# ✅ 只传 BP 资产路径
{"bp_path": "/Game/BP_HelloMirror.BP_HelloMirror"}
# 内部 _cmd_set_pin_default_value 会自动从 BP 找 EventGraph + 节点
```

---

## 🧪 端到端验证脚本

完整可用脚本（保存在 `scripts/_verify_http_bridge.py`）：
```python
"""验证 HTTP Bridge 全链路"""
import urllib.request, json, os, socket, time

PORT = 8889
TOKEN_FILE = os.path.join(os.environ["APPDATA"], "ue5_bridge", "token")  # 等价 %APPDATA%\ue5_bridge\token

# 1. TCP 检查
s = socket.socket()
s.settimeout(2)
try:
    s.connect(("127.0.0.1", PORT))
    print(f"[TCP {PORT}] OK")
except Exception as e:
    print(f"[TCP {PORT}] FAIL: {e} -> 需启动 Bridge")
    raise SystemExit(1)

# 2. Token
token = open(TOKEN_FILE).read().strip()
print(f"[TOKEN] {token[:8]}...")

# 3. /command
payload = {
    "command": "set_pin_default_value",
    "params": {
        "bp_path": "/Game/BP_HelloMirror.BP_HelloMirror",
        "node_id": "打印字符串",
        "pin_name": "InString",
        "new_value": f"VERIFY_{int(time.time())}"
    }
}
req = urllib.request.Request(
    f"http://127.0.0.1:{PORT}/command",
    data=json.dumps(payload).encode('utf-8'),
    headers={"Content-Type": "application/json", "X-Skill-Token": token},
    method="POST"
)
resp = urllib.request.urlopen(req, timeout=10)
body = json.loads(resp.read().decode('utf-8'))
print(f"[HTTP /command] {resp.status} -> {body}")
assert body.get("success") == True, body
print("[OK] HTTP Bridge 端到端验证通过")
```

---

## 🔄 联动项

| 触发场景 | 联动动作 |
|:---|:---|
| UE 重启（手动/崩溃恢复） | **必须**重新 `ensure_http_bridge()`（或方式 B/C 启动） |
| pytest 端到端测试 | 测试 setup fixture 必须先 `runner.ensure_http_bridge()` |
| Agent 写操作任务 | 派单时备注"先 ensure_http_bridge"，防踩坑 |
| README/SKILL.md 提到"自动启动" | 这里是 README §"混合通道"夸大 —— 实测 ue5_runner 调用 build/connect 时才 ensure，**手动 urllib / 直接 /command 不 ensure** |

---

## 📚 经验入卡

- **HTTP Bridge ≠ Remote Execution**：前者需要显式启（线程后台），后者 UE 自动启（UDP 多播）
- **混合通道不是"全自动"**：读通道零配置（ue_send），写通道需 ensure（runner.ensure_http_bridge）
- **早期文档（8/5 实测）写的"6996"已过时：现役 HTTP 端口 = 8889**（环境变量 `UE5_BRIDGE_PORT` 可覆盖）

---

**入库路径**：`default/skills/ue5-automation/docs/SOP-UE-HTTP-BRIDGE.md`
**关联任务**：T-20260805-UE-FULL-CLOSURE
**关联复盘**：T-20260805-INC-026（待建立）

> 📄 **L2 命令清单**：`/command` 白名单 L2 批次 **21 条**（逐条签名 / 入参 / 在线正负路径 / raw 锚点）→ `docs/L2-COMMANDS.md`；全量 50 条速查 → `workflow.md` 附录 B。
