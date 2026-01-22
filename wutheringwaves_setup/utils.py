import bpy
import logging
import math
import os
import re
from collections import defaultdict, deque, namedtuple
from math import cos, pi, sin
from typing import Any, Dict, List, Optional, Set, Tuple

import mathutils
from bpy.props import (
    BoolProperty,
    CollectionProperty,
    FloatProperty,
    FloatVectorProperty,
    IntProperty,
    StringProperty,
)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
logger.handlers.clear()
handler = logging.StreamHandler()
formatter = logging.Formatter("%(levelname)s - %(message)s")
handler.setFormatter(formatter)
logger.addHandler(handler)
logger.propagate = False

TEXTURE_TYPE_MAPPINGS = {
    "_D": (
        "Base Color",
        "Bangs Diffuse",
        "Hair Diffuse",
        "Face Diffuse",
        "Eye Diffuse",
    ),
    "_N": ("Normal Map",),
    "_HM": ("Hair HM", "Bangs HM"),
    "_HET": ("Eye HET", "Face HET"),
    "_ID": ("Mask ID",),
}

LIGHT_MODES = {
    0: "Default",
    1: "Sunrise",
    2: "Day",
    3: "Sunset",
    4: "Night",
    5: "Rainy",
    6: "Custom",
}

TextureSearchParameters = namedtuple(
    "TextureSearchParameters",
    ["base_part", "version", "suffix", "original_name", "mode"],
)
MaterialDetails = namedtuple(
    "MaterialDetails", ["base_part", "version", "original_name"]
)
MaterialTextureData = namedtuple(
    "MaterialTextureData",
    [
        "material",
        "material_info",
        "texture_suffixes",
        "textures",
        "tex_dir",
        "tex_mode",
    ],
)


def get_armature_from_modifiers(mesh):
    for modifier in mesh.modifiers:
        if modifier.type == "ARMATURE" and modifier.object:
            return modifier.object
    return None


def load_image(path: str) -> Optional[bpy.types.Image]:
    try:
        img = bpy.data.images.get(os.path.basename(path))
        if not img:
            logger.info(f"Loading texture: {os.path.basename(path)}")
            img = bpy.data.images.load(path)
            img.alpha_mode = "CHANNEL_PACKED"
            img.colorspace_settings.name = "sRGB" if "_D" in path else "Non-Color"
        return img
    except Exception as e:
        logger.error(f"Failed to load texture image {path}: {str(e)}")
        return None


def find_texture_node(
    material: bpy.types.Material, name: str
) -> Optional[bpy.types.Node]:
    if not material.node_tree:
        return None
    return next(
        (
            node
            for node in material.node_tree.nodes
            if node.name == name and node.type == "TEX_IMAGE"
        ),
        None,
    )


def find_texture(
    textures: List[Any], patterns: List[str], tex_dir: str
) -> Optional[bpy.types.Image]:
    for pattern in patterns:
        for file in textures:
            fname = file.name if hasattr(file, "name") else file
            if re.match(pattern, fname):
                return load_image(os.path.join(tex_dir, fname))
    return None


def set_texture(
    material: bpy.types.Material, image: bpy.types.Image, nodes: Tuple[str]
):
    for node_name in nodes:
        if node := find_texture_node(material, node_name):
            node.image = image


def set_node_input(material: bpy.types.Material, input_name: str, value: float):
    if not material.node_tree:
        return
    for node in material.node_tree.nodes:
        if (
            node.type == "GROUP"
            and node.node_tree
            and node.node_tree.name in ["Shadow Mask Converter", "Texture Converter"]
        ):
            for inp in node.inputs:
                if inp.type == "VALUE" and input_name in inp.name:
                    inp.default_value = value


def darken_eye_colors(mesh: bpy.types.Object):
    try:
        if not mesh.data.vertex_colors:
            mesh.data.vertex_colors.new()

        vertex_color_layer = mesh.data.vertex_colors.active
        eye_material_indices = {
            i
            for i, slot in enumerate(mesh.material_slots)
            if slot.material and "Eye" in slot.material.name
        }

        if not eye_material_indices:
            return

        was_in_object_mode = bpy.context.mode != "VERTEX_PAINT"

        if was_in_object_mode:
            bpy.ops.object.mode_set(mode="VERTEX_PAINT")

        for poly in mesh.data.polygons:
            if poly.material_index in eye_material_indices:
                for loop_idx in poly.loop_indices:
                    vertex_color_layer.data[loop_idx].color = (0, 0, 0, 1)

        if was_in_object_mode:
            bpy.ops.object.mode_set(mode="OBJECT")
    except Exception as e:
        logger.error(f"Failed to darken eye vertex colors: {str(e)}")


def split_material_name(mat_name: str) -> Tuple[str, str]:
    parts = mat_name.split("_", 2)
    if len(parts) < 2:
        return "", ""

    category_part = parts[1]
    words = re.findall(r"[A-Z][a-z]*", category_part)
    if not words:
        return "", category_part if len(parts) <= 2 else "_" + parts[2]

    base_part = words[-1]
    try:
        version_start = category_part.rindex(base_part) + len(base_part)
        version = category_part[version_start:]
        if len(parts) > 2:
            version += "_" + parts[2]
    except ValueError:
        version = ""
    return base_part, version


def get_mesh_data(context, mesh_name):
    data = next(
        (m for m in context.scene.mesh_texture_mappings if m.mesh_name == mesh_name),
        None,
    )
    if not data:
        data = context.scene.mesh_texture_mappings.add()
        data.mesh_name = mesh_name
        data.tex_mode = True
        data.star_move = False
        data.hair_trans = False
    return data


def set_solid_view():
    for area in bpy.context.screen.areas:
        if area.type == "VIEW_3D":
            area.spaces.active.shading.type = "SOLID"
            break


def set_material_view():
    for area in bpy.context.screen.areas:
        if area.type == "VIEW_3D":
            area.spaces.active.shading.type = "MATERIAL"
            break


def get_suffix():
    base_objects = [
        o for o in bpy.data.objects if o.name.startswith("Light Direction")]
    return (
        "." + base_objects[-1].name.split(".")[-1]
        if len(base_objects) > 1 and "." in base_objects[-1].name
        else ""
    )


def make_texture_patterns(params: TextureSearchParameters):
    patterns = []
    
    # For Version mode (tex_mode=False), add alternative texture patterns first
    # These patterns support: _Switch_D (e.g., Down_Switch_D) and Damage variants (e.g., DownDamage_D)
    if not params.mode:  # Version mode
        if params.original_name:
            if match := re.search(r"MI_(.*)", params.original_name):
                base = match.group(1)
                base_no_ver = re.sub(r"[0-9_]+$", "", base)
                
                # Switch pattern: Down_D -> Down_Switch_D
                switch_pat = f"T_{base_no_ver}_Switch{params.suffix}"
                patterns.append(switch_pat)
                
                # Damage pattern: Down_D -> DownDamage_D
                damage_pat = f"T_{base_no_ver}Damage{params.suffix}"
                patterns.append(damage_pat)
        else:
            base_no_ver = re.sub(r"[0-9_]+$", "", params.base_part)
            
            # Switch pattern with regex
            switch_pat = f"T_.*?{base_no_ver}_Switch{params.suffix}"
            patterns.append(switch_pat)
            
            # Damage pattern with regex
            damage_pat = f"T_.*?{base_no_ver}Damage{params.suffix}"
            patterns.append(damage_pat)

    # Original logic for base and version patterns
    if params.original_name:
        if match := re.search(r"MI_(.*)", params.original_name):
            base = match.group(1)
            base_no_ver = re.sub(r"[0-9_]+$", "", base)

            replacements = {"Up": "Upper", "Eye": "Eyes", "Star": "Up"}

            for k, v in replacements.items():
                if k in base:
                    base_pat = f"T_{base_no_ver}{params.suffix}"
                    ver_pat = f"T_{base}{params.suffix}"
                    patterns.extend(
                        [ver_pat, base_pat] if not params.mode else [
                            base_pat, ver_pat]
                    )
                    patterns.extend([p.replace(k, v) for p in patterns[:]])
                    return list(dict.fromkeys(patterns))

            base_pat = f"T_{base_no_ver}{params.suffix}"
            ver_pat = f"T_{base}{params.suffix}"
            patterns.extend(
                [ver_pat, base_pat] if not params.mode else [base_pat, ver_pat]
            )
    else:
        base_no_ver = re.sub(r"[0-9_]+$", "", params.base_part)

        replacements = {"Up": "Upper", "Eye": "Eyes", "Star": "Up"}

        for k, v in replacements.items():
            if k in params.base_part:
                base_pat = f"T_.*?{base_no_ver}{params.suffix}"
                ver_pat = f"T_.*?{params.base_part}{params.version}{params.suffix}"
                patterns.extend(
                    [ver_pat, base_pat] if not params.mode else [base_pat, ver_pat]
                )
                patterns.extend([p.replace(k, v) for p in patterns[:]])
                return list(dict.fromkeys(patterns))

        base_pat = f"T_.*?{base_no_ver}{params.suffix}"
        ver_pat = f"T_.*?{params.base_part}{params.version}{params.suffix}"
        patterns.extend([ver_pat, base_pat]
                        if not params.mode else [base_pat, ver_pat])

    return list(dict.fromkeys(patterns))


def apply_textures(mat_tex_data: MaterialTextureData):
    has_mask_id = False
    for suffix, nodes in mat_tex_data.texture_suffixes.items():
        params = TextureSearchParameters(
            mat_tex_data.material_info.base_part,
            mat_tex_data.material_info.version,
            suffix,
            mat_tex_data.material_info.original_name,
            mat_tex_data.tex_mode,
        )
        patterns = make_texture_patterns(params)
        img = find_texture(mat_tex_data.textures,
                           patterns, mat_tex_data.tex_dir)
        if img:
            set_texture(mat_tex_data.material, img, nodes)
            if suffix == "_ID":
                has_mask_id = True
    set_node_input(mat_tex_data.material, "Use ID Color",
                   1.0 if has_mask_id else 0.0)


def extract_character_name(name: str, title_case: bool = True) -> str:
    """
    Extracts the character name from the asset name.
    Example: R2T1ChangLiMd10011_LOD0 -> Changli (if title_case=True) or ChangLi
    """
    if match := re.search(r"R2T1(.+?)Md\d+_LOD\d+", name):
        extracted = match.group(1)
        return extracted.title() if title_case else extracted
    return name
