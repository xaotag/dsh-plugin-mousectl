"""
Cloudflare Turnstile challenge test using real system-level mouse input.

Goal: open a real Cloudflare-protected page in a real Chromium browser, locate
the "Verify you are human" checkbox, click it with Win32 SendInput (a genuine
trusted click), and report what actually happened.

This is deliberately honest about outcomes: it reports `solved`, `challenge`,
`blocked` or `error` rather than claiming success. Cloudflare's decision is
theirs to make, and a tool that reports what the page really says is more
useful than one that guesses.
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import time
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
# The Win32 engine lives in ../python in the published repo layout.
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "python"))
import winmouse  # noqa: E402
try:
    from browsercheck import CDP, cdp_targets, wait_port, _force_foreground, _raise_browser_window
except Exception as exc:  # pragma: no cover
    print(f"cannot import browsercheck helpers: {exc}")
    raise

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
PORT = 9344
PROFILE = os.path.join(os.environ.get("TEMP", "."), "mousectl_cf_profile")
ARGS = [
    "--new-window",
    "--window-size=1100,860",
    "--window-position=80,60",
    f"--remote-debugging-port={PORT}",
    "--remote-allow-origins=*",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-features=Translate",
    f"--user-data-dir={PROFILE}",
]

TARGETS = [
    # The "managed" widget renders the interactive checkbox ("click me").
    "https://peet.ws/turnstile-test/managed.html",
    "https://2captcha.com/demo/cloudflare-turnstile",
    "https://nopecha.com/demo/turnstile",
    "https://demo.turnstile.workers.dev/",
    "https://nowsecure.nl",
]


def shot(name: str) -> None:
    try:
        from PIL import ImageGrab
        ImageGrab.grab().save(name)
        print(f"    screenshot -> {name}")
    except Exception as exc:
        print(f"    screenshot failed: {exc}")


def locate_turnstile(ws) -> dict | None:
    """
    Find the Turnstile widget inside its iframe and return click coordinates
    in *screen* space together with the widget's page-relative box.

    The Turnstile checkbox lives in a cross-origin iframe, so we cannot reach
    into it from the top document. We locate the iframe element instead and
    aim at its left portion, where the checkbox sits.
    """
    js = r"""
    (() => {
      const out = [];
      const frames = [...document.querySelectorAll('iframe')];
      for (const f of frames) {
        const r = f.getBoundingClientRect();
        const src = f.getAttribute('src') || '';
        const cls = f.className || '';
        const title = f.getAttribute('title') || '';
        const isTs = /challenges\.cloudflare\.com|turnstile/i.test(src)
                  || /turnstile/i.test(cls) || /turnstile|verify/i.test(title);
        if (isTs || (r.width > 200 && r.height > 50 && r.height < 120)) {
          out.push({x: r.x, y: r.y, w: r.width, h: r.height,
                    src: src.slice(0, 120), cls: String(cls).slice(0, 80),
                    title, isTs});
        }
      }
      return out;
    })()
    """
    try:
        found = ws.eval_js(js)
    except Exception as exc:
        print(f"    widget probe failed: {exc}")
        return None
    if not found:
        return None
    # Prefer the frame we are confident is Turnstile.
    found.sort(key=lambda f: (not f.get("isTs"), -f["w"] * f["h"]))
    return found[0]


def page_state(ws) -> dict:
    """Read whatever the page tells us about its own challenge state."""
    js = r"""
    (() => {
      const t = (document.title || '');
      const body = (document.body ? document.body.innerText : '').slice(0, 600);
      const html = document.documentElement.outerHTML;
      return {
        title: t,
        url: location.href,
        bodyText: body,
        hasTurnstile: /challenges\.cloudflare\.com|turnstile/i.test(html),
        hasChallengeForm: !!document.querySelector('#challenge-form, #cf-challenge-running, .cf-turnstile'),
        successCookie: document.cookie.includes('cf_clearance'),
        iframes: document.querySelectorAll('iframe').length
      };
    })()
    """
    try:
        return ws.eval_js(js) or {}
    except Exception:
        return {}


def classify(state: dict) -> str:
    text = (state.get("bodyText") or "") + " " + (state.get("title") or "")
    low = text.lower()
    if state.get("successCookie"):
        return "solved"
    if "verify you are human" in low or "确认你是真人" in text or state.get("hasChallengeForm"):
        return "challenge"
    if "blocked" in low or "access denied" in low or "已被拦截" in text:
        return "blocked"
    if "success" in low or "verified" in low:
        return "solved"
    return "unknown"


def main() -> int:
    if not os.path.exists(EDGE):
        print(f"browser not found: {EDGE}")
        return 2
    # Start from a clean profile so Cloudflare sees a first visit, not a
    # session that already solved the challenge.
    if len(sys.argv) > 1 and sys.argv[1] == "--fresh":
        import shutil
        shutil.rmtree(PROFILE, ignore_errors=True)

    print("Cloudflare Turnstile test with real system-level input\n")
    proc = subprocess.Popen([EDGE, *ARGS, TARGETS[0]],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    ws = None
    try:
        if not wait_port(PORT):
            print("  remote debugging port never opened")
            return 1
        target = None
        for _ in range(40):
            for t in cdp_targets(PORT):
                if t.get("type") == "page" and "devtools" not in t.get("url", ""):
                    target = t
                    break
            if target:
                break
            time.sleep(0.5)
        if not target:
            print("  no page target")
            return 1

        ws = CDP(target["webSocketDebuggerUrl"])
        ws.call("Runtime.enable")
        ws.call("Page.enable")
        ws.call("Page.bringToFront")
        time.sleep(2.0)
        _raise_browser_window("nowsecure") or _raise_browser_window("Cloudflare")

        print("  waiting for the challenge to settle...")
        state = {}
        widget = None
        # The widget and the challenge appear asynchronously, so poll for the
        # widget rather than sampling the DOM once.
        for attempt in range(24):
            time.sleep(1.5)
            state = page_state(ws)
            kind = classify(state)
            if widget is None:
                widget = locate_turnstile(ws)
            print(f"    t={attempt*1.5:4.1f}s  state={kind:9}  iframes={state.get('iframes')}  "
                  f"widget={'yes' if widget else 'no '}  title={state.get('title','')[:40]!r}")
            if widget or kind in ("solved", "blocked"):
                break

        print(f"\n  url   : {state.get('url','')[:100]}")
        print(f"  title : {state.get('title','')[:80]}")
        print(f"  text  : {(state.get('bodyText') or '')[:160]!r}")
        shot("cf-01-loaded.png")

        kind = classify(state)
        if kind == "solved":
            print("\n  RESULT: already passed without interaction.")
            return 0
        if kind == "blocked":
            print("\n  RESULT: blocked before any click.")
            return 1

        widget = locate_turnstile(ws)
        print(f"\n  turnstile widget: {widget}")
        if not widget:
            # Fall back to the other known Turnstile demo pages before giving up.
            for alt in TARGETS[1:]:
                print(f"  no widget here; trying {alt}")
                try:
                    ws.call("Page.navigate", {"url": alt})
                except Exception as exc:
                    print(f"    navigate failed: {exc}")
                    break
                for _ in range(20):
                    time.sleep(1.5)
                    state = page_state(ws)
                    w = locate_turnstile(ws)
                    print(f"    t={_:>4}  iframes={state.get('iframes')}  widget={'yes' if w else 'no '}  "
                          f"title={state.get('title','')[:40]!r}")
                    if w:
                        widget = w
                        break
                if widget:
                    break
        if not widget:
            print("  RESULT: no Turnstile widget found on any candidate page.")
            shot("cf-02-nowidget.png")
            return 1
        print(f"\n  turnstile widget: {widget}")

        # Solve screen coordinates: the page viewport origin must be measured,
        # not assumed (see README: window origin != viewport origin).
        from browsercheck import solve_viewport_origin
        ox, oy, scale = solve_viewport_origin(ws)
        print(f"  viewport origin: ({ox:.1f},{oy:.1f}) scale={scale:.4f}")

        # The checkbox sits at the left edge of the widget, vertically centred.
        cb_x = widget["x"] + 30
        cb_y = widget["y"] + widget["h"] / 2
        sx = int(round(ox + cb_x * scale))
        sy = int(round(oy + cb_y * scale))
        print(f"  clicking checkbox at screen ({sx},{sy})")
        shot("cf-03-before-click.png")

        winmouse.click(sx, sy, duration=0.55,
                       failsafe=winmouse.Failsafe(enabled=False))
        print("  click sent; waiting for verification...")

        for attempt in range(24):
            time.sleep(1.5)
            state = page_state(ws)
            kind = classify(state)
            print(f"    t+{attempt*1.5:4.1f}s  state={kind:9}  title={state.get('title','')[:52]!r}")
            if kind in ("solved", "blocked"):
                break

        shot("cf-04-after-click.png")
        print(f"\n  final url   : {state.get('url','')[:100]}")
        print(f"  final title : {state.get('title','')[:80]}")
        print(f"  final text  : {(state.get('bodyText') or '')[:200]!r}")
        print(f"  cf_clearance cookie: {state.get('successCookie')}")

        final = classify(state)
        print(f"\n  RESULT: {final}")
        if final == "solved":
            print("  The real system-level click passed Cloudflare's check.")
        elif final == "challenge":
            print("  The click did not clear the challenge (still verifying / failed).")
        else:
            print(f"  Page ended in state: {final}")
        return 0 if final == "solved" else 1
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
