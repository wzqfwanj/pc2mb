@echo off
rem ===========================================================================
rem  PC2MB Windows Agent launcher
rem
rem  Usage:
rem    run.bat
rem    run.bat --server wss://wkeeli.cn/pc2mb/ws/agent --token YOUR_TOKEN
rem    run.bat --quality 55 --max-width 1280 --log-level DEBUG
rem
rem  Config priority: command line > environment variable > agent\agent.env
rem    PC2MB_SERVER / PC2MB_TOKEN / PC2MB_MAX_WIDTH / PC2MB_QUALITY / PC2MB_ENV_FILE
rem ===========================================================================
setlocal
cd /d "%~dp0"

rem --- Python 3.12 (override with PC2MB_PYTHON if installed elsewhere) -------
if not defined PC2MB_PYTHON set "PC2MB_PYTHON=C:\Users\26876\AppData\Local\Programs\Python\Python312\python.exe"
set "PYTHON_EXE=%PC2MB_PYTHON%"
if not exist "%PYTHON_EXE%" set "PYTHON_EXE=python"

rem --- dependency check (installs mss + websockets on first run) -------------
"%PYTHON_EXE%" -c "import mss, websockets" >nul 2>&1
if errorlevel 1 (
    echo [pc2mb] installing dependencies: mss websockets
    "%PYTHON_EXE%" -m pip install -r "%~dp0requirements.txt"
    if errorlevel 1 (
        echo [pc2mb] pip install failed, retrying with Tsinghua mirror ...
        "%PYTHON_EXE%" -m pip install -r "%~dp0requirements.txt" -i https://pypi.tuna.tsinghua.edu.cn/simple
    )
)

rem --- start the agent (all arguments are passed through) --------------------
"%PYTHON_EXE%" "%~dp0agent.py" %*
set "RC=%ERRORLEVEL%"

if not "%RC%"=="0" (
    echo.
    echo [pc2mb] agent exited with code %RC%
    echo [pc2mb] hint: code 2 = config / token error, check PC2MB_TOKEN
    pause
)
exit /b %RC%
