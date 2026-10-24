# SPDX-License-Identifier: GPL-3.0-or-later
"""Moving per-corner mesh data to and from image pixels through a UV map."""

import numpy as np

from .core import corner_uvs


def _loop_triangles(mesh):
    mesh.calc_loop_triangles()
    tris = np.empty(len(mesh.loop_triangles) * 3, dtype=np.int32)
    mesh.loop_triangles.foreach_get("loops", tris)
    return tris.reshape(-1, 3)


def rasterize_corners(mesh, values, size, uv_name=None, corner_mask=None):
    """Draw per-corner values into an image-sized buffer, interpolating across triangles.

    Returns (pixels (w*h, C), covered (w*h,) bool) or None when the mesh has no UVs.
    UV islands outside 0-1 are wrapped back into the 0-1 tile.
    """
    uv = corner_uvs(mesh, uv_name)
    if uv is None:
        return None
    w, h = size
    values = np.asarray(values, dtype=np.float32)
    nch = values.shape[1]
    out = np.zeros((h, w, nch), dtype=np.float32)
    covered = np.zeros((h, w), dtype=bool)

    tris = _loop_triangles(mesh)
    if corner_mask is not None:
        tris = tris[corner_mask[tris].all(axis=1)]

    px = uv * np.array([w, h], dtype=np.float32)
    for tri in tris:
        p = px[tri]
        # wrap the triangle into the first tile
        offset = np.floor(p.mean(axis=0) / np.array([w, h])) * np.array([w, h])
        p = p - offset
        x0 = max(int(np.floor(p[:, 0].min())), 0)
        x1 = min(int(np.ceil(p[:, 0].max())), w)
        y0 = max(int(np.floor(p[:, 1].min())), 0)
        y1 = min(int(np.ceil(p[:, 1].max())), h)
        if x0 >= x1 or y0 >= y1:
            continue
        xs, ys = np.meshgrid(np.arange(x0, x1) + 0.5, np.arange(y0, y1) + 0.5)
        (ax, ay), (bx, by), (cx, cy) = p
        det = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy)
        if abs(det) < 1e-12:
            continue
        l0 = ((by - cy) * (xs - cx) + (cx - bx) * (ys - cy)) / det
        l1 = ((cy - ay) * (xs - cx) + (ax - cx) * (ys - cy)) / det
        l2 = 1.0 - l0 - l1
        eps = -1e-4
        inside = (l0 >= eps) & (l1 >= eps) & (l2 >= eps)
        if not inside.any():
            continue
        v = values[tri]
        interp = l0[..., None] * v[0] + l1[..., None] * v[1] + l2[..., None] * v[2]
        region = out[y0:y1, x0:x1]
        region[inside] = interp[inside]
        covered[y0:y1, x0:x1] |= inside

    return out.reshape(-1, nch), covered.ravel()


def dilate(pixels, covered, size, iterations):
    """Bleed covered pixels outward by `iterations` pixels to hide UV seams."""
    if iterations <= 0:
        return pixels, covered
    w, h = size
    img = pixels.reshape(h, w, -1).copy()
    cov = covered.reshape(h, w).copy()
    for _ in range(iterations):
        acc = np.zeros_like(img)
        cnt = np.zeros((h, w), dtype=np.float32)
        for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            shifted = np.zeros_like(img)
            scov = np.zeros_like(cov)
            ys = slice(max(dy, 0), h + min(dy, 0))
            yd = slice(max(-dy, 0), h + min(-dy, 0))
            xs = slice(max(dx, 0), w + min(dx, 0))
            xd = slice(max(-dx, 0), w + min(-dx, 0))
            shifted[yd, xd] = img[ys, xs]
            scov[yd, xd] = cov[ys, xs]
            acc += shifted * scov[..., None]
            cnt += scov
        grow = (~cov) & (cnt > 0)
        if not grow.any():
            break
        img[grow] = acc[grow] / cnt[grow][:, None]
        cov |= grow
    return img.reshape(pixels.shape), cov.ravel()


def sample_at_corners(mesh, pixels, size, uv_name=None):
    """Bilinear sample image pixels at each corner's UV (repeat wrap). None without UVs."""
    uv = corner_uvs(mesh, uv_name)
    if uv is None:
        return None
    w, h = size
    img = pixels.reshape(h, w, -1)
    x = uv[:, 0] * w - 0.5
    y = uv[:, 1] * h - 0.5
    x0 = np.floor(x).astype(np.int64)
    y0 = np.floor(y).astype(np.int64)
    fx = (x - x0)[:, None]
    fy = (y - y0)[:, None]
    x1 = (x0 + 1) % w
    y1 = (y0 + 1) % h
    x0 %= w
    y0 %= h
    top = img[y0, x0] * (1 - fx) + img[y0, x1] * fx
    bottom = img[y1, x0] * (1 - fx) + img[y1, x1] * fx
    return (top * (1 - fy) + bottom * fy).astype(np.float32)


def resample(pixels, src_size, dst_size):
    """Nearest-neighbour resize between image sizes."""
    if tuple(src_size) == tuple(dst_size):
        return pixels
    sw, sh = src_size
    dw, dh = dst_size
    img = pixels.reshape(sh, sw, -1)
    xs = ((np.arange(dw) + 0.5) * sw / dw).astype(np.int64).clip(0, sw - 1)
    ys = ((np.arange(dh) + 0.5) * sh / dh).astype(np.int64).clip(0, sh - 1)
    return img[ys][:, xs].reshape(dw * dh, -1)


def blur_pixels(pixels, size, iterations, factor):
    """Repeated 3x3 box blur, blended by factor."""
    w, h = size
    img = pixels.reshape(h, w, -1).astype(np.float32)
    for _ in range(iterations):
        p = np.pad(img, ((1, 1), (1, 1), (0, 0)), mode='edge')
        box = sum(p[dy:dy + h, dx:dx + w] for dy in range(3) for dx in range(3)) / 9.0
        img = img * (1 - factor) + box * factor
    return img.reshape(pixels.shape)
