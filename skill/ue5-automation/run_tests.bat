@echo off
REM run_tests.bat - ue5-automation pytest launcher
REM T-20260727-PYTEST step 2 / T-20260805-UE-FULL-CLOSURE P0-3 extended
REM T-20260806-UE5AUTOMATION-FIX-AND-VERIFY Fix 3+4:
REM   - --timeout 30 -> 90 (test_ue_send_cli_no_ue needs 30-60s)
REM   - ASCII-only (English comments): cmd parses GBK/UTF-8 Chinese comments
REM     incorrectly (hang / fragment), so this file must stay pure ASCII.
REM
REM Usage:
REM   run_tests.bat              - all offline tests
REM   run_tests.bat reader       - reader tests only
REM   run_tests.bat analyzer     - analyzer tests only
REM   run_tests.bat log_parser   - log_parser tests only
REM   run_tests.bat mermaid      - mermaid_render tests only
REM   run_tests.bat online       - UE online tests
REM   run_tests.bat blueprint_editor - blueprint_editor mock + integration tests
REM   run_tests.bat e2e          - UE online + blueprint_editor all
REM   run_tests.bat all          - all offline tests

setlocal
cd /d "%~dp0"

set "MODE=%~1"
if "%MODE%"=="" set "MODE=all"
set "EXTRA_ARGS=%2 %3 %4 %5 %6 %7 %8 %9"

set "BASE=-v --tb=short --color=yes --timeout=90"
set "OFFLINE=%BASE% --ignore=tests\test_analyzer_phase2.py"

echo.
echo ==== ue5-automation pytest suite ====
echo.

if /i "%MODE%"=="reader"          goto RUN_READER
if /i "%MODE%"=="analyzer"        goto RUN_ANALYZER
if /i "%MODE%"=="log_parser"      goto RUN_LOG_PARSER
if /i "%MODE%"=="mermaid"         goto RUN_MERMAID
if /i "%MODE%"=="online"          goto RUN_ONLINE
if /i "%MODE%"=="blueprint_editor" goto RUN_BP_EDITOR
if /i "%MODE%"=="e2e"             goto RUN_E2E
if /i "%MODE%"=="all"             goto RUN_ALL

echo [ERROR] Unknown mode: %MODE%
echo Valid modes: reader, analyzer, log_parser, mermaid, online, blueprint_editor, e2e, all
exit /b 1

:RUN_READER
echo [reader] blueprint_reader tests
python -m pytest %BASE% scripts\test_reader.py %EXTRA_ARGS%
goto END

:RUN_ANALYZER
echo [analyzer] project_analyzer tests
python -m pytest %OFFLINE% tests\test_analyzer.py tests\test_analyzer_audit.py tests\test_analyzer_phase3.py %EXTRA_ARGS%
goto END

:RUN_LOG_PARSER
echo [log_parser] tests
python -m pytest %BASE% tests\test_log_parser_health.py %EXTRA_ARGS%
goto END

:RUN_MERMAID
echo [mermaid] mermaid_render tests
python -m pytest %OFFLINE% tests\test_mermaid_render_audit.py %EXTRA_ARGS%
goto END

:RUN_ONLINE
echo [online] UE online tests (with --ue-online)
python -m pytest %BASE% tests\test_analyzer_phase2.py tests\test_blueprint_editor_e2e.py tests\test_cpp_apis_online.py tests\test_build_blueprint.py --ue-online %EXTRA_ARGS%
goto END

:RUN_BP_EDITOR
echo [blueprint_editor] blueprint_editor mock + integration tests (offline)
python -m pytest %OFFLINE% tests\test_blueprint_editor_mock.py tests\test_blueprint_editor_integration.py %EXTRA_ARGS%
goto END

:RUN_E2E
echo [e2e] UE online (phase2 + blueprint_editor e2e + cpp_apis_online) + blueprint_editor mock/integration all
python -m pytest %BASE% --ue-online tests\test_analyzer_phase2.py tests\test_blueprint_editor_e2e.py tests\test_cpp_apis_online.py tests\test_blueprint_editor_mock.py tests\test_blueprint_editor_integration.py tests\test_build_blueprint.py %EXTRA_ARGS%
goto END

:RUN_ALL
echo [all] All offline tests
python -m pytest %OFFLINE% tests\ scripts\test_reader.py %EXTRA_ARGS%
goto END

:END
set "EC=%ERRORLEVEL%"
echo.
if %EC% equ 0 echo ==== [PASS] All tests passed ====
if %EC% equ 1 echo ==== [FAIL] Some tests failed (see details above) ====
if %EC% equ 5 echo ==== [NO TESTS] No tests collected ====
if %EC% gtr 1 if %EC% neq 5 echo ==== [ERROR] Exit code: %EC% ====
echo.

endlocal & exit /b %EC%
