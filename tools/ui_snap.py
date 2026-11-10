"""用 DevTools 协议驱动真实（非虚拟时间）的无头 Edge：打开页面、等待、截图并读取页面里的调试信息。仅用于测试界面。

  .venv\Scripts\python tools/ui_snap.py "http://127.0.0.1:8765/?replay=RUN&t=62&debug=1" out.png --wait 8
"""
from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
import tempfile
import time

import httpx
import websocket

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("out")
    ap.add_argument("--wait", type=float, default=8)
    ap.add_argument("--js", action="append", default=[], help="截图前执行的 JS（可多次）")
    ap.add_argument("--after", type=float, default=0, help="执行 JS 后再等待的秒数")
    ap.add_argument("--size", default="1600,900")
    args = ap.parse_args()
    w, h = map(int, args.size.split(","))
    prof = tempfile.mkdtemp(prefix="edgecdp_")
    proc = subprocess.Popen([EDGE, "--headless=new", "--remote-debugging-port=9333", f"--user-data-dir={prof}",
                             "--autoplay-policy=no-user-gesture-required", "--mute-audio", "--no-first-run",
                             f"--window-size={w},{h}", "about:blank"])
    try:
        for _ in range(50):
            try:
                tabs = httpx.get("http://127.0.0.1:9333/json").json()
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.2)
        page = next(t for t in tabs if t.get("type") == "page")
        ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=30)
        n = [0]

        def cmd(method, **params):
            n[0] += 1
            ws.send(json.dumps({"id": n[0], "method": method, "params": params}))
            while True:
                m = json.loads(ws.recv())
                if m.get("id") == n[0]:
                    return m.get("result", {})

        cmd("Emulation.setDeviceMetricsOverride", width=w, height=h, deviceScaleFactor=1, mobile=False)
        cmd("Page.navigate", url=args.url)
        time.sleep(args.wait)
        for js in args.js:
            cmd("Runtime.evaluate", expression=js, awaitPromise=True)
        if args.after:
            time.sleep(args.after)
        dbg = cmd("Runtime.evaluate", expression="(document.getElementById('dbg')||{}).textContent||''")
        info = cmd("Runtime.evaluate", expression="document.getElementById('clock').textContent+' | '+document.getElementById('bVision').textContent+' | '+document.getElementById('stats').textContent")
        print("dbg:", dbg.get("result", {}).get("value"))
        print("ui :", info.get("result", {}).get("value"))
        shot = cmd("Page.captureScreenshot", format="png")
        open(args.out, "wb").write(base64.b64decode(shot["data"]))
        print("saved", args.out)
        ws.close()
    finally:
        proc.kill()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
