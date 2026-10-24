# Color Data Master

A Blender 5.2 LTS extension for editing, converting and transferring color channel data on **vertex colors and textures** from one panel. It is a rebuild of Vertex Color Master (Andrew Palmer, GPL) for the color attribute system, with texture support added.

## Install

Edit > Preferences > Get Extensions > the dropdown at the top right > **Install from Disk**, and pick `color_data_master-1.0.0.zip`.

To work on the code, link or copy the `color_data_master` folder into your extensions folder (`%APPDATA%\Blender Foundation\Blender\5.2\extensions\user_default\`).

The panel is in the 3D Viewport sidebar (N), on the **Color Data** tab, in Object, Vertex Paint and Texture Paint mode.

## What it edits

- **Vertex Paint / Object mode:** the active color attribute, any type: byte or float, Vertex or Face Corner.
- **Texture Paint:** the image being painted (the active material paint slot, or the canvas in Single Image mode).
- **Edit Target** overrides that choice, for example to edit a texture from Object mode.

## Values: Stored, Linear, sRGB

Byte color attributes and 8-bit sRGB images store sRGB numbers. Float attributes and float images store linear numbers. Shaders always read linear, so a byte mask value of 0.5 reaches a shader as about 0.21.

The **Values** dropdown sets how numbers typed into the tools are read:

| Setting | Meaning | Use it for |
| --- | --- | --- |
| Stored (default) | The numbers in the layer, no conversion | Packed masks and game data |
| Linear | What shaders see | Values that drive shading math |
| sRGB | What color pickers show | Matching colors by eye |

The grayscale brush sliders follow the same setting. The panel shows the stored value, and the 0-255 value on byte layers.

## Tools

- **Brush:** colors or grayscale values, Mix / Add / Sub / Blur. These switch to the essentials Paint Hard or Blur brush when needed. **X** flips colors and **V** opens the pie menu.
- **Channels:** Fill, Clear, Invert, Posterize, Remap, Blur, and Random Island Values on the active channels (random, or evenly spaced between Min and Max with Order Based). Vertex and face selection masks are respected, in Texture Paint too.
- **Isolate Channel:** copies one channel to a temporary grayscale layer (`CDM-ISO_<channel>_<name>`). Paint it with any tool, then use **Apply** or **Discard**. This works on color attributes and textures.
- **Transfer:** pick a source and destination from Color, Texture, Weights, UV and Normals. Modes are Channel, Channel to RGB, All Channels and Luminance, with Replace / Add / Subtract / Multiply / Divide / Lighten / Darken.
  - A **mesh layer to a texture** is drawn through a UV map, with padding around the UV islands.
  - A **texture to a mesh layer** is sampled at each corner's UV.
  - **Swap** exchanges a channel between two color layers or textures.
- **Misc:** Random Island Colors, Linear and Circular Gradient, plus Blender's HSV, Brightness/Contrast and Dirty Vertex Colors in Vertex Paint.

## Notes

- Texture edits change the image in memory. Save the image (Image > Save) to keep them.
- Islands are found by mesh connectivity. With **Split at UV Seams** on (the default, like the original add-on), UV seams split them too.
- Vertex Color Master's isolate layers (`VCM-ISO_...`) are not recognised. Apply them in the old add-on first.
