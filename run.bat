@echo off
REM Personal Finance - one-command launcher for Windows.
REM
REM   run.bat            create the virtual environment if needed, then launch
REM   run.bat --test     run the test suite instead of launching
REM   run.bat --update   reinstall dependencies, then launch

setlocal
cd /d "%~dp0"

set VENV=.venv
set STAMP=%VENV%\.requirements-installed

if not exist "%VENV%" (
    where py >nul 2>nul
    if %ERRORLEVEL%==0 (
        echo Creating a virtual environment in %VENV% ...
        py -3 -m venv "%VENV%"
    ) else (
        where python >nul 2>nul
        if %ERRORLEVEL%==0 (
            echo Creating a virtual environment in %VENV% ...
            python -m venv "%VENV%"
        ) else (
            echo Python 3.10 or newer is required but was not found.
            echo Install it from https://www.python.org/downloads/ and run this again.
            exit /b 1
        )
    )
)

call "%VENV%\Scripts\activate.bat"

if "%1"=="--update" goto install
if not exist "%STAMP%" goto install
goto run

:install
echo Installing dependencies ...
python -m pip install --upgrade pip >nul
python -m pip install -r requirements.txt
if errorlevel 1 exit /b 1
echo installed > "%STAMP%"

:run
if "%1"=="--test" (
    python -m pytest
    exit /b %ERRORLEVEL%
)

echo.
echo Starting Personal Finance. It will open in your browser at http://localhost:8501
echo Your data stays in the data\ folder next to this script. Press Ctrl+C to stop.
echo.
python -m streamlit run app.py
