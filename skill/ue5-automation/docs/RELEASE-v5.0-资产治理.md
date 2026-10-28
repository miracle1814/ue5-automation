# v5.0 · P1 资产治理（批量操作 + 任务级资产回滚）

> 2026-10-28 · P1 第一批（资产线）。AnimBP 纵深单独立项：需要新增 C++ 节点类型支持，
> 不塞进本批，避免半成品。

---

## 1. 解决什么

P0 阶段解决了「代码/图」的可回滚，但资产层还有两个真实痛点：

1. **批量重命名/搬迁**只能一个个手动做，容易漏、容易覆盖；
2. **资产级撤销缺失**：任务里新建的蓝图撤不掉、删掉的资产找不回
   （UE 事务只管内存对象，不管磁盘资产）。

---

## 2. 新增能力（白名单 87 → 90）

### 2.1 `batch_asset_ops` — 批量重命名/搬迁

```json
{"command": "batch_asset_ops", "params": {
  "items": [{"from": "/Game/A/BP_X", "to": "/Game/B/BP_X"}],
  "dry_run": true}}
```

- **默认 dry-run**：只做预检（源存在？目标冲突？）→ 返回逐条 `plan`；
- 真跑：`dry_run=false` + `confirm=true`；逐条 `rename_asset` + 回读验证
  （目标存在且源消失才算成功）；
- 目标已存在 → 冲突，**不覆盖**；总数上限 50；
- 守卫对 dry-run 豁免（只读预检不需要 confirm）。

### 2.2 `task_assets` — 任务资产账本

返回任务的新建/删除/快照清单（`created_assets` / `deleted_assets` /
`asset_snapshots`）。任务结束后仍可查（历史账本）。

### 2.3 `rollback_task_assets` — 任务级资产回滚

```json
{"command": "rollback_task_assets", "params": {"task_id": "20261028-03",
                                               "confirm": true}}
```

- **删除本任务新建的资产**（蓝图/材质/UMG 创建时自动记账）；
- **从删除前快照恢复被删资产**（`delete_asset` 成功前自动把 `.uasset`
  复制到 `%APPDATA%/ue5_bridge/snapshots/<task_id>/`）；
- 只动账本里记录过的资产，绝不扫描/猜测；
- 恢复是**文件级**：写回原路径；编辑器内已加载的包需要重载
  （`reload_asset` 不可用时返回 `reload_required=true`，提示重启/Rescan）；
- **修改类资产不在本命令范围**（用 `agent_undo` 的事务回滚）；
- 需 `confirm=true`；有失败项 → `success=false` + `_fail_loud`。

---

## 3. 真机验证（5.1 + 5.8）

流程：`task_begin` → 建两个蓝图 → 保存并删除其中一个 → 查账本 →
`rollback_task_assets` → 验证新建被删、被删从快照恢复 → 批量重命名 dry-run/真跑。

| 断言 | 5.1 | 5.8 |
|---|---|---|
| 账本记录 created / deleted | ✅ | ✅ |
| 删除前快照存在（22,053 / 24,353 B） | ✅ | ✅ |
| 回滚删除新建资产 | ✅ | ✅ |
| 回滚恢复被删资产（文件写回） | ✅ | ✅ |
| 恢复后 `reload_required=true`（两引擎均无 reload_asset） | ✅ | ✅ |
| batch dry-run 计划正确 | ✅ | ✅ |
| batch 真跑重命名 + 回读验证 | ✅ | ✅ |
| 清理测试资产 | ✅ | ✅ |

---

## 4. 边界（写清，避免误期待）

1. **恢复不等于热重载**：文件写回后，已加载的包在编辑器里仍是旧状态；
   当前两引擎 Python 未暴露 `reload_asset` → 需要重启编辑器或 Content Browser
   Rescan。命令会返回 `reload_required=true`。
2. **只覆盖账本资产**：账本只记 build_blueprint / material_create / widget_create
   与 delete_asset；手动在编辑器里建的资产不在账本内。
3. **修改类资产**：用 `agent_undo`（事务），不要期待资产回滚处理。
4. **快照清理**：快照存于 `%APPDATA%/ue5_bridge/snapshots/`，当前不自动清理
   （避免误删回滚依据）；后续可加按时间清理。

---

## 5. 测试与产物

- 新增 `tests/test_asset_governance.py` **13 项**（dry-run/冲突/confirm/账本/
  回滚/快照/守卫豁免/注册）；
- 全量 **756 passed / 0 failed**；
- 移植包：`dist/ue5-automation-v5.0-portable.zip`。

---

## 6. 下一步（P1 剩余）

- **AnimBP 纵深**（独立项，需 C++ 节点类型支持：StateMachine/State/Transition/
  SequencePlayer/BlendSpacePlayer）；
- 响应塑形（`_fields`）与 namespace 分派；
- DataTable CSV 读写；
- 打包构建钩子。
