# scripts — 安装后自检

这些脚本只依赖 Python 标准库，不需要安装 `requests`。
`bridge_client.py` 是共享客户端：统一 `/health` 与 `/command health` 的字段解析，
并对连接失败 / HTTP 5xx 做有限重试。

## doctor.py — 体检

```bash
python scripts/doctor.py
python scripts/doctor.py --json
python scripts/doctor.py --port 8890 --token <token> --retries 3
```

检查：token 文件、`/health`、`bridge_loaded`、bridge 版本、项目路径。
退出码 0 = 通过，1 = 有 FAIL。失败时输出 `code` / `message` / `hint`。

## smoke.py — 5 分钟主路径冒烟

```bash
python scripts/smoke.py
python scripts/smoke.py --json
python scripts/smoke.py --name BP_Smoke --pkg /Game/Test/ --retries 2
```

会在项目里创建/替换 `/Game/Test/BP_Smoke`，跑：

```text
health → build_blueprint → search_nodes → apply_graph
      → compile → read_pin → save_asset
```

判定：`verified=true`、`compiled=true`、读回 `Hello from skill smoke`、`saved=true`。
`--json` 的每个失败步骤都带 `code` / `message` / `hint` / `http_status`。

**重试口径：** 默认 `--retries 2`（最多 3 次尝试），仅对连接失败和 HTTP 5xx
重试；401/403 立即失败，不会拿错误 token 空转。

**注意：** 目标资产已存在时会先删除再重建；脚本不会在结束后自动删除测试资产，
确认后可手动删除 `/Game/Test/BP_Smoke`。
