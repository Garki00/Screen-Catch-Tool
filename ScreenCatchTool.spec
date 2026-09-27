# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置（P7）。

    .venv\\Scripts\\python.exe -m PyInstaller --noconfirm ScreenCatchTool.spec

产物：``dist/ScreenCatchTool/ScreenCatchTool.exe``（onedir）。选 onedir 而不是 onefile：
启动快、排错容易，且我们的 cache/config/logs 本来就要落在 exe 旁边（``app/paths.py``
里 ``sys.frozen`` 时以 exe 所在目录为根），onefile 反而每次解包到临时目录。

要点：
- ``app/web/static`` 与 ``app/web/templates`` 必须进包（web 界面全靠它们，且
  socket.io.min.js 是本地化的，没有 CDN 兜底）；
- ``engineio.async_drivers.threading`` 必须显式 hiddenimport——Flask-SocketIO 在运行时
  按名字挑驱动，静态分析看不到，漏了会导致 web 服务起不来；
- ``windows-capture`` 的发行版元数据要一起拷，否则自检里的版本号显示「未知版本」；
- 未用到的 Qt 模块与 Pillow/pandas 之类一律排除，压体积。
"""

from pathlib import Path

from PyInstaller.utils.hooks import copy_metadata

ROOT = Path(SPECPATH).resolve()   # SPECPATH 由 PyInstaller 注入，就是本 spec 所在目录

datas = [
    (str(ROOT / "app" / "web" / "static"), "app/web/static"),
    (str(ROOT / "app" / "web" / "templates"), "app/web/templates"),
    (str(ROOT / "README.md"), "."),
]
datas += copy_metadata("windows-capture")

hiddenimports = [
    # Flask-SocketIO / engineio：运行时动态选驱动与 WebSocket 实现
    "engineio.async_drivers.threading",
    "engineio.async_drivers._websocket_wsgi",
    "simple_websocket",
    "flask_socketio",
    "socketio",
    "werkzeug.serving",
    # pywin32
    "win32timezone",
    "pythoncom",
    "pywintypes",
    # 这些是在函数里 import 的（为了启动速度），显式写上更稳
    "cv2",
    "numpy",
    "windows_capture",
]

excludes = [
    "tkinter",
    "PIL",          # 装了 Pillow 但代码里一处都没用（缩略图走 QImage 缩放）
    "test",
    "unittest",
    "pytest",
    "IPython",
    "pandas",
    "scipy",
    "matplotlib",
    "notebook",
    "setuptools._distutils",
    # Qt 里没用到的模块
    "PySide6.Qt3DAnimation", "PySide6.Qt3DCore", "PySide6.Qt3DExtras", "PySide6.Qt3DInput",
    "PySide6.Qt3DLogic", "PySide6.Qt3DRender",
    "PySide6.QtBluetooth", "PySide6.QtCharts", "PySide6.QtDataVisualization",
    "PySide6.QtDesigner", "PySide6.QtGraphs", "PySide6.QtHelp", "PySide6.QtHttpServer",
    "PySide6.QtLocation", "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets",
    "PySide6.QtNetworkAuth", "PySide6.QtNfc", "PySide6.QtOpcUa",
    "PySide6.QtPdf", "PySide6.QtPdfWidgets", "PySide6.QtPositioning",
    "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuick3D", "PySide6.QtQuickControls2",
    "PySide6.QtQuickTest", "PySide6.QtQuickWidgets",
    "PySide6.QtRemoteObjects", "PySide6.QtScxml", "PySide6.QtSensors",
    "PySide6.QtSerialBus", "PySide6.QtSerialPort", "PySide6.QtSpatialAudio",
    "PySide6.QtSql", "PySide6.QtStateMachine", "PySide6.QtTest", "PySide6.QtTextToSpeech",
    "PySide6.QtUiTools", "PySide6.QtWebChannel", "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineQuick", "PySide6.QtWebEngineWidgets", "PySide6.QtWebSockets",
]

a = Analysis(
    ["run.py"],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)


# ---------------------------------------------------------------------------
# 剪掉运行时用不到的二进制/资源。
# excludes 只管 Python 模块，PySide6 的 hook 仍会把没用的 Qt DLL 与翻译文件拷进来；
# cv2 的 ffmpeg 视频插件（我们只用 cv2.imwrite/imencode，不用 VideoCapture）也同理。
# 每一条都在下面「候选清单」里说明了为什么可以删——删错了的表现是启动报 ImportError
# 或缺 DLL，实测启动+web+捕获+托盘都会立刻暴露。
# ---------------------------------------------------------------------------
PRUNE = (
    # 视频/音频 I/O：完全没用
    "opencv_videoio_ffmpeg",
    # Qt 的软件 OpenGL 回退（20MB）：我们的控件走 raster，不需要 OpenGL
    "opengl32sw.dll",
    # Qt 自带翻译（7MB）：界面文案都是硬编码中文
    "PySide6/translations/",
    "PySide6/qml/",
    # 用不到的 Qt 插件目录（保留 platforms / styles / imageformats / iconengines）
    "plugins/sqldrivers/",
    "plugins/qmltooling/",
    "plugins/designer/",
    "plugins/multimedia/",
    "plugins/canbus/",
    "plugins/virtualkeyboard/",
    "plugins/scenegraph/",
    "plugins/generic/tuiotouch",
    # 用不到的 Qt 模块（DLL 与 Python 绑定文件）
    "Qt6Quick", "Qt6Qml", "Qt6Pdf", "Qt6Designer", "Qt6Sql", "Qt6Test",
    "Qt6Charts", "Qt6DataVisualization", "Qt6Bluetooth", "Qt6SerialPort",
    "Qt6Multimedia", "Qt6Sensors", "Qt6Nfc", "Qt6Positioning", "Qt6Location",
    "Qt6RemoteObjects", "Qt6Scxml", "Qt6TextToSpeech", "Qt6SpatialAudio",
    "Qt6WebEngine", "Qt6WebSockets", "Qt6HttpServer", "Qt6OpcUa", "Qt6Graphs",
    "PySide6/QtQuick", "PySide6/QtQml", "PySide6/QtPdf", "PySide6/QtDesigner",
    "PySide6/QtSql", "PySide6/QtTest", "PySide6/QtCharts", "PySide6/QtDataVisualization",
    "PySide6/QtBluetooth", "PySide6/QtSerialPort", "PySide6/QtSerialBus",
    "PySide6/QtMultimedia", "PySide6/QtSensors", "PySide6/QtNfc",
    "PySide6/QtPositioning", "PySide6/QtLocation", "PySide6/QtRemoteObjects",
    "PySide6/QtScxml", "PySide6/QtTextToSpeech", "PySide6/QtSpatialAudio",
    "PySide6/QtWebEngine", "PySide6/QtWebChannel", "PySide6/QtWebSockets",
    "PySide6/QtHttpServer", "PySide6/QtOpcUa", "PySide6/QtGraphs", "PySide6/QtHelp",
    "PySide6/QtUiTools", "PySide6/QtStateMachine", "PySide6/QtNetworkAuth",
    "PySide6/Qt3D",
)


def prune(toc, what: str):
    """按 PRUNE 里的子串过滤 TOC（PyInstaller 6.x 的条目是 (dest, src, kind)）。"""
    kept, dropped = [], []
    for entry in toc:
        dest = str(entry[0]).replace("\\", "/")
        (dropped if any(p in dest for p in PRUNE) else kept).append(entry)
    print(f"[spec] {what}: 保留 {len(kept)} 项，剪掉 {len(dropped)} 项")
    for dest in sorted(str(e[0]) for e in dropped)[:40]:
        print(f"[spec]   - {dest}")
    return kept


a.binaries = prune(a.binaries, "binaries")
a.datas = prune(a.datas, "datas")

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ScreenCatchTool",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # 窗口程序：没有黑框；日志一律写 logs/screencatch.log
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="ScreenCatchTool",
)
