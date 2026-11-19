"""Shade from photos (Nov 19, 2026): which Sephora SKU an Amazon listing's photos show, when the text can't tell.

Two comparisons per Sephora SKU, on images already downloaded (see fetch helpers):
  render  - Sephora's SKU product photo vs each Amazon photo, both cropped to the product and shrunk to 24x24 colour
            thumbnails. Brands give Amazon and Sephora the same renders, so the right shade is usually a near copy.
  swatch  - Sephora's colour chip (s<sku>+sw.jpg) vs the main colours of the Amazon photos (white background left out):
            distance (CIE76 delta E) from the chip to the closest main colour.
resolve(amazon_paths, sku_imgs) -> {sku_id, method, score, margin, ranked}; sku_imgs = {sku_id: {"main": path, "sw": path}}.
Only the render comparison picks (distance < 4 and 20% better than the next SKU: 96% right on Nick's leads); the
swatch colour is returned as a hint for review. Else None ("photos can't decide"). Needs Pillow and numpy.
"""
from __future__ import annotations

import os

import numpy as np
from PIL import Image

BG_L, BG_C = 90.0, 8.0     # near-white background: L* above 90 and chroma below 8


def _lab(rgb: np.ndarray) -> np.ndarray:
    c = rgb.astype(np.float64) / 255.0
    c = np.where(c > 0.04045, ((c + 0.055) / 1.055) ** 2.4, c / 12.92)
    m = np.array([[0.4124, 0.3576, 0.1805], [0.2126, 0.7152, 0.0722], [0.0193, 0.1192, 0.9505]])
    xyz = c @ m.T / np.array([0.95047, 1.0, 1.08883])
    f = np.where(xyz > 0.008856, np.cbrt(xyz), 7.787 * xyz + 16 / 116)
    return np.stack([116 * f[..., 1] - 16, 500 * (f[..., 0] - f[..., 1]), 200 * (f[..., 1] - f[..., 2])], -1)


def _load(path, size=None):
    try:
        im = Image.open(path).convert("RGB")
    except Exception:  # noqa: BLE001
        return None
    if size:
        im.thumbnail((size, size))
    return np.asarray(im)


def _fg_mask(lab):
    chroma = np.hypot(lab[..., 1], lab[..., 2])
    return ~((lab[..., 0] > BG_L) & (chroma < BG_C))


def _crop_thumb(arr, n=24):
    lab = _lab(arr)
    m = _fg_mask(lab)
    if m.sum() < 50:
        return None
    ys, xs = np.where(m)
    a = arr[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    im = Image.fromarray(a).resize((n, n), Image.BILINEAR)
    return _lab(np.asarray(im))


def _main_colours(arr, k=6, iters=8):
    lab = _lab(arr).reshape(-1, 3)
    lab = lab[_fg_mask(lab)]
    if len(lab) < 50:
        return []
    rng = np.random.default_rng(0)
    if len(lab) > 6000:
        lab = lab[rng.choice(len(lab), 6000, replace=False)]
    cen = lab[rng.choice(len(lab), min(k, len(lab)), replace=False)]
    for _ in range(iters):
        d = ((lab[:, None, :] - cen[None]) ** 2).sum(-1)
        a = d.argmin(1)
        cen = np.array([lab[a == i].mean(0) if (a == i).any() else cen[i] for i in range(len(cen))])
    share = np.bincount(a, minlength=len(cen)) / len(lab)
    return [(cen[i], share[i]) for i in range(len(cen)) if share[i] >= 0.04]


def _chip(path):
    arr = _load(path)
    if arr is None:
        return None
    h, w = arr.shape[:2]
    return _lab(arr[h // 4:3 * h // 4, w // 4:3 * w // 4]).reshape(-1, 3).mean(0)


def resolve(amazon_paths: list, sku_imgs: dict, min_margin_render=0.20, max_render=4.0) -> dict:
    amz = [a for a in (_load(p, 300) for p in amazon_paths if p and os.path.exists(p)) if a is not None]
    if not amz or not sku_imgs:
        return {"sku_id": None, "method": None, "how": "no photos"}
    a_th = [t for t in (_crop_thumb(a) for a in amz) if t is not None]
    a_col = [c for a in amz[:3] for c in _main_colours(a)]
    render, swatch = [], []
    for sid, d in sku_imgs.items():
        m = _load(d.get("main"), 300) if d.get("main") else None
        t = _crop_thumb(m) if m is not None else None
        if t is not None and a_th:
            render.append((min(float(np.sqrt(((t - x) ** 2).sum(-1)).mean()) for x in a_th), sid))
        c = _chip(d.get("sw")) if d.get("sw") else None
        if c is not None and a_col:
            swatch.append((min(float(np.sqrt(((c - cc) ** 2).sum())) for cc, _ in a_col), sid))
    out = {"sku_id": None, "method": None, "how": "photos can't decide"}
    render.sort(); swatch.sort()
    if len(render) >= 2:
        best, nxt = render[0][0], render[1][0]
        rel = (nxt - best) / max(nxt, 1e-6)
        out["render"] = [(round(x, 2), s) for x, s in render[:4]]
        if rel >= min_margin_render and best < max_render:
            out.update(sku_id=render[0][1], method="render", score=round(best, 2), margin=round(rel, 3),
                       how=f"Amazon photo matches Sephora's {render[0][1]} photo (distance {best:.1f}, next {nxt:.1f})")
    # the swatch colour is only a hint for review: on Nick's leads it picked the right shade 5 times in 21
    if len(swatch) >= 2:
        out["swatch"] = [(round(x, 2), s) for x, s in swatch[:4]]
        if out["sku_id"] is None:
            out["hint"] = swatch[0][1]
    return out
