# 快速开始（10 分钟）

> 面向**第一次安装**的用户。完整手册见 `SKILL.md`；排错见 `pitfalls.md`。
> 支持 UE **5.1** 与 **5.8**（预编译 DLL 随包；5.2–5.7 需自行编译，见 `bridge/BlueprintPythonBridge/Source/BUILD.md`）。

## 第 1 步：装桥插件（2 分钟）

把两样东西放进你的 UE 项目：

```
<你的项目>/Plugins/BlueprintPythonBridge/    ← 整个插件目录（uplugin + Binaries）
<你的项目>/Content/Python/                   ← scripts/ 下全部 .py 与 .tmpl
<你的项目>/Content/ui/index.html             ← 管理端页面（可选，但强烈建议）
```

DLL 选择：**5.1 项目**用 `bridge/BlueprintPythonBridge/Binaries/Win64/`；
**5.8 项目**用 `bridge/prebuilt-5.8/` 下的 DLL 与 `UnrealEditor.modules`（两者必须同目录、成对替换）。

## 第 2 步：让桥自启（1 分钟）

新建 `<你的项目>/Content/Python/init_unreal.py`：

```python
import traceback
try:
    import ue5_bridge
    ue5_bridge.start()
except Exception:
    import unreal
    unreal.log_error("[ue5-automation] bridge 启动失败:\n" + traceback.format_exc())
```

> ⚠️ **UE 5.8 起项目级 `Content/Python/init_unreal.py` 不再自动执行**（5.1 正常）。
> 5.8 项目二选一：
> - 用 `packaging/portable/bootstrap-plugin/ue5-automation-bootstrap`（纯 Python 引导插件，
>   放进项目 `Plugins/` 并启用；`EnabledByDefault=true`）；
> - 或自建任一 Content-only 插件，把上面的 init 代码放进它的
>   `Content/Python/init_unreal.py`。
>
> 移植包（`dist/*-portable.zip`）已内置该引导插件，一键部署时自动安装并启用。

## 第 3 步：确认依赖（1 分钟）

编辑器内的 Python 需要 `fastapi / uvicorn / pydantic`（多数人已在用）。
缺失时引擎 Output Log 会提示，用**引擎自带 python** 装：

```
<UE安装目录>/Engine/Binaries/ThirdParty/Python3/Win64/python.exe -m pip install fastapi uvicorn pydantic
```

## 第 4 步：启动并验证（3 分钟）

1. 重启 UE 编辑器 → Output Log 出现 `[ue5_bridge] [OK] 服务已在后台启动 → http://127.0.0.1:8889/docs`
2. token 在 `%APPDATA%\ue5_bridge\token`（首启动自动生成）
3. 浏览器打开管理端：`http://127.0.0.1:8889/ui?token=<粘贴 token>` —— 看到「任务指挥台」即成功
4. 让 AI 客户端接入 MCP：`http://127.0.0.1:8889/mcp`（token 用 `X-Skill-Token` 头或 `?token=`）

## 第 5 步：第一次真实任务（3 分钟）

对你的 AI 说：**"用 ue5-automation 帮我给 BP_XXX 加一个 BeginPlay 时打印一条日志"**。

正确的行为序列（管理端会实时显示）：

1. `ue5_task_begin` —— 回显需求要点 + 计划步骤（**你此刻能纠偏**）
2. `ue5_search_blueprint_nodes` → `ue5_apply_blueprint_graph` —— 查签名后整图写入
3. `ue5_compile_and_diagnose` —— 编译并拿结构化错误（失败会自动修，≤5 轮）
4. `ue5_task_end` —— 复盘（实现什么/耗了多少轮/经验）

**判好坏的三个信号**：
- 写图返回 `verified: true`（读回验证通过）
- 编译返回 `compiled: true`（收敛判据；`errors` 非空不代表没修好）
- 任务卡上没有「⚠ 未验证」徽章（有 = agent 只是自述成功，没做验证）

## 常见问题

| 现象 | 处理 |
|---|---|
| 编辑器日志 `Incompatible or missing module` | DLL 与 `UnrealEditor.modules` 必须成对、与引擎**次版本**一致 |
| `/health` 里 `engine_version: unknown` | 正常，`/health` 不触达 UE；看 `/ui/api/task` 是否有数据 |
| 写图报「引脚不存在或类型不可连」 | UE 不允许该类型直连（如 float→bool），先在 spec 里加转换/比较节点 |
| 诊断 `errors` 为空但编译失败 | 日志尚未落盘，工具会自动等 2 秒重读；仍空请查编辑器日志 |
| 截图没有 actor 标注 | 引擎内 Pillow 不可用（已知边界），标注自动跳过、截图本身正常 |
