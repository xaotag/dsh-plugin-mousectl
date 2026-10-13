"""
Win32 system-level mouse automation with anti-detection oriented humanization.

Design goals
------------
1. Real input, not synthetic DOM events.
   Clicks are injected at the OS level via SendInput, so the browser sees
   genuine trusted input (``event.isTrusted === true``), a real cursor
   position, real hover/focus transitions and real ENTER/LEAVE ordering.
   JavaScript ``element.click()`` produces ``isTrusted === false`` and is the
   single most common automation signal, so we never use it.

2. Human-like trajectories.
   The public click API accepts an ELEMENT CENTER, not an exact point, and
   applies an in-element offset plus a curved (cubic Bezier) path with eased
   timing. Repeated clicks never land on the identical pixel.

3. No dependencies.
   Pure standard library + ctypes. Requires no pip install.

Safety
------
- The physical cursor is moved by design (user-selected backend).
- ``Failsafe`` aborts automation when the cursor is yanked to a screen corner,
  mirroring pyautogui's convention.
- ``--dry-run`` prints the planned action without injecting input.

Usage
-----
    python winmouse.py click --element "搜索" 
    python winmouse.py click --x 500 --y 300
    python winmouse.py move --x 100 --y 200
    python winmouse.py drag --x1 100 --y1 100 --x2 400 --y2 300
    python winmouse.py scroll --amount -3
    python winmouse.py pos
    python winmouse.py screen

Element resolution strategy (in priority order)
----------------------------------------------
1. ``--x/--y``  : explicit absolute screen coordinates.
2. ``--point``  : explicit pixel within the element, e.g. ``--point 0.5,0.3``.
3. ``--element``: UI Automation element located by accessible name. We then
   query the element's bounding rectangle and click an offset point inside it.
   This is what makes clicks land "somewhere natural" in a control instead of
   always dead center, and is also how we support real browsers.

Note on window geometry
-----------------------
``--window`` restricts search to a top-level window (substring match on the
title) so ``--element`` does not resolve a control in some unrelated app.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import math
import random
import sys
import time
from ctypes import wintypes

# --------------------------------------------------------------------------
# Win32 bindings
# --------------------------------------------------------------------------

user32 = ctypes.WinDLL("user32", use_last_error=True)

ULONG_PTR = wintypes.WPARAM

INPUT_MOUSE = 0
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000

SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79

WHEEL_DELTA = 120


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
user32.SendInput.restype = wintypes.UINT
user32.GetSystemMetrics.argtypes = (ctypes.c_int,)
user32.GetSystemMetrics.restype = ctypes.c_int
user32.SetCursorPos.argtypes = (ctypes.c_int, ctypes.c_int)
user32.SetCursorPos.restype = wintypes.BOOL
user32.EnumWindows.argtypes = (
    ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM),
    wintypes.LPARAM,
)
user32.EnumWindows.restype = wintypes.BOOL
user32.IsWindowVisible.argtypes = (wintypes.HWND,)
user32.IsWindowVisible.restype = wintypes.BOOL
user32.GetWindowTextLengthW.argtypes = (wintypes.HWND,)
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
user32.GetWindowTextW.restype = ctypes.c_int


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]


user32.GetCursorPos.argtypes = (ctypes.POINTER(POINT),)
user32.GetCursorPos.restype = wintypes.BOOL


class MouseToolError(RuntimeError):
    """Raised when an action cannot be performed."""


# --------------------------------------------------------------------------
# Failsafe
# --------------------------------------------------------------------------


class Failsafe:
    """Abort automation if the user slams the cursor into a screen corner."""

    def __init__(self, enabled: bool = True, margin: int = 4):
        self.enabled = enabled
        self.margin = margin
        # Remember the corner sizes so the check can scale with the desktop.
        self._w = max(user32.GetSystemMetrics(SM_CXVIRTUALSCREEN), 1)
        self._h = max(user32.GetSystemMetrics(SM_CYVIRTUALSCREEN), 1)

    def check(self) -> None:
        if not self.enabled:
            return
        x, y = get_position()
        corners = (
            (self.margin, self.margin),
            (self._w - self.margin, self.margin),
            (self.margin, self._h - self.margin),
            (self._w - self.margin, self._h - self.margin),
        )
        for cx, cy in corners:
            if abs(x - cx) <= self.margin and abs(y - cy) <= self.margin:
                raise MouseToolError(
                    f"Failsafe triggered: cursor at corner ({x},{y}). "
                    "Automation aborted."
                )


# --------------------------------------------------------------------------
# Core primitives
# --------------------------------------------------------------------------


def get_position() -> tuple[int, int]:
    pt = POINT()
    if not user32.GetCursorPos(ctypes.byref(pt)):
        raise ctypes.WinError(ctypes.get_last_error())
    return pt.x, pt.y


def virtual_screen() -> tuple[int, int, int, int]:
    """Return (left, top, width, height) of the whole virtual desktop."""
    return (
        user32.GetSystemMetrics(SM_XVIRTUALSCREEN),
        user32.GetSystemMetrics(SM_YVIRTUALSCREEN),
        user32.GetSystemMetrics(SM_CXVIRTUALSCREEN),
        user32.GetSystemMetrics(SM_CYVIRTUALSCREEN),
    )


def _send(*inputs: INPUT) -> None:
    arr = (INPUT * len(inputs))(*inputs)
    sent = user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))
    if sent != len(inputs):
        raise ctypes.WinError(ctypes.get_last_error())


def _mouse_input(flags: int, dx: int = 0, dy: int = 0, data: int = 0) -> INPUT:
    mi = MOUSEINPUT(dx, dy, data, flags, 0, 0)
    return INPUT(type=INPUT_MOUSE, mi=mi)


def _to_absolute(x: int, y: int) -> tuple[int, int]:
    """
    Convert screen pixels to the 0..65535 normalized space that
    MOUSEEVENTF_ABSOLUTE expects.

    Important: for multi-monitor setups with negative coordinates we must use
    MOUSEEVENTF_VIRTUALDESK, otherwise the mapping only covers the primary
    monitor and clicks land on the wrong screen.
    """
    vx, vy, vw, vh = virtual_screen()
    if vw <= 1 or vh <= 1:
        raise MouseToolError("Invalid virtual screen metrics")
    nx = int(round((x - vx) * 65535.0 / (vw - 1)))
    ny = int(round((y - vy) * 65535.0 / (vh - 1)))
    return max(0, min(65535, nx)), max(0, min(65535, ny))


def move_to(x: int, y: int, duration: float = 0.0, failsafe: Failsafe | None = None) -> None:
    """
    Move the cursor to (x, y).

    ``duration > 0`` interpolates with a human-like curved path; otherwise the
    move is instantaneous.
    """
    if duration <= 0:
        # SetCursorPos respects the same coordinate space and is cheaper for
        # instantaneous moves, but SendInput keeps a single consistent path.
        nx, ny = _to_absolute(x, y)
        _send(
            _mouse_input(
                MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK, nx, ny
            )
        )
        return

    start = get_position()
    for px, py, dt in _human_path(start, (x, y), duration):
        if failsafe:
            failsafe.check()
        nx, ny = _to_absolute(int(round(px)), int(round(py)))
        _send(
            _mouse_input(
                MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK, nx, ny
            )
        )
        time.sleep(dt)


def _ease_in_out(t: float) -> float:
    """Smoothstep-style easing: slow start, fast middle, slow stop."""
    return t * t * (3.0 - 2.0 * t)


def _bezier(p0, p1, p2, p3, t: float) -> tuple[float, float]:
    mt = 1.0 - t
    a = mt * mt * mt
    b = 3 * mt * mt * t
    c = 3 * mt * t * t
    d = t * t * t
    x = a * p0[0] + b * p1[0] + c * p2[0] + d * p3[0]
    y = a * p0[1] + b * p1[1] + c * p2[1] + d * p3[1]
    return x, y


def _human_path(
    start: tuple[int, int], end: tuple[int, int], duration: float
) -> list[tuple[float, float, float]]:
    """
    Build a curved, non-uniformly timed path from start to end.

    Returns a list of (x, y, sleep_seconds).
    """
    sx, sy = start
    ex, ey = end
    dist = math.hypot(ex - sx, ey - sy)

    # Longer moves get more samples; short moves stay cheap.
    steps = max(12, min(80, int(dist / 8) + random.randint(8, 16)))
    if duration <= 0:
        duration = max(0.05, min(0.6, dist / 1400.0))

    # Control points perpendicular to the straight line create the arc.
    dx, dy = ex - sx, ey - sy
    norm = math.hypot(dx, dy) or 1.0
    px, py = -dy / norm, dx / norm
    bow = random.uniform(-0.12, 0.12) * dist
    c1 = (sx + dx * 0.33 + px * bow, sy + dy * 0.33 + py * bow)
    c2 = (sx + dx * 0.66 + px * bow, sy + dy * 0.66 + py * bow)

    path: list[tuple[float, float, float]] = []
    prev_t = 0.0
    for i in range(1, steps + 1):
        t = _ease_in_out(i / steps)
        x, y = _bezier((sx, sy), c1, c2, (ex, ey), t)
        # Overshoot jitter keeps the path from looking mathematically perfect.
        keep = 0.35 if i < steps else 0.0
        x += random.uniform(-1.0, 1.0) * keep
        y += random.uniform(-1.0, 1.0) * keep
        dt = max(0.0, (t - prev_t) * duration)
        prev_t = t
        path.append((x, y, dt))
    return path


def _press_release(button: str) -> None:
    down, up = {
        "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
        "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
        "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
    }[button]
    _send(_mouse_input(down))
    time.sleep(random.uniform(0.04, 0.11))
    _send(_mouse_input(up))


def click(
    x: int,
    y: int,
    button: str = "left",
    clicks: int = 1,
    interval: float = 0.0,
    duration: float = 0.25,
    move_first: bool = True,
    failsafe: Failsafe | None = None,
    dry_run: bool = False,
    quiet: bool = False,
) -> None:
    """
    Click at absolute screen coordinates.

    ``move_first`` performs a humanized approach move before pressing, which is
    what produces authentic hover/MOUSEOVER events on the target element.
    ``quiet`` suppresses the dry-run notice so a caller emitting JSON on stdout
    does not have to filter human text out of its own stream.
    """
    if dry_run:
        if not quiet:
            print(f"[dry-run] would click {button} x{clicks} at ({x},{y}) duration={duration}")
        return

    for i in range(clicks):
        if move_first:
            move_to(x, y, duration=duration, failsafe=failsafe)
        else:
            # Still ensure a real move so the browser tracks the pointer.
            nx, ny = _to_absolute(x, y)
            _send(
                _mouse_input(
                    MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK,
                    nx,
                    ny,
                )
            )
        if failsafe:
            failsafe.check()
        _press_release(button)
        if i < clicks - 1:
            gap = interval if interval > 0 else random.uniform(0.08, 0.22)
            time.sleep(gap)


def scroll(amount: int, x: int | None = None, y: int | None = None,
           failsafe: Failsafe | None = None, dry_run: bool = False) -> None:
    """Scroll the wheel. Positive = up, negative = down (Windows convention)."""
    if dry_run:
        print(f"[dry-run] would scroll {amount} notches at ({x},{y})")
        return
    if x is not None and y is not None:
        move_to(x, y, duration=0.12, failsafe=failsafe)
    delta = int(amount * WHEEL_DELTA)
    _send(_mouse_input(MOUSEEVENTF_WHEEL, data=ctypes.c_ulong(delta & 0xFFFFFFFF).value))


def drag(x1: int, y1: int, x2: int, y2: int, button: str = "left",
         duration: float = 0.5, failsafe: Failsafe | None = None,
         dry_run: bool = False) -> None:
    if dry_run:
        print(f"[dry-run] would drag {button} ({x1},{y1}) -> ({x2},{y2})")
        return
    down, up = {
        "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
        "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
        "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
    }[button]

    move_to(x1, y1, duration=0.2, failsafe=failsafe)
    _send(_mouse_input(down))
    time.sleep(random.uniform(0.05, 0.12))
    move_to(x2, y2, duration=duration, failsafe=failsafe)
    time.sleep(random.uniform(0.05, 0.12))
    _send(_mouse_input(up))


# --------------------------------------------------------------------------
# Window / element discovery via UI Automation
# --------------------------------------------------------------------------


def list_windows(visible_only: bool = True) -> list[tuple[int, str]]:
    """Enumerate top-level windows as (hwnd, title)."""
    out: list[tuple[int, str]] = []
    EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _cb(hwnd, _lparam):
        if visible_only and not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length > 0:
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            if buf.value.strip():
                out.append((hwnd, buf.value))
        return True

    user32.EnumWindows(EnumProc(_cb), 0)
    return out


def _normalize_title(text: str) -> str:
    """
    Strip invisible characters that break naive substring matching.

    Chromium-family browsers insert a zero-width space (U+200B) into titles
    such as "Microsoft\\u200b Edge", so matching the literal string
    "Microsoft Edge" fails even though the window is plainly there.
    """
    invisible = "\u200b\u200c\u200d\ufeff\u00ad\u2060"
    for ch in invisible:
        text = text.replace(ch, "")
    return text


def find_window(needle: str) -> int | None:
    """
    Case-insensitive substring match on visible window titles.

    Matching is done on a normalized title so invisible characters do not
    defeat it; whitespace is also collapsed so "Microsoft  Edge" matches
    "Microsoft Edge".
    """
    target = norm_title(needle)
    for hwnd, title in list_windows():
        if target in norm_title(title):
            return hwnd
    return None


def find_windows(needle: str) -> list[tuple[int, str]]:
    """All visible windows whose normalized title contains ``needle``."""
    target = norm_title(needle)
    return [(h, t) for h, t in list_windows() if target in norm_title(t)]


def norm_title(s: str) -> str:
    """Normalize a title for comparison: strip invisibles, collapse spaces."""
    return " ".join(_normalize_title(s).split()).lower()


user32.GetWindowRect.argtypes = (wintypes.HWND, ctypes.POINTER(RECT))
user32.GetWindowRect.restype = wintypes.BOOL


def get_window_rect(hwnd: int) -> tuple[int, int, int, int]:
    """Return (left, top, right, bottom) of a window in screen pixels."""
    r = RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(r)):
        raise ctypes.WinError(ctypes.get_last_error())
    return r.left, r.top, r.right, r.bottom


def _uia_element_rect(name: str, window_hwnd: int | None, timeout: float = 5.0):
    """
    Locate a UI Automation element by accessible name and return its screen rect.

    Only imports comtypes/uiautomation lazily so the rest of the tool keeps
    working without them.
    """
    try:
        import comtypes.client as cc
        from comtypes.gen import UIAutomationClient as UIA  # type: ignore
    except Exception:
        try:
            import comtypes.client as cc
            cc.GetModule("UIAutomationCore.dll")
            from comtypes.gen import UIAutomationClient as UIA  # type: ignore
        except Exception as exc:  # pragma: no cover
            raise MouseToolError(
                "Element lookup needs UI Automation. Install with:\n"
                "    pip install comtypes\n"
                f"(import failed: {exc})"
            ) from exc

    # CLSID_CUIAutomation; passing the interface makes comtypes cast the
    # returned object to IUIAutomation instead of a generic IDispatch.
    CLSID_CUIAutomation = "{ff48dba4-60ef-4201-aa87-54103eef594e}"
    uia = cc.CreateObject(CLSID_CUIAutomation, interface=UIA.IUIAutomation)

    deadline = time.time() + timeout
    last_exc: Exception | None = None
    while time.time() < deadline:
        try:
            root = uia.ElementFromHandle(window_hwnd) if window_hwnd else uia.GetRootElement()
            cond = uia.CreatePropertyCondition(UIA.UIA_NamePropertyId, name)
            el = root.FindFirst(UIA.TreeScope_Descendants, cond)
            if el:
                r = el.CurrentBoundingRectangle
                w = int(r.right - r.left)
                h = int(r.bottom - r.top)
                if w > 0 and h > 0:
                    return int(r.left), int(r.top), w, h
        except Exception as exc:
            last_exc = exc
        time.sleep(0.25)

    detail = f" (last error: {last_exc})" if last_exc else ""
    raise MouseToolError(f"Could not locate element named {name!r}{detail}")


def _point_in_rect(rect, point: str | None):
    """Pick a click point inside a rect; default is a jittered in-element spot."""
    left, top, w, h = rect
    if point:
        fx, fy = (float(v) for v in point.split(","))
    else:
        # Avoid dead center and the extreme edges: center-ish clicks look
        # scripted, corner clicks can miss padding or hit resize affordances.
        fx = random.uniform(0.30, 0.70)
        fy = random.uniform(0.30, 0.70)
    x = int(round(left + w * fx))
    y = int(round(top + h * fy))
    return x, y


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--dry-run", action="store_true", help="print the action only")
    p.add_argument("--no-failsafe", action="store_true", help="disable corner failsafe")
    p.add_argument("--json", action="store_true", help="emit one JSON object on stdout")


def _target_point(args) -> tuple[int, int]:
    if args.x is not None and args.y is not None:
        return args.x, args.y
    if args.element:
        hwnd = find_window(args.window) if args.window else None
        rect = _uia_element_rect(args.element, hwnd)
        return _point_in_rect(rect, args.point)
    raise MouseToolError("Provide either --x/--y or --element NAME")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="winmouse",
        description="Win32 system-level mouse automation (real input, humanized).",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("click", help="click at a point or on a named element")
    c.add_argument("--x", type=int)
    c.add_argument("--y", type=int)
    c.add_argument("--element", help="accessible name of the target control")
    c.add_argument("--window", help="restrict element search to this window title")
    c.add_argument("--point", help="offset inside element, e.g. 0.5,0.3")
    c.add_argument("--button", default="left", choices=["left", "right", "middle"])
    c.add_argument("--clicks", type=int, default=1)
    c.add_argument("--interval", type=float, default=0.0)
    c.add_argument("--duration", type=float, default=0.25)
    _add_common(c)

    m = sub.add_parser("move", help="move the cursor")
    m.add_argument("--x", type=int, required=True)
    m.add_argument("--y", type=int, required=True)
    m.add_argument("--duration", type=float, default=0.3)
    _add_common(m)

    s = sub.add_parser("scroll", help="scroll the wheel")
    s.add_argument("--amount", type=int, required=True, help="+up / -down notches")
    s.add_argument("--x", type=int)
    s.add_argument("--y", type=int)
    _add_common(s)

    d = sub.add_parser("drag", help="press, move, release")
    d.add_argument("--x1", type=int, required=True)
    d.add_argument("--y1", type=int, required=True)
    d.add_argument("--x2", type=int, required=True)
    d.add_argument("--y2", type=int, required=True)
    d.add_argument("--button", default="left", choices=["left", "right", "middle"])
    d.add_argument("--duration", type=float, default=0.5)
    _add_common(d)

    sub.add_parser("pos", help="print current cursor position").add_argument(
        "--json", action="store_true")
    sub.add_parser("screen", help="print virtual screen metrics").add_argument(
        "--json", action="store_true")
    w = sub.add_parser("windows", help="list visible top-level windows")
    w.add_argument("--filter", help="substring filter")
    w.add_argument("--json", action="store_true")
    r = sub.add_parser("wrect", help="print a window's screen rectangle")
    r.add_argument("--window", required=True, help="window title substring")
    r.add_argument("--json", action="store_true", help="emit JSON")
    return ap


def _safe_print(text: str) -> None:
    """
    Print text without dying on console encoding limits.

    On Chinese Windows the console code page is GBK, so a window title
    containing e.g. a zero-width space or an emoji raises UnicodeEncodeError
    and would abort the whole command. We degrade those characters instead.
    """
    try:
        print(text)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "utf-8"
        print(text.encode(enc, errors="replace").decode(enc, errors="replace"))


def _emit(payload: dict, as_json: bool) -> None:
    """
    Emit a machine-readable result when ``--json`` is set.

    The plugin consumes JSON only, because parsing human text is brittle and a
    GBK console would mangle non-ASCII window titles (a bug we actually hit).
    """
    if as_json:
        # ensure_ascii keeps the payload safe regardless of console encoding.
        _safe_print(json.dumps(payload, ensure_ascii=True))


def _fail(message: str, as_json: bool, code: int = 1) -> int:
    if as_json:
        _emit({"ok": False, "error": message}, True)
        return 0  # the plugin reads ok:false from JSON; 0 avoids noisy stderr
    print(f"error: {message}", file=sys.stderr)
    return code


def main(argv: list[str] | None = None) -> int:
    # Prefer UTF-8 output where the stream allows it; harmless if unsupported.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    args = build_parser().parse_args(argv)
    as_json = bool(getattr(args, "json", False))
    failsafe = Failsafe(enabled=not getattr(args, "no_failsafe", False))

    try:
        if args.cmd == "pos":
            x, y = get_position()
            _emit({"ok": True, "x": x, "y": y}, as_json)
            if not as_json:
                print(x, y)
        elif args.cmd == "screen":
            vx, vy, vw, vh = virtual_screen()
            _emit({"ok": True, "left": vx, "top": vy, "width": vw, "height": vh}, as_json)
            if not as_json:
                print(vx, vy, vw, vh)
        elif args.cmd == "windows":
            rows = []
            for hwnd, title in list_windows():
                if args.filter and norm_title(args.filter) not in norm_title(title):
                    continue
                rows.append({"hwnd": hwnd, "title": title})
                if not as_json:
                    _safe_print(f"{hwnd}\t{title}")
            _emit({"ok": True, "windows": rows}, as_json)
        elif args.cmd == "wrect":
            hwnd = find_window(args.window)
            if hwnd is None:
                return _fail(f"no visible window matching {args.window!r}", as_json, 4)
            left, top, right, bottom = get_window_rect(hwnd)
            payload = {
                "ok": True, "hwnd": hwnd, "left": left, "top": top,
                "right": right, "bottom": bottom,
                "width": right - left, "height": bottom - top,
            }
            _emit(payload, as_json)
            if not as_json:
                _safe_print(f"hwnd={hwnd} rect=({left},{top})-({right},{bottom}) "
                            f"size={right - left}x{bottom - top}")
        elif args.cmd == "move":
            before = get_position()
            if args.dry_run:
                _emit({"ok": True, "x": args.x, "y": args.y,
                       "previous_x": before[0], "previous_y": before[1]}, as_json)
                if not as_json:
                    print(f"[dry-run] would move to ({args.x},{args.y})")
            else:
                move_to(args.x, args.y, duration=args.duration, failsafe=failsafe)
                after = get_position()
                _emit({"ok": True, "x": after[0], "y": after[1],
                       "previous_x": before[0], "previous_y": before[1]}, as_json)
                if not as_json:
                    print(f"moved to ({after[0]},{after[1]})")
        elif args.cmd == "click":
            x, y = _target_point(args)
            click(x, y, button=args.button, clicks=args.clicks,
                  interval=args.interval, duration=args.duration,
                  failsafe=failsafe, dry_run=args.dry_run, quiet=as_json)
            _emit({"ok": True, "clicked": not args.dry_run, "x": x, "y": y,
                   "button": args.button, "clicks": args.clicks}, as_json)
            if not as_json and not args.dry_run:
                print(f"clicked {args.button} x{args.clicks} at ({x},{y})")
        elif args.cmd == "scroll":
            scroll(args.amount, args.x, args.y, failsafe=failsafe, dry_run=args.dry_run)
            _emit({"ok": True, "amount": args.amount, "x": args.x, "y": args.y}, as_json)
        elif args.cmd == "drag":
            drag(args.x1, args.y1, args.x2, args.y2, button=args.button,
                 duration=args.duration, failsafe=failsafe, dry_run=args.dry_run)
            _emit({"ok": True, "x1": args.x1, "y1": args.y1,
                   "x2": args.x2, "y2": args.y2}, as_json)
    except MouseToolError as exc:
        return _fail(str(exc), as_json, 2)
    except OSError as exc:
        # Includes ctypes.WinError results; note ctypes.WinError is a factory
        # function, not an exception class, so it must not be used in `except`.
        return _fail(f"win32 error: {exc}", as_json, 3)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
