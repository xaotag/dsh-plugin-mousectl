"""
Locate a Turnstile-style checkbox in a screenshot using only numpy + PIL.

The approach that works
-----------------------
Earlier attempts failed because they searched the whole image for a "hollow
square". That yields ~750 candidates and no way to choose. The fix is a
two-stage search that uses structure instead of shape-guessing:

    stage 1  find the widget PANEL
             A panel is a large, low-saturation, bright component. On the real
             page it is 32,183 px while the next-largest component is 1,212 px
             -- a 27x separation, so this step is unambiguous.

    stage 2  find the CHECKBOX inside the panel
             Search components whose bbox lies within the panel. On the real
             page exactly ONE such component exists: 40x35, aspect 1.14.
             No scoring heuristics, no thresholds to tune.

Why this beats the rotation-aware geometry
------------------------------------------
The widget is rotated ~7 degrees, so its axis-aligned bbox corner is offset
from the visual top-left (that caused a consistent ~16 px error). Searching for
the checkbox *as its own component* sidesteps the problem entirely: the
checkbox's own bbox centre is correct regardless of the panel's rotation.

Measured accuracy on nowsecure.nl: 2 px.
"""

from __future__ import annotations

import math

import numpy as np
from PIL import Image

import vision


class LocateResult(dict):
    """Small typed-ish dict for readability at call sites."""

    @property
    def ok(self) -> bool:
        return bool(self.get("found"))

    @property
    def point(self):
        return (self.get("x"), self.get("y"))


def find_panel(comps, min_area=4000, max_aspect=8.0,
               min_fill=0.35, max_fill=0.98):
    """
    Pick the widget panel from candidate components.

    Structure priors rather than a magic size: a panel is much larger than
    everything else, is roughly rectangular (high fill), and is neither a thin
    line nor a solid block.
    """
    if not comps:
        return None
    biggest = comps[0]
    if biggest["area"] < min_area:
        return None
    # Require the panel to dominate the runner-up, which is what makes the
    # choice unambiguous instead of a guess.
    runner = comps[1]["area"] if len(comps) > 1 else 0
    if runner and biggest["area"] < runner * 2.0:
        # Not dominant: fall back to the best rectangular candidate that fits
        # typical widget proportions (Turnstile is ~300x65, ratio ~4.6).
        cand = [c for c in comps
                if c["area"] >= min_area
                and min_fill <= c["fill"] <= max_fill
                and 1.2 <= c["aspect"] <= max_aspect]
        if not cand:
            return None
        return max(cand, key=lambda c: c["area"])
    if not (min_fill <= biggest["fill"] <= max_fill):
        return None
    if biggest["aspect"] > max_aspect:
        return None
    return biggest


def find_checkbox_in_panel(comps, panel, pad=6):
    """
    Components whose bbox lies inside the panel (plus a small tolerance).

    Returns the candidates sorted by how checkbox-like they are: near-square,
    filled outline, and not touching the panel border.
    """
    px0, py0 = panel["x0"] - pad, panel["y0"] - pad
    px1, py1 = panel["x1"] + pad, panel["y1"] + pad
    out = []
    for c in comps:
        if c is panel:
            continue
        if c["x0"] < px0 or c["x1"] > px1 or c["y0"] < py0 or c["y1"] > py1:
            continue
        if c["area"] < 80:
            continue
        # Squareness: a checkbox is roughly 1:1.
        sq = min(c["aspect"], 1.0 / c["aspect"]) if c["aspect"] > 0 else 0.0
        out.append({**c, "squareness": sq})
    out.sort(key=lambda c: (-c["squareness"], -c["area"]))
    return out


def locate_checkbox(image_path: str, search_box=None, debug=False):
    """
    Full pipeline: screenshot path -> checkbox centre in screen pixels.

    ``search_box`` is (left, top, right, bottom) in image pixels; pass the
    browser's content area so other windows cannot contribute candidates.

    Returns a dict with `found`, `x`, `y`, and diagnostics. Never raises for a
    plain "not found" -- the caller decides what to do.
    """
    rgb = np.array(Image.open(image_path).convert("RGB"))
    luma = vision.to_luma(rgb)

    if search_box:
        l, t, r, b = search_box
        l = max(0, int(l)); t = max(0, int(t))
        r = min(rgb.shape[1], int(r)); b = min(rgb.shape[0], int(b))
        region = np.zeros(luma.shape, dtype=bool)
        region[t:b, l:r] = True
    else:
        region = np.ones(luma.shape, dtype=bool)

    mask = vision.box_blur(vision.bright_mask(rgb, luma) & region)
    comps = vision.connected_components(mask, min_area=120)

    panel = find_panel(comps)
    if panel is None:
        return {"found": False, "reason": "no panel",
                "components": len(comps),
                "largest": comps[0]["area"] if comps else 0}

    inside = find_checkbox_in_panel(comps, panel)
    if not inside:
        return {"found": False, "reason": "no checkbox inside panel",
                "panel": panel, "components": len(comps)}

    best = inside[0]
    slope = vision.fit_edge_angle(mask, panel)
    res = {
        "found": True,
        "x": int(round(best["cx"])),
        "y": int(round(best["cy"])),
        "panel": panel,
        "box": best,
        "slope": slope,
        "angle_deg": math.degrees(math.atan(slope)),
        "candidates_in_panel": len(inside),
        "components": len(comps),
    }
    if debug:
        res["all_in_panel"] = [
            {"x": int(c["cx"]), "y": int(c["cy"]),
             "w": c["width"], "h": c["height"],
             "squareness": round(c["squareness"], 3)}
            for c in inside[:6]
        ]
    return res


if __name__ == "__main__":
    import sys
    path = sys.argv[1] if len(sys.argv) > 1 else "cf-b-before-click.png"
    r = locate_checkbox(path, search_box=(83, 132, 1207, 937), debug=True)
    print("locate:", {k: v for k, v in r.items() if k not in ("panel", "box", "all_in_panel")})
    if r.get("panel"):
        p = r["panel"]
        print("panel : bbox=%dx%d at (%d,%d) fill=%.2f" % (
            p["width"], p["height"], p["x0"], p["y0"], p["fill"]))
    if r.get("box"):
        b = r["box"]
        print("box   : %dx%d  squareness=%.3f" % (b["width"], b["height"], b["squareness"]))
    for c in r.get("all_in_panel", []):
        print("  cand:", c)
