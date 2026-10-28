# 环境记录（v6.5）

> 交叉验证时除了比文件哈希，也要比运行环境。本文件记录 v6.5 的验证环境。
> 不含用户名、token、工程私有路径。

## 机器 A（本次产物生成/验证）

| 项 | 值 |
|---|---|
| OS | Windows 10.0.26100（x64） |
| Shell | Git Bash / PowerShell |
| Python（离线测试） | 3.11.2 |
| fastapi | 0.141.1 |
| pydantic | 2.13.5 |
| uvicorn | 0.53.0 |
| pytest | 8.x（含 pytest-timeout） |
| UE 5.1 | 5.1.1-23901901+++UE5+Release-5.1 |
| UE 5.8 | 5.8.2-56702186+++UE5+Release-5.8 |
| 桥 DLL | 5.1 / 5.8 预编译双引擎（v6.5 未改 C++） |

## 本次执行的命令与结果

```text
python -m pytest -q skill/ue5-automation/tests
  → 820 passed, 95 skipped, 0 failed

python packaging/portable/verify_release.py
  → OK version / tool_profiles / security_invariants

python tools/check_tool_profile.py（移植包内）
  → gateway 12 / core 18 / full 34, commands 101

python tools/verify_release.py --run-tests   # 仅源码仓库
  → 额外跑 pytest；移植包自动跳过（不含 tests）
```

## 机器 B 需要填写的模板

| 项 | 值 |
|---|---|
| OS | |
| Python | |
| fastapi / pydantic / uvicorn | |
| UE 版本 | |
| `verify_release.py --json` 的 `ok` | |
| `RELEASE-MANIFEST.json` 对比 | added / missing / changed |

## 注意

- `verify_release.py` 的 `--compare` 比较的是**逐文件 SHA256**，
  不比较 zip 整包哈希（zip 受压缩率/时间戳影响）。
- 移植包不包含 `tests/`；离线全量测试需在源码仓库执行。
- 任何 token 文件都不应出现在 Manifest/包/日志中。
