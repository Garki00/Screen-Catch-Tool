@echo off
rem Screen Capture Tool - 打包脚本（P7）
rem 产物：dist\ScreenCatchTool\ScreenCatchTool.exe（onedir，双击即用）
setlocal
cd /d "%~dp0"

set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo 找不到虚拟环境 %PY%，先执行 setup.bat
    exit /b 1
)

echo [1/3] 清理旧的 build\ 与 dist\
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

echo [2/3] PyInstaller 打包（onedir，窗口模式）
"%PY%" -m PyInstaller --noconfirm --clean ScreenCatchTool.spec || exit /b 1

echo [3/3] 完成
if not exist "dist\ScreenCatchTool\ScreenCatchTool.exe" (
    echo 打包失败：没有找到 exe
    exit /b 1
)
for %%F in ("dist\ScreenCatchTool\ScreenCatchTool.exe") do echo   %%F  %%~zF 字节
echo.
echo 运行：dist\ScreenCatchTool\ScreenCatchTool.exe
echo 首次启动会在 exe 同级目录生成 config\ cache\ logs\
endlocal
