# 视觉自检 SOP（完整版）

> 从 SKILL.md §3.8 迁出（v4.6 分层）。四步速查仍在 SKILL.md。

## 视觉自检 SOP — 截图反馈闭环（agent 必须遵守）

**v0.23 新增。** 一句话规则：**改完必看一眼 —— 每轮视觉相关的修改后调
`ue5_screenshot` 确认画面真的变了，看不到预期变化就别声称成功**。
（blender-mcp 的明星功能是"AI 检查自己的产出"；本工具的"眼睛"由此补齐。）

**用法（MCP 工具 `ue5_screenshot` · 工具数 26→27 · 需 v3.7 版桥 DLL）：**

```
ue5_screenshot()                                    # 当前编辑器视口（快速预览）
ue5_screenshot(source="agent_camera",
               camera={"pitch":-30,"yaw":45,"roll":0})   # 确定性机位，可多角度环绕连拍
ue5_start_pie() → ue5_screenshot(source="pie")      # PIE 运行画面（未运行会明确报错）
```

返回：base64 JPEG 图（长边默认 768，JPEG q80 —— token 成本纪律）+ 元数据
（`saved_path` / `camera_used`+`camera_source` 机位溯源 / `annotated_actors`）。
全尺寸原图只落盘 `<项目>/Saved/Screenshots/ue5-automation/`（仪表盘画面面板
同源），不进上下文。actor 名标注需 Pillow（可选依赖：缺失时跳过标注并告警，
**绝不因缺 Pillow 失败**）。

**硬规则：**

- **机位溯源必须先看**：`camera_source=fallback`（没找到视口/玩家，用了兜底
  机位）→ 这张图的构图不可据此下结论（UEBox 实测教训），先修复取景再判断
- **多角度防呆**：同一场景改 `camera` 的 pitch/yaw 连拍对照 —— 图像几乎相同
  说明看的是缓存/错位画面，不是新渲染
- **PIE 未运行 ≠ 拿编辑器画面凑数**：`source="pie"` 在 PIE 未运行时明确报错，
  不要把编辑器视图当成游戏运行证据
- **超时即报错**：截图文件 2s 未就绪（编辑器失焦会暂停帧推进）会明确失败，
  绝不返回"成功但无文件"；此时把编辑器调到前台重试
- **路径边界**：落盘只允许约定目录内的相对路径（防逃逸）；改路径策略属于
  安全边界变更，需要人工确认
- **标注依赖 Pillow**：环境没有 Pillow 时工具照常出图，只是无标注 —— 不要
  把"无标注"当成"场景为空"


