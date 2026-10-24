# SPDX-License-Identifier: GPL-3.0-or-later
"""Isolate one channel of a color attribute or texture as a grayscale layer to paint on."""

import bpy
import numpy as np
from bpy.props import BoolProperty, EnumProperty

from .core import (
    ISO_PREFIX, AttrTarget, active_target, channel_index, color_owner,
    isolated_info, poll_mesh,
)
from .props import CHANNEL_ITEMS
from . import texture


def _gray(values, idx):
    out = np.empty_like(values)
    out[:, 0] = out[:, 1] = out[:, 2] = values[:, idx]
    out[:, 3] = 1.0
    return out


def _attr_raw_set(attr, values):
    prop = 'color_srgb' if attr.data_type == 'BYTE_COLOR' else 'color'
    attr.data.foreach_set(prop, np.ascontiguousarray(values, dtype=np.float32).ravel())


def _image_raw(image):
    w, h = image.size
    buf = np.empty(w * h * 4, dtype=np.float32)
    image.pixels.foreach_get(buf)
    return buf.reshape(-1, 4)


def _save_brush(context):
    settings = context.scene.color_data_master
    owner = color_owner(context)
    settings.iso_color = owner.color
    settings.iso_secondary_color = owner.secondary_color
    # re-applies brush_value / brush_secondary_value as gray brush colors
    settings.brush_value = settings.brush_value


def _restore_brush(context):
    settings = context.scene.color_data_master
    owner = color_owner(context)
    owner.color = settings.iso_color
    owner.secondary_color = settings.iso_secondary_color


class CDM_OT_isolate_channel(bpy.types.Operator):
    """Copy one channel into a temporary grayscale layer and paint on that instead"""
    bl_idname = "color_data_master.isolate_channel"
    bl_label = "Isolate Channel"
    bl_options = {'REGISTER', 'UNDO'}

    channel: EnumProperty(name="Channel", items=CHANNEL_ITEMS, default='R')

    @classmethod
    def poll(cls, context):
        return poll_mesh(context)

    def execute(self, context):
        target = active_target(context)
        if target is None:
            self.report({'ERROR'}, "No color attribute or paint texture to isolate")
            return {'CANCELLED'}
        if isolated_info(target):
            self.report({'ERROR'}, "Apply or discard the isolated channel first")
            return {'CANCELLED'}
        iso_name = f"{ISO_PREFIX}_{self.channel}_{target.name}"
        idx = channel_index(self.channel)

        if target.kind == 'ATTR':
            mesh = target.mesh
            if iso_name in mesh.color_attributes:
                self.report({'ERROR'}, f"'{iso_name}' already exists. Apply or discard it first")
                return {'CANCELLED'}
            data_type, domain = target.attr.data_type, target.attr.domain
            raw = target.read_raw('AUTO')
            mesh.color_attributes.new(iso_name, data_type, domain)
            # adding an attribute can invalidate existing attribute references
            iso = mesh.color_attributes[iso_name]
            _attr_raw_set(iso, _gray(raw, idx))
            mesh.color_attributes.active_color = iso
            mesh.update()
        else:
            src = target.image
            if bpy.data.images.get(iso_name):
                self.report({'ERROR'}, f"'{iso_name}' already exists. Apply or discard it first")
                return {'CANCELLED'}
            w, h = src.size
            iso = bpy.data.images.new(iso_name, w, h, alpha=True, float_buffer=src.is_float)
            iso.colorspace_settings.name = src.colorspace_settings.name
            iso.pixels.foreach_set(np.ascontiguousarray(_gray(_image_raw(src), idx)).ravel())
            iso.update()
            ip = context.tool_settings.image_paint
            iso["cdm_prev_mode"] = ip.mode
            iso["cdm_prev_canvas"] = ip.canvas.name if ip.canvas else ""
            iso["cdm_prev_edit_target"] = context.scene.color_data_master.edit_target
            ip.mode = 'IMAGE'
            ip.canvas = iso
            context.scene.color_data_master.edit_target = 'IMAGE'
            for area in context.screen.areas if context.screen else ():
                if area.type == 'IMAGE_EDITOR':
                    area.spaces.active.image = iso

        _save_brush(context)
        return {'FINISHED'}


class CDM_OT_apply_isolated(bpy.types.Operator):
    """Write the isolated channel back to the layer it came from, or discard it"""
    bl_idname = "color_data_master.apply_isolated"
    bl_label = "Apply Isolated Channel"
    bl_options = {'REGISTER', 'UNDO'}

    discard: BoolProperty(name="Discard", default=False,
                          description="Throw away changes instead of applying them")

    @classmethod
    def poll(cls, context):
        return poll_mesh(context) and isolated_info(active_target(context)) is not None

    def execute(self, context):
        target = active_target(context)
        src_name, ch = isolated_info(target)
        idx = channel_index(ch)

        if target.kind == 'ATTR':
            mesh = target.mesh
            src_attr = mesh.color_attributes.get(src_name)
            if src_attr is None and not self.discard:
                self.report({'ERROR'}, f"Color attribute '{src_name}' no longer exists")
                return {'CANCELLED'}
            if not self.discard:
                src = AttrTarget(mesh, src_attr)
                if src_attr.domain == target.attr.domain and src_attr.data_type == target.attr.data_type:
                    raw = src.read_raw('AUTO')
                    raw[:, idx] = target.read_raw('AUTO')[:, 0]
                    _attr_raw_set(src_attr, raw)
                else:
                    corners = src.read('AUTO')
                    corners[:, idx] = target.read('AUTO')[:, 0]
                    src.write(corners, 'AUTO')
            mesh.color_attributes.remove(mesh.color_attributes[target.name])
            if src_name in mesh.color_attributes:
                mesh.color_attributes.active_color = mesh.color_attributes[src_name]
            mesh.update()
        else:
            iso = target.image
            src = bpy.data.images.get(src_name)
            if src is None and not self.discard:
                self.report({'ERROR'}, f"Texture '{src_name}' no longer exists")
                return {'CANCELLED'}
            if not self.discard:
                gray = texture.resample(_image_raw(iso), iso.size, src.size)
                raw = _image_raw(src)
                raw[:, idx] = gray[:, 0]
                src.pixels.foreach_set(np.ascontiguousarray(raw).ravel())
                src.update()
            ip = context.tool_settings.image_paint
            ip.mode = iso.get("cdm_prev_mode", 'MATERIAL')
            prev_canvas = bpy.data.images.get(iso.get("cdm_prev_canvas", ""))
            ip.canvas = prev_canvas if prev_canvas else src
            context.scene.color_data_master.edit_target = iso.get("cdm_prev_edit_target", 'AUTO')
            for area in context.screen.areas if context.screen else ():
                if area.type == 'IMAGE_EDITOR' and area.spaces.active.image == iso:
                    area.spaces.active.image = src
            bpy.data.images.remove(iso)

        _restore_brush(context)
        return {'FINISHED'}


classes = (
    CDM_OT_isolate_channel,
    CDM_OT_apply_isolated,
)
