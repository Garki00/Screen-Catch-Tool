# Screen Capture Tool

[![Python Version](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Windows%2010%2B-lightgrey.svg)](https://www.microsoft.com/windows)

[English](README_en.md) | [中文](README_zh.md)

---

实时屏幕捕获工具：桌面端（PySide6 界面 + 系统托盘）按设定间隔截图，web 端（Flask + Socket.IO）实时推送、看历史、下载。

### 功能特性

- **实时屏幕捕获**：可配置间隔（2-10秒）捕获指定窗口或显示器
- **桌面界面**：基于 PySide6 的实时预览界面，支持系统托盘
- **Web 界面**：Flask + Socket.IO 驱动的 Web 仪表板，支持局域网内任意设备访问
- **自定义裁剪区域**：在 GUI 中定义捕获区域，应用于 Web 输出
- **历史记录管理**：自动缓存管理，可配置保留数量
- **独立可执行文件**：使用 PyInstaller 构建单文件夹分发版（约 193 MB）

### 系统要求

- **操作系统**：Windows 10 1903+ 或 Windows 11（需要 Windows Graphics Capture API）
- **Python 版本**：3.11+（已在 3.13.9 上测试）

### 快速开始

#### 1. 环境配置

```bat
setup.bat
```

或手动执行：

```bat
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

#### 2. 运行应用

```bat
.venv\Scripts\python.exe -m app
```

**命令行参数：**

```bat
-m app --autostart              # 启动后立即开始捕获
-m app --port 9200              # 自定义 web 端口
-m app --no-web                 # 仅桌面端，不启动 web 服务
-m app --selfcheck              # 打印版本信息并生成默认配置
-m app --capture --seconds 35 --interval 2
-m app --capture --target window:记事本
-m app --capture --target monitor:2
```

#### 3. 访问 Web 界面

在同一台机器的浏览器中打开 `http://127.0.0.1:9178`。

**局域网访问**（手机/平板）：
1. 编辑 `config/settings.json`：将 `server.host` 设为 `"0.0.0.0"`
2. 从其他设备访问：`http://<电脑局域网IP>:9178`
3. 如有提示，允许通过 Windows 防火墙

### 打包独立可执行文件

```bat
build.bat
```

输出：`dist\ScreenCatchTool\ScreenCatchTool.exe`（约 193 MB，无需安装 Python）

- **无控制台窗口**：所有日志输出到 `logs\screencatch.log`
- **便携式**：可将整个 `dist\ScreenCatchTool\` 目录复制到任意 Windows 机器
- **自包含**：首次运行时自动创建 `config\`、`cache\`、`logs\` 目录

### 项目结构

```
app/
  ├── __main__.py           # 'python -m app' 入口点
  ├── main.py               # 命令行入口和应用初始化
  ├── paths.py              # 路径解析（开发/打包后）
  ├── config.py             # 配置管理
  ├── core/                 # 捕获引擎和存储
  ├── ui/                   # PySide6 GUI 组件
  └── web/                  # Flask + Socket.IO 服务器
tests/                      # 测试套件
config/settings.json        # 运行时配置
ScreenCatchTool.spec        # PyInstaller 打包配方
```

### 测试

```bat
.venv\Scripts\python.exe tests\test_core.py       # 50 个测试
.venv\Scripts\python.exe tests\test_gui.py        # 67 个测试
.venv\Scripts\python.exe tests\test_web.py        # 72 个测试
.venv\Scripts\python.exe tests\test_tray.py       # 36 个测试
```

### 技术细节

- **捕获方式**：Windows Graphics Capture API（通过 `windows-capture` 库）
- **兜底方案**：PrintWindow/BitBlt 用于静止内容
- **图片格式**：JPEG
- **缓存策略**：最多 20 张截图 + 钉住的图片
- **线程模型**：捕获回调在独立线程运行；UI 更新通过 Qt 信号

### 详细文档

- [项目计划](docs/项目计划.md) - 功能需求详细说明
- [开发说明](README_CN.md) - 完整的中文开发文档

### 许可证

MIT License - 详见 [LICENSE](LICENSE) 文件

### 贡献

欢迎提交 Issue 和 Pull Request！提交前请确保代码通过所有测试。
