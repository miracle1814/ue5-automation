@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo   UE5 自动化 Skill · 依赖安装
echo ============================================
echo.

REM ── [0/4] 定位 Python 解释器 ──────────────────────────────
REM T-20260915-UE5PKG-STANDALONE：独立部署机器 PATH 里可能只有 py 启动器，
REM 不再假定 pip 一定可用；找到哪个用哪个，全找不到给出可读指引。
REM v2.20：cd /d %%~dp0 保证以管理员运行（CWD=System32）时相对路径仍有效；
REM         并增加 Python >=3.10 版本闸。
set "PY="
where py >nul 2>&1
if %errorlevel% equ 0 set "PY=py -3"
if defined PY goto PYOK
where python >nul 2>&1
if %errorlevel% equ 0 set "PY=python"
if defined PY goto PYOK
where python3 >nul 2>&1
if %errorlevel% equ 0 set "PY=python3"
if defined PY goto PYOK

echo [ERROR] 未找到 Python（本 skill 需要 Python 3.10+）。
echo         请安装: https://www.python.org/downloads/
echo         安装时务必勾选 "Add python.exe to PATH"，装完重开本窗口再试。
pause
exit /b 1

:PYOK
echo [0/4] Python 解释器: %PY%
%PY% --version
if %errorlevel% neq 0 (
    echo [ERROR] %PY% 无法执行，请检查 Python 安装。
    pause
    exit /b 1
)
%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python 版本过低（需 3.10+）：
    %PY% --version
    echo         请升级 Python 后重试。
    pause
    exit /b 1
)
echo    后续运行 skill 脚本请使用同一解释器。
echo.

echo [1/4] 安装 Python 依赖...
%PY% -m pip install -r "%~dp0requirements.txt"
if %errorlevel% neq 0 (
    echo [WARN] pip 安装失败，请检查网络或 Python 环境
    echo        （仅 mermaid / init 联网功能受影响，核心链路仍可先继续）
    goto BRIDGE_DEPLOY
)
echo Done. Python 依赖安装完成
echo.

:BRIDGE_DEPLOY
echo [2/4] 检查 Node.js (mermaid 渲染需要)...
where node >nul 2>&1
if %errorlevel% neq 0 (
    echo [WARN] 未检测到 Node.js — mermaid_render.py 将使用在线 API 降级
    echo    如需离线渲染，请安装: https://nodejs.org/
) else (
    echo Done. Node.js 已就绪
)
echo.

echo [3/4] 部署 Bridge DLL...
echo.
echo 接下来会启动部署脚本。请确保 UE5 编辑器已关闭。
echo 部署前脚本会校验目标引擎版本（随包 DLL 仅适用 UE 5.1.x）。
echo.
pause
cd /d "%~dp0bridge\BlueprintPythonBridge"
%PY% deploy.py --auto-engine
if %errorlevel% neq 0 (
    echo.
    echo [WARN] Bridge 自动部署未成功 —— 不影响其余功能，但 L2 节点级操作需要它。
    echo   手动部署二选一：
    echo     a^) 把 bridge\BlueprintPythonBridge\ 整个文件夹复制到你项目的 Plugins\ 下
    echo     b^) 在本目录执行: %PY% deploy.py --target "你的UE项目路径"
    echo   版本不适配（5.2~5.5）的说明见 skill\ue5-automation\UE_VERSION_GUIDE.md
) else (
    echo Done. Bridge DLL 部署完成
)
echo.
echo ============================================
echo   [4/4] UE 侧 Python 依赖（必须手动装一次）
echo ============================================
echo   ue5_bridge.py 跑在 **UE 内置 Python** 里，fastapi / uvicorn 必须装到
echo   那个解释器（本机 pip 装了也没用）。缺了它 Bridge 起不来。
echo.
echo   做法：UE 编辑器 -^> Python 控制台，执行下面两行：
echo.
echo     import subprocess, sys
echo     subprocess.call([sys.executable, '-m', 'pip', 'install', 'fastapi', 'uvicorn', 'pydantic'])
echo.
echo   验证：UE Python 控制台里 `import fastapi, uvicorn` 不报错即可。
echo   详见 skill\ue5-automation\DEPLOY.md 第 8 步。
echo.
echo ============================================
echo   完成！
echo   下一步：查看 README.md 了解如何使用
echo ============================================
pause
endlocal
exit /b 0
