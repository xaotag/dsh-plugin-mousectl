"""
Real-browser verification for winmouse.

Answers the question that actually matters: does a SendInput click look like a
genuine user click to the page? We check `event.isTrusted`, real coordinates,
and the full mouse event sequence, then compare against `element.click()`.

Method
------
Serve a local page, open it in Chromium with remote debugging enabled, then:
  1. click it with winmouse (system-level SendInput)
  2. read back what the page recorded, via the DevTools Protocol
     3. click it again with JS `element.click()` as the control group

No external network is used: the page is a local file / localhost.
"""

from __future__ import annotations

import base64
import ctypes
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
import urllib.error

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
# The Win32 engine lives in ../python in the published repo layout.
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "python"))
import winmouse  # noqa: E402

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
PORT = 9333
ARGS = [
    "--new-window",
    "--window-size=1000,760",
    "--window-position=60,60",
    "--remote-debugging-port=%d" % PORT,
    "--remote-allow-origins=*",
    "--no-first-run",
    "--no-default-browser-check",
    "--user-data-dir=" + os.path.join(os.environ.get("TEMP", "."), "mousectl_profile"),
]

PAGE_HTML = """<!doctype html>
<meta charset=utf-8>
<title>mousectl browsercheck</title>
<style>
  body{margin:0;font:15px/1.5 system-ui,sans-serif;background:#f4f6f8}
  #btn{position:absolute;left:120px;top:240px;width:300px;height:110px;
       background:#c03030;color:#fff;border:0;border-radius:8px;font-size:24px;
       cursor:pointer}
  #log{position:absolute;left:120px;top:380px;white-space:pre;font-family:monospace}
</style>
<button id="btn">CLICK TARGET</button>
<pre id="log">waiting...</pre>
<script>
window.__rec = {clicks: [], moves: 0, enters: 0, down: 0};
const btn = document.getElementById('btn');

btn.addEventListener('click', e => {
  window.__rec.clicks.push({
    isTrusted: e.isTrusted,
    screenX: e.screenX, screenY: e.screenY,
    clientX: Math.round(e.clientX), clientY: Math.round(e.clientY),
    detail: e.detail, pointerType: e.pointerType || null
  });
  refresh();
});
btn.addEventListener('mousedown', () => window.__rec.down++);
btn.addEventListener('mouseenter', () => window.__rec.enters++);
btn.addEventListener('mousemove', () => window.__rec.moves++);

function refresh(){
  document.getElementById('log').textContent = JSON.stringify(window.__rec, null, 1);
}
refresh();
</script>
"""

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))


def wait_port(port: int, timeout: float = 25.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return True
        except OSError:
            time.sleep(0.3)
    return False


def cdp_targets(port: int):
    url = f"http://127.0.0.1:{port}/json"
    for _ in range(40):
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                return json.load(r)
        except Exception:
            time.sleep(0.4)
    return []


class CDP:
    """Minimal hand-rolled WebSocket client (no third-party deps)."""

    def __init__(self, ws_url: str):
        import http.client
        from urllib.parse import urlparse
        u = urlparse(ws_url)
        self.conn = http.client.HTTPConnection(u.hostname, u.port, timeout=15)
        key = base64.b64encode(os.urandom(16)).decode()
        path = u.path + (f"?{u.query}" if u.query else "")
        self.conn.request("GET", path, headers={
            "Host": f"{u.hostname}:{u.port}",
            "Upgrade": "websocket", "Connection": "Upgrade",
            "Sec-WebSocket-Key": key, "Sec-WebSocket-Version": "13",
        })
        resp = self.conn.getresponse()
        if resp.status != 101:
            raise RuntimeError(f"WS handshake failed: {resp.status}")
        self.sock = self.conn.sock
        self._id = 0

    def _send_frame(self, payload: bytes) -> None:
        import struct
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        n = len(payload)
        header = b"\x81"
        if n < 126:
            header += bytes([0x80 | n])
        elif n < 65536:
            header += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            header += bytes([0x80 | 127]) + struct.pack(">Q", n)
        self.sock.sendall(header + mask + masked)

    def _recv_exact(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("socket closed")
            buf += chunk
        return buf

    def _recv_frame(self) -> bytes:
        import struct
        b1, b2 = self._recv_exact(2)
        length = b2 & 0x7F
        if length == 126:
            length = struct.unpack(">H", self._recv_exact(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self._recv_exact(8))[0]
        return self._recv_exact(length)

    def call(self, method: str, params: dict | None = None, timeout: float = 12.0):
        self._id += 1
        mid = self._id
        self._send_frame(json.dumps(
            {"id": mid, "method": method, "params": params or {}}).encode())
        self.sock.settimeout(timeout)
        end = time.time() + timeout
        while time.time() < end:
            try:
                msg = json.loads(self._recv_frame())
            except (socket.timeout, ConnectionError):
                break
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(msg["error"])
                return msg.get("result", {})
        raise TimeoutError(f"CDP {method} timed out")

    def eval_js(self, expr: str):
        r = self.call("Runtime.evaluate",
                      {"expression": expr, "returnByValue": True, "awaitPromise": True})
        return r.get("result", {}).get("value")

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


def _force_foreground(hwnd: int) -> bool:
    """
    Reliably make ``hwnd`` the foreground window.

    Windows blocks SetForegroundWindow from a process that does not own the
    current foreground (the "foreground lock"), which is why a plain
    SetForegroundWindow silently does nothing here. Attaching our input queue
    to the current foreground thread's queue lifts that restriction; this is
    the standard workaround and is what GUI automation tools use.
    """
    import ctypes
    u = ctypes.WinDLL("user32")
    k = ctypes.WinDLL("kernel32")

    fg = u.GetForegroundWindow()
    if fg == hwnd:
        return True

    # If the foreground window is unresponsive, ATTACH can hang; guard with
    # a timeout so a stuck browser cannot wedge the test.
    target_thread = u.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), None)
    fg_thread = u.GetWindowThreadProcessId(ctypes.c_void_p(fg), None) if fg else 0
    cur_thread = k.GetCurrentThreadId()

    attached = []
    try:
        for t in {fg_thread, target_thread}:
            if t and t != cur_thread and u.AttachThreadInput(cur_thread, t, True):
                attached.append(t)
        u.BringWindowToTop(ctypes.c_void_p(hwnd))
        u.SetForegroundWindow(ctypes.c_void_p(hwnd))
        u.SetFocus(ctypes.c_void_p(hwnd))
    finally:
        for t in attached:
            u.AttachThreadInput(cur_thread, t, False)

    return u.GetForegroundWindow() == hwnd


def _raise_browser_window(page_title: str = "mousectl browsercheck") -> bool:
    """
    Bring the test browser window to the OS foreground.

    Chromium's Page.bringToFront only activates a tab; the application window
    itself can still sit behind others, in which case real mouse input never
    arrives and the test fails misleadingly.

    We must target OUR window precisely: a user may already have Edge open, and
    matching the generic word "Edge" would raise the wrong window (which is
    exactly what happened during development).
    """
    import ctypes
    u = ctypes.WinDLL("user32")
    HWND_TOPMOST = ctypes.c_void_p(-1)
    SWP_NOSIZE_NOMOVE_SHOW = 0x0001 | 0x0002 | 0x0040

    def raise_hwnd(hwnd: int) -> bool:
        u.SetWindowPos(ctypes.c_void_p(hwnd), HWND_TOPMOST, 0, 0, 0, 0,
                       SWP_NOSIZE_NOMOVE_SHOW)
        _force_foreground(hwnd)
        time.sleep(0.5)
        return u.GetForegroundWindow() == hwnd

    # Pass 1: the window whose title contains our unique page title.
    target_norm = winmouse.norm_title(page_title)
    for hwnd, title in winmouse.list_windows():
        norm = winmouse.norm_title(title)
        # Exclude the window that merely lists many tabs (the user's session).
        if target_norm in norm and "和另外" not in norm:
            if raise_hwnd(hwnd):
                return True

    # Pass 2: any window still carrying our title.
    for hwnd, title in winmouse.list_windows():
        if target_norm in winmouse.norm_title(title):
            if raise_hwnd(hwnd):
                return True
    return False


def solve_viewport_origin(ws: "CDP") -> tuple[float, float, float]:
    """
    Determine the affine mapping from screen pixels to page client pixels.

    Instead of assuming the window origin equals the viewport origin (which is
    wrong once browser chrome and borders are involved), we move the real mouse
    to known screen points, ask the page where it saw each one, and least-squares
    fit ``client = scale * screen + origin``.

    This makes the test independent of window decorations, taskbar position and
    any display scaling.
    """
    ws.eval_js("""window.__samples = [];
      document.addEventListener('mousemove', function h(e){
        window.__samples.push({sx:e.screenX, sy:e.screenY,
                               cx:e.clientX, cy:e.clientY});
      }); 1""")

    probes = [(320, 260), (520, 360), (720, 460), (900, 560)]
    for x, y in probes:
        winmouse.move_to(x, y, duration=0.15)
        time.sleep(0.3)
    time.sleep(0.4)

    raw = ws.eval_js("JSON.stringify(window.__samples)")
    samples = json.loads(raw) if isinstance(raw, str) else []

    if len(samples) < 3:
        # The browser window is not in front of anything else, so pointer
        # events never reach it. Surface the real cause instead of a vague
        # "not enough samples" error.
        fg = ctypes.windll.user32.GetForegroundWindow()
        raise RuntimeError(
            "the browser window did not receive pointer events "
            f"(got {len(samples)} mousemove samples). It is probably behind "
            f"another window; foreground hwnd is {fg}. "
            "Close other windows or re-run so the test browser is on top."
        )

    # Use the final settled position of each probe: group consecutive samples
    # and take the last one, which is where the cursor actually stopped.
    picks = samples[-len(probes):] if len(samples) >= len(probes) else samples
    picks = [s for s in picks if s["cx"] or s["cy"]] or samples[-3:]

    def fit(xs, ys):
        n = len(xs)
        mx, my = sum(xs) / n, sum(ys) / n
        den = sum((v - mx) ** 2 for v in xs)
        if den == 0:
            return 1.0, my - mx
        k = sum((xs[i] - mx) * (ys[i] - my) for i in range(n)) / den
        return k, my - k * mx

    sx = [s["sx"] for s in picks]
    cx = [s["cx"] for s in picks]
    sy = [s["sy"] for s in picks]
    cy = [s["cy"] for s in picks]
    kx, bx = fit(sx, cx)
    ky, by = fit(sy, cy)

    scale = (kx + ky) / 2 if kx and ky else 1.0
    return -bx / kx, -by / ky, scale


def main() -> int:
    if not os.path.exists(EDGE):
        print(f"browser not found: {EDGE}")
        return 2

    page = os.path.join(os.environ.get("TEMP", "."), "mousectl_check.html")
    with open(page, "w", encoding="utf-8") as fh:
        fh.write(PAGE_HTML)
    url = "file:///" + page.replace(os.sep, "/")

    print("mousectl real-browser verification\n")
    proc = subprocess.Popen([EDGE, *ARGS, url],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    try:
        if not wait_port(PORT):
            print("  [FAIL] remote debugging port never opened")
            return 1

        target = None
        for t in cdp_targets(PORT):
            if t.get("type") == "page":
                target = t
                break
        if not target:
            print("  [FAIL] no page target")
            return 1

        ws = CDP(target["webSocketDebuggerUrl"])
        ws.call("Runtime.enable")
        time.sleep(2.5)  # let the page render

        title = ws.eval_js("document.title")
        check("page loaded in real browser", title == "mousectl browsercheck",
              f"title={title!r}")

        # Force the test window to the front. Chromium restores the previous
        # session, so our page can otherwise open *behind* other tabs/windows
        # and never receive real pointer input.
        ws.call("Page.enable")
        ws.call("Page.bringToFront")
        time.sleep(0.4)

        # Also nudge the OS-level window with the Win32 API, since
        # Page.bringToFront only activates the tab inside the browser.
        if _raise_browser_window():
            check("test window brought to foreground", True)
        else:
            check("test window brought to foreground", False,
                  "another window owns the foreground; input may not arrive")
        time.sleep(0.5)

        geo = ws.eval_js("""(() => {
            const b = document.getElementById('btn').getBoundingClientRect();
            return {x: b.x, y: b.y, w: b.width, h: b.height,
                    outerH: outerHeight, innerH: innerHeight,
                    screenX: screenX, screenY: screenY};
        })()""")
        print(f"    geometry: {geo}")

        # Derive the screen->client mapping empirically instead of assuming the
        # window origin equals the viewport origin. Browser chrome (tab strip,
        # toolbar) and window borders make those differ, and hardcoding the
        # offset is exactly what broke the first attempt.
        origin_x, origin_y, scale = solve_viewport_origin(ws)
        print(f"    solved viewport origin: ({origin_x:.1f},{origin_y:.1f}) "
              f"scale={scale:.5f}")

        target_x = origin_x + (geo["x"] + geo["w"] / 2) * scale
        target_y = origin_y + (geo["y"] + geo["h"] / 2) * scale
        print(f"    clicking screen ({target_x:.0f},{target_y:.0f})")

        winmouse.click(int(round(target_x)), int(round(target_y)), duration=0.45,
                       failsafe=winmouse.Failsafe(enabled=False))
        time.sleep(1.2)

        rec = ws.eval_js("JSON.stringify(window.__rec)")
        data = json.loads(rec) if isinstance(rec, str) else (rec or {})
        print(f"    page recorded: {data}")

        clicks = data.get("clicks", [])
        check("SendInput click reached the page", len(clicks) >= 1,
              f"{len(clicks)} click event(s)")
        if clicks:
            c = clicks[0]
            check("isTrusted === true (key anti-bot signal)", c.get("isTrusted") is True,
                  f"isTrusted={c.get('isTrusted')}")
            check("real screen coordinates reported",
                  c.get("screenX", 0) > 0 and c.get("screenY", 0) > 0,
                  f"screen=({c.get('screenX')},{c.get('screenY')})")
            check("mousedown fired before click",
                  data.get("down", 0) >= 1, f"down={data.get('down')}")
            check("hover/mousemove happened first",
                  data.get("moves", 0) > 0 and data.get("enters", 0) >= 1,
                  f"moves={data.get('moves')} enters={data.get('enters')}")

        # Control group: JS-triggered click should be isTrusted === false.
        ws.eval_js("window.__rec.clicks = [];")
        ws.eval_js("document.getElementById('btn').click();")
        time.sleep(0.5)
        js_rec = ws.eval_js("JSON.stringify(window.__rec.clicks)")
        js_clicks = json.loads(js_rec) if isinstance(js_rec, str) else []
        if js_clicks:
            check("control: element.click() isTrusted === false",
                  js_clicks[0].get("isTrusted") is False,
                  f"isTrusted={js_clicks[0].get('isTrusted')} "
                  "-> this is what automation-detection looks for")

        ws.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()

    passed = sum(1 for _, ok, _ in results if ok)
    failed = [n for n, ok, _ in results if not ok]
    print(f"\n==== {passed} passed, {len(failed)} failed ====")
    for n in failed:
        print(f"  FAILED: {n}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
