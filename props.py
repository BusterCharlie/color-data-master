# SPDX-License-Identifier: GPL-3.0-or-later

import bpy
from bpy.props import (
    BoolProperty, EnumProperty, FloatProperty, FloatVectorProperty,
    IntProperty, StringProperty,
)

from .core import active_target, color_owner, isolated_info, value_to_linear
from .data import LAYER_KINDS

CHANNEL_ITEMS = (
    ('R', "R", "Red channel"),
    ('G', "G", "Green channel"),
    ('B', "B", "Blue channel"),
    ('A', "A", "Alpha channel"),
)

VALUE_SPACE_ITEMS = (
    ('AUTO', "Stored", "Values as stored in the layer: sRGB for byte layers and 8-bit sRGB "
     "images, linear for float layers. Best for packed mask data"),
    ('LINEAR', "Linear", "Scene linear values, as read by shaders"),
    ('SRGB', "sRGB", "sRGB encoded values, as shown by color pickers"),
)

BLEND_ITEMS = (
    ('REPLACE', "Replace", "Replace destination with source"),
    ('ADD', "Add", "Destination + source, clamped to 0-1"),
    ('SUB', "Subtract", "Destination - source, clamped to 0-1"),
    ('MUL', "Multiply", "Destination x source"),
    ('DIV', "Divide", "Destination / source, clamped to 0-1 (unchanged where source is 0)"),
    ('LIGHTEN', "Lighten", "Maximum of the two"),
    ('DARKEN', "Darken", "Minimum of the two"),
)

TRANSFER_MODE_ITEMS = (
    ('CHANNEL', "Channel", "Source channel into destination channel"),
    ('TO_RGB', "Channel to RGB", "Source channel into destination R, G and B (grayscale)"),
    ('RGBA', "All Channels", "Every channel into the same channel"),
    ('LUMINANCE', "Luminance", "Source RGB luminance into the destination channel"),
)


def _update_active_channels(self, context):
    if self.use_grayscale or not self.match_brush_to_active_channels:
        return
    if isolated_info(active_target(context)):
        return
    owner = color_owner(context)
    owner.color = [1.0 if c in self.active_channels else 0.0 for c in 'RGB']


def _update_brush_values(self, context):
    target = active_target(context)
    owner = color_owner(context)
    v1 = value_to_linear(self.brush_value, target, self.value_space)
    v2 = value_to_linear(self.brush_secondary_value, target, self.value_space)
    owner.color = (v1, v1, v1)
    owner.secondary_color = (v2, v2, v2)


def _toggle_grayscale(self, context):
    owner = color_owner(context)
    if self.use_grayscale:
        self.stored_color = owner.color
        self.stored_secondary_color = owner.secondary_color
        _update_brush_values(self, context)
    else:
        owner.color = self.stored_color
        owner.secondary_color = self.stored_secondary_color


class ColorDataMasterSettings(bpy.types.PropertyGroup):
    # -- general --------------------------------------------------------
    edit_target: EnumProperty(
        name="Edit Target",
        items=(
            ('AUTO', "By Mode", "Texture Paint mode edits the paint texture, other modes the "
             "active color attribute"),
            ('ATTR', "Color Attribute", "Always edit the active color attribute"),
            ('IMAGE', "Texture", "Always edit the active paint texture"),
        ),
        default='AUTO',
    )
    value_space: EnumProperty(
        name="Values",
        description="How values typed into the tools are interpreted",
        items=VALUE_SPACE_ITEMS,
        default='AUTO',
    )
    uv_map: StringProperty(
        name="UV Map",
        description="UV map used between textures and mesh data. Empty uses the active UV map",
    )
    bake_padding: IntProperty(
        name="Padding",
        description="Pixels to bleed outward from UV islands when writing to a texture",
        default=4, min=0, max=64,
    )

    # -- brush ----------------------------------------------------------
    use_grayscale: BoolProperty(
        name="Use Grayscale",
        description="Use value sliders instead of color pickers for the brush",
        default=False, update=_toggle_grayscale,
    )
    match_brush_to_active_channels: BoolProperty(
        name="Match Active Channels",
        description="Set the brush color to match the active channels",
        default=True, update=_update_active_channels,
    )
    brush_value: FloatProperty(
        name="Brush Value", min=0.0, max=1.0, default=1.0,
        description="Primary brush value, in the chosen value space",
        update=_update_brush_values,
    )
    brush_secondary_value: FloatProperty(
        name="Brush Secondary Value", min=0.0, max=1.0, default=0.0,
        description="Secondary brush value, in the chosen value space",
        update=_update_brush_values,
    )
    stored_color: FloatVectorProperty(subtype='COLOR', default=(1.0, 0.0, 0.0))
    stored_secondary_color: FloatVectorProperty(subtype='COLOR', default=(0.0, 0.0, 0.0))
    # brush colors from before isolating a channel, restored on apply / discard
    iso_color: FloatVectorProperty(subtype='COLOR', default=(1.0, 0.0, 0.0))
    iso_secondary_color: FloatVectorProperty(subtype='COLOR', default=(0.0, 0.0, 0.0))

    # -- channels -------------------------------------------------------
    active_channels: EnumProperty(
        name="Active Channels",
        options={'ENUM_FLAG'},
        items=CHANNEL_ITEMS,
        default={'R', 'G', 'B'},
        update=_update_active_channels,
    )

    # -- transfer -------------------------------------------------------
    src_kind: EnumProperty(name="Source Type", items=LAYER_KINDS, default='ATTR')
    src_name: StringProperty(name="Source")
    src_channel: EnumProperty(name="Source Channel", items=CHANNEL_ITEMS, default='R')
    dst_kind: EnumProperty(name="Destination Type", items=LAYER_KINDS, default='ATTR')
    dst_name: StringProperty(name="Destination")
    dst_channel: EnumProperty(name="Destination Channel", items=CHANNEL_ITEMS, default='G')
    transfer_mode: EnumProperty(name="Mode", items=TRANSFER_MODE_ITEMS, default='CHANNEL')
    blend_mode: EnumProperty(name="Blend", items=BLEND_ITEMS, default='REPLACE')
