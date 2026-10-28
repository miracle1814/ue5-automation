# v6.5 最终交付物清单

> 日期：2026-10-28 · 版本：v6.5.4 / skill v1.14.0
> 来源合并：`已优化版本A`（UETool v6.2.1-dev）· 逐项合并，未整包覆盖。

## 1. 交付物

| # | 交付物 | 位置 | 说明 |
|---|---|---|---|
| 1 | 源码仓库 | 本目录 | 未提交/未发布；按用户授权决定 |
| 2 | 移植包 | `dist/ue5-automation-v6.5.4-portable.zip` | 1.22 MB，89 文件 |
| 3 | 发布清单 | 包内 `RELEASE-MANIFEST.json` | 逐文件 SHA256（88 文件） |
| 4 | 包内自检 | `tools/verify_release.py` | 无需 UE，可跨机复跑 |
| 5 | 工具面自检 | `tools/check_tool_profile.py` | gateway 12 / core 18 / full 34 |
| 6 | 合并报告 | `docs/MERGE-v6.5-已优化版本A.md` | 采纳/改造/拒绝矩阵 |
| 7 | 验证记录 | `docs/CROSS-VALIDATION-v6.5.md` | UE 5.1 + 5.8 实录 |
| 8 | 环境记录 | `docs/ENVIRONMENT-v6.5.md` | Python/依赖/引擎版本 |

移植包 SHA256：以交付时 `dist/` 下实际 zip 的哈希为准（每次重新打包都会变化；
可用 `certutil -hashfile <zip> SHA256` 复核）。

## 2. 包内容

```text
install.bat / README-新机测试.md / VERSION.txt / RELEASE-MANIFEST.json
plugin/BlueprintPythonBridge/   # uplugin + 5.1 DLL + Source
plugin/prebuilt-5.8/            # 5.8 DLL + modules
plugin/ue5-automation-extras/   # 5.8 纯 Python 引导插件
host/python/                    # 101 条命令的 bridge + runner + 工具脚本
host/ui/index.html              # 只读管理面板
tools/                          # deploy/check_env/verify_release/check_tool_profile
docs/                           # QUICKSTART/SKILL/合并/验证/安全/历史计划
```

不包含：`%APPDATA%/ue5_bridge/token`、任何日志、任何本机路径/工程名、
源码仓库 tests（移植包只带运行时代码）。

## 3. 验收命令（新机）

```bat
install.bat
python tools\check_env.py <项目>.uproject
python tools\verify_release.py
python tools\check_tool_profile.py
```

预期：

- `check_env.py` → FAIL 0；
- `verify_release.py` → version / tool_profiles / security_invariants 全 OK；
- `check_tool_profile.py` → 12/18/34，commands 101。

真机按 `README-新机测试.md` ①–⑦；v6.5 新能力重点见 ⑦（会话锁/幂等/场景/
AnimBP/413）。

## 4. 源码仓库验收

```cmd
pip install -r requirements-dev.txt
python -m pytest skill/ue5-automation/tests --timeout=90
python packaging/portable/verify_release.py --run-tests
```

当前结果：**820 passed / 95 skipped / 0 failed**；发布自检通过。

## 5. 已知边界

- AnimBP 只读：5.8 能枚举 15 张图；节点级 `Nodes` 未暴露 → 诚实降级；
  写入通道未开放。
- 会话锁是协作式；绕过 `execute_command` 的直连端点不经过锁层。
- 场景真机本轮只跑了 command/wait（未启 PIE）；PIE 属性/截图场景沿用
  v6.2–v6.4 记录。
- 无 TLS；跨机请自建隧道。
- 真机 5.1/5.8 均已验证；5.2–5.7 仍需自行重编译 bridge（既有边界）。
