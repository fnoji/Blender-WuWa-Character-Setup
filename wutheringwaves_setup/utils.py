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

# Shader Type Constants
SHADER_TYPE_JAREDNYTS = "JAREDNYTS"
SHADER_TYPE_JONN = "JONN_GATHERING"

# JaredNyts Shader Texture Mappings (default)
TEXTURE_TYPE_MAPPINGS_JAREDNYTS = {
    "_D": (
        "Base Color",
        "Bangs Diffuse",
        "Hair Diffuse",
        "Face Diffuse",
        "Eye Diffuse",
        "Body Diffuse",
        "Npc Diffuse",
        "NPC Diffuse",
    ),
    "_N": ("Normal Map",),
    "_HM": ("Hair HM", "Bangs HM", "Normal Map"),
    "_HET": ("Eye HET", "Face HET"),
    "_ID": ("Mask ID",),
}

# Jonn Gathering Wives Shader Texture Mappings
# Maps texture file suffix to actual Image Texture node names in the materials
TEXTURE_TYPE_MAPPINGS_JONN = {
    "_D": (
        "Base Color",        # WW - Main (Body)
        "Face Diffuse",      # WW - Face
        "Eye Diffuse",       # WW - Eye
        "Hair Diffuse",      # WW - Hair
        "Bangs Diffuse",     # WW - Bangs
    ),
    "_N": ("Normal Map",),
    "_FTM": ("FTM",),
    "_LD": (
        "LD",           # WW - Main (Body)
        "Bangs LD",     # WW - Hair/Bangs (node shares name)
    ),
    "_HM": (
        "Hair HM",      # WW - Hair
        "Bangs HM",     # WW - Bangs
    ),
    "_HET": (
        "Eye HET",      # WW - Eye
        "Face HET",     # WW - Face
    ),
    "_ID": ("Mask ID",),
    "_RGID": ("Mask RGID",),  # RGID uses dedicated RGID node
    "_Skin": ("Skin",),
}

# Alias for backward compatibility
TEXTURE_TYPE_MAPPINGS = TEXTURE_TYPE_MAPPINGS_JAREDNYTS


def get_texture_mappings(shader_type: str) -> dict:
    """Returns the appropriate texture mappings for the given shader type."""
    if shader_type == SHADER_TYPE_JONN:
        return TEXTURE_TYPE_MAPPINGS_JONN
    return TEXTURE_TYPE_MAPPINGS_JAREDNYTS


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
        "shader_type",
    ],
)


def get_armature_from_modifiers(mesh):
    for modifier in mesh.modifiers:
        if modifier.type == "ARMATURE" and modifier.object:
            return modifier.object
    return None


def get_target_mesh(context) -> Optional[bpy.types.Object]:
    """
    Resolves the target character mesh from the current context.
    Prioritizes active object if it's a Mesh.
    If active object is Armature, looks for a Mesh that uses this Armature.
    """
    active_obj = context.active_object
    if not active_obj:
        return None

    if active_obj.type == 'MESH':
        return active_obj
    
    if active_obj.type == 'ARMATURE':
        # Find mesh that targets this armature
        # We iterate over selected objects first to prioritize selection
        # If not found in selection, check all objects (fallback)
        
        # Priority 1: Selected Mesh that uses this armature (if multiple selected)
        # However, requirement says "If multiple selected, prioritize higher (light orange)". 
        # But 'active_object' IS the light orange one.
        # If active is Armature, we need the associated mesh.
        
        candidates = [
            obj for obj in bpy.data.objects 
            if obj.type == 'MESH' and get_armature_from_modifiers(obj) == active_obj
        ]
        
        if candidates:
            return candidates[0]
            
    return None


def load_image(path: str, shader_type: str = SHADER_TYPE_JAREDNYTS) -> Optional[bpy.types.Image]:
    try:
        img = bpy.data.images.get(os.path.basename(path))
        if not img:
            logger.info(f"Loading texture: {os.path.basename(path)}")
            img = bpy.data.images.load(path)
            img.alpha_mode = "CHANNEL_PACKED"
            
            # Color Space Logic
            if shader_type == SHADER_TYPE_JONN:
                # Jonn Mode: _D, _Skin, _ID, _RGID, _HM, _LD are sRGB
                needs_srgb = (
                    "_D" in path 
                    or "_Skin" in path 
                    or "_ID" in path 
                    or "_RGID" in path
                    or "_HM" in path
                    or "_LD" in path
                )
            else:
                # JaredNyts Mode: _D, _Skin are sRGB. _ID is Non-Color.
                needs_srgb = "_D" in path or "_Skin" in path

            img.colorspace_settings.name = "sRGB" if needs_srgb else "Non-Color"
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
    textures: List[Any], patterns: List[str], tex_dir: str, shader_type: str = SHADER_TYPE_JAREDNYTS
) -> Optional[bpy.types.Image]:
    for pattern in patterns:
        for file in textures:
            fname = file.name if hasattr(file, "name") else file
            if re.match(pattern, fname):
                return load_image(os.path.join(tex_dir, fname), shader_type)
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
    # Strip _SEETHRU suffix for base name extraction (texture import compatibility)
    if mat_name.endswith("_SEETHRU"):
        mat_name = mat_name[:-8]
    elif " " in mat_name and mat_name.split(" ")[0].endswith("_SEETHRU"):
        # Handle "WW - Face_SEETHRU Changli" -> "WW - Face Changli"
        parts_space = mat_name.split(" ")
        parts_space[0] = parts_space[0][:-8]  # Remove _SEETHRU from first part
        mat_name = " ".join(parts_space)
    
    parts = mat_name.split("_", 2)
    if len(parts) < 2:
        return "", ""

    category_part = parts[1]
    
    # Handle NPC naming where category might be just "Npc" or similar simple names
    if category_part in ["Npc", "NH", "NPC"]:
        # Try to extract the part name from the end of the remaining string
        # e.g. FemaleMS_003_Bangs -> Bangs
        if len(parts) > 2:
            remaining = parts[2]
            # Split by rightmost underscore to get suffix
            if "_" in remaining:
                suffix = remaining.rsplit("_", 1)[-1]
                
                # Clean suffix if it starts with numbers but contains text (e.g. 001Face -> Face)
                # This ensures we match 'WW - Face' instead of 'WW - 001Face'
                if re.match(r"^\d+[A-Za-z]", suffix):
                    suffix = re.sub(r"^\d+", "", suffix)
                
                return suffix, ""
            
            # If no underscore, maybe the whole thing is the part?
            # Also clean here just in case
            if re.match(r"^\d+[A-Za-z]", remaining):
                return re.sub(r"^\d+", "", remaining), ""
            
            return remaining, ""
         
        base_part = category_part
        # Logic for version: likely the next part is Body/Face etc.
        if len(parts) > 2:
            version = "_" + parts[2]
        else:
            version = ""
        return base_part, version

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
    """Get suffix for control objects. Supports JaredNyts and Jonn shaders."""
    # Check for JaredNyts objects first
    jarednyts_objects = [o for o in bpy.data.objects if o.name.startswith("Light Direction")]
    # Check for Jonn objects
    jonn_objects = [o for o in bpy.data.objects if o.name.startswith("Main Light")]
    
    base_objects = jarednyts_objects if jarednyts_objects else jonn_objects
    
    return (
        "." + base_objects[-1].name.split(".")[-1]
        if len(base_objects) > 1 and "." in base_objects[-1].name
        else ""
    )


def make_texture_patterns(params: TextureSearchParameters):
    patterns = []
    
    # Special handling for shared textures like Skin
    # These textures are typically named generically without material-specific prefixes
    if params.suffix == "_Skin":
        # Add generic Skin patterns first
        patterns.append("T_.*?Skin")
        patterns.append("Texture_Skin")
        patterns.append(".*Skin")  # Very loose match as fallback
        return patterns
    
    # NPC support: Add patterns for NpcBody etc.
    # If base_part is Npc, we want T_NpcBody_D.png etc. 
    # Current logic extracts base_no_ver from base_part.
    
    # For Version mode (tex_mode=False), add alternative texture patterns first
    if not params.mode:  # Version mode
        if params.original_name:
            if match := re.search(r"MI_(.*)", params.original_name):
                base = match.group(1)
                base_no_ver = re.sub(r"[0-9_]+$", "", base)
                
                # Switch pattern
                switch_pat = f"T_{base_no_ver}_Switch{params.suffix}"
                patterns.append(switch_pat)
                
                # Damage pattern
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

    # Standard patterns
    if params.original_name:
        if match := re.search(r"MI_(.*)", params.original_name):
            base = match.group(1)
            base_no_ver = re.sub(r"[0-9_]+$", "", base)

            # Strip NH_ prefix if present for finding normalized textures
            if base.startswith("NH_"):
                base_no_nh = base[3:]
                base_no_ver_no_nh = re.sub(r"[0-9_]+$", "", base_no_nh)
                
                # Add No-NH patterns (T_FemaleMS_003_Bangs...)
                patterns.append(f"T_{base_no_nh}{params.suffix}")
                patterns.append(f"T_{base_no_ver_no_nh}{params.suffix}")
            
            # Special logic for Up02 to allow fallback to Down, preventing generic Up match
            if "Up02" in base:
                # 1. Search specific Up02
                patterns.append(f"T_{base}{params.suffix}")
                # 2. Search Down
                patterns.append(f"T_.*?Down{params.suffix}")
                # Return immediately to avoid 'Up' -> 'Up' generic match
                return list(dict.fromkeys(patterns))

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
        # Fallback for constructed names
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
        
        # Generic construction
        # For NPC: base_part="Npc", version="Body". 
        # base_no_ver="Npc". 
        # patterns: T_...Npc... , T_...NpcBody...
        
        base_pat = f"T_.*?{base_no_ver}{params.suffix}"
        
        # Original: f"T_.*?{params.base_part}{params.version}{params.suffix}"
        # If version starts with "_", we might want to handle it.
        # But split_material_name puts "_" in version usually.
        # Check if version has leading underscore that matches file naming?
        # File naming usually: T_NpcBody_D.png -> "NpcBody"
        # base_part="Npc", version="_Body" -> "Npc_Body"? No usually concatenated or snake case.
        # Let's add patterns with and without underscore separator for version if it helps.
        
        ver_pat = f"T_.*?{params.base_part}{params.version}{params.suffix}"
        # Clean potential double underscores or issues
        ver_pat = ver_pat.replace("__", "_")
        
        # Special case for NPC loose matching
        if "Npc" in params.base_part:
             # Try stricter match first? Or just add it.
             # T_NpcBody is common.
             # base_part=Npc, version=_Body -> T_.*?Npc_Body
             # We also want T_.*?NpcBody
             ver_pat_joined = f"T_.*?{params.base_part}{params.version.replace('_', '')}{params.suffix}"
             patterns.append(ver_pat_joined)

        # Special Case: Up02 often implies Lower Body parts which share "Down" textures
        if "Up02" in params.base_part:
             # Add Patterns for "Down"
             patterns.append(f"T_.*?Down{params.suffix}")
             patterns.append(f"T_Down{params.suffix}")
             # Add specific Up02 if needed, but return to avoid generic 'Up' logic
             patterns.append(ver_pat)
             patterns.append(base_pat)
             return list(dict.fromkeys(patterns))

        patterns.extend([ver_pat, base_pat]
                        if not params.mode else [base_pat, ver_pat])


    return list(dict.fromkeys(patterns))


def apply_textures(mat_tex_data: MaterialTextureData):
    has_mask_id = False
    shader_type = mat_tex_data.shader_type
    
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
                           patterns, mat_tex_data.tex_dir, shader_type)
        
        # JaredNyts Mode: _FTM Fallback for _ID
        if not img and suffix == "_ID" and shader_type == SHADER_TYPE_JAREDNYTS:
             # Try finding _FTM
             ftm_params = TextureSearchParameters(
                mat_tex_data.material_info.base_part,
                mat_tex_data.material_info.version,
                "_FTM",
                mat_tex_data.material_info.original_name,
                mat_tex_data.tex_mode,
            )
             ftm_patterns = make_texture_patterns(ftm_params)
             # _FTM is Non-Color in JaredNyts
             img = find_texture(mat_tex_data.textures, ftm_patterns, mat_tex_data.tex_dir, shader_type)
             # If found, it will be assigned to 'nodes' which are the _ID slots (Mask ID)
        
        if img:
            set_texture(mat_tex_data.material, img, nodes)
            if suffix == "_ID":
                has_mask_id = True
            
            # Jonn Mode: RGID Switch
            if suffix == "_RGID" and shader_type == SHADER_TYPE_JONN:
                # Find "Group" node and set input[3] to 1.0
                if mat_tex_data.material.node_tree:
                    for node in mat_tex_data.material.node_tree.nodes:
                        if node.name == "Group" and node.type == "GROUP":
                            if len(node.inputs) > 3:
                                node.inputs[3].default_value = 1.0

    set_node_input(mat_tex_data.material, "Use ID Color",
                   1.0 if has_mask_id else 0.0)


# Supported model prefixes — order matters for matching
_MODEL_PREFIX_PATTERNS = [
    ("R2T1", re.compile(r"^R2T1")),
    ("NHT1", re.compile(r"^NHT1")),
    ("NH_",  re.compile(r"^NH_")),
    ("MB1",  re.compile(r"^MB1")),
    ("ML1",  re.compile(r"^ML1")),
    ("NA0",  re.compile(r"^NA0")),
    ("NM0",  re.compile(r"^NM0")),
]

# Prefixes that always have Biped skeletons (human characters/NPCs)
ALWAYS_BIPED_PREFIXES = {"R2T1", "NHT1", "NH_"}
# Prefixes that require a Biped check before rig generation
BIPED_CHECK_PREFIXES = {"MB1", "ML1", "NA0", "NM0"}


def get_model_prefix(name: str) -> Optional[str]:
    """Return the model prefix (R2T1/NHT1/NH_/MB1/ML1/NA0/NM0) or None if unknown."""
    if name.endswith("_Skeleton"):
        name = name[:-9]
    for prefix, pattern in _MODEL_PREFIX_PATTERNS:
        if pattern.match(name):
            return prefix
    return None


def extract_character_name(name: str, title_case: bool = True) -> str:
    """
    Extracts the character name from the asset name.
    Example: R2T1ChangLiMd10011_LOD0 -> Changli (if title_case=True) or ChangLi
    Supports R2T1 / NHT1 / NH_ / MB1 / ML1 / NA0 / NM0 prefixes.
    """
    # Remove _Skeleton suffix if present
    if name.endswith("_Skeleton"):
        name = name[:-9] 

    # R2T1 (Standard PC format with Md ID)
    if match := re.search(r"R2T1(.+?)Md\d+_LOD\d+", name):
        extracted = match.group(1)
        return extracted.title() if title_case else extracted
    
    # MB1 (Monster/Boss format with Md ID)
    # Example: MB1FuludelisiMd00411_LOD0 -> Fuludelisi
    if match := re.search(r"MB1(.+?)Md\d+(?:_\w+)?_LOD\d+", name):
        extracted = match.group(1)
        return extracted.title() if title_case else extracted

    # ML1 (Lord format with Md ID, may have _Body_ before LOD)
    # Example: ML1FerLianMd00201_Body_LOD0 -> Ferlian
    if match := re.search(r"ML1(.+?)Md\d+(?:_\w+)?_LOD\d+", name):
        extracted = match.group(1)
        return extracted.title() if title_case else extracted

    # NA0 / NM0 (Animal format)
    # Example: NA010_LOD0 -> Na010, NM0Xxx_LOD0 -> Nm0Xxx
    if match := re.search(r"((?:NA0|NM0).+?)_LOD\d+", name):
        extracted = match.group(1)
        return extracted.title() if title_case else extracted

    # NHT1 (NPC format without Md ID)
    if match := re.search(r"NHT1(.+?)_LOD\d+", name):
        extracted = match.group(1)
        return extracted.title() if title_case else extracted

    # NH_ (Generic NPC format)
    if match := re.search(r"NH_(.+?)_LOD\d+", name):
        extracted = match.group(1)
        return extracted.title() if title_case else extracted
    
    # Fallback/Pass-through if no match
    return name

