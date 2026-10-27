"""A skin must keep the original UV/topology even when paint preparation changes it."""
from __future__ import annotations

import importlib.util
import io
from pathlib import Path
import subprocess
import sys

import numpy as np
from PIL import Image
import pytest

from image_to_3dlab.glb_skins import _image_bytes, _source, paint_images
from image_to_3dlab.blender import find_blender
from image_to_3dlab.glb_turn import build_glb, parse_glb
from scripts.blender_bake_skin import _paint_contract


def _cube(colour, alternate=False):
    faces = [((-1, -1, -1), (-1, -1, 1), (-1, 1, 1), (-1, 1, -1)),
             ((1, -1, 1), (1, -1, -1), (1, 1, -1), (1, 1, 1)),
             ((-1, -1, 1), (-1, -1, -1), (1, -1, -1), (1, -1, 1)),
             ((-1, 1, -1), (-1, 1, 1), (1, 1, 1), (1, 1, -1)),
             ((1, -1, -1), (-1, -1, -1), (-1, 1, -1), (1, 1, -1)),
             ((-1, -1, 1), (1, -1, 1), (1, 1, 1), (-1, 1, 1))]
    positions = np.asarray(faces, dtype='<f4').reshape(-1, 3)
    normals = np.repeat([np.cross(np.subtract(f[1], f[0]), np.subtract(f[2], f[0])) / 4
                         for f in faces], 4, axis=0).astype('<f4')
    uv = np.tile(np.array([[0, 0], [1, 0], [1, 1], [0, 1]], dtype='<f4'), (6, 1))
    tri = (0, 1, 3, 1, 2, 3) if alternate else (0, 1, 2, 0, 2, 3)
    indices = np.array([base + i for base in range(0, 24, 4) for i in tri], dtype='<u2')
    blobs = [positions.tobytes(), normals.tobytes(), uv.tobytes(), indices.tobytes()]
    for rgb in (colour, (0, 220, 0), (128, 128, 255)):
        stream = io.BytesIO()
        Image.new('RGB', (16, 16), rgb).save(stream, format='PNG')
        blobs.append(stream.getvalue())
    binary = bytearray()
    views = []
    for blob in blobs:
        binary.extend(b'\0' * (-len(binary) % 4))
        views.append({'buffer': 0, 'byteOffset': len(binary), 'byteLength': len(blob)})
        binary.extend(blob)
    doc = {'asset': {'version': '2.0'}, 'buffers': [{'byteLength': len(binary)}],
           'bufferViews': views,
           'accessors': [{'bufferView': i, 'componentType': 5123 if i == 3 else 5126,
                          'count': 36 if i == 3 else 24,
                          'type': ('VEC3', 'VEC3', 'VEC2', 'SCALAR')[i],
                          **({'min': [-1, -1, -1], 'max': [1, 1, 1]} if i == 0 else {})}
                         for i in range(4)],
           'meshes': [{'primitives': [{'attributes': {'POSITION': 0, 'NORMAL': 1, 'TEXCOORD_0': 2},
                                       'indices': 3, 'material': 0}]}],
           'nodes': [{'mesh': 0}], 'scenes': [{'nodes': [0]}], 'scene': 0,
           'materials': [{'normalTexture': {'index': 2}, 'pbrMetallicRoughness': {
               'baseColorTexture': {'index': 0}, 'metallicRoughnessTexture': {'index': 1}}}],
           'textures': [{'source': i} for i in range(3)],
           'images': [{'bufferView': i + 4, 'mimeType': 'image/png'} for i in range(3)]}
    return build_glb(doc, bytes(binary))


def test_uv_transform_is_refused_instead_of_silently_mispainting():
    doc, binary = parse_glb(_cube((0, 0, 255)))
    doc['materials'][0]['pbrMetallicRoughness']['baseColorTexture']['extensions'] = {
        'KHR_texture_transform': {'rotation': 1.0}}
    with pytest.raises(ValueError, match='untransformed'):
        _paint_contract(build_glb(doc, binary))


@pytest.mark.skipif(importlib.util.find_spec('bpy') is None and find_blender() is None,
                    reason='requires native Blender or bpy')
def test_real_bake_preserves_original_topology_uv_and_normal_when_paint_topology_differs(tmp_path):
    original = _cube((0, 0, 255))
    paint = _cube((0, 255, 0), alternate=True)
    source, target, output = (tmp_path / name for name in ('paint.glb', 'original.glb', 'skin.glb'))
    source.write_bytes(paint)
    target.write_bytes(original)
    script = Path(__file__).resolve().parents[1] / 'scripts/blender_bake_skin.py'
    blender = find_blender()
    launcher = ([str(blender), '--background', '--python-exit-code', '1', '--python', str(script)]
                if blender else [sys.executable, str(script)])
    result = subprocess.run([*launcher, '--', str(source), str(target), str(output), '256'],
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    before, old_bin = parse_glb(original)
    prepared, paint_bin = parse_glb(paint)
    after, new_bin = parse_glb(output.read_bytes())
    def view(doc, binary, accessor):
        entry = doc['bufferViews'][doc['accessors'][accessor]['bufferView']]
        return binary[entry['byteOffset']:entry['byteOffset'] + entry['byteLength']]
    assert view(before, old_bin, 3) != view(prepared, paint_bin, 3)
    assert after['meshes'] == before['meshes']
    for index in range(4):
        assert view(before, old_bin, index) == view(after, new_bin, index)
    def normal(doc, binary):
        texture = doc['textures'][doc['materials'][0]['normalTexture']['index']]
        return _image_bytes(doc, binary, _source(texture))[0]
    assert normal(before, old_bin) == normal(after, new_bin)
    colour = _image_bytes(after, new_bin, paint_images(after)['baseColorTexture'])[0]
    pixels = np.asarray(Image.open(io.BytesIO(colour)).convert('RGB'))
    assert pixels[128, 128].tolist() == [0, 255, 0]
    assert target.read_bytes() == original and source.read_bytes() == paint
