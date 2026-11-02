# scripts — 安装后自检

这两个脚本只依赖 Python 标准库，不需要安装 `requests`。

## doctor.py — 体检

```bash
python scripts/doctor.py
python scripts/doctor.py --json
python scripts/doctor.py --port 8890 --token <token>
```

检查：token 文件、`/health`、`bridge_loaded`、bridge 版本、项目路径。
退出码 0 = 通过，1 = 有 FAIL。

## smoke.py — 5 分钟主路径冒烟

```bash
python scripts/smoke.py
python scripts/smoke.py --json
python scripts/smoke.py --name BP_Smoke --pkg /Game/Test/
```

会在项目里创建/替换 `/Game/Test/BP_Smoke`，跑：

```text
health → build_blueprint → search_nodes → apply_graph
      → compile → read_pin → save_asset
```

判定：`verified=true`、`compiled=true`、读回 `Hello from skill smoke`、`saved=true`。

**注意：** 目标资产已存在时会先删除再重建；脚本不会在结束后自动删除测试资产，
确认后可手动删除 `/Game/Test/BP_Smoke`。
