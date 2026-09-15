@echo off
chcp 65001 >nul 2>&1
setlocal enabledelayedexpansion
title 语音交互助手 - Voice Assistant

rem ===========================================================================
rem  start.bat —— 双击即可启动语音交互网页应用
rem
rem  它会自动完成：
rem    1) 找到可用的 Python
rem    2) 首次运行时创建独立虚拟环境 .venv 并安装依赖（只做一次）
rem    3) 检查 DEEPSEEK_API_KEY 环境变量
rem    4) 启动 Flask 服务并自动打开浏览器
rem ===========================================================================

rem 切换到脚本所在目录，保证双击时工作目录正确
cd /d "%~dp0"

echo.
echo ==============================================================
echo   语音交互助手  Voice Assistant
echo ==============================================================
echo.

rem ---------------------------------------------------------------------------
rem 第 1 步：查找 Python
rem ---------------------------------------------------------------------------
set "PY_CMD="
where py >nul 2>&1 && set "PY_CMD=py -3"
if not defined PY_CMD (
    where python >nul 2>&1 && set "PY_CMD=python"
)
if not defined PY_CMD (
    echo [错误] 没有检测到 Python。
    echo        请先安装 Python 3.9 或更高版本： https://www.python.org/downloads/
    echo        安装时记得勾选 "Add Python to PATH"。
    echo.
    pause
    exit /b 1
)
echo [1/4] 已找到 Python：!PY_CMD!

rem ---------------------------------------------------------------------------
rem 第 2 步：准备虚拟环境（第一次运行会比较慢，需要联网下载 Flask）
rem ---------------------------------------------------------------------------
set "VENV_PY=%~dp0.venv\Scripts\python.exe"

if not exist "!VENV_PY!" (
    echo [2/4] 首次运行，正在创建虚拟环境 .venv ...
    %PY_CMD% -m venv ".venv"
    if errorlevel 1 (
        echo [错误] 创建虚拟环境失败。请确认 Python 安装完整（含 venv 模块）。
        echo.
        pause
        exit /b 1
    )
    echo       正在安装依赖（Flask + edge-tts），请稍候 ...
    "!VENV_PY!" -m pip install --upgrade pip -q
    "!VENV_PY!" -m pip install -r "requirements.txt" -q
    if errorlevel 1 (
        echo [错误] 依赖安装失败。可尝试手动执行：
        echo        .venv\Scripts\python.exe -m pip install -r requirements.txt
        echo.
        pause
        exit /b 1
    )
    echo       依赖安装完成。
) else (
    echo [2/4] 虚拟环境已存在，跳过安装。
)

rem 兜底：虚拟环境里缺依赖就补装一次。
rem 这里同时检查 flask 和 edge_tts —— 从旧版本升级上来的 .venv 里没有 edge_tts，
rem 漏检的话会静默退化到浏览器朗读，用户以为升级没生效。
"!VENV_PY!" -c "import flask, edge_tts" >nul 2>&1
if errorlevel 1 (
    echo       检测到依赖不完整，正在补装（Flask / edge-tts）...
    "!VENV_PY!" -m pip install -r "requirements.txt" -q
    "!VENV_PY!" -c "import flask, edge_tts" >nul 2>&1
    if errorlevel 1 (
        echo       [提示] edge-tts 安装失败，应用仍可启动，将使用浏览器原生朗读。
    )
)

rem ---------------------------------------------------------------------------
rem 第 3 步：检查 DEEPSEEK_API_KEY
rem ---------------------------------------------------------------------------
echo [3/4] 检查环境变量 DEEPSEEK_API_KEY ...

rem 先看当前进程环境变量
if defined DEEPSEEK_API_KEY (
    echo       已检测到密钥，可以正常对话。
    goto :run
)

rem 再看 Windows 用户级环境变量（可能刚设置、当前终端还没刷新）
for /f "usebackq delims=" %%i in (`powershell -NoProfile -Command "[Environment]::GetEnvironmentVariable('DEEPSEEK_API_KEY','User')" 2^>nul`) do set "USER_KEY=%%i"
if defined USER_KEY (
    set "DEEPSEEK_API_KEY=!USER_KEY!"
    echo       已从用户环境变量读取到密钥。
    goto :run
)

echo.
echo ==============================================================
echo   [提示] 未找到 DEEPSEEK_API_KEY，应用仍会启动，
echo          但发送消息时会提示密钥缺失。
echo ==============================================================
echo.
echo   设置方法（任选其一）：
echo.
echo   方法 A - 永久设置（推荐）：
echo     1. Win 键搜索「环境变量」，打开「编辑系统环境变量」
echo     2. 点击「环境变量」-^> 用户变量「新建」
echo     3. 变量名填 DEEPSEEK_API_KEY
echo        变量值填你的密钥（形如 sk-xxxxxxxx）
echo     4. 确定后关闭本窗口，重新双击 start.bat
echo.
echo   方法 B - 只对本次会话有效（在 PowerShell 里执行）：
echo     $env:DEEPSEEK_API_KEY="sk-你的密钥"
echo     .\.venv\Scripts\python.exe app.py
echo.
echo   API Key 申请地址：https://platform.deepseek.com/api_keys
echo.
echo ==============================================================
echo.
pause

:run
rem ---------------------------------------------------------------------------
rem 第 4 步：启动服务（app.py 会自动打开浏览器）
rem ---------------------------------------------------------------------------
echo [4/4] 正在启动服务 ...
echo.
"!VENV_PY!" app.py

echo.
echo 服务已退出。
pause
endlocal
