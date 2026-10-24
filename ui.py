# SPDX-License-Identifier: GPL-3.0-or-later

import bpy

from .core import active_target, color_owner, isolated_info, paint_settings, value_from_linear
from .data import COLOR_KINDS

PAINT_MODES = {'VERTEX_PAINT', 'TEXTURE_PAINT'}


def _target_label(target):
    if target.kind == 'ATTR':
        a = target.attr
        kind = "Byte" if a.data_type == 'BYTE_COLOR' else "Float"
        domain = "Face Corner" if a.domain == 'CORNER' else "Vertex"
        return f"{a.name}  ({kind}, {domain})", 'GROUP_VCOL'
    img = target.image
    depth = "Float" if img.is_float else "8-bit"
    return f"{img.name}  ({depth}, {img.colorspace_settings.name})", 'IMAGE_DATA'


def _search_args(context, kind):
    obj = context.active_object
    if kind == 'ATTR':
        return obj.data, "color_attributes"
    if kind == 'IMAGE':
        return bpy.data, "images"
    if kind == 'VGROUP':
        return obj, "vertex_groups"
    if kind == 'UV':
        return obj.data, "uv_layers"
    return None


def draw_target(context, layout, settings, target):
    col = layout.column(align=True)
    row = col.row(align=True)
    row.prop(settings, "edit_target", text="")
    row.prop(settings, "value_space", text="")
    if target is None:
        col.label(text="Nothing to edit: add a color attribute or texture", icon='ERROR')
        return
    text, icon = _target_label(target)
    col.label(text=text, icon=icon)
    if target.is_byte:
        col.label(text="8-bit: values round to 1/255 steps", icon='INFO')
    iso = isolated_info(target)
    if iso:
        box = layout.box()
        box.label(text=f"Isolated {iso[1]} of '{iso[0]}'", icon='HIDE_OFF')
        row = box.row(align=True)
        row.operator("color_data_master.apply_isolated", text="Apply").discard = False
        row.operator("color_data_master.apply_isolated", text="Discard").discard = True


def draw_brush(context, layout, settings, target, pie=False):
    obj = context.active_object
    if obj.mode not in PAINT_MODES:
        return
    ps = paint_settings(context)
    owner = color_owner(context)
    iso = isolated_info(target)
    col = layout.column(align=True)
    col.label(text="Brush")
    if not pie and not iso:
        col.prop(settings, "use_grayscale")
        col.prop(settings, "match_brush_to_active_channels")
    row = col.row(align=True)
    if iso or settings.use_grayscale:
        row.prop(settings, "brush_value", text="F", slider=True)
        row.prop(settings, "brush_secondary_value", text="B", slider=True)
    else:
        row.prop(owner, "color", text="")
        row.prop(owner, "secondary_color", text="")
    row.operator("color_data_master.flip_colors", text="", icon='FILE_REFRESH')
    if target is not None and (iso or settings.use_grayscale):
        stored = value_from_linear(owner.color[0], target, 'AUTO')
        hint = f"Stored: {stored:.3f}"
        if target.is_byte:
            hint += f"  ({round(stored * 255)}/255)"
        col.label(text=hint)
    col.operator("color_data_master.fill_brush_color",
                 text="Fill With Value" if iso else "Fill With Color")

    col = layout.column(align=True)
    row = col.row(align=True)
    for mode, label in (('MIX', "Mix"), ('ADD', "Add"), ('SUB', "Sub"), ('BLUR', "Blur")):
        row.operator("color_data_master.brush_blend", text=label).blend_mode = mode
    if ps.brush:
        col.prop(ps.brush, "strength", slider=True)
        if obj.mode == 'VERTEX_PAINT' and not iso:
            col.prop(ps.brush, "use_alpha", text="Affect Alpha")


def draw_channels(context, layout, settings, target):
    iso = isolated_info(target)
    col = layout.column(align=True)
    if not iso:
        col.label(text="Active Channels")
        col.row(align=True).prop(settings, "active_channels", expand=True)
        row = col.row(align=True)
        single = len(settings.active_channels) == 1
        ch = next(iter(settings.active_channels)) if single else 'R'
        row.operator("color_data_master.isolate_channel", text="Isolate Active Channel").channel = ch
        row.enabled = single and target is not None
    col = layout.column(align=True)
    row = col.row(align=True)
    row.operator("color_data_master.fill", text="Fill").value = 1.0
    row.operator("color_data_master.fill", text="Clear").value = 0.0
    row = col.row(align=True)
    row.operator("color_data_master.invert", text="Invert")
    row.operator("color_data_master.posterize", text="Posterize")
    row = col.row(align=True)
    row.operator("color_data_master.remap", text="Remap")
    row.operator("color_data_master.blur", text="Blur")
    col.operator("color_data_master.random_island_channels", text="Random Island Values")
    if not iso:
        col.operator("color_data_master.normalize_mask", text="Normalize Blend Mask")


def draw_transfer(context, layout, settings):
    col = layout.column(align=True)
    col.label(text="Transfer")
    for side in ("src", "dst"):
        kind = getattr(settings, f"{side}_kind")
        row = col.row(align=True)
        split = row.split(factor=0.3, align=True)
        split.prop(settings, f"{side}_kind", text="")
        sub = split.split(factor=0.78, align=True)
        args = _search_args(context, kind)
        if args:
            sub.prop_search(settings, f"{side}_name", *args, text="")
        else:
            sub.label(text="Custom Normals")
        sub.prop(settings, f"{side}_channel", text="")
    col = layout.column(align=True)
    row = col.row(align=True)
    row.prop(settings, "transfer_mode", text="")
    row.prop(settings, "blend_mode", text="")
    row = col.row(align=True)
    row.scale_y = 1.2
    row.operator("color_data_master.transfer", icon='FORWARD')
    if settings.src_kind in COLOR_KINDS and settings.dst_kind in COLOR_KINDS:
        row.operator("color_data_master.swap", text="Swap", icon='ARROW_LEFTRIGHT')
    if 'IMAGE' in (settings.src_kind, settings.dst_kind) and settings.src_kind != settings.dst_kind:
        col = layout.column(align=True)
        col.prop_search(settings, "uv_map", context.active_object.data, "uv_layers", text="UV")
        col.prop(settings, "bake_padding")


def draw_misc(context, layout, settings, target):
    obj = context.active_object
    col = layout.column(align=True)
    col.label(text="Misc")
    if obj.mode == 'VERTEX_PAINT' and not isolated_info(target):
        col.operator("paint.vertex_color_hsv", text="Adjust HSV")
        col.operator("paint.vertex_color_brightness_contrast", text="Brightness / Contrast")
        col.operator("paint.vertex_color_dirt", text="Dirty Vertex Colors")
    col.operator("color_data_master.random_island_colors", text="Random Island Colors")
    row = col.row(align=True)
    row.operator("color_data_master.gradient", text="Linear Gradient").circular = False
    row.operator("color_data_master.gradient", text="Circular Gradient").circular = True


class CDM_PT_main(bpy.types.Panel):
    bl_label = "Color Data Master"
    bl_idname = "CDM_PT_main"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Color Data"

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj is not None and obj.type == 'MESH' and obj.mode in {'OBJECT'} | PAINT_MODES

    def draw(self, context):
        settings = context.scene.color_data_master
        target = active_target(context)
        draw_target(context, self.layout, settings, target)


class CDM_PT_brush(bpy.types.Panel):
    bl_label = "Brush"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Color Data"
    bl_parent_id = "CDM_PT_main"

    @classmethod
    def poll(cls, context):
        return context.active_object.mode in PAINT_MODES

    def draw(self, context):
        draw_brush(context, self.layout, context.scene.color_data_master, active_target(context))


class CDM_PT_channels(bpy.types.Panel):
    bl_label = "Channels"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Color Data"
    bl_parent_id = "CDM_PT_main"

    def draw(self, context):
        draw_channels(context, self.layout, context.scene.color_data_master, active_target(context))


class CDM_PT_transfer(bpy.types.Panel):
    bl_label = "Transfer"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Color Data"
    bl_parent_id = "CDM_PT_main"

    @classmethod
    def poll(cls, context):
        return not isolated_info(active_target(context))

    def draw(self, context):
        draw_transfer(context, self.layout, context.scene.color_data_master)


class CDM_PT_misc(bpy.types.Panel):
    bl_label = "Misc"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Color Data"
    bl_parent_id = "CDM_PT_main"

    def draw(self, context):
        draw_misc(context, self.layout, context.scene.color_data_master, active_target(context))


class CDM_MT_pie(bpy.types.Menu):
    bl_label = "Color Data Master"
    bl_idname = "CDM_MT_pie"

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj is not None and obj.type == 'MESH' and obj.mode in PAINT_MODES

    def draw(self, context):
        settings = context.scene.color_data_master
        target = active_target(context)
        iso = isolated_info(target)
        pie = self.layout.menu_pie()
        draw_brush(context, pie.column(), settings, target, pie=True)   # left
        draw_channels(context, pie.column(), settings, target)          # right
        draw_misc(context, pie.column(), settings, target)              # bottom
        col = pie.column()                                              # top
        if iso:
            col.label(text=f"Isolated {iso[1]} of '{iso[0]}'")
            row = col.row(align=True)
            row.operator("color_data_master.apply_isolated", text="Apply").discard = False
            row.operator("color_data_master.apply_isolated", text="Discard").discard = True
        elif target is not None:
            col.label(text="Isolate Channel")
            row = col.row(align=True)
            for ch in "RGBA":
                row.operator("color_data_master.isolate_channel", text=ch).channel = ch


classes = (
    CDM_PT_main,
    CDM_PT_brush,
    CDM_PT_channels,
    CDM_PT_transfer,
    CDM_PT_misc,
    CDM_MT_pie,
)
