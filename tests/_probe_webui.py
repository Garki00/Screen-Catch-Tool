"""用本机已装的 Chrome（CDP，headless=new）真实渲染 web 界面并截图。

    .venv/Scripts/python.exe tests/_probe_webui.py [url] [出图目录]

只用于人工检视/排查（P5），不是自动验收脚本：它会打印关键 DOM 状态、
把桌面/平板/手机三种宽度的整页截图写到出图目录。
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import requests
import websocket  # websocket-client

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:9178"
OUT = Path(sys.argv[2] if len(sys.argv) > 2 else "cache/webui")
PORT = 9333

CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
]


class Cdp:
    def __init__(self, ws_url: str) -> None:
        self.ws = websocket.create_connection(ws_url, timeout=30, max_size=64 * 1024 * 1024)
        self.seq = 0

    def send(self, method: str, session: str | None = None, **params):
        self.seq += 1
        message = {"id": self.seq, "method": method, "params": params}
        if session:
            message["sessionId"] = session
        self.ws.send(json.dumps(message))
        while True:
            raw = json.loads(self.ws.recv())
            if raw.get("id") == self.seq:
                if "error" in raw:
                    raise RuntimeError(f"{method} -> {raw['error']}")
                return raw.get("result", {})

    def evaluate(self, expression: str, session: str):
        result = self.send("Runtime.evaluate", session,
                           expression=expression, returnByValue=True, awaitPromise=True)
        return result.get("result", {}).get("value")

    def screenshot(self, session: str, path: Path, full_page: bool = False) -> None:
        params = {"format": "png"}
        if full_page:
            metrics = self.send("Page.getLayoutMetrics", session)
            size = metrics.get("cssContentSize") or metrics.get("contentSize")
            params["captureBeyondViewport"] = True
            params["clip"] = {"x": 0, "y": 0, "width": size["width"],
                              "height": min(size["height"], 4000), "scale": 1}
        data = self.send("Page.captureScreenshot", session, **params)["data"]
        path.write_bytes(base64.b64decode(data))
        print(f"  截图：{path}  ({path.stat().st_size / 1024:.0f} KB)")


def main() -> int:
    chrome = next((p for p in CHROME_CANDIDATES if Path(p).is_file()), None)
    if chrome is None:
        print("没找到 Chrome/Edge")
        return 1
    OUT.mkdir(parents=True, exist_ok=True)
    profile = Path(tempfile.mkdtemp(prefix="sct-chrome-"))
    print(f"Chrome: {chrome}\n配置目录: {profile}")
    proc = subprocess.Popen([
        chrome, "--headless=new", f"--remote-debugging-port={PORT}",
        f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check",
        "--disable-gpu", "--hide-scrollbars", "--window-size=1440,960",
        "--remote-allow-origins=*", "about:blank",
    ])

    try:
        version = None
        for _ in range(60):
            try:
                version = requests.get(f"http://127.0.0.1:{PORT}/json/version", timeout=1).json()
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.4)
        if not version:
            print("Chrome 调试端口没起来")
            return 1
        print(f"浏览器：{version.get('Browser')}")

        cdp = Cdp(version["webSocketDebuggerUrl"])
        target = cdp.send("Target.createTarget", url="about:blank")["targetId"]
        session = cdp.send("Target.attachToTarget", targetId=target, flatten=True)["sessionId"]
        cdp.send("Page.enable", session)
        cdp.send("Runtime.enable", session)
        cdp.send("Emulation.setDeviceMetricsOverride", session, width=1440, height=960,
                 deviceScaleFactor=1, mobile=False)

        print(f"\n打开 {URL}")
        cdp.send("Page.navigate", session, url=URL)
        time.sleep(6)

        probe = """(() => {
          const live = document.getElementById('live');
          const strip = document.getElementById('strip');
          const thumbImgs = [...strip.querySelectorAll('img')];
          return {
            title: document.title,
            pills: {
              capture: document.getElementById('pillCapture').innerText,
              conn: document.getElementById('pillConn').innerText,
            },
            badge: document.getElementById('viewBadge').innerText,
            live: {
              hidden: live.hidden,
              src: live.getAttribute('src'),
              natural: live.naturalWidth + 'x' + live.naturalHeight,
              complete: live.complete,
            },
            placeholderHidden: document.getElementById('placeholder').hidden,
            thumbs: strip.children.length,
            thumbsLoaded: thumbImgs.filter(i => i.naturalWidth > 0).length,
            firstThumbTime: (strip.querySelector('.thumb__time') || {}).innerText,
            shotInfo: document.getElementById('shotInfo').innerText,
            foot: {
              target: document.getElementById('footTarget').innerText,
              interval: document.getElementById('footInterval').innerText,
              crop: document.getElementById('footCrop').innerText,
              cache: document.getElementById('footCache').innerText,
            },
            historyCount: document.getElementById('historyCount').innerText,
            transport: (window.__t || 'n/a'),
            layout: getComputedStyle(document.querySelector('.layout')).gridTemplateColumns,
            stripFlow: getComputedStyle(strip).gridAutoFlow,
            errors: window.__errors || [],
          };
        })()"""
        # 顺手记录页面侧的 JS 错误
        cdp.send("Runtime.evaluate", session, expression="""
          (() => { window.__errors = [];
                   window.addEventListener('error', e => window.__errors.push(String(e.message)));
                   const s = window.io && io('', {autoConnect:false});
                   return 'hook'; })()""")
        state = cdp.evaluate(probe, session)
        print("\n[桌面 1440x960]")
        for key, value in state.items():
            print(f"  {key}: {value}")
        cdp.screenshot(session, OUT / "desktop-full.png", full_page=True)
        cdp.screenshot(session, OUT / "desktop-viewport.png")

        print("\n[交互] 点历史缩略图 / 回到最新 / 钉住")
        clicked = cdp.evaluate("""(() => {
          const thumbs = document.querySelectorAll('#strip .thumb');
          if (thumbs.length < 2) return {error: '缩略图不足'};
          thumbs[1].click();
          return {count: thumbs.length, clickedIndex: 1,
                  clickedName: thumbs[1].getAttribute('title')};
        })()""", session)
        time.sleep(1.2)
        after = cdp.evaluate("""(() => ({
          badge: document.getElementById('viewBadge').innerText,
          src: document.getElementById('live').getAttribute('src'),
          info: document.getElementById('shotInfo').innerText,
          activeThumbs: document.querySelectorAll('.thumb--active').length,
          backVisible: !document.getElementById('btnBackLive').hidden,
          downloadLabel: document.getElementById('btnDownload').innerText,
          pinLabel: document.getElementById('btnPin').innerText,
        }))()""", session)
        print(f"  点缩略图：{clicked}")
        for key, value in after.items():
            print(f"    {key}: {value}")
        cdp.screenshot(session, OUT / "desktop-history.png")

        pinned = cdp.evaluate("""(() => {
          document.getElementById('btnPin').click();
          return 'clicked';
        })()""", session)
        time.sleep(1.5)
        pin_state = cdp.evaluate("""(() => ({
          pinLabel: document.getElementById('btnPin').innerText,
          pinDots: document.querySelectorAll('.thumb__pin').length,
        }))()""", session)
        print(f"  钉住：{pinned} -> {pin_state}")

        back = cdp.evaluate("document.getElementById('btnBackLive').click(); 'ok'", session)
        time.sleep(1.2)
        badge = cdp.evaluate("document.getElementById('viewBadge').innerText", session)
        print(f"  回到最新：{back} -> badge={badge}")

        # 下载按钮实际拿到的响应
        download = cdp.evaluate("""(async () => {
          const res = await fetch('/api/download/latest');
          const buf = await res.arrayBuffer();
          return {status: res.status, type: res.headers.get('content-type'),
                  disp: res.headers.get('content-disposition'), bytes: buf.byteLength};
        })()""", session)
        print(f"  下载接口：{download}")
        cdp.screenshot(session, OUT / "desktop-final.png")

        # ---------------- 钉住最新 + 缓存轮转 ----------------
        print("\n[钉住最新一张 + 缓存轮转]")
        cdp.evaluate("document.getElementById('btnBackLive').click(); 'ok'", session)
        time.sleep(0.6)
        cdp.evaluate("document.getElementById('btnPin').click(); 'ok'", session)
        time.sleep(1.5)
        pin = cdp.evaluate("""(() => ({
          label: document.getElementById('btnPin').innerText,
          dots: document.querySelectorAll('.thumb__pin').length,
          toast: document.getElementById('toast').innerText,
        }))()""", session)
        print(f"  钉住最新：{pin}")
        cdp.screenshot(session, OUT / "desktop-pinned.png")

        print("  等 13 秒观察缓存轮转（间隔 5 秒、缓存已满）…")
        time.sleep(13)
        rotation = cdp.evaluate("""(() => {
          const imgs = [...document.querySelectorAll('#strip img')];
          return {
            thumbs: document.querySelectorAll('#strip .thumb').length,
            broken: imgs.filter(i => i.complete && i.naturalWidth === 0).length,
            notLoaded: imgs.filter(i => !i.complete).length,
            historyCount: document.getElementById('historyCount').innerText,
            pinDots: document.querySelectorAll('.thumb__pin').length,
            liveNatural: document.getElementById('live').naturalWidth + 'x'
                       + document.getElementById('live').naturalHeight,
            liveComplete: document.getElementById('live').complete,
          };
        })()""", session)
        print(f"  轮转后：{rotation}")
        cdp.screenshot(session, OUT / "desktop-after-rotation.png")

        cdp.evaluate("document.getElementById('btnPin').click(); 'ok'", session)
        time.sleep(1.5)
        unpin = cdp.evaluate("""(() => ({
          label: document.getElementById('btnPin').innerText,
          dots: document.querySelectorAll('.thumb__pin').length,
          toast: document.getElementById('toast').innerText,
        }))()""", session)
        print(f"  取消钉住：{unpin}")

        # ---------------- 响应式 ----------------
        for label, width, height, dpr, mobile in (("平板", 820, 1180, 2, True),
                                                   ("手机", 390, 844, 3, True)):
            cdp.send("Emulation.setDeviceMetricsOverride", session, width=width, height=height,
                     deviceScaleFactor=dpr, mobile=mobile)
            time.sleep(1.5)
            layout = cdp.evaluate("""(() => ({
              cols: getComputedStyle(document.querySelector('.layout')).gridTemplateColumns,
              stripFlow: getComputedStyle(document.getElementById('strip')).gridAutoFlow,
              stripCols: getComputedStyle(document.getElementById('strip')).gridTemplateColumns,
              stripOverflowX: getComputedStyle(document.getElementById('strip')).overflowX,
              bodyScrollW: document.body.scrollWidth,
              winW: window.innerWidth,
              colsCount: getComputedStyle(document.querySelector('.strip')).gridTemplateColumns.split(' ').length,
            }))()""", session)
            print(f"\n[{label} {width}x{height} dpr{dpr}] {layout}")
            cdp.screenshot(session, OUT / f"{'tablet' if width > 500 else 'phone'}-full.png",
                           full_page=True)

        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except Exception:  # noqa: BLE001
            proc.kill()
        shutil.rmtree(profile, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
