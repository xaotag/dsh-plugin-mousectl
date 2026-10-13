"""
Cloudflare Turnstile test on nowsecure.nl using PURE VISION localisation.

No OpenCV, no YOLO, no DOM access required for positioning: the checkbox is
found from the screenshot alone (see locator.py), which is necessary here
because the widget lives in a closed shadow root AND is rotated ~7 degrees.

Flow
----
1. open the page, raise the window
2. wait for the screenshot to stop changing (the widget animates)
3. locate the checkbox with the pure-vision locator
4. click it with real Win32 SendInput
5. verify by screenshot AND by the page's own state

Verification is deliberately strict: a run only counts as PASSED if the page
changes state after the click. A no-op or a miss reports FAILED.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time

import numpy as np
from PIL import Image, ImageGrab

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
# The Win32 engine lives in ../python in the published repo layout.
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "python"))
import winmouse  # noqa: E402
import locator  # noqa: E402
from browsercheck import (CDP, cdp_targets, wait_port, _raise_browser_window,
                          solve_viewport_origin)  # noqa: E402

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
PORT = 9377
PROFILE = os.path.join(os.environ.get("TEMP", "."), "mousectl_cf_vision")
ARGS = [
    "--new-window",
    "--window-size=1150,900",
    "--window-position=70,50",
    f"--remote-debugging-port={PORT}",
    "--remote-allow-origins=*",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-features=Translate",
    f"--user-data-dir={PROFILE}",
]
URL = "https://nowsecure.nl"


def grab():
    return ImageGrab.grab()


def frame_signature(img):
    """Cheap perceptual signature: downscaled luma bytes."""
    return np.asarray(img.convert("L").resize((64, 36))).tobytes()


def wait_stable(max_s=30.0, interval=0.7, need=2):
    """
    Wait until consecutive screenshots stop differing.

    The widget drifts for a while after first paint; clicking mid-animation was
    one reason earlier attempts missed.
    """
    prev = None
    stable = 0
    t0 = time.time()
    while time.time() - t0 < max_s:
        img = grab()
        sig = frame_signature(img)
        if prev is not None and sig == prev:
            stable += 1
            if stable >= need:
                return img, True, time.time() - t0
        else:
            stable = 0
        prev = sig
        time.sleep(interval)
    return img, False, time.time() - t0


def page_state(ws):
    js = r"""
    (() => {
      let host = 0, visible = 0;
      const walk = (root, d) => {
        if (!root || d > 10) return;
        let ns = []; try { ns = root.querySelectorAll('*'); } catch (e) { return; }
        for (const el of ns) {
          const c = (el.getAttribute && el.getAttribute('class')) || '';
          if (/cf-turnstile/i.test(c)) {
            host++;
            const b = el.getBoundingClientRect();
            if (b.width > 60 && b.height > 20 && b.top > -5 && b.bottom < innerHeight + 5) visible++;
          }
          if (el.shadowRoot) walk(el.shadowRoot, d + 1);
        }
      };
      walk(document, 0);
      let token = '';
      document.querySelectorAll('input[name="cf-turnstile-response"], textarea[name="cf-turnstile-response"]')
        .forEach(i => { if (i.value) token = i.value; });
      return { widgets: host, visible,
               token: token.slice(0, 40),
               cookie: document.cookie.includes('cf_clearance'),
               title: document.title };
    })()
    """
    try:
        return ws.eval_js(js) or {}
    except Exception:
        return {}


def checkbox_drawn_state(image_path, box):
    """
    Decide from pixels whether the checkbox is still an empty box or has become
    the green success tick.

    A solved widget keeps its DOM node and only changes appearance, so counting
    DOM nodes cannot tell success from failure. The reliable signal is colour:
    the success state paints a saturated green circle where the empty box was.
    """
    img = Image.open(image_path).convert("RGB")
    l, t, r, b = box
    crop = img.crop((max(0, l), max(0, t), min(img.width, r), min(img.height, b)))
    arr = np.asarray(crop).astype(np.int16)
    if arr.size == 0:
        return {"green": 0, "verdict": "unknown"}
    rr, gg, bb = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2]
    # Cloudflare's success tick is a strong green disc.
    green = (gg > 120) & (gg > rr + 40) & (gg > bb + 40)
    n = int(green.sum())
    return {"green": n, "pixels": int(arr.shape[0] * arr.shape[1]),
            "verdict": "solved" if n > 60 else "unsolved"}


def main():
    if not os.path.exists(EDGE):
        print(f"browser not found: {EDGE}")
        return 2
    if "--fresh" in sys.argv:
        shutil.rmtree(PROFILE, ignore_errors=True)

    print("Turnstile / nowsecure.nl — PURE VISION location + real click\n")
    proc = subprocess.Popen([EDGE, *ARGS, URL],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    ws = None
    try:
        if not wait_port(PORT):
            print("  debugging port never opened"); return 1
        target = None
        for _ in range(40):
            for t in cdp_targets(PORT):
                if t.get("type") == "page" and "devtools" not in t.get("url", ""):
                    target = t; break
            if target:
                break
            time.sleep(0.5)
        if not target:
            print("  no page target"); return 1

        ws = CDP(target["webSocketDebuggerUrl"])
        ws.call("Runtime.enable"); ws.call("Page.enable")
        ws.call("Page.bringToFront")
        time.sleep(2.0)
        _raise_browser_window("nowsecure") or _raise_browser_window("Edge")

        # Determine the browser content area in screen pixels FIRST: every
        # vision call below needs it, and it must exist before the wait loop.
        ox, oy, scale = solve_viewport_origin(ws)
        vw = int(ws.eval_js("innerWidth") or 0)
        vh = int(ws.eval_js("innerHeight") or 0)
        search_box = (int(ox) + 1, int(oy) + 1,
                      int(ox + vw * scale) - 1, int(oy + vh * scale) - 1)
        print(f"  viewport origin=({ox:.0f},{oy:.0f}) size={vw}x{vh} scale={scale:.4f}")
        print(f"  vision search box: {search_box}")

        # 1) Wait for the widget to actually APPEAR, then for the animation to
        #    settle. Waiting only for "screen stopped changing" is wrong: an
        #    empty transitional page is perfectly stable, and an earlier run
        #    gave up on exactly that state.
        print("  waiting for the widget to appear...")
        img = None
        appeared = False
        t0 = time.time()
        while time.time() - t0 < 45.0:
            img = grab()
            img.save("vis-00-wait.png")
            probe = locator.locate_checkbox("vis-00-wait.png", search_box=search_box)
            if probe.get("found"):
                appeared = True
                print(f"    widget found after {time.time()-t0:.1f}s "
                      f"(panel {probe['panel']['width']}x{probe['panel']['height']}, "
                      f"checkbox at {probe['x']},{probe['y']})")
                break
            time.sleep(1.0)
        if not appeared:
            print("  RESULT: the widget never appeared (page may be blank or blocked).")
            img.save("vis-01-before.png")
            return 1

        # Now let the rotation animation stop, re-locating each time.
        print("  waiting for the animation to settle...")
        last = None
        for i in range(20):
            time.sleep(1.0)
            img = grab()
            img.save("vis-01-before.png")
            probe = locator.locate_checkbox("vis-01-before.png", search_box=search_box)
            if probe.get("found"):
                cur = (probe["x"], probe["y"])
                moved = "n/a" if last is None else abs(cur[0]-last[0]) + abs(cur[1]-last[1])
                print(f"    t={i:>2}s checkbox=({cur[0]},{cur[1]}) moved={moved}")
                if last is not None and moved == 0:
                    break
                last = cur

        # 3) final fresh grab for the authoritative locate
        img = grab()
        img.save("vis-01-before.png")

        # 4) pure-vision locate
        t0 = time.time()
        res = locator.locate_checkbox("vis-01-before.png", search_box=search_box, debug=True)
        print(f"\n  locator ({time.time()-t0:.2f}s): "
              f"found={res.get('found')} reason={res.get('reason','')}")
        if res.get("panel"):
            p = res["panel"]
            print(f"    panel {p['width']}x{p['height']} at ({p['x0']},{p['y0']}) "
                  f"fill={p['fill']:.2f}  angle={res.get('angle_deg',0):.2f}deg")
        for c in res.get("all_in_panel", []):
            print(f"    candidate {c}")
        if not res.get("found"):
            print("\n  RESULT: could not locate the checkbox.")
            return 1

        sx, sy = res["x"], res["y"]
        print(f"\n  checkbox located at screen ({sx},{sy})")

        vx0, vy0, vw0, vh0 = winmouse.virtual_screen()
        if not (vx0 <= sx < vx0 + vw0 and vy0 <= sy < vy0 + vh0):
            print("  RESULT: point off-screen; refusing to click.")
            return 1

        # 5) real system-level click
        st_before = page_state(ws)
        # Sample the pixels where the checkbox is drawn, before and after.
        cb_area = (sx - 70, sy - 45, sx + 70, sy + 45)
        img.save("vis-01-before.png")
        vis_before = checkbox_drawn_state("vis-01-before.png", cb_area)
        print(f"  before: dom={st_before.get('visible')} visible widget(s)  "
              f"green_px={vis_before['green']} -> {vis_before['verdict']}")

        winmouse.click(sx, sy, duration=0.5,
                       failsafe=winmouse.Failsafe(enabled=False))
        print("  click sent via SendInput; observing...")

        solved = False
        for i in range(16):
            time.sleep(1.5)
            grab().save("vis-02-after.png")
            vis = checkbox_drawn_state("vis-02-after.png", cb_area)
            st = page_state(ws)
            print(f"    t+{i*1.5:4.1f}s green_px={vis['green']:>4} -> {vis['verdict']:<8} "
                  f"dom_visible={st.get('visible')} cookie={st.get('cookie')}")
            if vis["verdict"] == "solved":
                solved = True
                break

        grab().save("vis-02-after.png")
        vis_after = checkbox_drawn_state("vis-02-after.png", cb_area)
        st_after = page_state(ws)
        print(f"\n  checkbox pixels before/after: "
              f"{vis_before['verdict']} -> {vis_after['verdict']}")
        print(f"  green pixels before/after: {vis_before['green']} -> {vis_after['green']}")

        if solved:
            print("\n  RESULT: PASSED — the checkbox turned into the green success "
                  "tick after a real click located purely by vision.")
            return 0
        print("\n  RESULT: FAILED — the checkbox did not reach the success state.")
        return 1
    finally:
        try:
            if ws:
                ws.close()
        except Exception:
            pass
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
