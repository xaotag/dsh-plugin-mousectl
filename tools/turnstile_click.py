"""
Cloudflare Turnstile "click me" test.

The widget is not a plain top-level <iframe>: Cloudflare injects it into a
shadow root (and it can live in a nested frame), so
`document.querySelectorAll('iframe')` from the top document does NOT see it.
That is exactly why an earlier probe reported "no widget" while the checkbox
was plainly visible on screen.

Strategy
--------
1. Search through open shadow roots recursively for the Turnstile container.
2. If the DOM search finds nothing, fall back to *visual* location: the widget
   is a light rounded box inside the page, and we can find its checkbox by
   scanning the screenshot for the widget's frame.
3. Click the located point with real system-level SendInput.
4. Report what the page actually says afterwards.
"""

from __future__ import annotations

import ctypes
import json
import os
import shutil
import subprocess
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
# The Win32 engine lives in ../python in the published repo layout.
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "python"))
import winmouse  # noqa: E402
from browsercheck import (CDP, cdp_targets, wait_port, _force_foreground,
                          _raise_browser_window, solve_viewport_origin)  # noqa: E402

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
PORT = 9355
PROFILE = os.path.join(os.environ.get("TEMP", "."), "mousectl_cf2")
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


def shot(name):
    try:
        from PIL import ImageGrab
        ImageGrab.grab().save(name)
        print(f"      screenshot -> {name}")
    except Exception as exc:
        print(f"      screenshot failed: {exc}")


def deep_find_widget(ws):
    """
    Locate the Turnstile widget, descending through open shadow roots.

    Returns its viewport-space rect, or None. Also returns any nested frame
    rects we can see, so we can aim at the frame when the inner document is
    cross-origin (which it is for challenges.cloudflare.com).
    """
    js = r"""
    (() => {
      const hits = [];
      const walk = (root, depth) => {
        if (!root || depth > 8) return;
        let nodes;
        try { nodes = root.querySelectorAll('*'); } catch (e) { return; }
        for (const el of nodes) {
          // 1) Turnstile's own container/iframe anywhere in the tree.
          const tag = el.tagName ? el.tagName.toLowerCase() : '';
          const src = el.getAttribute ? (el.getAttribute('src') || '') : '';
          const cls = el.getAttribute ? (el.getAttribute('class') || '') : '';
          const id  = el.getAttribute ? (el.getAttribute('id') || '') : '';
          const looks = /challenges\.cloudflare\.com|turnstile/i.test(src)
                     || /turnstile|cf-/i.test(cls) || /turnstile|cf-/i.test(id)
                     || tag === 'iframe';
          if (looks) {
            const r = el.getBoundingClientRect();
            if (r.width >= 60 && r.height >= 30) {
              hits.push({x:r.x, y:r.y, w:r.width, h:r.height, tag,
                         src: src.slice(0,110), cls: String(cls).slice(0,70),
                         id: String(id).slice(0,50), depth});
            }
          }
          // 2) descend into open shadow roots
          if (el.shadowRoot) walk(el.shadowRoot, depth + 1);
        }
      };
      walk(document, 0);
      // Also report the visible page text so we can confirm the prompt.
      return {hits, text: (document.body ? document.body.innerText : '').slice(0,300)};
    })()
    """
    try:
        return ws.eval_js(js) or {"hits": []}
    except Exception as exc:
        print(f"      deep probe failed: {exc}")
        return {"hits": []}


def visual_find_checkbox(path, search_box=None, expect=None):
    """
    Find the Turnstile checkbox from the screenshot.

    We cannot trust getBoundingClientRect() here: nowsecure.nl draws the widget
    inside a 3D CSS transform, so the DOM rect is rotated away from where the
    checkbox appears. Instead we look for the checkbox's own signature: a small
    hollow square, i.e. a dark square *outline* surrounded by near-white pixels.

    ``search_box`` limits the scan to the browser's content area so unrelated
    windows cannot win. ``expect`` is an approximate screen position; when
    given, the candidate nearest to it is chosen, because edge-ratio scoring
    alone cannot tell the widget apart from unrelated checkbox-like UI.
    """
    try:
        from PIL import Image
    except Exception:
        return None
    try:
        img = Image.open(path).convert("RGB")
    except Exception:
        return None
    if search_box:
        img = img.crop(search_box)
        off_x, off_y = search_box[0], search_box[1]
    else:
        off_x = off_y = 0
    w, h = img.size
    px = img.load()

    def is_white(x, y):
        if x < 0 or y < 0 or x >= w or y >= h:
            return False
        r, g, b = px[x, y]
        # The widget panel is off-white (~#f0eee9) and the checkbox interior
        # matches it, so the threshold has to sit below pure white.
        return r > 200 and g > 200 and b > 195 and abs(r - b) < 40

    def is_dark(x, y):
        if x < 0 or y < 0 or x >= w or y >= h:
            return False
        r, g, b = px[x, y]
        # The checkbox outline is anti-aliased mid-grey (~150) on this page,
        # not black, so a <120 threshold misses it entirely.
        return r < 190 and g < 195 and b < 195

    best = []
    # Measured on a real page: the Turnstile checkbox renders ~48-56 px square
    # here (the widget is scaled up by the page's transform), so the earlier
    # 22-32 px search range never matched it at all.
    for size in (48, 50, 52, 54, 46, 56, 44, 58, 40, 60, 36, 30, 26, 24):
        step = 2
        for y in range(0, h - size, step):
            for x in range(0, w - size, step):
                # Quick gate: the centre of a checkbox is the panel colour.
                cx, cy = x + size // 2, y + size // 2
                if not is_white(cx, cy):
                    continue
                # Walk the four edges: they must be mostly dark (the outline).
                edge_hits = 0
                edge_total = 0
                for t in range(0, size, 2):
                    for (ex, ey) in ((x + t, y), (x + t, y + size - 1),
                                     (x, y + t), (x + size - 1, y + t)):
                        edge_total += 1
                        if is_dark(ex, ey):
                            edge_hits += 1
                # Just outside the box the panel continues (white-ish).
                outside_white = sum(
                    1 for (ox2, oy2) in ((x - 4, cy), (x + size + 4, cy),
                                         (cx, y - 4), (cx, y + size + 4))
                    if is_white(ox2, oy2)
                )
                ratio = edge_hits / edge_total
                if ratio > 0.70 and outside_white >= 2:
                    score = ratio * 100 + outside_white
                    best.append((score, x, y, size, ratio))

    if not best:
        return None

    # Choose by proximity to the expected checkbox location when the caller
    # supplies one. Edge-ratio scoring alone picked unrelated UI (a sidebar
    # row) because every checkbox-like glyph scores similarly.
    if expect:
        ex, ey = expect
        scored = sorted(
            best,
            key=lambda c: ((off_x + c[1] + c[3] / 2 - ex) ** 2
                           + (off_y + c[2] + c[3] / 2 - ey) ** 2) ** 0.5,
        )
        score, x, y, size, ratio = scored[0]
        return {
            "x": off_x + x + size // 2,
            "y": off_y + y + size // 2,
            "size": size,
            "edge_ratio": round(ratio, 3),
            "score": round(score, 2),
            "distance": round(((off_x + x + size / 2 - ex) ** 2
                               + (off_y + y + size / 2 - ey) ** 2) ** 0.5, 1),
        }

    score, x, y, size, ratio = max(best, key=lambda c: c[0])
    # Click the centre of the box.
    return {
        "x": off_x + x + size // 2,
        "y": off_y + y + size // 2,
        "size": size,
        "edge_ratio": round(ratio, 3),
        "score": round(score, 2),
    }


def page_state(ws):
    """
    Read the page's own challenge state.

    Note: the "verify you are human" text lives inside Cloudflare's
    cross-origin iframe, so `document.body.innerText` never contains it. We
    therefore judge presence by the widget's DOM node and its size, not text.
    """
    js = r"""
    (() => {
      const t = document.title || '';
      const body = document.body ? document.body.innerText : '';
      // Count visible cf-turnstile containers (descending shadow roots).
      let widgets = 0, visibleWidgets = 0;
      const walk = (root, depth) => {
        if (!root || depth > 8) return;
        let nodes = [];
        try { nodes = root.querySelectorAll('*'); } catch (e) { return; }
        for (const el of nodes) {
          const cls = (el.getAttribute && (el.getAttribute('class') || '')) || '';
          const id  = (el.getAttribute && (el.getAttribute('id') || '')) || '';
          if (/cf-turnstile|turnstile/i.test(cls + id)) {
            widgets++;
            const r = el.getBoundingClientRect();
            if (r.width > 80 && r.height > 30 && r.top > -5 && r.bottom < innerHeight + 5) {
              visibleWidgets++;
            }
          }
          if (el.shadowRoot) walk(el.shadowRoot, depth + 1);
        }
      };
      walk(document, 0);
      return {
        title: t, url: location.href, text: body.slice(0, 300),
        widgets, visibleWidgets,
        cookie: document.cookie,
        hasWidget: visibleWidgets > 0
      };
    })()
    """
    try:
        return ws.eval_js(js) or {}
    except Exception:
        return {}


def main():
    if not os.path.exists(EDGE):
        print(f"browser not found: {EDGE}")
        return 2
    if "--fresh" in sys.argv:
        shutil.rmtree(PROFILE, ignore_errors=True)

    print("Cloudflare Turnstile 'click me' test — real system-level input\n")
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
            if target: break
            time.sleep(0.5)
        if not target:
            print("  no page target"); return 1

        ws = CDP(target["webSocketDebuggerUrl"])
        ws.call("Runtime.enable"); ws.call("Page.enable")
        ws.call("Page.bringToFront"); time.sleep(2.5)
        _raise_browser_window("nowsecure") or _raise_browser_window("Edge")
        time.sleep(1.0)

        print("  waiting for the checkbox to render...")
        probe = {"hits": []}
        for i in range(20):
            time.sleep(1.5)
            probe = deep_find_widget(ws)
            st = page_state(ws)
            n = len(probe.get("hits", []))
            print(f"    t={i*1.5:4.1f}s  frames/shadow-hits={n}  prompt={'yes' if st.get('hasWidget') else 'no '}  title={st.get('title','')[:34]!r}")
            if n or st.get("hasWidget"):
                break

        print(f"\n  page text: {(probe.get('text') or '')[:140]!r}")
        print("  candidates found in DOM:")
        for hsel in probe.get("hits", []):
            print(f"    {hsel}")
        shot("cf-a-rendered.png")

        # Resolve screen coordinates from viewport coordinates.
        ox, oy, scale = solve_viewport_origin(ws)
        print(f"\n  viewport origin=({ox:.1f},{oy:.1f}) scale={scale:.4f}")

        # Restrict the visual search to the browser's own client area, so the
        # taskbar, desktop and other applications cannot produce false
        # positives (they did: a taskbar edge scored as a "checkbox").
        vw_px = int(ws.eval_js("innerWidth") or 0)
        vh_px = int(ws.eval_js("innerHeight") or 0)
        search_box = (int(ox) + 1, int(oy) + 1,
                      int(ox + vw_px * scale) - 1, int(oy + vh_px * scale) - 1)
        print(f"  visual search box (screen px): {search_box}")

        sx = sy = None
        hits = probe.get("hits", [])

        # Only consider widgets that are actually inside the visible viewport.
        # Cloudflare keeps hidden/staged copies of the widget in the DOM; the
        # earlier run picked one at y=-212 and clicked off-screen.
        vw = ws.eval_js("window.innerWidth") or 0
        vh = ws.eval_js("window.innerHeight") or 0
        print(f"  viewport size: {vw}x{vh}")
        visible = [
            f for f in hits
            if f["w"] >= 80 and f["h"] >= 30
            and f["x"] >= -5 and f["y"] >= -5
            and f["x"] + f["w"] <= vw + 5 and f["y"] + f["h"] <= vh + 5
        ]
        print(f"  visible candidates: {len(visible)} of {len(hits)}")
        for f in visible:
            print(f"    {f['tag']} cls={f['cls']!r} at ({f['x']:.0f},{f['y']:.0f}) "
                  f"{f['w']:.0f}x{f['h']:.0f}")

        best = None
        for f in visible:
            if "challenges.cloudflare.com" in (f.get("src") or ""):
                best = f
                break
        if best is None and visible:
            # Prefer a real cf-turnstile container, else the largest.
            turnstile = [f for f in visible if "turnstile" in (f.get("cls","") + f.get("id","")).lower()]
            best = max(turnstile or visible, key=lambda f: f["w"] * f["h"])

        if best:
            vx = best["x"] + min(30, best["w"] * 0.12)
            vy = best["y"] + best["h"] / 2
            dom_sx = int(round(ox + vx * scale)); dom_sy = int(round(oy + vy * scale))
            print(f"  DOM rect: {best['cls']!r} {best['w']:.0f}x{best['h']:.0f} "
                  f"-> viewport ({vx:.0f},{vy:.0f}) -> screen ({dom_sx},{dom_sy})")

        # Prefer the VISUAL position, but only search inside the widget's own
        # neighbourhood. The DOM rect is rotated by the page's 3D transform, so
        # it does not give the checkbox directly -- yet it reliably tells us
        # WHERE the widget is, which is enough to exclude the rest of the UI.
        region = search_box
        expect = None
        if best:
            pad = 140
            wx = ox + best["x"] * scale
            wy = oy + best["y"] * scale
            region = (int(max(search_box[0], wx - pad)),
                      int(max(search_box[1], wy - pad)),
                      int(min(search_box[2], wx + best["w"] * scale + pad)),
                      int(min(search_box[3], wy + best["h"] * scale + pad)))
            # The 3D transform only shifts the drawn widget, so the DOM rect's
            # centre is still a good neighbourhood hint for the checkbox.
            expect = (int(ox + (best["x"] + 26) * scale),
                      int(oy + (best["y"] + best["h"] / 2) * scale))
            print(f"  widget neighbourhood: {region}  expect near {expect}")

        v = visual_find_checkbox("cf-a-rendered.png", search_box=region, expect=expect)
        print(f"  visual locator: {v}")
        if v:
            sx, sy = v["x"], v["y"]
            print(f"  -> using VISUAL checkbox position ({sx},{sy})")

        if sx is None and best:
            sx, sy = dom_sx, dom_sy
            print(f"  -> falling back to DOM rect position ({sx},{sy})")

        if sx is None:
            print("\n  RESULT: could not locate the checkbox.")
            return 1

        # Never inject a click outside the desktop: a negative or huge
        # coordinate means our mapping is wrong, and SendInput would succeed
        # while hitting nothing.
        vx0, vy0, vw0, vh0 = winmouse.virtual_screen()
        if not (vx0 <= sx < vx0 + vw0 and vy0 <= sy < vy0 + vh0):
            print(f"\n  RESULT: computed click point ({sx},{sy}) is outside the "
                  f"screen {vw0}x{vh0}; refusing to click blindly.")
            return 1
        if sy < 0 or sy > 40:
            print(f"  (click point y={sy} looks plausible)")

        print(f"\n  CLICKING the checkbox at screen ({sx},{sy}) with real SendInput")
        shot("cf-b-before-click.png")

        # Record what the page looked like at the click point before clicking,
        # so we can prove the click landed on the widget and not on blank page.
        winmouse.click(sx, sy, duration=0.6,
                       failsafe=winmouse.Failsafe(enabled=False))

        print("  clicked; observing the page...")
        st = {}
        for i in range(20):
            time.sleep(1.5)
            st = page_state(ws)
            print(f"    t+{i*1.5:4.1f}s  visible_widgets={st.get('visibleWidgets')}  "
                  f"cf_clearance={'yes' if 'cf_clearance' in (st.get('cookie') or '') else 'no '}  "
                  f"text={(st.get('text') or '')[:40]!r}")
            if st.get("visibleWidgets") == 0:
                break

        shot("cf-c-after-click.png")
        st = page_state(ws)
        print(f"\n  final url   : {st.get('url','')[:90]}")
        print(f"  final title : {st.get('title','')[:70]}")
        print(f"  final text  : {(st.get('text') or '')[:200]!r}")
        print(f"  widgets total/visible: {st.get('widgets')}/{st.get('visibleWidgets')}")
        print(f"  cf_clearance cookie present: {'cf_clearance' in (st.get('cookie') or '')}")

        if st.get("visibleWidgets", 1) > 0:
            print("\n  RESULT: the 'verify you are human' box is STILL displayed.")
            print("          The real click did not clear this challenge.")
            return 1
        print("\n  RESULT: the widget is gone — the challenge appears cleared.")
        return 0
    finally:
        try:
            if ws: ws.close()
        except Exception:
            pass
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
