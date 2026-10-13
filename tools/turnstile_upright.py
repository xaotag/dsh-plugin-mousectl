"""
Turnstile test on a page whose widget is NOT 3D-transformed.

Why this file exists
--------------------
nowsecure.nl renders the Turnstile checkbox inside a rotating CSS cube, and the
checkbox itself lives in a closed shadow root, so neither DOM nor CDP can give
us its position. That made every failure there a *locating* failure, which
never answered the real question:

    Does Cloudflare Turnstile accept a genuine system-level SendInput click?

This page (2captcha's official Turnstile demo) renders the widget upright and
plainly in the DOM, so the checkbox position is reliable and the only remaining
variable is the quality of the click itself.

It also waits for the widget to stop moving before clicking, which the
nowsecure page needed and did not get.
"""

from __future__ import annotations

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
from browsercheck import (CDP, cdp_targets, wait_port, _raise_browser_window,
                          solve_viewport_origin)  # noqa: E402

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
PORT = 9366
PROFILE = os.path.join(os.environ.get("TEMP", "."), "mousectl_ts_upright")
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

URL = "https://2captcha.com/demo/cloudflare-turnstile"


def shot(name):
    try:
        from PIL import ImageGrab
        ImageGrab.grab().save(name)
        print(f"      screenshot -> {name}")
    except Exception as exc:
        print(f"      screenshot failed: {exc}")


def widget_state(ws):
    """
    Report the widget's position and whether it looks settled.

    Turnstile's checkbox lives in a closed shadow root, so we cannot read it
    directly. We CAN read the host container's rect and whether the challenge
    has produced a response token, which is what actually matters.
    """
    js = r"""
    (() => {
      let host = null, rect = null;
      const walk = (root, d) => {
        if (!root || d > 10) return;
        let ns = []; try { ns = root.querySelectorAll('*'); } catch (e) { return; }
        for (const el of ns) {
          const c = (el.getAttribute && el.getAttribute('class')) || '';
          const id = (el.getAttribute && el.getAttribute('id')) || '';
          if (/cf-turnstile/i.test(c) && !host) {
            const b = el.getBoundingClientRect();
            if (b.width > 80 && b.height > 20) { host = el; rect = b; }
          }
          if (el.shadowRoot) walk(el.shadowRoot, d + 1);
        }
      };
      walk(document, 0);
      // The token appears in a hidden input once the challenge is solved.
      let token = '';
      document.querySelectorAll('input[name="cf-turnstile-response"], textarea[name="cf-turnstile-response"]')
        .forEach(i => { if (i.value) token = i.value; });
      // Any visible success marker on the demo page.
      const body = document.body ? document.body.innerText : '';
      return {
        host: !!host,
        rect: rect ? {x: rect.x, y: rect.y, w: rect.width, h: rect.height} : null,
        token: token ? token.slice(0, 40) : '',
        solvedText: /success|verified|通过|成功/i.test(body),
        bodyText: body.slice(0, 300)
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

    print("Turnstile (upright widget) — real system-level click test\n")
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
        _raise_browser_window("Turnstile") or _raise_browser_window("2captcha") \
            or _raise_browser_window("Edge")
        time.sleep(1.0)

        # 1) wait for the widget
        print("  waiting for the widget...")
        st = {}
        for i in range(20):
            time.sleep(1.5)
            st = widget_state(ws)
            print(f"    t={i*1.5:4.1f}s host={st.get('host')} rect={st.get('rect')}")
            if st.get("host"):
                break
        if not st.get("host"):
            print("  RESULT: widget never appeared.")
            shot("ts-upright-no-widget.png")
            return 1

        # 2) wait for it to STOP MOVING. This is the step the nowsecure run
        #    lacked: the widget drifts for a while after first paint.
        print("\n  waiting for the widget to stop moving...")
        prev = None
        settled = None
        for i in range(16):
            time.sleep(1.0)
            cur = widget_state(ws).get("rect")
            moved = "n/a" if prev is None else round(
                abs(cur["x"] - prev["x"]) + abs(cur["y"] - prev["y"]), 1)
            print(f"    t={i:>2}s rect=({cur['x']:.0f},{cur['y']:.0f}) moved={moved}")
            if prev is not None and moved == 0:
                settled = cur
                print("    -> settled")
                break
            prev = cur
        if settled is None:
            settled = widget_state(ws).get("rect")
            print("    -> never fully settled; using the last position")

        # Record the token BEFORE clicking. 2captcha's demo can pre-seed a
        # placeholder token, and an earlier run reported a false PASS because
        # it saw "XXXX.DUMMY.TOKEN.XXXX" that was already there.
        pre = widget_state(ws)
        pre_token = pre.get("token") or ""
        print(f"  token before click: {pre_token or '(none)'}")
        if pre_token:
            print("  note: a token is already present; the widget may be pre-solved.")
            print("  pressing the demo's Reset button first to get a clean state")
            try:
                ws.eval_js("""(() => {
                  const b = [...document.querySelectorAll('button,input[type=button]')]
                    .find(x => /reset/i.test(x.textContent || x.value || ''));
                  if (b) { b.click(); return 'clicked'; }
                  return 'no reset button';
                })()""")
                time.sleep(2.5)
                pre = widget_state(ws)
                pre_token = pre.get("token") or ""
                print(f"  token after reset : {pre_token or '(none)'}")
            except Exception as exc:
                print(f"  reset failed: {exc}")

        # 3) resolve screen coordinates
        settled = widget_state(ws).get("rect") or settled
        ox, oy, scale = solve_viewport_origin(ws)
        print(f"\n  viewport origin=({ox:.1f},{oy:.1f}) scale={scale:.4f}")
        print(f"  widget viewport rect: {settled}")

        # The checkbox is at the widget's left, vertically centred.
        vx = settled["x"] + 30
        vy = settled["y"] + settled["h"] / 2
        sx = int(round(ox + vx * scale)); sy = int(round(oy + vy * scale))
        print(f"  -> clicking checkbox at screen ({sx},{sy})")

        vx0, vy0, vw0, vh0 = winmouse.virtual_screen()
        if not (vx0 <= sx < vx0 + vw0 and vy0 <= sy < vy0 + vh0):
            print("  RESULT: computed point is off-screen; refusing to click.")
            return 1

        shot("ts-upright-before.png")
        winmouse.click(sx, sy, duration=0.6,
                       failsafe=winmouse.Failsafe(enabled=False))
        print("  click sent; waiting for the challenge to resolve...")

        ok = False
        for i in range(24):
            time.sleep(1.5)
            st = widget_state(ws)
            tok = st.get("token") or ""
            print(f"    t+{i*1.5:4.1f}s token={'yes' if tok else 'no '} "
                  f"solvedText={st.get('solvedText')} text={(st.get('bodyText') or '')[:50]!r}")
            if tok or st.get("solvedText"):
                ok = True
                break

        shot("ts-upright-after.png")
        st = widget_state(ws)
        print(f"\n  final token      : {(st.get('token') or '(none)')[:40]}")
        print(f"  final body text  : {(st.get('bodyText') or '')[:200]!r}")

        final_token = st.get("token") or ""
        # Only count a token that was NOT already there before the click.
        earned = bool(final_token) and final_token != pre_token
        print(f"  token before/after: {pre_token[:24] or '(none)'!r} -> "
              f"{final_token[:24] or '(none)'!r}")

        if earned:
            print("\n  RESULT: PASSED — a NEW response token appeared after a "
                  "genuine system-level click.")
            return 0
        if final_token and final_token == pre_token:
            print("\n  RESULT: INCONCLUSIVE — the token was already present before "
                  "the click, so this run did not prove anything.")
            return 2
        if st.get("solvedText"):
            print("\n  RESULT: page reports success (no token visible in DOM).")
            return 0
        print("\n  RESULT: no new token; the click did not clear the challenge.")
        return 1
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
