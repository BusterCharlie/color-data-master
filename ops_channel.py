# SPDX-License-Identifier: GPL-3.0-or-later
"""Channel edits on the active target, and transfers between layers."""

import bpy
import numpy as np
from bpy.props import (
    BoolProperty, BoolVectorProperty, EnumProperty, FloatProperty, FloatVectorProperty, IntProperty,
    StringProperty,
)

from .core import (
    ImageTarget, active_target, channel_index, channel_mask, color_owner, corner_selection_mask,
    isolated_info, poll_mesh,
)
from .data import COLOR_KINDS, Layer, convert_domain, pixel_selection_mask
from .props import KIND_CHANNEL_ITEMS, kind_channel
from . import texture

LUMA = np.array([0.299, 0.587, 0.114], dtype=np.float32)


def blend(s, d, mode):
    """Apply source s onto destination d, like a layer blend mode (d is the base)."""
    if mode == 'ADD':
        return np.clip(d + s, 0.0, 1.0)
    if mode == 'SUB':
        return np.clip(d - s, 0.0, 1.0)
    if mode == 'MUL':
        return d * s
    if mode == 'DIV':
        # dividing by zero leaves the destination unchanged
        return np.clip(np.where(s == 0.0, d, d / np.where(s == 0.0, 1.0, s)), 0.0, 1.0)
    if mode == 'LIGHTEN':
        return np.maximum(d, s)
    if mode == 'DARKEN':
        return np.minimum(d, s)
    return s + d * 0.0  # REPLACE, broadcast to d's shape


def min_weight(settings):
    """Threshold for Layer.write on vertex groups, or None to keep every vertex."""
    return settings.min_weight if settings.skip_low_weights else None


def edit_mask(context, target, settings):
    """Selection mask for the target in its own domain, only in paint modes."""
    obj = context.active_object
    if target.kind == 'ATTR':
        return corner_selection_mask(target.mesh) if obj.mode == 'VERTEX_PAINT' else None
    if obj.mode == 'TEXTURE_PAINT':
        return pixel_selection_mask(context, target, settings)
    return None


def write_corner_values(context, target, corners, channels, space, corner_mask):
    """Write per-corner RGBA to the target, drawing through UVs when it's a texture."""
    settings = context.scene.color_data_master
    if target.kind == 'ATTR':
        target.write(corners, space, mask=corner_mask, channels=channels)
        return None
    mesh = context.active_object.data
    result = texture.rasterize_corners(mesh, corners, target.size, settings.uv_map or None, corner_mask)
    if result is None:
        return "Mesh has no UV map to draw into the texture with"
    pixels, covered = texture.dilate(*result, target.size, settings.bake_padding)
    target.write(pixels, space, mask=covered, channels=channels)
    return None


class ChannelOperator:
    """Shared setup: resolve target, channels and selection mask."""
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return poll_mesh(context)

    def setup(self, context):
        settings = context.scene.color_data_master
        target = active_target(context)
        if target is None:
            self.report({'ERROR'}, "No color attribute or paint texture to edit")
            return None
        iso = isolated_info(target)
        channels = channel_mask('RGB' if iso else settings.active_channels)
        return settings, target, channels, edit_mask(context, target, settings)


class CDM_OT_fill(ChannelOperator, bpy.types.Operator):
    """Fill the active channels with a value, or the RGB channels with a color"""
    bl_idname = "color_data_master.fill"
    bl_label = "Fill Channels"

    value: FloatProperty(name="Value", default=1.0, min=0.0, max=1.0)
    use_color: BoolProperty(name="Fill with Color", default=False,
                            description="Ignore active channels and fill RGB with a color")
    color: FloatVectorProperty(name="Color", subtype='COLOR', default=(1.0, 1.0, 1.0),
                               min=0.0, max=1.0)

    def execute(self, context):
        prep = self.setup(context)
        if prep is None:
            return {'CANCELLED'}
        settings, target, channels, mask = prep
        if self.use_color and not isolated_info(target):
            space = 'LINEAR'
            values = target.read(space)
            values[:, :3] = self.color
            channels = channel_mask('RGB')
        else:
            space = settings.value_space
            values = target.read(space)
            values[:] = self.value
        target.write(values, space, mask=mask, channels=channels)
        return {'FINISHED'}

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "value", slider=True)
        layout.prop(self, "use_color")
        if self.use_color:
            layout.prop(self, "color", text="")


class CDM_OT_fill_brush_color(ChannelOperator, bpy.types.Operator):
    """Fill RGB with the brush color, respecting the selection mask"""
    bl_idname = "color_data_master.fill_brush_color"
    bl_label = "Fill with Brush Color"

    def execute(self, context):
        prep = self.setup(context)
        if prep is None:
            return {'CANCELLED'}
        _settings, target, _channels, mask = prep
        values = target.read('LINEAR')
        values[:, :3] = color_owner(context).color
        target.write(values, 'LINEAR', mask=mask, channels=channel_mask('RGB'))
        return {'FINISHED'}


class CDM_OT_invert(ChannelOperator, bpy.types.Operator):
    """Invert the active channels"""
    bl_idname = "color_data_master.invert"
    bl_label = "Invert Channels"

    def execute(self, context):
        prep = self.setup(context)
        if prep is None:
            return {'CANCELLED'}
        settings, target, channels, mask = prep
        values = 1.0 - target.read(settings.value_space)
        target.write(values, settings.value_space, mask=mask, channels=channels)
        return {'FINISHED'}


class CDM_OT_posterize(ChannelOperator, bpy.types.Operator):
    """Reduce the active channels to a number of evenly spaced values"""
    bl_idname = "color_data_master.posterize"
    bl_label = "Posterize Channels"

    steps: IntProperty(name="Steps", default=2, min=2, max=256,
                       description="Number of distinct values to keep")

    def execute(self, context):
        prep = self.setup(context)
        if prep is None:
            return {'CANCELLED'}
        settings, target, channels, mask = prep
        n = self.steps - 1
        values = np.round(target.read(settings.value_space) * n) / n
        target.write(values, settings.value_space, mask=mask, channels=channels)
        return {'FINISHED'}


class CDM_OT_remap(ChannelOperator, bpy.types.Operator):
    """Remap the active channels from one range to another"""
    bl_idname = "color_data_master.remap"
    bl_label = "Remap Channels"

    min0: FloatProperty(name="Input Min", default=0.0, min=0.0, max=1.0)
    max0: FloatProperty(name="Input Max", default=1.0, min=0.0, max=1.0)
    min1: FloatProperty(name="Output Min", default=0.0, min=0.0, max=1.0)
    max1: FloatProperty(name="Output Max", default=1.0, min=0.0, max=1.0)
    clamp: BoolProperty(name="Clamp", default=True, description="Clamp results to the output range")
    auto_range: BoolProperty(
        name="Auto Range", default=False,
        description="Use the lowest and highest values found in the active channels "
                    "(within the selection) as the input range")
    per_channel: BoolProperty(
        name="Per Channel", default=True,
        description="Find the input range of each active channel separately, "
                    "instead of one range shared by all of them")

    def execute(self, context):
        prep = self.setup(context)
        if prep is None:
            return {'CANCELLED'}
        settings, target, channels, mask = prep
        values = target.read(settings.value_space)
        if self.auto_range:
            lo0, hi0 = value_range(values, mask, channels, self.per_channel)
            # show the measured range in the redo panel
            used = lo0[channels]
            if len(used):
                self.min0, self.max0 = float(used.min()), float(hi0[channels].max())
        else:
            lo0 = np.full(4, self.min0, dtype=np.float32)
            hi0 = np.full(4, self.max0, dtype=np.float32)
        r0 = hi0 - lo0
        flat = r0 == 0.0
        out = (values - lo0) * (self.max1 - self.min1) / np.where(flat, 1.0, r0) + self.min1
        # a channel with no spread maps to output min (manual) or stays unchanged (auto)
        out[:, flat] = values[:, flat] if self.auto_range else self.min1
        if self.clamp:
            lo, hi = sorted((self.min1, self.max1))
            out = np.clip(out, lo, hi)
        target.write(out, settings.value_space, mask=mask, channels=channels)
        return {'FINISHED'}

    def draw(self, context):
        layout = self.layout
        row = layout.row(align=True)
        row.prop(self, "auto_range")
        sub = row.row(align=True)
        sub.prop(self, "per_channel")
        sub.enabled = self.auto_range
        col = layout.column(align=True)
        col.enabled = not self.auto_range
        col.label(text="Input Range")
        col.prop(self, "min0", text="Min", slider=True)
        col.prop(self, "max0", text="Max", slider=True)
        col = layout.column(align=True)
        col.label(text="Output Range")
        col.prop(self, "min1", text="Min", slider=True)
        col.prop(self, "max1", text="Max", slider=True)
        layout.prop(self, "clamp")


def value_range(values, mask, channels, per_channel):
    """(low, high) per RGBA channel over the masked elements of the active channels."""
    sel = values if mask is None else values[mask]
    if len(sel) == 0:
        return np.zeros(4, dtype=np.float32), np.ones(4, dtype=np.float32)
    lo, hi = sel.min(axis=0), sel.max(axis=0)
    if not per_channel and channels.any():
        lo = np.full(4, lo[channels].min(), dtype=np.float32)
        hi = np.full(4, hi[channels].max(), dtype=np.float32)
    return lo.astype(np.float32), hi.astype(np.float32)


RGBA_ITEMS = [(c, c, f"The {c} channel") for c in "RGBA"]


class CDM_OT_normalize_mask(ChannelOperator, bpy.types.Operator):
    """Scale R, G, B and A so they add up to 1, for 4-layer blend masks"""
    bl_idname = "color_data_master.normalize_mask"
    bl_label = "Normalize Blend Mask"

    method: EnumProperty(
        name="Method",
        items=[
            ('EQUAL', "Equal", "All four channels have the same priority and are scaled to add up to 1"),
            ('REMAINDER', "Remainder", "The other three are scaled down to at most 1 in total, "
                                        "and one channel gets what is left"),
            ('STACK', "Stack", "Channels are layered in a priority order: a higher channel takes "
                               "its share first and the ones below it only keep what is left"),
        ],
        default='EQUAL',
    )
    remainder: EnumProperty(name="Remainder", items=RGBA_ITEMS, default='A',
                            description="The channel that gets what the others leave")
    fill: EnumProperty(
        name="Empty",
        items=[('NONE', "Leave", "Leave areas where every channel is 0 black")] + RGBA_ITEMS,
        default='NONE',
        description="Where every channel is 0, set this channel to 1",
    )
    order_1: EnumProperty(name="Top", items=RGBA_ITEMS, default='A')
    order_2: EnumProperty(name="2nd", items=RGBA_ITEMS, default='B')
    order_3: EnumProperty(name="3rd", items=RGBA_ITEMS, default='G')
    order_4: EnumProperty(name="Bottom", items=RGBA_ITEMS, default='R')
    hard: BoolProperty(
        name="Hard", default=False,
        description="Clear the lower channels completely wherever a higher one is above the cutoff, "
                    "instead of letting them keep what is left",
    )
    cutoff: FloatProperty(name="Cutoff", default=0.0, min=0.0, max=1.0,
                          description="A higher channel overrides the ones below it where it is above this")
    bottom_fills: BoolProperty(
        name="Bottom Fills Rest", default=False,
        description="Set the bottom channel to whatever the channels above it leave, so all four add up to 1",
    )

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.row().prop(self, "method", expand=True)
        if self.method == 'EQUAL':
            layout.row().prop(self, "fill", expand=True)
        elif self.method == 'REMAINDER':
            layout.row().prop(self, "remainder", expand=True)
        else:
            for i in range(1, 5):
                layout.row().prop(self, f"order_{i}", expand=True)
            row = layout.row()
            row.prop(self, "hard")
            sub = row.row()
            sub.active = self.hard
            sub.prop(self, "cutoff")
            layout.prop(self, "bottom_fills")

    def execute(self, context):
        prep = self.setup(context)
        if prep is None:
            return {'CANCELLED'}
        settings, target, _channels, mask = prep
        if isolated_info(target):
            self.report({'ERROR'}, "Apply or discard the isolated channel first")
            return {'CANCELLED'}
        values = target.read(settings.value_space)
        if self.method == 'STACK':
            order = [getattr(self, f"order_{i}") for i in range(1, 5)]
            if len(set(order)) != 4:
                self.report({'ERROR'}, "Use each channel once in the stack order")
                return {'CANCELLED'}
            out = stack_weights(values, order, self.hard, self.cutoff, self.bottom_fills)
        else:
            out = normalize_weights(values, self.method, self.remainder, self.fill)
        target.write(out, settings.value_space, mask=mask)
        return {'FINISHED'}


def normalize_weights(values, method, remainder='A', fill='NONE'):
    """Make the four channels of each row add up to 1 (see CDM_OT_normalize_mask)."""
    w = np.clip(values, 0.0, 1.0).astype(np.float32)
    total = w.sum(axis=1, keepdims=True)
    if method == 'EQUAL':
        empty = total[:, 0] < 1e-6
        w = np.where(total < 1e-6, 0.0, w / np.maximum(total, 1e-6))
        if fill != 'NONE':
            w[empty, "RGBA".index(fill)] = 1.0
        return w.astype(np.float32)
    r = "RGBA".index(remainder)
    others = [i for i in range(4) if i != r]
    rest = w[:, others]
    rest_total = rest.sum(axis=1, keepdims=True)
    rest = np.where(rest_total > 1.0, rest / np.maximum(rest_total, 1e-8), rest)
    w[:, others] = rest
    w[:, r] = np.clip(1.0 - rest.sum(axis=1), 0.0, 1.0)
    return w


def stack_weights(values, order, hard=False, cutoff=0.0, bottom_fills=False):
    """Layer the channels top to bottom (order = channel letters, top first) so they never add up past 1."""
    w = np.clip(values, 0.0, 1.0).astype(np.float32)
    idx = ["RGBA".index(c) for c in order]
    left = np.ones(len(w), dtype=np.float32)
    covered = np.zeros(len(w), dtype=bool)
    for i in idx:
        v = np.where(covered, 0.0, w[:, i]) if hard else w[:, i]
        v = np.minimum(v, left)
        w[:, i] = v
        left -= v
        if hard:
            covered |= v > cutoff
    if bottom_fills:
        w[:, idx[-1]] += left
    return w


class CDM_OT_blur(ChannelOperator, bpy.types.Operator):
    """Blur the active channels across the mesh or texture"""
    bl_idname = "color_data_master.blur"
    bl_label = "Blur Channels"

    factor: FloatProperty(name="Factor", default=0.5, min=0.0, max=1.0)
    iterations: IntProperty(name="Iterations", default=1, min=1, max=200)

    def execute(self, context):
        prep = self.setup(context)
        if prep is None:
            return {'CANCELLED'}
        settings, target, channels, mask = prep
        space = settings.value_space
        values = target.read(space)
        if target.kind == 'IMAGE':
            out = texture.blur_pixels(values, target.size, self.iterations, self.factor)
        else:
            out = smooth_corners(target.mesh, values, self.iterations, self.factor)
        target.write(out, space, mask=mask, channels=channels)
        return {'FINISHED'}


def smooth_corners(mesh, corners, iterations, factor):
    from .core import corners_to_points, loop_vertex_indices
    pts, _has = corners_to_points(mesh, corners)
    edges = np.empty(len(mesh.edges) * 2, dtype=np.int32)
    mesh.edges.foreach_get("vertices", edges)
    a, b = edges.reshape(-1, 2).T
    cnt = np.bincount(a, minlength=len(pts)) + np.bincount(b, minlength=len(pts))
    has = cnt > 0
    for _ in range(iterations):
        acc = np.zeros_like(pts)
        np.add.at(acc, a, pts[b])
        np.add.at(acc, b, pts[a])
        mean = acc[has] / cnt[has][:, None]
        pts[has] = pts[has] * (1.0 - factor) + mean * factor
    return pts[loop_vertex_indices(mesh)]


# ---------------------------------------------------------------------------
# Transfers

def _layers(context, settings):
    src = Layer(context, settings.src_kind, settings.src_name)
    dst = Layer(context, settings.dst_kind, settings.dst_name)
    return src, dst, src.error() or dst.error()


class CDM_OT_transfer(bpy.types.Operator):
    """Transfer data from the source layer into the destination layer"""
    bl_idname = "color_data_master.transfer"
    bl_label = "Transfer"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return poll_mesh(context)

    def execute(self, context):
        settings = context.scene.color_data_master
        src, dst, err = _layers(context, settings)
        if err:
            self.report({'ERROR'}, err)
            return {'CANCELLED'}
        space = settings.value_space
        try:
            values, mask = convert_domain(src, src.read(space), dst, settings)
        except ValueError as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        current = dst.read(space)
        out = current.copy()
        chans = np.zeros(4, dtype=bool)
        sidx = channel_index(kind_channel(src.kind, settings.src_channel))
        didx = channel_index(kind_channel(dst.kind, settings.dst_channel))
        mode, op = settings.transfer_mode, settings.blend_mode

        if mode == 'CHANNEL':
            out[:, didx] = blend(values[:, sidx], current[:, didx], op)
            chans[didx] = True
        elif mode == 'LUMINANCE':
            out[:, didx] = blend(values[:, :3] @ LUMA, current[:, didx], op)
            chans[didx] = True
        elif mode == 'TO_RGB':
            out[:, :3] = blend(values[:, sidx:sidx + 1], current[:, :3], op)
            chans[:3] = True
        else:  # RGBA
            out = blend(values, current, op)
            chans[:] = True

        if dst.kind == 'VGROUP' and mode in {'TO_RGB', 'RGBA'}:
            chans[:] = False
            chans[0] = True
        # Mesh -> texture: everything outside the islands and padding becomes 0, unless a
        # face selection limits the transfer (then only the selected islands are touched).
        baked = src.domain == 'CORNER' and dst.domain == 'PIXEL'
        if baked and settings.clear_outside_uvs and corner_selection_mask(src.mesh) is None:
            outside = ~mask
            out[np.ix_(outside, chans)] = 0.0
            mask = None
        dst.write(out, space, chans, mask, min_weight=min_weight(settings))
        return {'FINISHED'}


class CDM_OT_reverse_transfer(bpy.types.Operator):
    """Swap the source and destination, to transfer back the other way"""
    bl_idname = "color_data_master.reverse_transfer"
    bl_label = "Reverse Direction"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        s = context.scene.color_data_master
        # channels as raw indices: a Weights side has no channel identifier to read back
        src = (s.src_kind, s.src_name, s.get("src_channel", 0))
        dst = (s.dst_kind, s.dst_name, s.get("dst_channel", 1))
        # kinds first: their update picks a default name, which is then overwritten
        s.src_kind, s.dst_kind = dst[0], src[0]
        s.src_name, s.dst_name = dst[1], src[1]
        for side, kind, channel in (("src", dst[0], dst[2]), ("dst", src[0], src[2])):
            s[f"{side}_channel"] = min(channel, len(KIND_CHANNEL_ITEMS[kind]) - 1)
        return {'FINISHED'}


class CDM_OT_swap(bpy.types.Operator):
    """Swap the source and destination channels"""
    bl_idname = "color_data_master.swap"
    bl_label = "Swap Channels"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return poll_mesh(context)

    def execute(self, context):
        settings = context.scene.color_data_master
        src, dst, err = _layers(context, settings)
        if err:
            self.report({'ERROR'}, err)
            return {'CANCELLED'}
        if src.kind not in COLOR_KINDS or dst.kind not in COLOR_KINDS:
            self.report({'ERROR'}, "Swap works between color attributes and textures only")
            return {'CANCELLED'}
        space = settings.value_space
        sidx = channel_index(settings.src_channel)
        didx = channel_index(settings.dst_channel)
        try:
            s_in_d, d_mask = convert_domain(src, src.read(space), dst, settings)
            d_in_s, s_mask = convert_domain(dst, dst.read(space), src, settings)
        except ValueError as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        same = src.kind == dst.kind and src.name == dst.name
        if same:
            vals = src.read(space)
            vals[:, [sidx, didx]] = vals[:, [didx, sidx]]
            sel = d_mask
            src.write(vals, space, channel_mask(settings.src_channel + settings.dst_channel), sel)
            return {'FINISHED'}
        new_dst = dst.read(space)
        new_dst[:, didx] = s_in_d[:, sidx]
        new_src = src.read(space)
        new_src[:, sidx] = d_in_s[:, didx]
        dst.write(new_dst, space, channel_mask(settings.dst_channel), d_mask)
        src.write(new_src, space, channel_mask(settings.src_channel), s_mask)
        return {'FINISHED'}



def _target_layer(context, target):
    """The transfer Layer for an active target (color attribute or texture)."""
    if isinstance(target, ImageTarget):
        return Layer(context, 'IMAGE', target.image.name)
    return Layer(context, 'ATTR', target.attr.name)


def _group_items(self, context):
    items = [('NONE', "None", "Leave this channel as it is")]
    obj = context.active_object if context else None
    if obj is not None:
        items += [(g.name, g.name, "") for g in obj.vertex_groups]
    return items


def _packed_sets(groups):
    """Prefixes with <prefix>_R/_G/_B/_A groups, most complete first: {prefix: {channel: name}}."""
    sets = {}
    for name in groups:
        head, sep, tail = name.rpartition("_")
        if sep and head and tail in ("R", "G", "B", "A"):
            sets.setdefault(head, {})[tail] = name
    return sorted(sets.items(), key=lambda kv: -len(kv[1]))


class CDM_OT_pack_weights(bpy.types.Operator):
    """Pack up to four vertex groups into the R, G, B and A channels of a color attribute or texture"""
    bl_idname = "color_data_master.pack_weights"
    bl_label = "Pack Weights"
    bl_options = {'REGISTER', 'UNDO'}

    group_r: EnumProperty(name="R", items=_group_items)
    group_g: EnumProperty(name="G", items=_group_items)
    group_b: EnumProperty(name="B", items=_group_items)
    group_a: EnumProperty(name="A", items=_group_items)
    new_attribute: BoolProperty(
        name="New Attribute", default=True,
        description="Pack into a new color attribute instead of the color data being edited",
    )
    name: StringProperty(name="Name", default="Weights")
    data_type: EnumProperty(
        name="Type",
        items=[('FLOAT_COLOR', "Float", "Stores the weights exactly, as linear values"),
               ('BYTE_COLOR', "Byte", "8 bits per channel, stored as sRGB: weights round to 1/255 "
                                      "and look darker in the viewport than in a float layer")],
        default='FLOAT_COLOR',
    )

    @classmethod
    def poll(cls, context):
        return poll_mesh(context) and len(context.active_object.vertex_groups) > 0

    def invoke(self, context, event):
        groups = [g.name for g in context.active_object.vertex_groups]
        sets = _packed_sets(groups)
        if sets:
            # groups named like an earlier Unpack: <prefix>_R, _G, _B, _A
            prefix, chans = sets[0]
            for c in "RGBA":
                setattr(self, f"group_{c.lower()}", chans.get(c, 'NONE'))
            self.name = prefix
        else:
            # otherwise the first four groups, in order
            for i, c in enumerate("rgba"):
                setattr(self, f"group_{c}", groups[i] if i < len(groups) else 'NONE')
        if active_target(context) is None:
            self.new_attribute = True
        return context.window_manager.invoke_props_dialog(self)

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        col = layout.column(align=True)
        for c in "rgba":
            col.prop(self, f"group_{c}")
        layout.prop(self, "new_attribute")
        if self.new_attribute:
            layout.prop(self, "name")
            layout.row().prop(self, "data_type", expand=True)

    def execute(self, context):
        settings = context.scene.color_data_master
        obj = context.active_object
        mesh = obj.data
        if self.new_attribute:
            attr = mesh.color_attributes.new(self.name or "Weights", self.data_type, 'POINT')
            mesh.color_attributes.active_color = attr
            dst = Layer(context, 'ATTR', attr.name)
        else:
            target = active_target(context)
            if target is None:
                self.report({'ERROR'}, "No color attribute or paint texture to pack into")
                return {'CANCELLED'}
            if isolated_info(target):
                self.report({'ERROR'}, "Apply or discard the isolated channel first")
                return {'CANCELLED'}
            dst = _target_layer(context, target)
        space = settings.value_space
        names = [getattr(self, f"group_{c}") for c in "rgba"]
        chans = np.array([n != 'NONE' and obj.vertex_groups.get(n) is not None for n in names])
        if not chans.any():
            self.report({'ERROR'}, "Pick at least one vertex group")
            return {'CANCELLED'}
        packed = np.zeros((len(mesh.loops), 4), dtype=np.float32)
        packed[:, 3] = 1.0
        for i, n in enumerate(names):
            if chans[i]:
                packed[:, i] = Layer(context, 'VGROUP', n).read(space)[:, 0]
        if self.new_attribute:
            chans[:] = True  # a new layer gets 0 in unused RGB and 1 in unused A
        src = Layer(context, 'VGROUP', names[int(np.argmax(chans))])
        try:
            values, mask = convert_domain(src, packed, dst, settings)
        except ValueError as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        if dst.domain == 'PIXEL' and settings.clear_outside_uvs and corner_selection_mask(mesh) is None:
            values[np.ix_(~mask, chans)] = 0.0
            mask = None
        dst.write(values, space, chans, mask)
        return {'FINISHED'}


class CDM_OT_unpack_weights(bpy.types.Operator):
    """Copy channels of the color data being edited into vertex groups, one group per channel"""
    bl_idname = "color_data_master.unpack_weights"
    bl_label = "Unpack Color"
    bl_options = {'REGISTER', 'UNDO'}

    channels: BoolVectorProperty(name="Channels", size=4, default=(True, True, True, False),
                                 description="The channels to copy into vertex groups")
    prefix: StringProperty(name="Prefix", description="Groups are named <prefix>_R, <prefix>_G and so on")

    @classmethod
    def poll(cls, context):
        return poll_mesh(context) and active_target(context) is not None

    def invoke(self, context, event):
        target = active_target(context)
        if not self.prefix:
            self.prefix = _target_layer(context, target).name
        self.channels = [c in context.scene.color_data_master.active_channels for c in "RGBA"]
        return context.window_manager.invoke_props_dialog(self)

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        row = layout.row(align=True)
        row.prop(self, "channels", text="Channels", toggle=True)
        layout.prop(self, "prefix")
        settings = context.scene.color_data_master
        row = layout.row(align=True)
        row.prop(settings, "skip_low_weights", text="")
        sub = row.row(align=True)
        sub.active = settings.skip_low_weights
        sub.prop(settings, "min_weight", text="Skip Weights at or Below")

    def execute(self, context):
        settings = context.scene.color_data_master
        target = active_target(context)
        if isolated_info(target):
            self.report({'ERROR'}, "Apply or discard the isolated channel first")
            return {'CANCELLED'}
        if not any(self.channels):
            self.report({'ERROR'}, "Pick at least one channel")
            return {'CANCELLED'}
        obj = context.active_object
        src = _target_layer(context, target)
        prefix = self.prefix or src.name
        space = settings.value_space
        for i, c in enumerate("RGBA"):
            if not self.channels[i]:
                continue
            name = f"{prefix}_{c}"
            if obj.vertex_groups.get(name) is None:
                obj.vertex_groups.new(name=name)
            dst = Layer(context, 'VGROUP', name)
            try:
                values, mask = convert_domain(src, src.read(space), dst, settings)
            except ValueError as e:
                self.report({'ERROR'}, str(e))
                return {'CANCELLED'}
            only = np.zeros(4, dtype=bool)
            only[0] = True
            dst.write(values[:, [i, i, i, i]], space, only, mask, min_weight=min_weight(settings))
        return {'FINISHED'}

classes = (
    CDM_OT_fill,
    CDM_OT_fill_brush_color,
    CDM_OT_invert,
    CDM_OT_posterize,
    CDM_OT_remap,
    CDM_OT_normalize_mask,
    CDM_OT_blur,
    CDM_OT_transfer,
    CDM_OT_reverse_transfer,
    CDM_OT_swap,
    CDM_OT_pack_weights,
    CDM_OT_unpack_weights,
)
