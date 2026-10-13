"""
Self-test for winmouse: proves clicks are genuine OS-level input.

Strategy: open a real Win32 window we fully control, paint a known background,
click at a computed point, and verify that the window actually received a
trusted mouse message at the expected coordinates. This avoids depending on a
network or a specific browser being installed.

Two independent checks:
  A. Raw Win32 check  - a tkinter window records <Button-1> events and asserts
                        the event coordinates and event delivery.
  B. Pixel check      - screenshot a region before/after a click on a page
                        button and confirm the pixels changed.

Run:
    python selftest.py          # all local checks
    python selftest.py --browser "C:\\path\\to\\chrome.exe"
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
# The Win32 engine lives in ../python in the published repo layout.
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "python"))
import winmouse  # noqa: E402

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))


# ---------------------------------------------------------------- basic API


def test_basic_api() -> None:
    print("\n== basic API ==")
    vx, vy, vw, vh = winmouse.virtual_screen()
    check("virtual screen metrics sane", vw > 100 and vh > 100, f"{vw}x{vh} @ ({vx},{vy})")

    # Absolute-coordinate mapping must round-trip within a pixel.
    for x, y in [(0, 0), (vw // 2, vh // 2), (vw - 1, vh - 1)]:
        nx, ny = winmouse._to_absolute(x, y)
        check(f"abs-map ({x},{y}) in range", 0 <= nx <= 65535 and 0 <= ny <= 65535,
              f"-> ({nx},{ny})")

    start = winmouse.get_position()
    target = (vx + vw // 2, vy + vh // 2)

    # How close the cursor is expected to land. 2 px is the honest tolerance for
    # the normalized 0..65535 coordinate round-trip.
    def lands_ok(settle: float) -> tuple[bool, tuple[int, int]]:
        winmouse.move_to(target[0], target[1], duration=0.25)
        # SendInput is asynchronous: reading too early observes a mid-flight
        # position. The cursor can also be moved by whatever has focus, so we
        # retry before calling this a failure.
        time.sleep(settle)
        pos = winmouse.get_position()
        return (abs(pos[0] - target[0]) <= 2 and abs(pos[1] - target[1]) <= 2), pos

    ok, mid = lands_ok(0.45)
    lane = f"{start} -> {mid}"
    if not ok:
        # One retry: a foreground app can steal the cursor between our move and
        # the read. Retrying distinguishes that from a real mapping bug.
        ok2, mid2 = lands_ok(0.7)
        lane += f"  (retry: {mid2})"
        ok = ok2
    check("move_to lands on target", ok, lane)

    # Humanized path must produce >1 sample and stay bounded.
    path = winmouse._human_path((0, 0), (400, 300), 0.3)
    check("human path is curved/multi-step", len(path) > 10, f"{len(path)} samples")
    check("human path total sleep ~= duration",
          abs(sum(p[2] for p in path) - 0.3) < 0.05,
          f"{sum(p[2] for p in path):.3f}s")

    winmouse.move_to(*start, duration=0.2)


def _make_clickable(hwnd: int) -> bool:
    """
    Ensure ``hwnd`` is actually frontmost before we click it.

    Windows refuses SetForegroundWindow for background processes, so a test
    window can silently stay *behind* other windows; the click then lands on
    whatever is really on top and the test fails for the wrong reason. We force
    topmost, attach the input thread to bypass the foreground lock, and verify.
    """
    import ctypes
    u = ctypes.WinDLL("user32")
    k = ctypes.WinDLL("kernel32")

    u.SetWindowPos(ctypes.c_void_p(hwnd), ctypes.c_void_p(-1), 0, 0, 0, 0,
                   0x0001 | 0x0002 | 0x0040)  # NOSIZE|NOMOVE|SHOWWINDOW

    fg = u.GetForegroundWindow()
    target_thread = u.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), None)
    fg_thread = u.GetWindowThreadProcessId(ctypes.c_void_p(fg), None) if fg else 0
    cur = k.GetCurrentThreadId()

    attached = []
    try:
        for t in {fg_thread, target_thread}:
            if t and t != cur and u.AttachThreadInput(cur, t, True):
                attached.append(t)
        u.BringWindowToTop(ctypes.c_void_p(hwnd))
        u.SetForegroundWindow(ctypes.c_void_p(hwnd))
        u.SetFocus(ctypes.c_void_p(hwnd))
    finally:
        for t in attached:
            u.AttachThreadInput(cur, t, False)

    time.sleep(0.5)
    return u.GetForegroundWindow() == hwnd


def _window_at(x: int, y: int) -> int | None:
    """Root window currently under the given screen point."""
    import ctypes
    u = ctypes.WinDLL("user32")

    class POINT(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

    u.WindowFromPoint.restype = ctypes.c_void_p
    u.WindowFromPoint.argtypes = [POINT]
    u.GetAncestor.restype = ctypes.c_void_p
    h = u.WindowFromPoint(POINT(x, y))
    if not h:
        return None
    return u.GetAncestor(ctypes.c_void_p(h), 2)  # GA_ROOT


# ------------------------------------------------- A. real window click test


def test_window_click() -> None:
    print("\n== A. real Win32 window click ==")
    try:
        import tkinter as tk
    except Exception as exc:
        check("tkinter available", False, str(exc))
        return

    events: list[tuple[int, int]] = []
    ready = threading.Event()

    def run_ui() -> None:
        root = tk.Tk()
        root.title("mousectl-selftest")
        root.geometry("420x320+140+140")
        # Force above other windows so the click cannot be stolen by whatever
        # happens to be in front (Edge was covering it during a failed run).
        root.attributes("-topmost", True)
        canvas = tk.Canvas(root, width=420, height=320, bg="#204060",
                           highlightthickness=0)
        canvas.pack(fill="both", expand=True)
        canvas.create_rectangle(60, 60, 200, 140, fill="#c05050", tags="btn")
        canvas.create_text(130, 100, text="CLICK", fill="white", tags="btn")

        def on_click(ev):
            # ev.x / ev.y are window-relative, so converting to screen coords
            # lets us compare against what the automation layer intended.
            events.append((ev.x_root, ev.y_root))
            ready.set()

        canvas.bind("<Button-1>", on_click)
        root.after(15000, root.destroy)
        root.mainloop()

    t = threading.Thread(target=run_ui, daemon=True)
    t.start()
    time.sleep(1.8)

    hwnd = winmouse.find_window("mousectl-selftest")
    check("test window is enumerable", hwnd is not None, f"hwnd={hwnd}")
    if hwnd is None:
        return

    # Click the centre of the red button: window origin (140,140) + (130,100).
    target = (140 + 130, 140 + 100)
    _make_clickable(hwnd)

    top = _window_at(*target)
    check("target window is frontmost before clicking", top == hwnd,
          f"window-at-point={top} target={hwnd}")

    winmouse.click(target[0], target[1], duration=0.3,
                   failsafe=winmouse.Failsafe(enabled=False))
    got = ready.wait(timeout=3.0)

    check("click delivered a real WM_LBUTTONDOWN", got,
          f"received {len(events)} event(s)")
    if got:
        ex, ey = events[0]
        near = abs(ex - target[0]) <= 3 and abs(ey - target[1]) <= 3
        check("click landed at intended screen point", near,
              f"expected ~{target}, got ({ex},{ey})")

    # Double-click should deliver two events.
    _make_clickable(hwnd)
    events.clear()
    ready.clear()
    winmouse.click(target[0], target[1], clicks=2, duration=0.15,
                   failsafe=winmouse.Failsafe(enabled=False))
    time.sleep(0.8)
    check("double click delivered 2 events", len(events) >= 2, f"{len(events)} events")

    # Right-click must NOT fire the left-button handler.
    _make_clickable(hwnd)
    events.clear()
    ready.clear()
    winmouse.click(target[0], target[1], button="right", duration=0.15,
                   failsafe=winmouse.Failsafe(enabled=False))
    time.sleep(0.6)
    check("right click does not fire left handler", len(events) == 0,
          f"{len(events)} left events")


# ------------------------------------------------------ B. pixel-change test


def _grab(region):
    from PIL import ImageGrab
    return ImageGrab.grab(bbox=region)


def test_pixel_change() -> None:
    print("\n== B. pixel-change verification ==")
    try:
        from PIL import ImageGrab  # noqa: F401
    except Exception as exc:
        check("Pillow ImageGrab available", False, str(exc))
        return

    import tkinter as tk

    def run_ui() -> None:
        root = tk.Tk()
        root.title("mousectl-pixel")
        root.geometry("360x260+200+200")
        root.attributes("-topmost", True)
        cv = tk.Canvas(root, width=360, height=260, bg="#101010",
                       highlightthickness=0)
        cv.pack(fill="both", expand=True)
        rect = cv.create_rectangle(80, 80, 280, 180, fill="#101010", outline="")
        cv.tag_bind(rect, "<Button-1>",
                    lambda e: cv.itemconfig(rect, fill="#30d060"))
        root.after(15000, root.destroy)
        root.mainloop()

    threading.Thread(target=run_ui, daemon=True).start()
    time.sleep(1.8)

    hwnd = winmouse.find_window("mousectl-pixel")
    if hwnd:
        _make_clickable(hwnd)
        time.sleep(0.4)

    # Screen region covering the rectangle (window at +200+200 plus title bar).
    region = (200 + 90, 200 + 40 + 90, 200 + 270, 200 + 40 + 170)
    try:
        before = _grab(region)
    except Exception as exc:
        check("screen capture works", False, str(exc))
        return

    target = (200 + 180, 200 + 40 + 130)
    top = _window_at(*target)
    check("pixel-test window is frontmost", hwnd is not None and top == hwnd,
          f"window-at-point={top} target={hwnd}")

    winmouse.click(target[0], target[1], duration=0.25,
                   failsafe=winmouse.Failsafe(enabled=False))
    time.sleep(0.9)
    after = _grab(region)

    changed = before.tobytes() != after.tobytes()
    check("click changed on-screen pixels", changed)

    if changed:
        # Confirm it turned green-ish, proving the right widget was hit.
        px = after.convert("RGB").getpixel((after.width // 2, after.height // 2))
        greenish = px[1] > px[0] + 30 and px[1] > px[2] + 30
        check("clicked widget turned green", greenish, f"center pixel={px}")


# ------------------------------------------------------------ C. browser test


def test_browser(exe: str | None) -> None:
    print("\n== C. real browser click ==")
    if not exe:
        print("  [SKIP] no --browser path provided")
        return
    if not os.path.exists(exe):
        check("browser executable exists", False, exe)
        return

    page = os.path.join(tempfile.gettempdir(), "mousectl_page.html")
    with open(page, "w", encoding="utf-8") as fh:
        fh.write(
            "<!doctype html><meta charset=utf-8><title>mousectl-browser</title>"
            "<style>body{margin:0;font:16px sans-serif}"
            "#b{position:absolute;left:100px;top:200px;width:260px;height:90px;"
            "background:#c03030;color:#fff;border:0;font-size:22px}</style>"
            "<button id=b onclick=\"document.title='CLICKED';"
            "this.style.background='#20c060'\">HIT TARGET</button>"
            "<div id=trust style='position:absolute;left:100px;top:320px'>"
            "trusted=?</div>"
            "<script>document.getElementById('b').addEventListener('click',"
            "e=>{document.getElementById('trust').textContent="
            "'isTrusted='+e.isTrusted});</script>"
        )

    proc = subprocess.Popen([
        exe, "--new-window", "--window-size=900,700",
        "--window-position=50,50", f"file:///{page.replace(os.sep, '/')}",
    ])
    time.sleep(6)

    hwnd = winmouse.find_window("mousectl-browser")
    check("browser window found", hwnd is not None, f"hwnd={hwnd}")
    if hwnd is None:
        proc.terminate()
        return

    # Chromium window origin is the window top-left; page content starts below
    # the toolbar, so we click generously inside the red button's area and then
    # verify by pixel colour rather than trusting an exact offset.
    try:
        from PIL import ImageGrab
    except Exception:
        ImageGrab = None

    btn_screen = (50 + 100 + 130, 50 + 90 + 200 + 45)
    winmouse.click(btn_screen[0], btn_screen[1], duration=0.35,
                   failsafe=winmouse.Failsafe(enabled=False))
    time.sleep(1.2)

    if ImageGrab:
        shot = ImageGrab.grab(bbox=(btn_screen[0] - 60, btn_screen[1] - 30,
                                    btn_screen[0] + 60, btn_screen[1] + 30))
        px = shot.convert("RGB").getpixel((shot.width // 2, shot.height // 2))
        greenish = px[1] > px[0] + 30
        check("browser button reacted (green)", greenish, f"pixel={px}")
    proc.terminate()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--browser", help="path to chrome/msedge executable")
    args = ap.parse_args()

    print("mousectl selftest")
    test_basic_api()
    test_window_click()
    test_pixel_change()
    test_browser(args.browser)

    print(f"\n==== {len(PASS)} passed, {len(FAIL)} failed ====")
    if FAIL:
        for name in FAIL:
            print(f"  FAILED: {name}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
