#!/usr/bin/env python3
"""Bake a repaint onto the original Finish UVs, publishing only new paint images.

Run in headless Blender: -- painted.glb original.glb skin.glb [texture_size].
Blender's exporter is deliberately not used: even an unchanged import/export can
reorder vertices. The original GLB supplies every non-paint byte and property.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from blender_bake_detail import _bake, _bbox, _import, _target_image, fit, parse_args, ray_reach
from image_to_3dlab.glb_skins import dress, paint_images
from image_to_3dlab.glb_turn import build_glb, parse_glb


def _paint_contract(data: bytes) -> dict:
    document, _ = parse_glb(data)
    used = {p.get("material") for m in document.get("meshes", []) for p in m.get("primitives", [])}
    if used != {0}:
        raise ValueError("skin baking requires the finished model's single paint atlas")
    pbr = document["materials"][0].get("pbrMetallicRoughness", {})
    if set(paint_images(document)) != {"baseColorTexture", "metallicRoughnessTexture"}:
        raise ValueError("skin baking requires colour and metallic-roughness maps")
    for role in ("baseColorTexture", "metallicRoughnessTexture"):
        ref = pbr[role]
        if ref.get("texCoord", 0) != 0 or ref.get("extensions"):
            raise ValueError("skin baking requires the untransformed primary UV map")
    return {"baseColorFactor": pbr.get("baseColorFactor", [1, 1, 1, 1]),
            "metallicFactor": pbr.get("metallicFactor", 1),
            "roughnessFactor": pbr.get("roughnessFactor", 1)}


def _paint_carrier(colour: bytes, shine: bytes) -> bytes:
    offset = (len(colour) + 3) & ~3
    binary = colour + b"\0" * (offset - len(colour)) + shine
    return build_glb({
        "asset": {"version": "2.0"}, "buffers": [{"byteLength": len(binary)}],
        "bufferViews": [{"buffer": 0, "byteOffset": 0, "byteLength": len(colour)},
                        {"buffer": 0, "byteOffset": offset, "byteLength": len(shine)}],
        "images": [{"bufferView": i, "mimeType": "image/png"} for i in range(2)],
        "textures": [{"source": i} for i in range(2)],
        "materials": [{"pbrMetallicRoughness": {
            "baseColorTexture": {"index": 0}, "metallicRoughnessTexture": {"index": 1}}}],
    }, binary)


def main() -> int:
    import bpy
    from mathutils import Matrix
    from image_to_3dlab.glb_skins import _image_bytes

    args = parse_args(list(sys.argv))
    source, original, output = Path(args.source), Path(args.target), Path(args.output)
    source_data, original_data = source.read_bytes(), original.read_bytes()
    if _paint_contract(source_data) != _paint_contract(original_data):
        raise ValueError("repaint material factors differ from the original; refusing a texture-only swap")
    source_doc, source_binary = parse_glb(source_data)
    bpy.ops.wm.read_factory_settings(use_empty=True)
    sources, targets = _import(bpy, str(source)), _import(bpy, str(original))
    if not sources or len(targets) != 1 or not targets[0].data.uv_layers:
        raise ValueError("skin baking needs a painted source and one finished mesh with UVs")
    target = targets[0]
    hlo, hhi = _bbox(sources)
    llo, lhi = _bbox([target])
    shape = fit(tuple(hhi - hlo), tuple(lhi - llo))
    if not shape.ok:
        raise ValueError(f"source and original do not line up (axis spread {shape.spread:.3f})")
    offset = (llo + lhi) / 2 - (hlo + hhi) / 2 * shape.scale
    move = Matrix.Translation(offset) @ Matrix.Scale(shape.scale, 4)
    for obj in sources:
        obj.matrix_world = move @ obj.matrix_world
    bpy.context.view_layer.update()
    scene = bpy.context.scene
    scene.render.engine = "CYCLES"
    scene.cycles.device = "CPU"
    scene.cycles.samples = 1
    scene.render.threads_mode = "FIXED"
    scene.render.threads = 2
    bake = scene.render.bake
    bake.use_selected_to_active = True
    bake.use_clear = True
    bake.margin = 16
    bake.cage_extrusion = ray_reach(tuple(target.dimensions))
    bake.max_ray_distance = bake.cage_extrusion * 2
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="skin-bake-", dir=output.parent) as scratch:
        scratch = Path(scratch)
        pictures = []
        # Read raw glTF paint maps. Material factors remain on the original GLB,
        # so baking the imported shader's already-multiplied values would apply them twice.
        for role, image_index in paint_images(source_doc).items():
            blob, mime = _image_bytes(source_doc, source_binary, image_index)
            image_path = scratch / (role + (".webp" if mime == "image/webp" else ".png"))
            image_path.write_bytes(blob)
            source_image = bpy.data.images.load(str(image_path), check_existing=False)
            is_colour = role == "baseColorTexture"
            space = "sRGB" if is_colour else "Non-Color"
            source_image.colorspace_settings.name = space
            emit = bpy.data.materials.new(role)
            emit.use_nodes = True
            nodes, links = emit.node_tree.nodes, emit.node_tree.links
            nodes.clear()
            tex = nodes.new("ShaderNodeTexImage")
            tex.image = source_image
            emission = nodes.new("ShaderNodeEmission")
            surface = nodes.new("ShaderNodeOutputMaterial")
            links.new(tex.outputs["Color"], emission.inputs["Color"])
            links.new(emission.outputs["Emission"], surface.inputs["Surface"])
            for obj in sources:
                for slot in obj.material_slots:
                    slot.material = emit
            image, _ = _target_image(bpy, target, "skin_" + role, args.size)
            image.colorspace_settings.name = space
            _bake(bpy, sources, target, "EMIT")
            image.filepath_raw = str(scratch / (role + "-baked.png"))
            image.file_format = "PNG"
            image.save()
            pictures.append(Path(image.filepath_raw).read_bytes())
        result = dress(original_data, _paint_carrier(*pictures))
        partial = scratch / "skin.glb"
        partial.write_bytes(result)
        partial.replace(output)
    print("SKIN_BAKE::" + json.dumps({"source": str(source), "original": str(original),
          "output": str(output), "size": args.size, "scale": shape.scale,
          "axis_spread": shape.spread, "geometry": "original", "normal_map": "original"}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
