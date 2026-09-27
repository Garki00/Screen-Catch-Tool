@echo off
REM Screen Capture Tool - 一键环境安装（Windows）
REM 用法：双击本文件，或在 cmd 里执行 setup.bat
setlocal
cd /d "%~dp0"

set PY=
where py >nul 2>nul && set PY=py -3
if not defined PY (
  where python >nul 2>nul && set PY=python
)
if not defined PY (
  echo [错误] 未找到 Python，请先安装 Python 3.11+ 并加入 PATH。
  exit /b 1
)

echo [1/2] 创建虚拟环境 .venv
if not exist ".venv\Scripts\python.exe" (
  %PY% -m venv .venv || (echo [错误] 创建虚拟环境失败 & exit /b 1)
) else (
  echo      已存在，跳过
)

echo [2/3] 安装运行依赖 requirements.txt
.venv\Scripts\python.exe -m pip install --upgrade pip || exit /b 1
.venv\Scripts\python.exe -m pip install -r requirements.txt || exit /b 1

echo [3/3] 安装测试依赖 requirements-dev.txt（可跳过，只影响跑测试）
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt || echo     测试依赖安装失败，运行程序不受影响

echo.
echo 完成。自检：.venv\Scripts\python.exe -m app --selfcheck
echo 启动界面：.venv\Scripts\python.exe -m app
endlocal
