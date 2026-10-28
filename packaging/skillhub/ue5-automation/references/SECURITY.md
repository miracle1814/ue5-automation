# 安全模型与权限说明

本 Skill 操作的是用户本机的 UE 工程，安全边界必须写清楚。

## 边界

- bridge 仅监听 `127.0.0.1`，不对外网开放；
- 所有请求必须带 `X-Skill-Token`；没有默认 token；
- token 存在 `%APPDATA%\ue5_bridge\token`，可用 `UE5_BRIDGE_TOKEN` 覆盖；
- 命令白名单，不接受任意 Python/代码执行；
- 资产路径只允许 `/Game/`，拒绝 `..`、`.`、反斜杠、绝对路径；
- 日志读取只允许项目日志/Saved/临时目录，且必须是 `.log`；
- 场景 JUnit 输出有路径白名单；
- 删除/批量操作默认 dry-run 或需要 `confirm`；
- 不向外部服务上传工程内容；无遥测；
- HTTP 请求体上限 8MB；
- 幂等缓存/锁/回执都有数量上限，TTL 有界。

## 用户授权

执行以下操作前必须得到用户明确同意：

- 删除资产；
- 批量重命名/搬迁；
- 替换已有图（`apply_graph{replace:true}`）；
- 保存/覆盖工程资产；
- 向工程写入新资产；
- 启停 PIE；
- 撤销用户已有操作。

只读查询（health/read/list/logs）不需要额外确认。

## 多 Agent

- 任务锁由 `task_begin` 自动持有；
- 显式会话锁由 `session_lock_acquire` 持有；
- 两者互相可见；
- 锁是协作式的，不是编辑器强制锁；所有写操作走 `execute_command` 才经过锁层；
- `force=true` 只抢会话锁，不抢任务锁。

## 不会做的事

- 不上传工程文件到外部；
- 不读取用户文档/浏览器数据；
- 不执行用户未授权的 shell 命令；
- 不把 token 写进日志或返回体；
- 不在失败时伪造 `verified=true`。

## 报告问题

请通过项目主页的 Issue 或安全公告联系作者：

```text
https://github.com/miracle1814/ue5-automation
```
