"""
Project a point inside a 3D-transformed element to screen coordinates.

Why this exists
---------------
nowsecure.nl renders the Turnstile widget inside a rotating CSS cube:

    .cube  transform: matrix3d(0.99396, 0.082496, -0.0723726, 0, ...)
           perspective: 277.75px
           translateZ: 111.1px

`getBoundingClientRect()` returns the *axis-aligned bounding box* of the
transformed element -- for a rotated square that box is larger than the square
and its edges do not correspond to the checkbox at all. That is why a visual
"hollow square" detector and a naive rect-centre pick both missed.

Correct approach
----------------
The point we want is expressed in the element's own untransformed coordinate
space (e.g. the checkbox centre at ~(30, 35) inside a 222x70 widget). We push
that point through the element's accumulated transform chain, apply the
perspective divide, and land on real screen pixels.

This is a small 4x4 matrix pipeline, implemented with plain lists so the tool
keeps its zero-dependency property.
"""

from __future__ import annotations

import math


def _parse_matrix(transform: str):
    """
    Parse a CSS transform string into a 4x4 matrix (row-major list of lists).

    Supports `matrix(a,b,c,d,e,f)` (2D) and `matrix3d(m11..m44)` (3D).
    Returns the identity for `none` or anything unparseable.
    """
    if not transform or transform == "none":
        return _identity()
    t = transform.strip()
    if t.startswith("matrix3d("):
        vals = [float(v) for v in t[len("matrix3d("):-1].split(",")]
        if len(vals) != 16:
            return _identity()
        # CSS matrix3d is column-major: m11 m12 m13 m14  m21 ...
        # Build row-major so that row i, col j = vals[j*4 + i].
        return [[vals[c * 4 + r] for c in range(4)] for r in range(4)]
    if t.startswith("matrix("):
        vals = [float(v) for v in t[len("matrix("):-1].split(",")]
        if len(vals) != 6:
            return _identity()
        a, b, c, d, e, f = vals
        return [
            [a, c, 0.0, e],
            [b, d, 0.0, f],
            [0.0, 0.0, 1.0, 0.0],
            [0.0, 0.0, 0.0, 1.0],
        ]
    return _identity()


def _identity():
    return [[1.0 if i == j else 0.0 for j in range(4)] for i in range(4)]


def _multiply(a, b):
    """Row-major 4x4 matrix product a*b."""
    out = [[0.0] * 4 for _ in range(4)]
    for i in range(4):
        for j in range(4):
            out[i][j] = sum(a[i][k] * b[k][j] for k in range(4))
    return out


def _translation(tx, ty, tz):
    m = _identity()
    m[0][3], m[1][3], m[2][3] = tx, ty, tz
    return m


def _apply(m, p):
    """Apply a row-major 4x4 matrix to a 3D point (x, y, z, 1)."""
    x, y, z = p
    return (
        m[0][0] * x + m[0][1] * y + m[0][2] * z + m[0][3],
        m[1][0] * x + m[1][1] * y + m[1][2] * z + m[1][3],
        m[2][0] * x + m[2][1] * y + m[2][2] * z + m[2][3],
        m[3][0] * x + m[3][1] * y + m[3][2] * z + m[3][3],
    )


def project_point(chain, local_x, local_y):
    """
    Project a point given in the innermost element's local coordinates out to
    screen (viewport) coordinates.

    ``chain`` is an ordered list from the OUTERMOST ancestor to the element the
    local point belongs to. Each entry is a dict with:
        - transform      : CSS transform string
        - perspective    : CSS perspective string (e.g. "277.75px" or "none")
        - origin         : CSS transform-origin, "x y" in px (optional)
        - rect           : {x, y, w, h} of this element in viewport space
        - offsetW/offsetH: layout size
        - paddingLeft/Top, borderLeft/Top : optional, to locate content box

    Returns (screen_x, screen_y).

    The key subtlety: getBoundingClientRect on an ancestor already reflects
    that ancestor's own transform, so we only accumulate transforms for the
    elements at and below the point of interest, and use the outermost rect
    only as the final viewport anchor.
    """
    # Start from the accumulated matrix = identity, then compose each level.
    mat = _identity()
    persp = None
    anchor_x = anchor_y = 0.0

    for i, level in enumerate(chain):
        tf = _parse_matrix(level.get("transform", "none"))
        ow = float(level.get("offsetW") or 0)
        oh = float(level.get("offsetH") or 0)

        # transform-origin defaults to the element's centre.
        ox = oy = None
        origin = level.get("origin")
        if origin and origin != "none":
            parts = origin.replace("px", "").split()
            if len(parts) >= 2:
                try:
                    ox, oy = float(parts[0]), float(parts[1])
                except ValueError:
                    ox = oy = None
        if ox is None:
            ox, oy = ow / 2.0, oh / 2.0

        # Translate so the origin is at 0, apply the transform, translate back.
        to_origin = _translation(-ox, -oy, 0.0)
        from_origin = _translation(ox, oy, 0.0)
        local = _multiply(from_origin, _multiply(tf, to_origin))

        # Offsets place this element inside its parent's coordinate space.
        off_x = float(level.get("offsetLeft") or 0)
        off_y = float(level.get("offsetTop") or 0)
        place = _translation(off_x, off_y, 0.0)

        mat = _multiply(mat, _multiply(place, local))

        p = level.get("perspective")
        if p and p != "none":
            try:
                persp = float(p.replace("px", ""))
            except ValueError:
                persp = None

        # The outermost element's rect gives us the viewport anchor. Its own
        # transform is already accounted for above, so we take the rect of the
        # element ABOVE it (or the document) as the origin.
        if i == 0:
            r = level.get("rect") or {}
            anchor_x = float(r.get("x") or 0)
            anchor_y = float(r.get("y") or 0)

    x, y, z, w = _apply(mat, (float(local_x), float(local_y), 0.0))

    # Perspective divide. CSS perspective applies as 1 / (1 - z/perspective).
    if persp and persp != 0:
        denom = 1.0 - (z / persp)
        if abs(denom) > 1e-6:
            x /= denom
            y /= denom

    return anchor_x + x, anchor_y + y


def project_via_rect(rect, local_x, local_y, offset_w, offset_h):
    """
    Simple 2D fallback: map a local point through an element's bounding rect.

    Valid only when the element is NOT rotated; provided so callers can compare
    the two answers and detect that a 3D transform is present (they will
    disagree).
    """
    sx = rect["x"] + (local_x / offset_w) * rect["w"] if offset_w else rect["x"]
    sy = rect["y"] + (local_y / offset_h) * rect["h"] if offset_h else rect["y"]
    return sx, sy


if __name__ == "__main__":
    # Reproduce the real chain measured on nowsecure.nl and print the answer,
    # so the maths can be checked without a browser.
    chain = [
        {
            "transform": "matrix3d(1,0,0,0, 0,1,0,0, 0,0,1,0, 0,0,111.1,1)",
            "perspective": "none",
            "rect": {"x": 442.3456726074219, "y": 383.4715576171875,
                     "w": 240.17208862304688, "h": 203.428466796875},
            "offsetW": 222, "offsetH": 222, "offsetLeft": 0, "offsetTop": 0,
            "origin": "111px 111px",
        },
        {
            "transform": "matrix3d(0.99396,0.082496,-0.0723726,0,"
                         "-0.0869602,0.994351,-0.0608664,0,"
                         "0.0669425,0.0667923,0.995519,0, 0,0,0,1)",
            "perspective": "none",
            "rect": {"x": 442.3456726074219, "y": 383.4715576171875,
                     "w": 240.17208862304688, "h": 203.428466796875},
            "offsetW": 222, "offsetH": 222, "offsetLeft": 0, "offsetTop": 0,
            "origin": "111px 111px",
        },
    ]
    # The widget sits on the cube face; its checkbox is near the face's top-left.
    print("projected checkbox:", project_point(chain, 30, 30))
