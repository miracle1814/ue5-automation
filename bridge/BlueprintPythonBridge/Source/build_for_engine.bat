@echo off
setlocal
chcp 65001 >nul
REM ================================================================
REM  build_for_engine.bat — 为指定 UE 引擎编译 BlueprintPythonBridge
REM ================================================================
REM  用法（在任意 cmd 窗口）：
REM      build_for_engine.bat "<UE引擎根目录>"
REM  例：
REM      build_for_engine.bat "C:\Program Files\Epic Games\UE_5.4"
REM      build_for_engine.bat "C:\Program Files\Epic Games\UE_5.4"
REM  或先设环境变量 UE5_ENGINE_PATH 后直接运行：build_for_engine.bat
REM
REM  编译成功后自动：
REM    ① 把新 DLL 复制到 ..\Binaries\Win64\（旧 DLL 备份为 .bak-<时间戳>）
REM    ② 打印产物路径
REM  前置条件：已装 Visual Studio 2022（含 C++ 桌面工作负载）
REM ================================================================

set "UE_ROOT=%~1"
if "%UE_ROOT%"=="" if defined UE5_ENGINE_PATH set "UE_ROOT=%UE5_ENGINE_PATH%"
if "%UE_ROOT%"=="" (
    echo [ERROR] 未指定引擎目录。
    echo         用法: build_for_engine.bat "C:\Program Files\Epic Games\UE_5.4"
    echo         或先设置环境变量 UE5_ENGINE_PATH
    exit /b 1
)
if not exist "%UE_ROOT%\Engine\Build\BatchFiles\RunUAT.bat" (
    echo [ERROR] 引擎目录无效（找不到 Engine\Build\BatchFiles\RunUAT.bat）: %UE_ROOT%
    exit /b 1
)

set "PLUGIN_ROOT=%~dp0.."
for %%I in ("%PLUGIN_ROOT%") do set "PLUGIN_ROOT=%%~fI"
set "UPLUGIN=%PLUGIN_ROOT%\BlueprintPythonBridge.uplugin"
if not exist "%UPLUGIN%" (
    echo [ERROR] 找不到 uplugin: %UPLUGIN%
    exit /b 1
)

echo ================================================================
echo  编译 BlueprintPythonBridge
echo  UE_ROOT : %UE_ROOT%
echo  Plugin  : %UPLUGIN%
echo ================================================================
echo.

call "%UE_ROOT%\Engine\Build\BatchFiles\RunUAT.bat" BuildPlugin ^
    -Plugin="%UPLUGIN%" ^
    -Package="%PLUGIN_ROOT%\Out" ^
    -TargetPlatforms=Win64

if errorlevel 1 (
    echo.
    echo [FAIL] 编译失败。常见处理：
    echo   1) 确认已装 VS2022 且含 "使用 C++ 的桌面开发" 工作负载；
    echo   2) 若报 Win32Exception 998 / 并行编译崩溃：
    echo      把 Source\BuildConfiguration.xml 复制到
    echo      %%APPDATA%%\Unreal Engine\UnrealBuildTool\ 后重试；
    echo   3) 高版本引擎 API 变动报错：见 Source\BUILD.md「版本适配」。
    exit /b 1
)

set "NEW_DLL=%PLUGIN_ROOT%\Out\BlueprintPythonBridge\Binaries\Win64\UnrealEditor-BlueprintPythonBridge.dll"
set "NEW_MODULES=%PLUGIN_ROOT%\Out\BlueprintPythonBridge\Binaries\Win64\UnrealEditor.modules"
if not exist "%NEW_DLL%" (
    echo [FAIL] 编译流程结束但未找到产物 DLL: %NEW_DLL%
    exit /b 1
)

set "DST_DIR=%PLUGIN_ROOT%\Binaries\Win64"
if not exist "%DST_DIR%" mkdir "%DST_DIR%"
if exist "%DST_DIR%\UnrealEditor-BlueprintPythonBridge.dll" (
    for /f "tokens=1-4 delims=/ " %%a in ("%date%") do set "TS=%%d%%b%%c"
    set "TS=%TS%_%time:~0,2%%time:~3,2%%time:~6,2%"
    set "TS=%TS: =0%"
    copy /y "%DST_DIR%\UnrealEditor-BlueprintPythonBridge.dll" "%DST_DIR%\UnrealEditor-BlueprintPythonBridge.dll.bak-%TS%" >nul
    echo [OK] 旧 DLL 已备份为 .bak-%TS%
)
copy /y "%NEW_DLL%" "%DST_DIR%\UnrealEditor-BlueprintPythonBridge.dll" >nul
REM v3.2: UnrealEditor.modules is UE's module manifest (contains BuildId); a missing one makes UE refuse the plugin with 'Incompatible or missing module' - copy it together with the DLL.
if exist "%NEW_MODULES%" (
    copy /y "%NEW_MODULES%" "%DST_DIR%\UnrealEditor.modules" >nul
    echo [OK] UnrealEditor.modules synced
) else (
    echo [WARN] UnrealEditor.modules not found next to the built DLL
)
if errorlevel 1 (
    echo [FAIL] 复制 DLL 失败（UE 编辑器可能开着锁定 DLL，请关闭后重试）
    exit /b 1
)

echo.
echo ================================================================
echo [OK] 编译并部署完成
echo   新 DLL : %DST_DIR%\UnrealEditor-BlueprintPythonBridge.dll
echo   下一步 : 重启 UE 编辑器加载新插件；用完整路径核对版本
echo ================================================================
endlocal
exit /b 0
