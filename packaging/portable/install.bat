@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"
title ue5-automation v4.0 一键部署

echo ============================================================
echo   ue5-automation v4.0（定稿版）— 新机一键部署
echo ============================================================
echo.

REM ── 找一个可用的 Python（py 启动器优先，其次 python）──
set "PY="
where py >nul 2>&1 && set "PY=py -3"
if not defined PY (
    where python >nul 2>&1 && set "PY=python"
)
if not defined PY (
    echo [ERROR] 本机没有 Python（py / python 都找不到）。
    echo         请先安装 Python 3.8+ 并勾选 "Add to PATH"：
    echo         https://www.python.org/downloads/windows/
    echo.
    echo         注意：这里需要的是 **Windows 上的 Python**（用来跑部署脚本），
    echo         不是 UE 编辑器内自带的那个。
    pause
    exit /b 1
)
echo [OK] Python: 
%PY% --version
echo.

echo ── 第 1 步：部署到你的 UE 项目 ──
echo   把项目的 .uproject 文件直接拖进这个窗口，然后回车。
echo   例：D:\MyGame\MyGame.uproject
echo.
set "PROJ="
set /p PROJ=项目路径: 
if "!PROJ!"=="" (
    echo [ERROR] 未输入项目路径。
    pause
    exit /b 1
)
echo.
%PY% "tools\deploy_portable.py" --project "!PROJ!"
if errorlevel 1 (
    echo.
    echo [FAIL] 部署失败，请把上面的报错发给维护者。
    pause
    exit /b 1
)

echo.
echo ── 第 2 步：环境自检（可选但强烈建议） ──
%PY% "tools\check_env.py" --project "!PROJ!"
echo.
echo ============================================================
echo  完成。接下来：
echo   1) 启动 UE 编辑器，看 Output Log 是否出现：
echo        [ue5_bridge] [OK] 服务已在后台启动 → http://127.0.0.1:8889/docs
echo   2) 浏览器打开管理端看板（token 在 %%APPDATA%%\ue5_bridge\token）：
echo        http://127.0.0.1:8889/ui?token=^<粘贴 token^>
echo   3) 详细验收清单见 README-新机测试.md
echo ============================================================
pause
