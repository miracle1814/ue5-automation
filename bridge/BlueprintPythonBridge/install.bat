@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
title BlueprintPythonBridge 一键安装
echo.
echo ==============================================
echo   BlueprintPythonBridge 一键部署
echo   随包 DLL 仅适用 UE 5.1.x（5.2~5.5 见 UE_VERSION_GUIDE.md）
echo ==============================================
echo.
echo 为避免 DLL 被锁定，请先关闭 UE 编辑器！
echo.

REM v2.20：py 启动器探测（与根 install_dependencies.bat 同口径），
REM   只有 py 没有 python 的机器旧版五个菜单全挂。
set "PY="
where py >nul 2>&1
if %errorlevel% equ 0 set "PY=py -3"
if defined PY goto PYOK
where python >nul 2>&1
if %errorlevel% equ 0 set "PY=python"
if defined PY goto PYOK
echo [ERROR] 未找到 Python，请先安装 Python 3.10+ 并加入 PATH。
pause
exit /b 1

:PYOK
:menu
echo ┌──────────────────────────────────────────────────┐
echo │  [1] 部署到项目 Plugins（自动探测 .uproject）    │
echo │  [2] 部署到引擎插件目录（所有项目可用）          │
echo │  [3] 打包为 ZIP（跨机部署）                      │
echo │  [4] 清理编译中间文件                            │
echo │  [Q] 退出                                        │
echo └──────────────────────────────────────────────────┘
echo.
set /p choice="请选择 [1-4/Q]: "

if "%choice%"=="1" goto deploy_project
if "%choice%"=="2" goto deploy_engine
if "%choice%"=="3" goto package_zip
if "%choice%"=="4" goto clean
if /i "%choice%"=="Q" goto end
echo 无效选项，请重试。
goto menu

:deploy_project
echo.
echo ── 部署到项目 Plugins ──
echo 自动从当前目录向上探测 .uproject；找不到时用:
echo   %PY% deploy.py --target "你的UE项目路径"
echo.
%PY% "%~dp0deploy.py"
if %errorlevel% neq 0 (
    echo [FAIL] 部署失败，请检查错误信息。
    pause
    goto menu
)
echo.
echo [OK] 编译产物已复制到项目 Plugins\BlueprintPythonBridge\
echo 请在 UE 编辑器中启用插件: Edit -^> Plugins -^> Blueprint Python Bridge
goto done

:deploy_engine
echo.
echo ── 部署到引擎插件目录 ──
echo.
echo [提示] 写引擎目录可能需要管理员权限；推荐优先用选项 1（项目级部署）。
echo 自动检测 UE 引擎路径，也可手动: %PY% deploy.py --engine "引擎根目录"
echo.
%PY% "%~dp0deploy.py" --auto-engine
if %errorlevel% neq 0 (
    echo [FAIL] 部署失败（可能需要管理员权限，或引擎版本非 5.1.x）。
    pause
    goto menu
)
echo.
echo [OK] 插件已安装到引擎目录，所有 UE 项目均可使用。
goto done

:package_zip
echo.
echo ── 打包 ZIP ──
%PY% "%~dp0deploy.py" --zip
if %errorlevel% neq 0 (
    echo [FAIL] 打包失败。
    pause
    goto menu
)
echo.
echo [OK] ZIP 包已生成在当前目录。
goto done

:clean
echo.
echo ── 清理编译中间文件 ──
%PY% "%~dp0deploy.py" --clean
goto done

:done
echo.
echo ─────────────────────────────────────────────────
echo 操作完成
echo ─────────────────────────────────────────────────
pause
goto menu

:end
echo 再见！
endlocal
exit /b 0
