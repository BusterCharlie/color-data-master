# SPDX-License-Identifier: GPL-3.0-or-later
"""Sources and destinations for the transfer tools.

A layer is a (kind, name) pair:
  ATTR    color attribute       IMAGE   image
  VGROUP  vertex group          UV      UV map
  NORMALS custom normals (no name)

Data is read as RGBA in either the CORNER domain (n_loops, 4) or the
PIXEL domain (w*h, 4), and moved between the two through a UV map.
"""

import bpy
import numpy as np

from .core import (
    AttrTarget, ImageTarget, corner_selection_mask, corner_uvs,
    corners_to_points, loop_vertex_indices,
)
from . import texture

LAYER_KINDS = (
    ('ATTR', "Color", "Color attribute", 'GROUP_VCOL', 0),
    ('IMAGE', "Texture", "Image texture", 'IMAGE_DATA', 1),
    ('VGROUP', "Weights", "Vertex group weights", 'GROUP_VERTEX', 2),
    ('UV', "UV", "UV map coordinates", 'GROUP_UVS', 3),
    ('NORMALS', "Normals", "Custom normals", 'NORMALS_VERTEX_FACE', 4),
)
COLOR_KINDS = {'ATTR', 'IMAGE'}


class Layer:
    def __init__(self, context, kind, name):
        self.context = context
        self.obj = context.active_object
        self.mesh = self.obj.data
        self.kind = kind
        self.name = name

    # -- lookup ----------------------------------------------------------
    def resolve(self):
        """The underlying datablock, or None if it doesn't exist."""
        if self.kind == 'ATTR':
            return self.mesh.color_attributes.get(self.name)
        if self.kind == 'IMAGE':
            img = bpy.data.images.get(self.name)
            return img if img and img.size[0] > 0 else None
        if self.kind == 'VGROUP':
            return self.obj.vertex_groups.get(self.name)
        if self.kind == 'UV':
            return self.mesh.uv_layers.get(self.name)
        return self.mesh  # normals always exist

    def error(self):
        if self.resolve() is None:
            label = dict((k[0], k[1]) for k in LAYER_KINDS)[self.kind]
            return f"{label} layer '{self.name}' not found"
        return None

    @property
    def domain(self):
        return 'PIXEL' if self.kind == 'IMAGE' else 'CORNER'

    @property
    def size(self):
        return tuple(self.resolve().size) if self.kind == 'IMAGE' else None

    def target(self):
        if self.kind == 'ATTR':
            return AttrTarget(self.mesh, self.resolve())
        if self.kind == 'IMAGE':
            return ImageTarget(self.resolve())
        return None

    # -- read ------------------------------------------------------------
    def read(self, space):
        """RGBA in this layer's own domain."""
        if self.kind in COLOR_KINDS:
            return self.target().read(space)
        n = len(self.mesh.loops)
        out = np.zeros((n, 4), dtype=np.float32)
        out[:, 3] = 1.0
        if self.kind == 'VGROUP':
            idx = self.resolve().index
            weights = np.zeros(len(self.mesh.vertices), dtype=np.float32)
            for v in self.mesh.vertices:
                for g in v.groups:
                    if g.group == idx:
                        weights[v.index] = g.weight
                        break
            w = weights[loop_vertex_indices(self.mesh)]
            out[:, 0] = out[:, 1] = out[:, 2] = w
        elif self.kind == 'UV':
            uv = corner_uvs(self.mesh, self.name)
            out[:, :2] = np.mod(uv, 1.0)
        elif self.kind == 'NORMALS':
            nrm = np.empty(n * 3, dtype=np.float32)
            self.mesh.corner_normals.foreach_get("vector", nrm)
            out[:, :3] = nrm.reshape(-1, 3) * 0.5 + 0.5
        return out

    # -- write -----------------------------------------------------------
    def write(self, values, space, channels, mask=None):
        """values: RGBA in this layer's domain. channels: bool[4] of channels to change."""
        if self.kind in COLOR_KINDS:
            self.target().write(values, space, mask=mask, channels=channels)
            return
        if self.kind == 'VGROUP':
            ch = int(np.argmax(channels)) if channels.any() else 0
            pts, has = corners_to_points(self.mesh, values[:, ch], mask)
            group = self.resolve()
            for i in np.nonzero(has)[0]:
                group.add([int(i)], float(pts[i]), 'REPLACE')
        elif self.kind == 'UV':
            uv_layer = self.resolve()
            uv = corner_uvs(self.mesh, self.name)
            new = values[:, :2]
            if mask is not None:
                uv[mask] = new[mask]
            else:
                uv = new
            uv_layer.uv.foreach_set("vector", np.ascontiguousarray(uv, dtype=np.float32).ravel())
        elif self.kind == 'NORMALS':
            n = len(self.mesh.loops)
            current = np.empty(n * 3, dtype=np.float32)
            self.mesh.corner_normals.foreach_get("vector", current)
            current = current.reshape(-1, 3)
            new = values[:, :3] * 2.0 - 1.0
            length = np.linalg.norm(new, axis=1, keepdims=True)
            new = np.where(length > 1e-8, new / np.maximum(length, 1e-8), current)
            if mask is not None:
                current[mask] = new[mask]
                new = current
            self.mesh.normals_split_custom_set([tuple(v) for v in new])
        self.mesh.update()


def convert_domain(src, values, dst, settings):
    """Move `values` read from layer src into dst's domain.

    Returns (values, mask) where mask limits which elements of dst get written.
    """
    mesh = src.mesh
    sel = corner_selection_mask(mesh)
    uv_name = settings.uv_map or None
    if src.domain == 'CORNER' and dst.domain == 'CORNER':
        return values, sel
    if src.domain == 'PIXEL' and dst.domain == 'CORNER':
        sampled = texture.sample_at_corners(mesh, values, src.size, uv_name)
        if sampled is None:
            raise ValueError("Mesh has no UV map to sample the texture with")
        return sampled, sel
    if src.domain == 'CORNER' and dst.domain == 'PIXEL':
        result = texture.rasterize_corners(mesh, values, dst.size, uv_name, sel)
        if result is None:
            raise ValueError("Mesh has no UV map to draw into the texture with")
        pixels, covered = texture.dilate(*result, dst.size, settings.bake_padding)
        return pixels, covered
    return texture.resample(values, src.size, dst.size), None


def pixel_selection_mask(context, image_target, settings):
    """Face selection mask drawn into pixel space, for Texture Paint with face masking."""
    obj = context.active_object
    mesh = obj.data
    if not mesh.use_paint_mask:
        return None
    sel = corner_selection_mask(mesh)
    ones = np.ones((len(mesh.loops), 1), dtype=np.float32)
    result = texture.rasterize_corners(mesh, ones, image_target.size, settings.uv_map or None, sel)
    return None if result is None else result[1]
