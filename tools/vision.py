"""
Pure numpy + PIL computer-vision helpers (no OpenCV, no scipy, no YOLO).

Why hand-rolled
---------------
This environment has numpy and PIL but not `cv2`, `scipy` or `skimage`, so
`findContours`, `minAreaRect`, `connectedComponents` and `Otsu` are all
unavailable. The operations below cover exactly what locating a UI widget
needs, and nothing more:

    otsu_threshold      - adaptive bright/dark cut, works on light and dark themes
    box_blur            - 3x3 smoothing to suppress single-pixel noise
    connected_components- two-pass labelling with union-find, returns component stats
    component_stats     - bbox, area, fill ratio, aspect
    fit_edge_angle      - least-squares slope of a component's top edge (rotation)
    rotate_about        - rotate an image about a point, with edge-safe fill
    template_score      - normalised cross-correlation for shape verification

Everything works on a 2-D uint8 luma array plus an optional RGB array, so the
caller converts once.
"""

from __future__ import annotations

import math

import numpy as np
from PIL import Image


# ---------------------------------------------------------------- thresholds


def to_luma(rgb: np.ndarray) -> np.ndarray:
    """Rec.601 luma as uint8. `rgb` is HxWx3."""
    if rgb.ndim == 2:
        return rgb.astype(np.uint8)
    return (0.299 * rgb[:, :, 0] + 0.587 * rgb[:, :, 1]
            + 0.114 * rgb[:, :, 2]).astype(np.uint8)


def otsu_threshold(luma: np.ndarray) -> int:
    """
    Otsu's method: the threshold that maximises between-class variance.

    Derived here rather than imported so the tool stays dependency-light, and
    so it adapts automatically to light and dark page themes instead of relying
    on a hardcoded "bright > 200".
    """
    hist = np.bincount(luma.ravel(), minlength=256).astype(np.float64)
    total = hist.sum()
    if total == 0:
        return 128
    prob = hist / total
    omega = np.cumsum(prob)
    mu = np.cumsum(prob * np.arange(256))
    mu_t = mu[-1]
    denom = omega * (1.0 - omega)
    # Guard the divide: only thresholds that actually split the histogram count.
    with np.errstate(divide="ignore", invalid="ignore"):
        sigma_b = np.where(denom > 0, (mu_t * omega - mu) ** 2 / denom, 0.0)
    return int(np.argmax(sigma_b))


def bright_mask(rgb: np.ndarray, luma: np.ndarray) -> np.ndarray:
    """
    Mask of "widget panel" pixels.

    A widget panel is a large, low-saturation, relatively bright region. We use
    Otsu for the adaptive level and additionally require low colour saturation,
    because saturated page decorations are the main source of false panels.
    """
    level = otsu_threshold(luma)
    # Panels sit above the page background; bias slightly above Otsu so we do
    # not swallow the background itself.
    cut = max(level, int(np.percentile(luma, 50)))
    if rgb.ndim == 3:
        mx = rgb.max(axis=2).astype(np.int16)
        mn = rgb.min(axis=2).astype(np.int16)
        sat = mx - mn
        low_sat = sat < 60
    else:
        low_sat = np.ones_like(luma, dtype=bool)
    return (luma >= cut) & low_sat


# ---------------------------------------------------------------- morphology


def box_blur(mask: np.ndarray) -> np.ndarray:
    """
    3x3 majority filter on a boolean mask, implemented with shifted adds.

    Removes isolated speckle that would otherwise become its own component.
    """
    m = mask.astype(np.uint8)
    acc = np.zeros_like(m, dtype=np.uint8)
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            acc += np.roll(np.roll(m, dy, axis=0), dx, axis=1)
    return acc >= 5


# ------------------------------------------------------- connected components


def connected_components(mask: np.ndarray, min_area: int = 40):
    """
    Two-pass connected-component labelling (4-connectivity) with union-find.

    Returns a list of dicts: {label, area, y0, y1, x0, x1, width, height,
    fill, aspect}. `fill` is area / bbox_area and `aspect` is width / height;
    both are what let us reject icon-like components that merely happen to be
    bright and rectangular.

    Provided because scipy.ndimage.label is unavailable here. Row-wise runs are
    labelled first, then merged across rows, which is far faster than a Python
    BFS over every pixel.
    """
    h, w = mask.shape
    labels = np.zeros((h, w), dtype=np.int32)
    parent = [0]

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    next_label = 1
    prev_runs: list[tuple[int, int, int]] = []  # (start, end, label)

    for y in range(h):
        row = mask[y]
        if not row.any():
            prev_runs = []
            continue
        # Find runs of True in this row.
        idx = np.flatnonzero(row)
        splits = np.flatnonzero(np.diff(idx) > 1)
        starts = np.concatenate(([idx[0]], idx[splits + 1]))
        ends = np.concatenate((idx[splits], [idx[-1]]))

        cur_runs: list[tuple[int, int, int]] = []
        for s, e in zip(starts, ends):
            label = 0
            # Merge with any overlapping run on the previous row.
            for ps, pe, pl in prev_runs:
                if ps <= e and s <= pe:
                    if label == 0:
                        label = pl
                    else:
                        union(label, pl)
            if label == 0:
                label = next_label
                parent.append(label)
                next_label += 1
            labels[y, s:e + 1] = label
            cur_runs.append((s, e, label))
        prev_runs = cur_runs

    # Flatten labels and gather statistics.
    stats: dict[int, dict] = {}
    ys, xs = np.nonzero(labels)
    lab = labels[ys, xs]
    roots = np.array([find(int(v)) for v in lab], dtype=np.int32)
    for root in np.unique(roots):
        sel = roots == root
        yy, xx = ys[sel], xs[sel]
        area = int(sel.sum())
        if area < min_area:
            continue
        y0, y1 = int(yy.min()), int(yy.max())
        x0, x1 = int(xx.min()), int(xx.max())
        bw, bh = x1 - x0 + 1, y1 - y0 + 1
        stats[int(root)] = {
            "label": int(root),
            "area": area,
            "y0": y0, "y1": y1, "x0": x0, "x1": x1,
            "width": bw, "height": bh,
            "fill": area / float(bw * bh),
            "aspect": bw / float(bh),
            "cx": (x0 + x1) / 2.0,
            "cy": (y0 + y1) / 2.0,
        }
    return sorted(stats.values(), key=lambda d: -d["area"])


# ------------------------------------------------------------------ geometry


def fit_edge_angle(mask: np.ndarray, comp: dict, which: str = "top",
                   max_dev: float = 0.35) -> float:
    """
    Least-squares slope (dy/dx) of a component's top (or bottom) edge.

    For each column inside the component we take the first set pixel; a line
    fit through those points recovers the widget's rotation. Columns whose first
    pixel deviates wildly (rounded corners, overhanging content) are rejected by
    an iterative trim, so a single stray column cannot skew the angle.
    """
    x0, x1 = comp["x0"], comp["x1"]
    y0, y1 = comp["y0"], comp["y1"]
    span = x1 - x0
    if span < 8:
        return 0.0

    pts_x: list[int] = []
    pts_y: list[int] = []
    for x in range(x0 + 2, x1 - 1):
        col = mask[y0:y1 + 1, x]
        nz = np.flatnonzero(col)
        if nz.size == 0:
            continue
        pts_x.append(x)
        pts_y.append(y0 + (nz[0] if which == "top" else nz[-1]))

    if len(pts_x) < 6:
        return 0.0

    X = np.asarray(pts_x, dtype=np.float64)
    Y = np.asarray(pts_y, dtype=np.float64)

    slope = 0.0
    for _ in range(4):
        if X.size < 6:
            break
        slope, intercept = np.polyfit(X, Y, 1)
        resid = Y - (slope * X + intercept)
        keep = np.abs(resid) <= max(2.0, max_dev * max(np.abs(resid).std(), 1.0) * 3)
        if keep.all():
            break
        X, Y = X[keep], Y[keep]
    return float(slope)


def rotate_about(rgb: np.ndarray, angle_deg: float, center: tuple[float, float],
                 fill: tuple[int, int, int] | None = None) -> np.ndarray:
    """
    Rotate an RGB array about a point, keeping the same canvas size.

    Used to de-rotate a widget before measuring it. `fill` defaults to the
    median border colour so the newly exposed corners blend with the page
    instead of creating high-contrast false edges (which measurably *increased*
    false candidates in an earlier attempt).
    """
    img = Image.fromarray(rgb.astype(np.uint8))
    if fill is None:
        border = np.concatenate([
            rgb[0, :, :], rgb[-1, :, :], rgb[:, 0, :], rgb[:, -1, :]
        ])
        fill = tuple(int(v) for v in np.median(border, axis=0))
    return np.array(
        img.rotate(angle_deg, resample=Image.BICUBIC, center=center,
                   fillcolor=fill)
    )


def inset_along_rotation(left_x: float, top_y: float, slope: float,
                         perp_in: float, horiz_in: float) -> tuple[float, float]:
    """
    Step `horiz_in` along a rotated top edge and `perp_in` perpendicular to it.

    The Turnstile checkbox sits a fixed distance in from the widget's top-left
    corner *measured in the widget's own rotated frame*. Using the axis-aligned
    bounding-box corner instead is what produced a consistent ~16 px vertical
    error earlier.
    """
    ang = math.atan(slope)
    # Unit vector along the edge.
    ux, uy = math.cos(ang), math.sin(ang)
    # Unit normal pointing "down" into the widget.
    nx, ny = -math.sin(ang), math.cos(ang)
    x = left_x + ux * horiz_in + nx * perp_in
    y = top_y + uy * horiz_in + ny * perp_in
    return x, y


def template_score(patch_luma: np.ndarray, templ_luma: np.ndarray) -> float:
    """
    Normalised cross-correlation of two same-size luma patches, in [-1, 1].

    Used to verify that a candidate really looks like the reference widget
    (empty checkbox) rather than merely being a bright rectangle.
    """
    a = patch_luma.astype(np.float64).ravel()
    b = templ_luma.astype(np.float64).ravel()
    if a.size != b.size or a.size == 0:
        return -1.0
    a = a - a.mean()
    b = b - b.mean()
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    if na < 1e-9 or nb < 1e-9:
        return -1.0
    return float(np.dot(a, b) / (na * nb))


if __name__ == "__main__":
    # Self-check the primitives on synthetic data so the maths is verifiable
    # without a browser.
    luma = np.zeros((100, 100), dtype=np.uint8)
    luma[20:60, 10:70] = 230
    luma = np.clip(luma.astype(int) + np.random.randint(-3, 4, luma.shape), 0, 255).astype(np.uint8)
    print("otsu:", otsu_threshold(luma))

    rgb = np.dstack([luma, luma, luma])
    m = box_blur(bright_mask(rgb, luma))
    comps = connected_components(m, min_area=50)
    print("components:", [(c["width"], c["height"], round(c["fill"], 2)) for c in comps[:3]])

    if comps:
        print("top-edge slope:", round(fit_edge_angle(m, comps[0]), 5))
        print("inset point:", tuple(round(v, 1) for v in
                                    inset_along_rotation(comps[0]["x0"],
                                                         comps[0]["y0"], 0.0, 30, 30)))
