# ue5-automation v6.5 移植包 · 新机测试指南

> **这是什么**：把 UE 编辑器里的"手动连蓝图"变成 AI 可调用、**可验证、可回滚、可审计**的接口。
> 本包自带 UE 5.1 / 5.8 两套预编译 DLL 与一键部署工具，新机 10 分钟可上线。
>
> **版本**：v6.5.4（skill v1.14.0）· 101 条白名单命令 / 34 个 MCP 工具
> **离线基线**：820 passed / 95 skipped / 0 failed（源码仓库）
> **构建日期**：2026-10-28

---

## 一、前置条件（只需两样）

1. **Windows** + 一个 UE 项目（引擎 **5.1** 或 **5.8**；其它 5.x 见第五节）
2. 本机装有 **Python 3.8+**（仅用于跑部署脚本，不是编辑器内那个）

> 检查：命令行执行 `py --version` 或 `python --version`，有输出即可。
> 没有就去 https://www.python.org/downloads/windows/ 装，安装时勾选 "Add Python to PATH"。

---

## 二、三步部署（约 5 分钟）

1. **解压**本包到任意目录（路径别含中文）
2. **双击 `install.bat`**
3. 把项目的 `.uproject` 文件**直接拖进窗口** → 回车

脚本会自动：读引擎版本 → 选对 DLL → 装插件 → 装宿主脚本与管理端 → 写自启脚本 → 在 `.uproject` 里启用插件 → 跑环境自检。

**部署完提示什么算成功**：看到 `═══ 小结：… FAIL 0 ═══`。

---

## 三、验收清单（逐项勾，约 5 分钟）

启动/重启 UE 编辑器后，按顺序验证：

### □ 1. 桥自启
Output Log 中搜索 `ue5_bridge`，应看到：
```
[ue5_bridge] [OK] 服务已在后台启动 → http://127.0.0.1:8889/docs
```
> 没有这行 → 见第四节 #1

### □ 2. C++ 桥加载
Output Log 中搜索 `BlueprintPythonBridge`，应看到 `InternalLoadLibrary` 且**没有**
`Incompatible or missing module`。
> 出现拒载 → 见第四节 #2

### □ 3. 管理端页面
取 token（文件 `%APPDATA%\ue5_bridge\token` 的内容），浏览器打开：
```
http://127.0.0.1:8889/ui?token=<粘贴token>
```
应看到「任务指挥台」：顶部 4 个 KPI、5 段阶段轨道、左侧任务列表。

### □ 4. 让 AI 干一件小事（核心验证）
对 AI 说：
> 用 ue5-automation 帮我给某个蓝图加一条 BeginPlay 打印日志，做完告诉我改了哪儿、怎么验证。

**预期过程**（管理端会实时显示）：
- 「需求来源」出现**你的原话** + 「我理解成」要点
- 「执行脉络」每步下面有**命令流水**（下发了什么命令、参数、UE 返回、UE 内动作）
- 「交付物与验证」出现改了哪些资产 + 分步验证指引

**三个判好坏的硬信号**：
| 信号 | 位置 | 合格标准 |
|---|---|---|
| 写图读回 | 命令流水 | `verified ✓` |
| 编译 | 命令流水 / 诊断返回 | `compiled ✓`（`errors` 非空不代表没修好） |
| 未验证徽章 | 任务卡右上 | **不应出现「⚠ 未验证」**（出现 = agent 只是自述成功） |

### □ 5. 截图闭环（可选）
让 AI「截一张编辑器画面给我看」→ 应在 `Saved/Screenshots/ue5-automation/` 落盘 PNG，
且管理端「交付物」里出现该截图。

### □ 6. 失败能诚实报错（可选但重要）
让 AI 改一个**不存在**的蓝图 → 应看到明确的失败（不是假成功），
管理端任务卡显示红色「失败」+ 原因。

### □ 7. 多 Agent / 新能力（v6.5，可选）
在浏览器/curl（带 `X-Skill-Token`）或 MCP `execute_command` 里逐条试：

| 命令 | 预期 |
|---|---|
| `session_lock_acquire{owner:"A",assets:["/Game/BP_X"]}` | success，acquired 小写路径 |
| 同资产再 acquire `owner:"B"` | 失败：`资产被其他会话锁定 ... 剩余 Ns` |
| `session_lock_release{owner:"A"}` | released=1 |
| 同一 `idempotency_key` 调 `health` 两次 | 第二次结果带 `idempotent_replay:true` |
| 同 key 换命令 | 失败：`idempotency_conflict` |
| `verify_scenario{start_pie:false,steps:[{type:"command",command:"health"}]}` | verified=true |
| 场景里 `delete_asset`（不带 allow_destructive） | 失败：`场景默认禁止高危命令` |
| `animbp_probe` 一个 AnimBP | is_animbp=true；`writes_supported=false` |
| 9MB POST `/command` | HTTP 413 |

> 会话锁是协作式；要强制互斥请让所有写操作走 `execute_command`（带 `session_id`）。

---

## 四、新机常见问题

| # | 现象 | 原因与处理 |
|---|---|---|
| 1 | 没有 `服务已在后台启动` | ① 插件未启用：Edit→Plugins 搜 Blueprint，勾选并重启 ② `Content\Python\init_unreal.py` 缺失 → 重跑 install.bat ③ 看日志中 `bridge 启动失败` 的具体异常 |
| 2 | `Incompatible or missing module` | DLL 与引擎版本不匹配，或 `UnrealEditor.modules` 未与 DLL 同目录 → 重跑 install.bat（它会按 EngineAssociation 选 DLL） |
| 3 | 日志提示 `FastAPI/uvicorn 未安装` | 用引擎自带 Python 装：`<UE目录>\Engine\Binaries\ThirdParty\Python3\Win64\python.exe -m pip install fastapi uvicorn pydantic` |
| 4 | 浏览器打开提示 401 未授权 | token 不对：`%APPDATA%\ue5_bridge\token` 的内容整段粘贴（URL 里 `?token=` 后面） |
| 5 | 端口 8889 被占用 | 关掉另一个已开的编辑器实例（本工具只允许一个桥），或设环境变量 `UE5_BRIDGE_PORT=8890` 后重启编辑器 |
| 6 | 管理端页面空白/一直“连接中” | 浏览器按 F12 看 Console；若是脚本报错请联系维护者（v4.0 已做语法回归测试） |
| 7 | 截图没有 actor 名字标注 | 已知边界：引擎内 Pillow 不可用（标注自动跳过，截图本身正常） |

---

## 五、引擎不是 5.1 / 5.8 怎么办

包内只有这两套预编译 DLL。其它 5.x（如 5.3/5.5）需要**自己编译一次**：

1. 装 Visual Studio 2022（含"使用 C++ 的桌面开发"）
2. 在包根目录执行（把引擎路径换成你的）：
   ```
   <引擎>\Engine\Build\BatchFiles\RunUAT.bat BuildPlugin ^
     -Plugin="<本包>\plugin\BlueprintPythonBridge\BlueprintPythonBridge.uplugin" ^
     -Package="<本包>\plugin\BlueprintPythonBridge\Out" -TargetPlatforms=Win64
   ```
3. 把 `Out\BlueprintPythonBridge\Binaries\Win64\` 里的 DLL 与 `UnrealEditor.modules`
   **成对**复制到项目的 `Plugins\BlueprintPythonBridge\Binaries\Win64\`
4. 源码在 `plugin\BlueprintPythonBridge\Source\`，编译报错参考 `docs\pitfalls.md` 的 B1~B6

---

## 六、包里有什么

```
install.bat                     ← 双击这个
README-新机测试.md               ← 本文
VERSION.txt
plugin/
  BlueprintPythonBridge/        ← 插件（含 C++ 源码 + 5.1 DLL + 头文件）
  prebuilt-5.8/                 ← 5.8 DLL + modules（按需覆盖）
host/
  python/                       ← 宿主脚本 23 个（部署到 Content/Python）
  ui/index.html                 ← 管理端页面（部署到 Content/ui）
tools/
  deploy_portable.py            ← 一键部署（可 --dry-run 预览）
  check_env.py                  ← 环境自检（包完整/项目/引擎/依赖/运行时）
docs/
  QUICKSTART.md                 ← 通用快速开始（含手动安装方式）
  SKILL.md                      ← AI 技能手册（agent 的行为规范）
  pitfalls.md                   ← 已知陷阱（P01-P19 蓝图 + B1-B17 构建/真机实录）
  VERIFICATION-RELEASE-3.9.md   ← 双引擎真机验证记录 + 已知边界
  MCP-ACCESS.md                 ← MCP 客户端接入方式
  reports/                      ← 历史验证与交付报告（4 份）
```

---

## 七、注意事项

- **token 不入包**：首次启动编辑器时在 `%APPDATA%\ue5_bridge\token` 自动生成，不要外传
- **别手动改 `Binaries\` 里的 DLL**：要换引擎版本就重跑 install.bat（会按 EngineAssociation 选）
- **一个桥只对应一个编辑器实例**：同时开两个编辑器会抢 8889 端口
- **测试数据**：任务历史存在 `%APPDATA%\ue5_bridge\tasks\`，删掉即清空（面板会变空）
- **想关掉自动打开面板**：设环境变量 `UE5_AUTO_OPEN_PANEL=0`

---

## 八、测试记录（新机填这张表回报）

| 项 | 结果 | 备注 |
|---|---|---|
| 引擎版本 / 项目名 | | |
| 部署脚本 FAIL 数 | | 期望 0 |
| ① 桥自启日志 | □通过 □失败 | |
| ② C++ 桥加载（无拒载） | □通过 □失败 | |
| ③ 管理端页面 | □通过 □失败 | |
| ④ AI 干小事（verified/compiled/无未验证徽章） | □通过 □失败 | |
| ⑤ 截图闭环 | □通过 □失败 □未测 | |
| ⑥ 失败诚实报错 | □通过 □失败 □未测 | |
| 遇到的报错原文 | | |
