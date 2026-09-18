# UE5 蓝图自动化助手 — 用户上手卡（1 页速览）

> Skill：`ue5-automation` · v0.15 · 2026-09-18 · **UE 5.1.x 开箱即用**（5.2–5.5 需重编译 bridge → `UE_VERSION_GUIDE.md`）
> 完整手册：SKILL.md ｜ 部署路线图：docs/SOP.md ｜ **UE 版本适配/升级：UE_VERSION_GUIDE.md** ｜ 调度内部：workflow.md

## 一句话：这工具能帮你做什么

在**已打开的 UE5 编辑器（随包预编译桥仅实测 5.1.x）**里，用一句中文自然语言驱动 AI 完成
「读蓝图 / 写蓝图 / 查报错 / 评项目 / 改蓝图」五类事；写操作走本机 HTTP Bridge
（127.0.0.1:8889），你无需手动翻 UE Python API，也不用写代码。

## 5 个场景 — 你只需说一句

| # | 场景 | 你想做什么 | 你只需说（路径/描述照抄改） |
|:--:|:---|:---|:---|
| **A** | 分析蓝图 | 读懂已有蓝图：变量清单、节点拓扑、流程图、引用资产、优化建议 | `分析蓝图 /Game/Blueprints/BP_EnemyController` |
| **B** | 创建蓝图 | 用自然语言搭蓝图骨架（资产+变量+组件，编译验证通过） | `创建一个可拾取物品蓝图，含拾取半径、物品类型枚举、拾取粒子特效` |
| **C** | 报错诊断 | 定位 UE 报错根因；可安全修复项会先征求你同意再改 | 直接把 Output Log 报错片段贴过来即可 |
| **D** | 项目评估 | 全项目架构体检：复杂度 / 耦合度 / 循环依赖 / Top20 热力图 | `分析项目 /Content/MyGame` |
| **E** | 增量修改 | 在已有蓝图上改参数、连线、断线、删节点（v0.7+） | `修改蓝图 /Game/BP_Enemy：把 PrintString 文本改为 "Enemy Spawned"` |

> 💡 不确定该用哪个？直接说你的目标即可，调度侧按优先级自动判定：
> **报错日志 → 蓝图路径分析 → 创建/新建 → 架构评估**（详见 SKILL.md「三、四种模式入口」与 workflow.md §5.2）。

## 开始前 — 前提条件（3 项）

1. **UE5 编辑器已打开目标项目**（写操作必需；只读分析也建议打开）
   —— ⚠️ **随包预编译桥（DLL）仅实测 UE 5.1.x**：先跑 `python scripts\check_ue_version.py`
   自检（30 秒，只读）；若你的引擎是 5.2+，**升级路径与出口见 `UE_VERSION_GUIDE.md`**
   （拿不到源码时只读分析/项目评估/报错诊断仍可用，写操作需先重编译桥）。
2. **Python Editor Script Plugin 已启用**：Edit → Plugins → 搜索 "Python" → 勾选；
   且 `Project Settings → Plugins → Python → Enable Remote Execution` 已勾选
   （读操作走 UDP 多播自动发现，不依赖固定端口）
3. **写操作走 HTTP Bridge 8889**：ue5_runner 的 build/connect 会自动检测并启动
   （`127.0.0.1:8889`，需 `X-Skill-Token` 头）；首次使用须 BlueprintPythonBridge 插件已加载

> 🆕 新电脑 / 新宿主机首次部署：按 **docs/SOP.md** 三步走——
> `python scripts/init.py` 探测预览 → `--apply` 落盘建议稿 → **人工确认门**
> （`--assign` 或手编 `output/role_assignments.yaml` 后 `--reconcile --apply`）→
> 三级连通测试（health → read → build）。

## 执行时会发生什么

1. 你说需求 → AI 判定模式 → 分派对应 Agent 开始处理
2. **创建（B）/ 修改（E）涉及写操作：AI 先出方案或干跑，经你确认后才动编辑器** —— 不会擅自改你的蓝图
3. 完成后汇报结果与报告/资产位置；编译失败会自动转入报错诊断
4. 报告为中文 Markdown，问题分级 🔴 阻断 / 🟡 警告 / 🟢 建议，不确定处标注 ⚠️ 待确认

## 卡住了去哪查

| 现象 | 去哪查 |
|:---|:---|
| 写操作超时 / Connection Refused / 403 | **docs/SOP.md §6 故障速查** → docs/SOP-UE-HTTP-BRIDGE.md 排查清单（403 查 `X-Skill-Token` 头） |
| 创建后没连线 / 参数没生效 / 分析看不到节点详情 | SKILL.md FAQ Q1/Q2 + **docs/pitfalls.md** |
| Python 插件不可用 | SKILL.md §七 7.2 |
| 端口 8889 被占 | SKILL.md FAQ Q3 |
| 已知陷阱（P01–P19：连线假阳性 / GC 回收 / CDO 时序 / 事件存在性误判漏建 / 同名节点定位 / spec 字段名契约不一致…） | **docs/pitfalls.md** |
| 初始化 / 部署 / 角色确认门卡住 | docs/SOP.md §3–§4 |
| 更细的功能参数、触发词、分工链 | SKILL.md §三 / workflow.md |

## 边界提醒

- 只支持 **UE5 蓝图**（随包桥 DLL **实测 5.1.x**；5.2–5.5 需按 `UE_VERSION_GUIDE.md` 重编译后再用）；不支持 UE4.x、C++ 源码级、已打包运行时的操作
- Mermaid 图默认本地渲染（L1）；L2 在线渲染会把图发第三方 `mermaid.ink`，敏感项目慎用
- 能力矩阵 / 27 个 C++ Bridge API / 各模式参数 → SKILL.md §三、§5.1

---
*本卡内容与 SKILL.md v0.13 + workflow.md 对齐；由 小岑（文档分析 Agent）起草 · T-20260909-SKILL-DOC-USERGUIDE，T-20260915-UE5PKG-VERSION-DOC 修订版本口径与升级指针，T-20260917-RELEASE-V218 同步 v0.13（D-1~D-4 修复随包）。*
