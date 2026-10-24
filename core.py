# SPDX-License-Identifier: GPL-3.0-or-later
"""Uniform access to color data, whatever it is stored in.

Every editable color source is wrapped as a *target*:
  AttrTarget  - a mesh color attribute (byte or float, point or corner domain)
  ImageTarget - an image's pixels

Mesh-side data is always exchanged as per-corner (loop) RGBA arrays, image data
as per-pixel RGBA arrays. Converting between the two goes through UVs
(see texture.py).

Value spaces:
  'AUTO'   - the numbers as stored (sRGB for byte layers / sRGB byte images,
             linear for float layers and images). Right for packed mask data.
  'LINEAR' - scene linear values, what shaders read.
  'SRGB'   - sRGB encoded values, what color pickers show.
"""

import numpy as np

CHANNELS = 'RGBA'
ISO_PREFIX = "CDM-ISO"


# ---------------------------------------------------------------------------
# Color space math

def srgb_to_linear(c):
    c = np.asarray(c, dtype=np.float32)
    return np.where(c <= 0.04045, c / 12.92,
                    np.power((np.maximum(c, 0.0) + 0.055) / 1.055, 2.4)).astype(np.float32)


def linear_to_srgb(c):
    c = np.asarray(c, dtype=np.float32)
    return np.where(c <= 0.0031308, c * 12.92,
                    1.055 * np.power(np.maximum(c, 0.0), 1.0 / 2.4) - 0.055).astype(np.float32)


def convert_rgba(rgba, from_space, to_space):
    """Convert the RGB part of an (N, 4) array between 'LINEAR' and 'SRGB'."""
    if from_space == to_space:
        return rgba
    out = rgba.copy()
    if to_space == 'LINEAR':
        out[:, :3] = srgb_to_linear(rgba[:, :3])
    else:
        out[:, :3] = linear_to_srgb(rgba[:, :3])
    return out


def channel_index(channel_id):
    return CHANNELS.index(channel_id) if channel_id in CHANNELS else 0


def channel_mask(active_channels):
    return np.array([c in active_channels for c in CHANNELS], dtype=bool)


# ---------------------------------------------------------------------------
# Mesh helpers

def loop_vertex_indices(mesh):
    vi = np.empty(len(mesh.loops), dtype=np.int32)
    mesh.loops.foreach_get("vertex_index", vi)
    return vi


def loop_face_indices(mesh):
    totals = np.empty(len(mesh.polygons), dtype=np.int32)
    mesh.polygons.foreach_get("loop_total", totals)
    return np.repeat(np.arange(len(mesh.polygons), dtype=np.int32), totals)


def corner_selection_mask(mesh):
    """Per-corner bool mask from the paint selection mask, or None when unmasked."""
    if mesh.use_paint_mask:
        sel = np.empty(len(mesh.polygons), dtype=bool)
        mesh.polygons.foreach_get("select", sel)
        return sel[loop_face_indices(mesh)]
    if mesh.use_paint_mask_vertex:
        sel = np.empty(len(mesh.vertices), dtype=bool)
        mesh.vertices.foreach_get("select", sel)
        return sel[loop_vertex_indices(mesh)]
    return None


def corner_uvs(mesh, uv_name=None):
    uv_layer = mesh.uv_layers.get(uv_name) if uv_name else mesh.uv_layers.active
    if uv_layer is None:
        return None
    uv = np.empty(len(mesh.loops) * 2, dtype=np.float32)
    uv_layer.uv.foreach_get("vector", uv)
    return uv.reshape(-1, 2)


def corners_to_points(mesh, values, mask=None):
    """Average per-corner values into per-vertex values. Returns (values, has_value)."""
    vi = loop_vertex_indices(mesh)
    if mask is not None:
        vi = vi[mask]
        values = values[mask]
    n = len(mesh.vertices)
    sums = np.zeros((n,) + values.shape[1:], dtype=np.float64)
    counts = np.zeros(n, dtype=np.int32)
    np.add.at(sums, vi, values)
    np.add.at(counts, vi, 1)
    has = counts > 0
    shape = (-1,) + (1,) * (values.ndim - 1)
    sums[has] /= counts[has].reshape(shape)
    return sums.astype(np.float32), has


# ---------------------------------------------------------------------------
# Targets

class AttrTarget:
    kind = 'ATTR'

    def __init__(self, mesh, attr):
        self.mesh = mesh
        self.attr = attr
        self.name = attr.name

    @property
    def is_byte(self):
        return self.attr.data_type == 'BYTE_COLOR'

    @property
    def stored_space(self):
        return 'SRGB' if self.is_byte else 'LINEAR'

    def resolve(self, space):
        return self.stored_space if space == 'AUTO' else space

    def _prop(self, space):
        return 'color_srgb' if self.resolve(space) == 'SRGB' else 'color'

    def read_raw(self, space='AUTO'):
        """Values in the attribute's own domain (points or corners)."""
        n = len(self.attr.data)
        buf = np.empty(n * 4, dtype=np.float32)
        self.attr.data.foreach_get(self._prop(space), buf)
        return buf.reshape(n, 4)

    def read(self, space='AUTO'):
        """Per-corner RGBA."""
        raw = self.read_raw(space)
        if self.attr.domain == 'POINT':
            return raw[loop_vertex_indices(self.mesh)]
        return raw

    def write(self, corners, space='AUTO', mask=None, channels=None):
        """Write per-corner RGBA, limited to masked corners and the given channels."""
        corners = np.asarray(corners, dtype=np.float32)
        if channels is not None and not channels.all():
            current = self.read(space)
            corners = np.where(channels[None, :], corners, current)
        if self.attr.domain == 'POINT':
            raw = self.read_raw(space)
            pts, has = corners_to_points(self.mesh, corners, mask)
            raw[has] = pts[has]
        else:
            if mask is not None:
                raw = self.read_raw(space)
                raw[mask] = corners[mask]
            else:
                raw = corners
        self.attr.data.foreach_set(self._prop(space), np.ascontiguousarray(raw).ravel())
        self.mesh.update()


class ImageTarget:
    kind = 'IMAGE'

    def __init__(self, image):
        self.image = image
        self.name = image.name

    @property
    def size(self):
        return tuple(self.image.size)

    @property
    def is_byte(self):
        return not self.image.is_float

    @property
    def stored_space(self):
        if self.is_byte and self.image.colorspace_settings.name == 'sRGB':
            return 'SRGB'
        return 'LINEAR'

    def resolve(self, space):
        return self.stored_space if space == 'AUTO' else space

    def read(self, space='AUTO'):
        w, h = self.size
        buf = np.empty(w * h * 4, dtype=np.float32)
        self.image.pixels.foreach_get(buf)
        return convert_rgba(buf.reshape(-1, 4), self.stored_space, self.resolve(space))

    def write(self, pixels, space='AUTO', mask=None, channels=None):
        pixels = np.asarray(pixels, dtype=np.float32)
        if channels is not None and not channels.all() or mask is not None:
            current = self.read(space)
            if channels is not None:
                pixels = np.where(channels[None, :], pixels, current)
            if mask is not None:
                current[mask] = pixels[mask]
                pixels = current
        pixels = convert_rgba(pixels, self.resolve(space), self.stored_space)
        if self.is_byte:
            pixels = np.clip(pixels, 0.0, 1.0)
        self.image.pixels.foreach_set(np.ascontiguousarray(pixels).ravel())
        self.image.update()


def paint_image(context, obj):
    """The image Texture Paint would paint on."""
    ip = context.tool_settings.image_paint
    if ip.mode == 'IMAGE':
        return ip.canvas
    mat = obj.active_material if obj else None
    if mat and len(mat.texture_paint_images):
        idx = min(mat.paint_active_slot, len(mat.texture_paint_images) - 1)
        return mat.texture_paint_images[idx]
    return None


def active_target(context):
    """The color data the channel tools act on, chosen by mode or the Edit Target setting."""
    obj = context.active_object
    if obj is None or obj.type != 'MESH':
        return None
    settings = context.scene.color_data_master
    use_image = settings.edit_target == 'IMAGE' or (
        settings.edit_target == 'AUTO' and obj.mode == 'TEXTURE_PAINT')
    if use_image:
        img = paint_image(context, obj)
        return ImageTarget(img) if img and img.size[0] > 0 else None
    attr = obj.data.color_attributes.active_color
    return AttrTarget(obj.data, attr) if attr else None


def isolated_info(target):
    """(source name, channel id) when the target is an isolate layer, else None."""
    if target is None:
        return None
    name = target.name
    plen = len(ISO_PREFIX)
    if name.startswith(ISO_PREFIX + "_") and len(name) > plen + 3 and name[plen + 2] == "_":
        ch = name[plen + 1]
        if ch in CHANNELS:
            return name[plen + 3:], ch
    return None


# ---------------------------------------------------------------------------
# Paint settings (brush colors live in unified settings by default since 5.x)

def paint_settings(context):
    obj = context.active_object
    ts = context.tool_settings
    if obj and obj.mode == 'TEXTURE_PAINT':
        return ts.image_paint
    return ts.vertex_paint


def color_owner(context):
    ps = paint_settings(context)
    ups = ps.unified_paint_settings
    if ups.use_unified_color or ps.brush is None:
        return ups
    return ps.brush


def value_to_linear(value, target, space):
    """A gray value typed by the user in `space` -> linear brush value."""
    resolved = target.resolve(space) if target else ('LINEAR' if space == 'AUTO' else space)
    if resolved == 'SRGB':
        return float(srgb_to_linear(value))
    return float(value)


def value_from_linear(value, target, space):
    resolved = target.resolve(space) if target else ('LINEAR' if space == 'AUTO' else space)
    if resolved == 'SRGB':
        return float(linear_to_srgb(value))
    return float(value)


def poll_mesh(context):
    obj = context.active_object
    return (obj is not None and obj.type == 'MESH'
            and obj.mode in {'OBJECT', 'VERTEX_PAINT', 'TEXTURE_PAINT'})
