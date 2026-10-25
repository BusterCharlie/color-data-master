# SPDX-License-Identifier: GPL-3.0-or-later
"""Brush helpers, random island colors and the gradient tool."""

import math

import bpy
import gpu
import numpy as np
from bpy.props import BoolProperty, EnumProperty, FloatProperty, FloatVectorProperty, IntProperty
from gpu_extras.batch import batch_for_shader
from mathutils import Vector

from .core import (
    active_target, channel_mask, color_owner, corner_selection_mask, isolated_info,
    loop_face_indices, loop_vertex_indices, paint_settings, poll_mesh, srgb_to_linear,
)
from .ops_channel import write_corner_values
from .props import CHANNEL_ITEMS


def _paint_mode_poll(context):
    obj = context.active_object
    return obj is not None and obj.type == 'MESH' and obj.mode in {'VERTEX_PAINT', 'TEXTURE_PAINT'}


# ---------------------------------------------------------------------------
# Brush

ESSENTIALS = {
    'VERTEX_PAINT': ("brushes/essentials_brushes-mesh_vertex.blend/Brush/", 'vertex_brush_type', 'BLUR'),
    'TEXTURE_PAINT': ("brushes/essentials_brushes-mesh_texture.blend/Brush/", 'image_brush_type', 'SOFTEN'),
}


def activate_essentials_brush(context, name):
    path, _type_prop, _blur = ESSENTIALS[context.active_object.mode]
    return bpy.ops.brush.asset_activate(
        asset_library_type='ESSENTIALS',
        asset_library_identifier="",
        relative_asset_identifier=path + name,
    )


class CDM_OT_brush_blend(bpy.types.Operator):
    """Switch to a paint brush with this blend mode, or the blur brush"""
    bl_idname = "color_data_master.brush_blend"
    bl_label = "Brush Blend Mode"
    bl_options = {'REGISTER', 'UNDO'}

    blend_mode: EnumProperty(
        name="Blend",
        items=(
            ('MIX', "Mix", ""), ('ADD', "Add", ""), ('SUB', "Subtract", ""),
            ('MUL', "Multiply", ""), ('LIGHTEN', "Lighten", ""), ('DARKEN', "Darken", ""),
            ('BLUR', "Blur", "Switch to the Blur brush"),
        ),
        default='MIX',
    )

    @classmethod
    def poll(cls, context):
        return _paint_mode_poll(context)

    def execute(self, context):
        ps = paint_settings(context)
        _path, type_prop, blur_type = ESSENTIALS[context.active_object.mode]
        owner = color_owner(context)
        colors = (tuple(owner.color), tuple(owner.secondary_color))
        try:
            if self.blend_mode == 'BLUR':
                activate_essentials_brush(context, "Blur")
            else:
                if ps.brush is None or getattr(ps.brush, type_prop) != 'DRAW':
                    activate_essentials_brush(context, "Paint Hard")
                ps.brush.blend = self.blend_mode
        except (RuntimeError, AttributeError) as e:
            self.report({'ERROR'}, f"Could not set brush: {e}")
            return {'CANCELLED'}
        owner = color_owner(context)
        if owner is ps.brush:  # per-brush colors: carry them over to the new brush
            owner.color, owner.secondary_color = colors
        return {'FINISHED'}


class CDM_OT_flip_colors(bpy.types.Operator):
    """Swap the primary and secondary brush colors (and grayscale values)"""
    bl_idname = "color_data_master.flip_colors"
    bl_label = "Flip Brush Colors"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return _paint_mode_poll(context)

    def execute(self, context):
        settings = context.scene.color_data_master
        if settings.use_grayscale or isolated_info(active_target(context)):
            v1, v2 = settings.brush_value, settings.brush_secondary_value
            settings.brush_secondary_value = v1
            settings.brush_value = v2  # update callback writes both brush colors
        else:
            owner = color_owner(context)
            c1 = tuple(owner.color)
            owner.color = owner.secondary_color
            owner.secondary_color = c1
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Mesh islands

def _connected_labels(n, a, b):
    """Component id per element, given pairs (a, b) of linked elements."""
    labels = np.arange(n, dtype=np.int64)
    while len(a):
        m = np.minimum(labels[a], labels[b])
        new = labels.copy()
        np.minimum.at(new, a, m)
        np.minimum.at(new, b, m)
        new = new[new]
        if np.array_equal(new, labels):
            break
        labels = new
    _, ids = np.unique(labels, return_inverse=True)
    return ids


def vertex_islands(mesh):
    """Island id per vertex (connected through edges)."""
    edges = np.empty(len(mesh.edges) * 2, dtype=np.int64)
    mesh.edges.foreach_get("vertices", edges)
    a, b = edges.reshape(-1, 2).T
    return _connected_labels(len(mesh.vertices), a, b)


def face_islands(mesh, split_seams=False):
    """Island id per face. With split_seams, faces only link across non-seam edges."""
    if not split_seams:
        first = np.empty(len(mesh.polygons), dtype=np.int64)
        mesh.polygons.foreach_get("loop_start", first)
        return vertex_islands(mesh)[loop_vertex_indices(mesh)[first]]
    loop_edge = np.empty(len(mesh.loops), dtype=np.int64)
    mesh.loops.foreach_get("edge_index", loop_edge)
    seam = np.empty(len(mesh.edges), dtype=bool)
    mesh.edges.foreach_get("use_seam", seam)
    loop_face = loop_face_indices(mesh)
    order = np.argsort(loop_edge, kind="stable")
    e, f = loop_edge[order], loop_face[order]
    # Loops sorted by edge: neighbours on the same edge are faces sharing it.
    link = (e[1:] == e[:-1]) & ~seam[e[1:]]
    return _connected_labels(len(mesh.polygons), f[:-1][link], f[1:][link])


def hsv_to_rgb(h, s, v):
    h = np.mod(h, 1.0) * 6.0
    i = np.floor(h).astype(int) % 6
    f = h - np.floor(h)
    p, q, t = v * (1 - s), v * (1 - s * f), v * (1 - s * (1 - f))
    r = np.choose(i, [v, q, p, p, t, v])
    g = np.choose(i, [t, v, v, q, p, p])
    b = np.choose(i, [p, p, t, v, v, q])
    return np.stack([r, g, b], axis=-1)


def rgb_to_hsv(rgb):
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    mx, mn = np.max(rgb, axis=-1), np.min(rgb, axis=-1)
    d = mx - mn
    safe = np.where(d == 0, 1, d)
    h = np.where(mx == r, (g - b) / safe, np.where(mx == g, 2 + (b - r) / safe, 4 + (r - g) / safe))
    h = np.where(d == 0, 0, np.mod(h / 6.0, 1.0))
    s = np.where(mx == 0, 0, d / np.where(mx == 0, 1, mx))
    return h, s, mx


class IslandOperator:
    bl_options = {'REGISTER', 'UNDO'}

    random_seed: IntProperty(name="Seed", default=1, min=1, max=10000)
    merge_similar: BoolProperty(
        name="Merge Similar", default=False,
        description="Give islands with the same face count the same color")
    split_seams: BoolProperty(
        name="Split at UV Seams", default=True,
        description="Treat parts of a mesh separated by UV seams as separate islands")
    order_based: BoolProperty(
        name="Order Based", default=False,
        description="Spread values evenly by island order instead of randomly")

    @classmethod
    def poll(cls, context):
        return poll_mesh(context)

    def island_keys(self, mesh):
        """Per-face island key and the list of keys in island order."""
        face_island = face_islands(mesh, self.split_seams)
        if self.merge_similar:
            counts = np.bincount(face_island)
            return counts[face_island], np.unique(counts[face_island])
        return face_island, np.unique(face_island)

    def write(self, context, target, face_rgba, space, channels):
        mesh = context.active_object.data
        corners = face_rgba[loop_face_indices(mesh)]
        mask = corner_selection_mask(mesh) if context.active_object.mode != 'OBJECT' else None
        err = write_corner_values(context, target, corners, channels, space, mask)
        if err:
            self.report({'ERROR'}, err)
            return {'CANCELLED'}
        return {'FINISHED'}


class CDM_OT_random_island_colors(IslandOperator, bpy.types.Operator):
    """Give each separate mesh island a random color"""
    bl_idname = "color_data_master.random_island_colors"
    bl_label = "Random Island Colors"

    randomize_hue: BoolProperty(name="Hue", default=True)
    randomize_saturation: BoolProperty(name="Saturation", default=False)
    randomize_value: BoolProperty(name="Value", default=False)
    base_hue: FloatProperty(name="H", default=0.0, min=0.0, max=1.0)
    base_saturation: FloatProperty(name="S", default=1.0, min=0.0, max=1.0)
    base_value: FloatProperty(name="V", default=1.0, min=0.0, max=1.0)

    def execute(self, context):
        target = active_target(context)
        if target is None:
            self.report({'ERROR'}, "No color attribute or paint texture to edit")
            return {'CANCELLED'}
        mesh = context.active_object.data
        face_key, keys = self.island_keys(mesh)
        rng = np.random.default_rng(self.random_seed)
        n = len(keys)
        if self.order_based and not self.merge_similar:
            ramp = np.arange(n) / max(n, 1)
            pick = lambda use, base: ramp if use else np.full(n, base)
        else:
            pick = lambda use, base: rng.random(n) if use else np.full(n, base)
        h = pick(self.randomize_hue, self.base_hue)
        s = pick(self.randomize_saturation, self.base_saturation)
        v = pick(self.randomize_value, self.base_value)

        if isolated_info(target):
            rgb = np.repeat(v[:, None], 3, axis=1) if self.randomize_value or self.order_based \
                else np.repeat(rng.random(n)[:, None], 3, axis=1)
            space = context.scene.color_data_master.value_space
        else:
            rgb = srgb_to_linear(hsv_to_rgb(h, s, v))
            space = 'LINEAR'
        island_rgba = np.concatenate([rgb, np.ones((n, 1))], axis=1).astype(np.float32)
        face_rgba = island_rgba[np.searchsorted(keys, face_key)]
        return self.write(context, target, face_rgba, space, channel_mask('RGB'))

    def draw(self, context):
        layout = self.layout
        col = layout.column(align=True)
        col.label(text="Randomize")
        for label, use, base in (("Hue", "randomize_hue", "base_hue"),
                                 ("Saturation", "randomize_saturation", "base_saturation"),
                                 ("Value", "randomize_value", "base_value")):
            row = col.row(align=True)
            row.prop(self, use, text=label)
            row.prop(self, base, slider=True)
        layout.prop(self, "split_seams")
        layout.prop(self, "merge_similar")
        row = layout.row()
        row.prop(self, "order_based")
        row.enabled = not self.merge_similar
        layout.prop(self, "random_seed")


class CDM_OT_random_island_channels(IslandOperator, bpy.types.Operator):
    """Give each mesh island a random value in each chosen channel"""
    bl_idname = "color_data_master.random_island_channels"
    bl_label = "Random Island Values"

    channels: EnumProperty(name="Channels", items=CHANNEL_ITEMS, options={'ENUM_FLAG'},
                           default={'R', 'G', 'B'})
    value_min: FloatProperty(name="Min", default=0.0, min=0.0, max=1.0)
    value_max: FloatProperty(name="Max", default=1.0, min=0.0, max=1.0)

    def invoke(self, context, event):
        self.channels = context.scene.color_data_master.active_channels
        return self.execute(context)

    def execute(self, context):
        target = active_target(context)
        if target is None:
            self.report({'ERROR'}, "No color attribute or paint texture to edit")
            return {'CANCELLED'}
        mesh = context.active_object.data
        face_key, keys = self.island_keys(mesh)
        rng = np.random.default_rng(self.random_seed)
        lo, hi = sorted((self.value_min, self.value_max))
        n = len(keys)
        if self.order_based and not self.merge_similar:
            t = np.repeat((np.arange(n) / max(n - 1, 1))[:, None], 4, axis=1)
        else:
            t = rng.random((n, 4))
        island_rgba = (lo + t * (hi - lo)).astype(np.float32)
        face_rgba = island_rgba[np.searchsorted(keys, face_key)]
        chans = channel_mask('RGB') if isolated_info(target) else channel_mask(self.channels)
        if isolated_info(target):
            face_rgba[:, 1] = face_rgba[:, 2] = face_rgba[:, 0]
        return self.write(context, target, face_rgba,
                          context.scene.color_data_master.value_space, chans)

    def draw(self, context):
        layout = self.layout
        layout.row(align=True).prop(self, "channels")
        layout.prop(self, "split_seams")
        layout.prop(self, "merge_similar")
        row = layout.row()
        row.prop(self, "order_based")
        row.enabled = not self.merge_similar
        layout.prop(self, "random_seed")
        layout.prop(self, "value_min", slider=True)
        layout.prop(self, "value_max", slider=True)


# ---------------------------------------------------------------------------
# Gradient

def _draw_gradient(op):
    a, b = op.points
    c1, c2 = op.draw_colors
    batch = batch_for_shader(op.line_shader, 'LINES', {"pos": [a, b], "color": [c1, c2]})
    op.line_shader.bind()
    batch.draw(op.line_shader)
    if op.circular:
        r = (b - a).length
        pts = [(a.x + r * math.cos(2 * math.pi * i / 64), a.y + r * math.sin(2 * math.pi * i / 64))
               for i in range(65)]
        batch = batch_for_shader(op.circle_shader, 'LINE_STRIP', {"pos": pts})
        op.circle_shader.bind()
        op.circle_shader.uniform_float("color", c2)
        batch.draw(op.circle_shader)


class CDM_OT_gradient(bpy.types.Operator):
    """Drag in the viewport to paint a gradient between the brush colors"""
    bl_idname = "color_data_master.gradient"
    bl_label = "Gradient"
    bl_options = {'REGISTER', 'UNDO'}

    circular: BoolProperty(name="Circular", default=False)
    use_hue_blend: BoolProperty(name="Hue Blend", default=False,
                                description="Blend through the hue wheel instead of straight RGB")
    start_color: FloatVectorProperty(name="Start", subtype='COLOR', size=3, default=(1, 0, 0))
    end_color: FloatVectorProperty(name="End", subtype='COLOR', size=3, default=(0, 1, 0))
    start: FloatVectorProperty(size=2, options={'HIDDEN'})
    end: FloatVectorProperty(size=2, options={'HIDDEN'})
    persp: FloatVectorProperty(size=16, options={'HIDDEN'})
    region_size: FloatVectorProperty(size=2, options={'HIDDEN'})

    @classmethod
    def poll(cls, context):
        return poll_mesh(context)

    def invoke(self, context, event):
        if context.area is None or context.area.type != 'VIEW_3D':
            self.report({'WARNING'}, "Run the gradient tool from the 3D Viewport")
            return {'CANCELLED'}
        owner = color_owner(context)
        self.start_color = owner.color
        self.end_color = owner.secondary_color
        self.line_shader = gpu.shader.from_builtin('SMOOTH_COLOR')
        self.circle_shader = gpu.shader.from_builtin('UNIFORM_COLOR')
        self.draw_colors = (tuple(owner.color) + (1.0,), tuple(owner.secondary_color) + (1.0,))
        self.points = None
        self.handle = None
        context.window_manager.modal_handler_add(self)
        context.workspace.status_text_set("Drag to draw the gradient line, Shift to snap, Esc / RMB to cancel")
        return {'RUNNING_MODAL'}

    def _finish(self, context):
        if self.handle is not None:
            bpy.types.SpaceView3D.draw_handler_remove(self.handle, 'WINDOW')
            self.handle = None
        context.workspace.status_text_set(None)
        context.area.tag_redraw()

    def modal(self, context, event):
        context.area.tag_redraw()
        mouse = Vector((event.mouse_region_x, event.mouse_region_y))
        if event.type in {'RIGHTMOUSE', 'ESC'}:
            self._finish(context)
            return {'CANCELLED'}
        if event.type in {'MIDDLEMOUSE', 'WHEELUPMOUSE', 'WHEELDOWNMOUSE'}:
            return {'PASS_THROUGH'}
        if self.points is None:
            if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
                self.points = [mouse, mouse.copy()]
                self.handle = bpy.types.SpaceView3D.draw_handler_add(
                    _draw_gradient, (self,), 'WINDOW', 'POST_PIXEL')
            return {'RUNNING_MODAL'}
        a = self.points[0]
        if event.shift:
            if abs(mouse.x - a.x) < 20:
                mouse.x = a.x
            elif abs(mouse.y - a.y) < 20:
                mouse.y = a.y
        self.points[1] = mouse
        if event.type == 'LEFTMOUSE' and event.value == 'RELEASE':
            self._finish(context)
            if (mouse - a).length < 2:
                return {'CANCELLED'}
            rv3d = context.region_data
            self.start = a
            self.end = mouse
            self.persp = [v for row in rv3d.perspective_matrix for v in row]
            self.region_size = (context.region.width, context.region.height)
            return self.execute(context)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        target = active_target(context)
        if target is None:
            self.report({'ERROR'}, "No color attribute or paint texture to edit")
            return {'CANCELLED'}
        obj = context.active_object
        mesh = obj.data
        co = np.empty(len(mesh.vertices) * 3, dtype=np.float32)
        mesh.vertices.foreach_get("co", co)
        co = np.c_[co.reshape(-1, 3), np.ones(len(mesh.vertices))]
        mat = np.array(self.persp).reshape(4, 4) @ np.array(obj.matrix_world)
        clip = co @ mat.T
        w = np.where(np.abs(clip[:, 3]) < 1e-8, 1e-8, clip[:, 3])
        width, height = self.region_size
        px = np.c_[(clip[:, 0] / w + 1) * 0.5 * width, (clip[:, 1] / w + 1) * 0.5 * height]

        a, b = np.array(self.start), np.array(self.end)
        d = b - a
        if self.circular:
            t = np.linalg.norm(px - a, axis=1) / max(np.linalg.norm(d), 1e-8)
        else:
            t = (px - a) @ d / max(d @ d, 1e-8)
        t = np.clip(t, 0.0, 1.0)[:, None]

        iso = isolated_info(target)
        c1, c2 = np.array(self.start_color), np.array(self.end_color)
        if iso:
            luma = np.array([0.299, 0.587, 0.114])
            c1, c2 = np.full(3, c1 @ luma), np.full(3, c2 @ luma)
        if self.use_hue_blend and not iso:
            h1, s1, v1 = rgb_to_hsv(c1)
            h2, s2, v2 = rgb_to_hsv(c2)
            dh = (h2 - h1 + 0.5) % 1.0 - 0.5
            rgb = hsv_to_rgb(h1 + dh * t[:, 0], s1 + (s2 - s1) * t[:, 0], v1 + (v2 - v1) * t[:, 0])
        else:
            rgb = c1 + (c2 - c1) * t

        corners = np.c_[rgb, np.ones(len(rgb))][loop_vertex_indices(mesh)].astype(np.float32)
        mask = corner_selection_mask(mesh) if obj.mode != 'OBJECT' else None
        err = write_corner_values(context, target, corners, channel_mask('RGB'), 'LINEAR', mask)
        if err:
            self.report({'ERROR'}, err)
            return {'CANCELLED'}
        return {'FINISHED'}

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "circular")
        layout.prop(self, "use_hue_blend")
        row = layout.row(align=True)
        row.prop(self, "start_color", text="")
        row.prop(self, "end_color", text="")


classes = (
    CDM_OT_brush_blend,
    CDM_OT_flip_colors,
    CDM_OT_random_island_colors,
    CDM_OT_random_island_channels,
    CDM_OT_gradient,
)
