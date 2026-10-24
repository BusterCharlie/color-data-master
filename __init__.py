# SPDX-License-Identifier: GPL-3.0-or-later
"""Color Data Master: edit, convert and transfer color channel data on vertex colors and textures.

Built on ideas from Vertex Color Master by Andrew Palmer (GPL).
"""

if "bpy" in locals():
    import importlib
    for _mod in (core, texture, data, props, ops_channel, ops_isolate, ops_misc, ui):
        importlib.reload(_mod)

import bpy

from . import core, texture, data, props, ops_channel, ops_isolate, ops_misc, ui

classes = (
    (props.ColorDataMasterSettings,)
    + ops_channel.classes
    + ops_isolate.classes
    + ops_misc.classes
    + ui.classes
)

addon_keymaps = []


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.color_data_master = bpy.props.PointerProperty(type=props.ColorDataMasterSettings)

    kc = bpy.context.window_manager.keyconfigs.addon
    if kc:
        for km_name in ("Vertex Paint", "Image Paint"):
            km = kc.keymaps.new(name=km_name)
            kmi = km.keymap_items.new("wm.call_menu_pie", 'V', 'PRESS')
            kmi.properties.name = ui.CDM_MT_pie.bl_idname
            addon_keymaps.append((km, kmi))
            kmi = km.keymap_items.new("color_data_master.flip_colors", 'X', 'PRESS')
            addon_keymaps.append((km, kmi))


def unregister():
    for km, kmi in addon_keymaps:
        km.keymap_items.remove(kmi)
    addon_keymaps.clear()
    del bpy.types.Scene.color_data_master
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
