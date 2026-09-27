# Screen Capture Tool

[![Python Version](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Windows%2010%2B-lightgrey.svg)](https://www.microsoft.com/windows)

[English](README.md) | [中文](README_zh.md)

---

A real-time screen capture tool for Windows with desktop GUI and web interface for remote viewing.

### Features

- **Real-time Screen Capture**: Capture specific windows or monitors at configurable intervals (2-10 seconds)
- **Desktop GUI**: PySide6-based interface with live preview and system tray support
- **Web Interface**: Flask + Socket.IO powered web dashboard accessible from any device on your network
- **Custom Crop Region**: Define capture regions in the GUI, applied to web output
- **History Management**: Automatic cache management with configurable retention
- **Standalone Executable**: Build single-folder distribution (~193 MB) with PyInstaller

### Requirements

- **OS**: Windows 10 1903+ or Windows 11 (Windows Graphics Capture API required)
- **Python**: 3.11+ (tested on 3.13.9)

### Quick Start

#### 1. Setup Environment

```bat
setup.bat
```

Or manually:

```bat
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

#### 2. Run Application

```bat
.venv\Scripts\python.exe -m app
```

**Command-line options:**

```bat
-m app --autostart              # Start capturing immediately
-m app --port 9200              # Use custom web port
-m app --no-web                 # Desktop GUI only
-m app --selfcheck              # Print version info and generate config
-m app --capture --seconds 35 --interval 2
-m app --capture --target window:Notepad
-m app --capture --target monitor:2
```

#### 3. Access Web Interface

Open `http://127.0.0.1:9178` in any browser on the same machine.

**For remote access** (mobile/tablet):
1. Edit `config/settings.json`: set `server.host` to `"0.0.0.0"`
2. Access from other devices: `http://<your-pc-ip>:9178`
3. Allow through Windows Firewall if prompted

### Build Standalone Executable

```bat
build.bat
```

Output: `dist\ScreenCatchTool\ScreenCatchTool.exe` (~193 MB, no Python required)

- **No console window**: All logs go to `logs\screencatch.log`
- **Portable**: Copy entire `dist\ScreenCatchTool\` folder to any Windows machine
- **Self-contained**: Creates `config\`, `cache\`, `logs\` on first run

### Project Structure

```
app/
  ├── __main__.py           # Entry point for 'python -m app'
  ├── main.py               # CLI entry & app initialization
  ├── paths.py              # Path resolution (dev/frozen)
  ├── config.py             # Config management
  ├── core/                 # Capture engine & storage
  ├── ui/                   # PySide6 GUI components
  └── web/                  # Flask + Socket.IO server
tests/                      # Test suites
config/settings.json        # Runtime configuration
ScreenCatchTool.spec        # PyInstaller build recipe
```

### Testing

```bat
.venv\Scripts\python.exe tests\test_core.py       # 50 tests
.venv\Scripts\python.exe tests\test_gui.py        # 67 tests
.venv\Scripts\python.exe tests\test_web.py        # 72 tests
.venv\Scripts\python.exe tests\test_tray.py       # 36 tests
```

### Technical Details

- **Capture**: Windows Graphics Capture API (via `windows-capture` library)
- **Fallback**: PrintWindow/BitBlt for static content
- **Image Format**: JPEG
- **Cache**: Maximum 20 screenshots + pinned images
- **Threading**: Capture callbacks run in separate threads; UI updates via Qt signals

### License

MIT License - See [LICENSE](LICENSE) file for details

### Contributing

Issues and pull requests are welcome! Please ensure code passes all tests before submitting.