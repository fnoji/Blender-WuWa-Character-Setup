import bpy
import bmesh
import logging
import math
import os
import re
from collections import defaultdict, deque, namedtuple
from math import cos, pi, sin
from typing import Any, Dict, List, Optional, Set, Tuple

import mathutils
from mathutils import Vector

from bpy.props import (
    BoolProperty,
    CollectionProperty,
    FloatProperty,
    FloatVectorProperty,
    IntProperty,
    StringProperty,
)
from bpy.types import Operator, Panel, PropertyGroup, Scene
from bpy_extras.io_utils import ImportHelper

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
logger.handlers.clear()
handler = logging.StreamHandler()
formatter = logging.Formatter("%(levelname)s - %(message)s")
handler.setFormatter(formatter)
logger.addHandler(handler)
logger.propagate = False

bl_info = {
    "name": "Shader (.fbx / .uemodel)",
    "author": "Akatsuki",
    "version": (1, 3),
    "blender": (4, 1, 1),
    "location": "View3D > UI > Wuthering Waves",
    "description": "Import and map shaders/textures for Wuthering Waves characters",
    "category": "Wuthering Waves",
}

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


def update_light(self, context):
    value = context.scene.light_mode_value
    if not (0 <= value <= 6):
        return
    if node_group := bpy.data.node_groups.get("Global Material Properties"):
        for node in node_group.nodes:
            if (
                node.type == "GROUP"
                and node.node_tree
                and node.node_tree.name == "Color Palette"
            ):
                for input in node.inputs:
                    if input.type == "VALUE" and input.name == "Value":
                        input.default_value = float(value)
                    if value == 6 and input.type == "RGBA":
                        if input.name == "Custom Ambient":
                            context.scene.amb_color = input.default_value
                        elif input.name == "Custom Light":
                            context.scene.light_color = input.default_value
                        elif input.name == "Custom Shadow":
                            context.scene.shadow_color = input.default_value
                        elif input.name == "Custom Rim Tint":
                            context.scene.rim_color = input.default_value


def update_shadow_transition_range(self, context):
    if not context.active_object or context.active_object.type != "MESH":
        return
    mesh_name = context.active_object.name.split(".")[0]
    data = next(
        (m for m in context.scene.mesh_texture_mappings if m.mesh_name == mesh_name),
        None,
    )
    if not data:
        data = context.scene.mesh_texture_mappings.add()
        data.mesh_name = mesh_name
    value = self.shadow_transition_range_value
    for slot in context.active_object.material_slots:
        if slot.material and slot.material.use_nodes:
            for node in slot.material.node_tree.nodes:
                if node.type == "GROUP" and node.node_tree:
                    for input in node.inputs:
                        if input.type == "VALUE" and "Shadow Transition Range" in input.name:
                            input.default_value = value


def update_face_shadow_softness(self, context):
    if not context.active_object or context.active_object.type != "MESH":
        return
    mesh_name = context.active_object.name.split(".")[0]
    data = next(
        (m for m in context.scene.mesh_texture_mappings if m.mesh_name == mesh_name),
        None,
    )
    if not data:
        data = context.scene.mesh_texture_mappings.add()
        data.mesh_name = mesh_name
    value = self.face_shadow_softness_value
    for slot in context.active_object.material_slots:
        if slot.material and slot.material.use_nodes:
            for node in slot.material.node_tree.nodes:
                if node.type == "GROUP" and node.node_tree:
                    for input in node.inputs:
                        if input.type == "VALUE" and "Face Shadow Softness" in input.name:
                            input.default_value = value


def update_shadow(self, context):
    value = context.scene.shadow_position
    if not (0.0 <= value <= 2.0):
        return
    if node_group := bpy.data.node_groups.get("Global Material Properties"):
        for node in node_group.nodes:
            if node.name == "Global Properties" and node.type == "GROUP_OUTPUT":
                if "Shadow Position" in node.inputs:
                    node.inputs["Shadow Position"].default_value = value


def update_catch_shadows(self, context):
    value = context.scene.catch_shadows
    if not (0 <= value <= 1):
        return
    if node_group := bpy.data.node_groups.get("Global Material Properties"):
        for node in node_group.nodes:
            if node.name == "Global Properties" and node.type == "GROUP_OUTPUT":
                if "Catch Shadows" in node.inputs:
                    node.inputs["Catch Shadows"].default_value = value


def update_colors(self, context):
    if context.scene.light_mode_value != 6:
        return
    custom_colors = {
        "Custom Ambient": context.scene.amb_color,
        "Custom Light": context.scene.light_color,
        "Custom Shadow": context.scene.shadow_color,
        "Custom Rim Tint": context.scene.rim_color,
    }
    if node_group := bpy.data.node_groups.get("Global Material Properties"):
        for node in node_group.nodes:
            if (
                node.type == "GROUP"
                and node.node_tree
                and node.node_tree.name == "Color Palette"
            ):
                for input in node.inputs:
                    if input.type == "RGBA" and input.name in custom_colors:
                        input.default_value = custom_colors[input.name]


def update_blush(self, context):
    if not context.active_object or context.active_object.type != "MESH":
        return
    mesh_name = context.active_object.name.split(".")[0]
    data = next(
        (m for m in context.scene.mesh_texture_mappings if m.mesh_name == mesh_name),
        None,
    )
    if not data:
        data = context.scene.mesh_texture_mappings.add()
        data.mesh_name = mesh_name
    value = self.blush_value
    for slot in context.active_object.material_slots:
        if (
            slot.material
            and slot.material.use_nodes
            and "WW - Face" in slot.material.name
        ):
            for node in slot.material.node_tree.nodes:
                if node.type == "GROUP" and node.node_tree:
                    for input in node.inputs:
                        if input.type == "VALUE" and "Blush" in input.name:
                            input.default_value = value


def update_disgust(self, context):
    if not context.active_object or context.active_object.type != "MESH":
        return
    mesh_name = context.active_object.name.split(".")[0]
    data = next(
        (m for m in context.scene.mesh_texture_mappings if m.mesh_name == mesh_name),
        None,
    )
    if not data:
        data = context.scene.mesh_texture_mappings.add()
        data.mesh_name = mesh_name
    value = self.disgust_value
    for slot in context.active_object.material_slots:
        if (
            slot.material
            and slot.material.use_nodes
            and "WW - Face" in slot.material.name
        ):
            for node in slot.material.node_tree.nodes:
                if node.type == "GROUP" and node.node_tree:
                    for input in node.inputs:
                        if input.type == "VALUE" and "Disgust" in input.name:
                            input.default_value = value


def update_metallic(self, context):
    if not context.active_object or context.active_object.type != "MESH":
        return
    mesh_name = context.active_object.name.split(".")[0]
    data = next(
        (m for m in context.scene.mesh_texture_mappings if m.mesh_name == mesh_name),
        None,
    )
    if not data:
        data = context.scene.mesh_texture_mappings.add()
        data.mesh_name = mesh_name
    value = self.metallic_value
    for slot in context.active_object.material_slots:
        if slot.material and slot.material.use_nodes:
            for node in slot.material.node_tree.nodes:
                if (
                    node.type == "GROUP"
                    and node.node_tree
                    and "WW - Main" in node.node_tree.name
                ):
                    for input in node.inputs:
                        if input.type == "VALUE" and "Enable Metallics" in input.name:
                            input.default_value = value


def update_specular(self, context):
    if not context.active_object or context.active_object.type != "MESH":
        return
    mesh_name = context.active_object.name.split(".")[0]
    data = next(
        (m for m in context.scene.mesh_texture_mappings if m.mesh_name == mesh_name),
        None,
    )
    if not data:
        data = context.scene.mesh_texture_mappings.add()
        data.mesh_name = mesh_name
    value = self.specular_value
    for slot in context.active_object.material_slots:
        if slot.material and slot.material.use_nodes:
            for node in slot.material.node_tree.nodes:
                if (
                    node.type == "GROUP"
                    and node.node_tree
                    and "WW - Main" in node.node_tree.name
                ):
                    for input in node.inputs:
                        if (
                            input.type == "VALUE"
                            and "Specular Multiplier" in input.name
                        ):
                            input.default_value = value


def add_scene_props():
    Scene.original_materials = StringProperty(default="")
    Scene.original_textures = StringProperty(default="")
    Scene.tex_dir = StringProperty(subtype="DIR_PATH")
    Scene.is_first_use = BoolProperty(default=True)
    Scene.outlines_enabled = BoolProperty(default=False)
    Scene.texture_priority_mode = BoolProperty(default=True)
    Scene.mesh_texture_mappings = CollectionProperty(type=MeshTextureData)
    Scene.light_mode_value = IntProperty(
        name="Light Mode",
        description="Select the lighting mode for the character",
        default=0,
        min=0,
        max=6,
        update=update_light,
    )
    Scene.blush_value = FloatProperty(
        name="Blush",
        description="Control the blush intensity on face materials",
        default=0.0,
        min=0.0,
        max=1.0,
        precision=1,
        update=update_blush,
    )
    Scene.disgust_value = FloatProperty(
        name="Disgust",
        description="Control the disgust expression intensity on face materials",
        default=0.0,
        min=0.0,
        max=1.0,
        precision=1,
        update=update_disgust,
    )
    Scene.metallic_value = FloatProperty(
        name="Metallic Value",
        description="Control the metallic appearance of materials",
        default=1.0,
        min=0.0,
        max=1.0,
        precision=1,
        update=update_metallic,
    )
    Scene.specular_value = FloatProperty(
        name="Specular Value",
        description="Control the specular intensity of materials",
        default=0.1,
        min=0.0,
        max=1.0,
        precision=1,
        update=update_specular,
    )
    Scene.amb_color = FloatVectorProperty(
        name="Ambient Color",
        description="Custom ambient light color",
        default=(1.0, 1.0, 1.0, 1.0),
        min=0.0,
        max=1.0,
        subtype="COLOR",
        size=4,
        update=update_colors,
    )
    Scene.light_color = FloatVectorProperty(
        name="Light Color",
        description="Custom light color",
        default=(1.0, 1.0, 1.0, 1.0),
        min=0.0,
        max=1.0,
        subtype="COLOR",
        size=4,
        update=update_colors,
    )
    Scene.face_shadow_softness_value = FloatProperty(
        name="Face Shadow Softness",
        description="Control the softness of shadows on the face",
        default=0.01,
        min=0.0,
        max=1.0,
        precision=2,
        update=update_face_shadow_softness,
    )
    Scene.shadow_transition_range_value = FloatProperty(
        name="Shadow Transition Range",
        description="Control the range of shadow transitions",
        default=0.01,
        min=0.0,
        max=1.0,
        precision=2,
        update=update_shadow_transition_range,
    )
    Scene.shadow_color = FloatVectorProperty(
        name="Shadow Color",
        description="Custom shadow color",
        default=(1.0, 1.0, 1.0, 1.0),
        min=0.0,
        max=1.0,
        subtype="COLOR",
        size=4,
        update=update_colors,
    )
    Scene.rim_color = FloatVectorProperty(
        name="Rim Color",
        description="Custom rim light color",
        default=(1.0, 1.0, 1.0, 1.0),
        min=0.0,
        max=1.0,
        subtype="COLOR",
        size=4,
        update=update_colors,
    )
    Scene.shader_file_path = StringProperty(
        name="Shader File",
        description="Path to the shader .blend file",
        subtype="FILE_PATH",
    )
    Scene.face_panel_file_path = StringProperty(
        name="Face Panel File",
        description="Path to the face panel .blend file",
        subtype="FILE_PATH",
    )
    Scene.shadow_position = FloatProperty(
        name="Shadow Position",
        description="Control the position of shadows",
        default=0.55,
        min=0.0,
        max=2.0,
        precision=2,
        update=update_shadow,
    )
    Scene.catch_shadows = IntProperty(
        name="Catch Shadows",
        description="Toggle whether objects catch shadows",
        default=1,
        min=0,
        max=1,
        update=update_catch_shadows,
    )


def init_scene():
    if not bpy.context.scene.is_first_use:
        return

    logger.info("Initializing scene for first use")

    world = bpy.data.worlds.get("World") or bpy.data.worlds.new("World")
    world.use_nodes = True
    bg = world.node_tree.nodes.get("Background") or world.node_tree.nodes.new(
        "ShaderNodeBackground"
    )
    bg.inputs[0].default_value = (0, 0, 0, 1)
    bg.inputs[1].default_value = 1.0

    if camera := bpy.data.objects.get("Camera"):
        camera.location = (0, -2, 1.25)
        camera.rotation_euler = (1.5708, 0, 0)

    bpy.context.scene.view_settings.view_transform = "Standard"
    bpy.context.scene.render.use_border = True
    bpy.context.scene.render.fps = 60
    bpy.context.scene.eevee.use_ssr = True
    bpy.context.scene.eevee.use_ssr_refraction = True
    bpy.context.scene.is_first_use = False


def import_node_groups(path: str):
    node_trees = ["Light Vectors", "WW - Outlines", "ResonatorStar Move"]
    objects = {
        "Light Direction": False,
        "Head Origin": False,
        "Head Forward": False,
        "Head Up": False,
        "Circle": False,
    }

    for name in node_trees:
        try:
            bpy.ops.wm.append(filename=name, directory=f"{path}/NodeTree")
            logger.info(f"Imported node tree: {name}")
        except Exception as e:
            logger.warning(f"Failed to append node tree {name}: {str(e)}")

    for obj_name in objects.keys():
        if not bpy.data.objects.get(obj_name):
            try:
                bpy.ops.wm.append(filename=obj_name,
                                  directory=f"{path}/Object")
                if obj_name == "Circle" and (circle := bpy.data.objects.get("Circle")):
                    circle.hide_viewport = True
                    circle.hide_render = True
                logger.info(f"Imported object: {obj_name}")
            except Exception as e:
                logger.warning(f"Failed to append object {obj_name}: {str(e)}")


def init_modifiers():
    ctx = bpy.context
    if not ctx.active_object or ctx.active_object.type != "MESH":
        return

    mesh_name = ctx.active_object.name.split(".")[0]
    suffix = get_suffix()
    setup_controls(ctx, mesh_name, suffix)
    set_modifiers(ctx, mesh_name, suffix)
    add_head_lock(mesh_name)
    apply_head_lock()
    logger.info(f"Initialized modifiers for {mesh_name}")


def setup_controls(ctx, mesh_name: str, suffix: str):
    control_objects = ["Light Direction",
                       "Head Origin", "Head Forward", "Head Up"]
    need_new = any(
        obj_name + suffix not in bpy.data.objects for obj_name in control_objects
    )

    if need_new:
        for obj_name in control_objects:
            if (
                obj_name + suffix not in bpy.data.objects
                and obj_name in bpy.data.objects
            ):
                orig = bpy.data.objects[obj_name]
                new_obj = orig.copy()
                new_obj.name = obj_name + suffix
                new_obj.location = orig.location.copy()
                new_obj.rotation_euler = orig.rotation_euler.copy()
                new_obj.scale = orig.scale.copy()
                bpy.context.collection.objects.link(new_obj)
                logger.info(f"Created control object: {new_obj.name}")

        head_origin = bpy.data.objects.get(f"Head Origin{suffix}")
        if head_origin:
            for child_name in ["Head Forward", "Head Up"]:
                if child := bpy.data.objects.get(f"{child_name}{suffix}"):
                    child.parent = head_origin
                    child.matrix_parent_inverse = head_origin.matrix_world.inverted()


def set_modifiers(ctx, mesh_name: str, suffix: str):
    for base_name in ["Light Vectors", "WW - Outlines", "ResonatorStar Move"]:
        if not (group := bpy.data.node_groups.get(base_name)):
            continue

        new_group_name = f"{base_name} {mesh_name}"
        new_group = bpy.data.node_groups.get(new_group_name) or group.copy()
        new_group.name = new_group_name

        modifier = ctx.active_object.modifiers.get(
            new_group_name
        ) or ctx.active_object.modifiers.new(new_group_name, "NODES")
        modifier.node_group = new_group

        if base_name == "Light Vectors":
            inputs = {
                "Input_3": f"Light Direction{suffix}",
                "Input_4": f"Head Origin{suffix}",
                "Input_5": f"Head Forward{suffix}",
                "Input_6": f"Head Up{suffix}",
            }
            for input_name, obj_name in inputs.items():
                if obj := bpy.data.objects.get(obj_name):
                    modifier[input_name] = obj

        elif base_name == "WW - Outlines":
            outline_mat_name = f"WW - Outlines {mesh_name}"
            outline_mat = (
                bpy.data.materials.get(outline_mat_name)
                or bpy.data.materials.get("WW - Outlines").copy()
            )
            outline_mat.name = outline_mat_name

            modifier["Input_3_use_attribute"] = True
            modifier["Input_3_attribute_name"] = "COL0"
            modifier["Input_7"] = 0.125

            materials = [
                slot.material
                for slot in ctx.active_object.material_slots
                if slot.material
                and slot.material.name.startswith("WW - ")
                and not any(ex in slot.material.name for ex in ["Eye", "ResonatorStar"])
            ]
            input_pairs = [
                (10, 5),
                (11, 9),
                (14, 15),
                (18, 19),
                (24, 25),
                (27, 26),
                (28, 29),
            ]
            for i, (mask, mat) in enumerate(input_pairs):
                modifier[f"Input_{mask}"] = materials[i] if i < len(
                    materials) else None
                modifier[f"Input_{mat}"] = outline_mat if i < len(
                    materials) else None
            modifier.show_viewport = ctx.scene.outlines_enabled

        elif base_name == "ResonatorStar Move":
            if circle := bpy.data.objects.get("Circle"):
                modifier["Input_2"] = circle
            modifier["Output_3_attribute_name"] = "move"

            data = get_mesh_data(ctx, mesh_name)
            modifier.show_viewport = data.star_move

        logger.info(f"Set up {base_name} modifier for {mesh_name}")


def add_head_lock(mesh_name: str):
    suffix = get_suffix()
    head_origin = bpy.data.objects.get(f"Head Origin{suffix}")
    mesh = bpy.data.objects.get(mesh_name)
    armature = get_armature_from_modifiers(mesh) if mesh else None
    if not head_origin or not armature:
        return

    head_bone = "c_head.x"
    if head_bone not in armature.data.bones:
        head_bone = "Bip001Head"
        if head_bone not in armature.data.bones:
            head_bone = armature.data.bones[0].name if armature.data.bones else None
    if not head_bone:
        return

    bone = armature.data.bones[head_bone]
    bone_head_local = bone.head_local
    relative_position = Vector((0, 0, 0.2))
    head_origin_local = bone_head_local + relative_position
    head_origin.location = head_origin_local

    for const in head_origin.constraints:
        head_origin.constraints.remove(const)

    constraint = head_origin.constraints.new("CHILD_OF")
    constraint.target = armature
    constraint.subtarget = head_bone

    bpy.context.view_layer.objects.active = head_origin
    bpy.ops.object.select_all(action="DESELECT")
    head_origin.select_set(True)
    context_override = bpy.context.copy()
    context_override["constraint"] = constraint
    try:
        bpy.ops.constraint.childof_set_inverse(
            context_override, constraint=constraint.name, owner="OBJECT"
        )
        logger.info(
            f"Applied head lock with relative position for {head_origin.name}")
    except Exception as e:
        logger.warning(f"Failed to set inverse for child constraint: {str(e)}")
    head_origin.select_set(False)


def apply_head_lock():
    suffix = get_suffix()
    if head_origin := bpy.data.objects.get(f"Head Origin{suffix}"):
        bpy.ops.object.select_all(action="DESELECT")
        head_origin.select_set(True)
        bpy.context.view_layer.objects.active = head_origin

        for constraint in head_origin.constraints:
            if constraint.type == "CHILD_OF":
                context_override = bpy.context.copy()
                context_override["constraint"] = constraint
                try:
                    bpy.ops.constraint.childof_set_inverse(
                        context_override, constraint=constraint.name, owner="OBJECT"
                    )
                    logger.info(
                        f"Applied head lock inverse for {head_origin.name}")
                except Exception as e:
                    logger.warning(
                        f"Failed to set inverse for child constraint: {str(e)}"
                    )
                break
        head_origin.select_set(False)


def set_star_shader(material: bpy.types.Material, mat_name: str, stars: Dict[str, int]):
    if material.use_nodes:
        for node in material.node_tree.nodes:
            if (
                node.type == "GROUP"
                and node.node_tree
                and mat_name in stars
                and node.node_tree.name == "Tacet Mark"
            ):
                for input in node.inputs:
                    if "Texture Slider" in input.name:
                        star_value = stars[mat_name]
                        input.default_value = {
                            4: 0, 5: 1, 6: 2}.get(star_value, 0)
                        logger.info(
                            f"Set star value to {input.default_value} for {mat_name}"
                        )


def make_texture_patterns(params: TextureSearchParameters):
    patterns = []

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


class MeshTextureData(PropertyGroup):
    mesh_name: StringProperty(name="Mesh Name", description="Name of the mesh")
    textures: StringProperty(
        name="Textures", description="List of textures for this mesh"
    )
    tex_mode: BoolProperty(
        name="Texture Mode", description="Texture priority mode", default=True
    )
    star_move: BoolProperty(
        name="Star Move", description="Enable star movement effect", default=False
    )
    hair_trans: BoolProperty(
        name="Hair Transparency",
        description="Enable hair transparency effect",
        default=False,
    )
    metallic_value: FloatProperty(
        name="Metallic Value",
        description="Control the metallic appearance of materials",
        default=1.0,
        min=0.0,
        max=1.0,
        precision=1,
        update=update_metallic,
    )
    specular_value: FloatProperty(
        name="Specular Value",
        description="Control the specular intensity of materials",
        default=0.1,
        min=0.0,
        max=1.0,
        precision=1,
        update=update_specular,
    )
    blush_value: FloatProperty(
        name="Blush",
        description="Control the blush intensity on face materials",
        default=0.0,
        min=0.0,
        max=1.0,
        precision=1,
        update=update_blush,
    )
    disgust_value: FloatProperty(
        name="Disgust",
        description="Control the disgust expression intensity on face materials",
        default=0.0,
        min=0.0,
        max=1.0,
        precision=1,
        update=update_disgust,
    )


class WW_OT_ImportShader(Operator, ImportHelper):
    bl_idname = "shader.import_shader"
    bl_label = "Import Shader"
    bl_description = "Import and apply WW shaders to the selected mesh"
    bl_options = {"REGISTER", "UNDO"}
    filename_ext = ".blend"
    filter_glob: StringProperty(default="*.blend", options={"HIDDEN"})

    def invoke(self, context, event):
        if hasattr(context.scene, "shader_file_path") and os.path.exists(
            context.scene.shader_file_path
        ):
            self.filepath = context.scene.shader_file_path
            return self.execute(context)
        return ImportHelper.invoke(self, context, event)

    def execute(self, context):
        if not self.validate_context(context):
            return {"CANCELLED"}

        active_obj = context.active_object
        mesh_name = active_obj.name.split(".")[0]
        has_shader = self.check_if_has_shader(context)

        logger.info(f"Starting shader import process for mesh: {mesh_name}")
        set_solid_view()

        if not has_shader:
            logger.info(
                f"Mesh {mesh_name} does not have WW shaders. Starting first-time import."
            )
            orig_mats = self.get_original_materials()
            logger.info(f"Original materials saved: {len(orig_mats)} objects")

            if not hasattr(context.scene, "shader_file_path") or not os.path.exists(
                context.scene.shader_file_path
            ):
                logger.info(f"Loading shader file from: {self.filepath}")
                if not self.import_materials(context):
                    return {"CANCELLED"}
                context.scene.shader_file_path = self.filepath
                logger.info(f"Shader file path saved: {self.filepath}")
            else:
                self.filepath = context.scene.shader_file_path
                logger.info(f"Using existing shader file: {self.filepath}")
                existing_materials = {mat.name for mat in bpy.data.materials}
                with bpy.data.libraries.load(self.filepath) as (data_from, data_to):
                    data_to.materials = [
                        mat_name
                        for mat_name in data_from.materials
                        if mat_name.startswith("WW - ")
                        and mat_name not in existing_materials
                    ]
                logger.info(
                    f"Loaded {len(data_to.materials)} additional shader materials"
                )
                import_node_groups(self.filepath)
                logger.info("Node groups imported")

            self.process_materials(context)
            context.scene.original_materials = str(orig_mats)
            logger.info("Original materials saved to scene")
            darken_eye_colors(context.active_object)
            logger.info("Eye colors adjusted")
            init_modifiers()
            logger.info("Modifiers initialized")
            bpy.ops.object.mode_set(mode="OBJECT")
            bpy.context.view_layer.objects.active = active_obj
            active_obj.select_set(True)
            logger.info(
                f"Shader import completed successfully for {mesh_name}")
            self.report({"INFO"}, "Shaders imported and applied successfully.")
        else:
            logger.info(
                f"Mesh {mesh_name} already has WW shaders. Checking existing setup."
            )
            shader_count = 0
            material_types = set()
            for slot in active_obj.material_slots:
                if slot.material and slot.material.name.startswith("WW - "):
                    shader_count += 1
                    if match := re.search(r"WW - ([A-Za-z]+)", slot.material.name):
                        material_types.add(match.group(1))

            logger.info(f"Found {shader_count} WW shaders on {mesh_name}")
            logger.info(
                f"Material types detected: {', '.join(material_types)}")
            logger.info(
                f"Skipping shader import, will proceed to texture import")
            self.report(
                {"INFO"},
                f"Mesh {mesh_name} already has shaders. Proceeding to texture import.",
            )

        logger.info(f"Starting texture import process for {mesh_name}")
        bpy.ops.shader.import_textures("INVOKE_DEFAULT")

        return {"FINISHED"}

    def check_if_has_shader(self, context):
        active_obj = context.active_object
        for slot in active_obj.material_slots:
            if slot.material and slot.material.name.startswith("WW - "):
                return True
        return False

    def validate_context(self, context):
        if not context.active_object or context.active_object.type != "MESH":
            self.report(
                {"ERROR"}, "Please select a mesh object to import shader.")
            logger.error("No valid mesh object selected for shader import")
            return False
        if not os.path.exists(self.filepath):
            self.report(
                {"ERROR"}, "Shader .blend file not found. Please check the path."
            )
            logger.error(f"Shader file not found: {self.filepath}")
            return False
        return True

    def get_original_materials(self):
        return {
            obj.name: [
                (slot.material.name if slot.material else None)
                for slot in obj.material_slots
            ]
            for obj in bpy.data.objects
            if obj.type == "MESH"
        }

    def import_materials(self, context):
        existing_materials = {mat.name for mat in bpy.data.materials}
        try:
            with bpy.data.libraries.load(self.filepath) as (data_from, data_to):
                data_to.materials = [
                    mat_name
                    for mat_name in data_from.materials
                    if mat_name.startswith("WW - ")
                    and mat_name not in existing_materials
                ]
            logger.info(f"Imported {len(data_to.materials)} shader materials")
            import_node_groups(self.filepath)
            init_scene()
            return True
        except Exception as e:
            self.report(
                {"ERROR"}, f"Failed to load materials from .blend file: {str(e)}"
            )
            logger.error(f"Material import failed: {str(e)}")
            return False

    def process_materials(self, context):
        mat_map = {"Eyes": "Eye", "Bang": "Bangs"}
        stars = {}
        mesh_name = context.active_object.name.split(".")[0]

        logger.info(f"Processing materials for {mesh_name}")
        processed_count = 0

        for slot in context.active_object.material_slots:
            if not slot.material or not slot.material.name.startswith("MI_"):
                continue

            mat_name = slot.material.name
            try:
                target_shader = self.get_target_shader(
                    mat_name, mat_map, stars)
                if not target_shader:
                    continue

                new_material = self.duplicate_material(
                    target_shader, mesh_name)
                slot.material = new_material
                set_star_shader(new_material, mat_name, stars)
                processed_count += 1
            except Exception as e:
                self.report(
                    {"INFO"}, f"Error processing material {mat_name}: {str(e)}")
                logger.error(f"Error processing material {mat_name}: {str(e)}")

        logger.info(f"Processed {processed_count} materials")

    def get_target_shader(
        self, mat_name: str, mat_map: Dict[str, str], stars: Dict[str, int]
    ):
        if "XingStar" in mat_name:
            if match := re.match(r"MI_(\d)XingStar", mat_name):
                stars[mat_name] = int(match.group(1))
                return "WW - ResonatorStar"
        else:
            base, version = split_material_name(mat_name)
            mapped = mat_map.get(base, base)
            return f"WW - {mapped}{version}"

    def duplicate_material(self, shader_name: str, mesh_name: str):
        unique_name = f"{shader_name} {mesh_name}"
        if unique_name in bpy.data.materials:
            return bpy.data.materials[unique_name]

        if shader_name in bpy.data.materials:
            material = bpy.data.materials[shader_name].copy()
        elif base_match := re.match(r"WW - ([A-Za-z]+)", shader_name):
            base_name = base_match.group(0)
            material = bpy.data.materials.get(
                base_name, bpy.data.materials.get("WW - Main")
            ).copy()
        else:
            material = bpy.data.materials["WW - Main"].copy()

        material.name = unique_name
        if material.use_nodes:
            for node in material.node_tree.nodes:
                if node.type == "TEX_IMAGE":
                    node.image = None
        return material


class WW_OT_ImportTextures(Operator, ImportHelper):
    bl_idname = "shader.import_textures"
    bl_label = "Import Textures"
    bl_description = "Import and apply textures to the selected mesh"
    bl_options = {"REGISTER", "UNDO"}
    filename_ext = ".png"
    filter_glob: StringProperty(
        default="*.png;*.jpg;*.jpeg", options={"HIDDEN"})
    directory: StringProperty(subtype="DIR_PATH")
    files: CollectionProperty(type=bpy.types.OperatorFileListElement)

    def invoke(self, context, event):
        set_solid_view()
        logger.info("Import Textures invoked, setting solid view")
        return super().invoke(context, event)

    def execute(self, context):
        if not self.validate_context(context):
            return {"CANCELLED"}

        active_obj = context.active_object
        mesh_name = active_obj.name.split(".")[0]
        logger.info(f"Starting texture import for {mesh_name}")

        self.clear_existing_textures(context)

        self.import_textures(context)

        data = get_mesh_data(context, mesh_name)
        self.assign_textures(context)

        shadow_hair_count = 0
        for slot in active_obj.material_slots:
            if (
                slot.material
                and slot.material.use_nodes
                and slot.material.name.startswith("WW - ")
            ):
                for node in slot.material.node_tree.nodes:
                    if (
                        node.type == "GROUP"
                        and node.node_tree
                        and "Shadows for Hair" in node.node_tree.name
                    ):
                        node.mute = False
                        shadow_hair_count += 1
        if shadow_hair_count > 0:
            logger.info(
                f"Unmuted {shadow_hair_count} 'Shadows for Hair' nodes")

        has_het_anywhere = False
        assigned_count = 0
        for slot in active_obj.material_slots:
            if (
                slot.material
                and slot.material.use_nodes
                and (
                    match := re.search(
                        r"WW - ([A-Za-z]+)(_?\d+|(?:_[^_]+)*)?", slot.material.name
                    )
                )
            ):
                base, version = match.group(1), match.group(2) or ""
                logger.info(
                    f"Processing material: {slot.material.name} (base: {base}, version: {version})"
                )

                original_name = self.get_original_material_name(
                    context, base, version)
                logger.info(f"Original material name: {original_name}")

                material_info = MaterialDetails(base, version, original_name)
                mat_tex_data = MaterialTextureData(
                    slot.material,
                    material_info,
                    TEXTURE_TYPE_MAPPINGS,
                    self.files,
                    self.directory,
                    data.tex_mode,
                )

                apply_textures(mat_tex_data)
                assigned_count += 1
                logger.info(
                    f"Applied textures to material: {slot.material.name}")

                if any(
                    n.image and "_HET" in n.image.name
                    for n in slot.material.node_tree.nodes
                    if n.type == "TEX_IMAGE"
                ):
                    has_het_anywhere = True
                    logger.info(
                        f"HET texture detected in material: {slot.material.name}"
                    )

        logger.info(f"Has HET textures: {has_het_anywhere}")
        see_through_count = 0
        for slot in active_obj.material_slots:
            if slot.material and slot.material.use_nodes:
                for node in slot.material.node_tree.nodes:
                    if (
                        node.type == "GROUP"
                        and node.node_tree
                        and "See Through" in node.node_tree.name
                    ):
                        old_state = node.mute
                        node.mute = not has_het_anywhere
                        see_through_count += 1
                        if old_state != node.mute:
                            logger.info(
                                f"Changed 'See Through' node state in {slot.material.name}: from {old_state} to {not has_het_anywhere}"
                            )

        if see_through_count > 0:
            logger.info(
                f"Updated {see_through_count} 'See Through' nodes to {not has_het_anywhere} (muted)"
            )

        data.hair_trans = has_het_anywhere
        logger.info(f"Set hair_trans to {has_het_anywhere} for {mesh_name}")

        set_material_view()
        logger.info("Material view set")
        bpy.ops.object.mode_set(mode="OBJECT")
        bpy.context.view_layer.objects.active = active_obj
        active_obj.select_set(True)
        logger.info(f"Texture import completed successfully for {mesh_name}")
        self.report({"INFO"}, "Textures imported and applied successfully.")
        return {"FINISHED"}

    def clear_existing_textures(self, context):
        active_obj = context.active_object
        texture_count = 0
        for slot in active_obj.material_slots:
            if slot.material and slot.material.use_nodes:
                mat_texture_count = 0
                for node in slot.material.node_tree.nodes:
                    if node.type == "TEX_IMAGE" and node.image:
                        node.image = None
                        mat_texture_count += 1
                        texture_count += 1
                if mat_texture_count > 0:
                    logger.info(
                        f"Cleared {mat_texture_count} textures from material: {slot.material.name}"
                    )
        logger.info(
            f"Cleared total of {texture_count} existing textures from {active_obj.name}"
        )

    def validate_context(self, context):
        if not context.active_object or context.active_object.type != "MESH":
            self.report(
                {"ERROR"}, "Please select a mesh object to import textures.")
            logger.error("No valid mesh object selected for texture import")
            return False
        if not self.files:
            self.report({"ERROR"}, "No texture files selected.")
            logger.error("No texture files selected for import")
            return False
        return True

    def import_textures(self, context):
        context.scene.tex_dir = self.directory
        logger.info(f"Texture directory set to: {self.directory}")

        imported_files = []
        for file in self.files:
            file_path = os.path.join(self.directory, file.name)
            logger.info(f"Loading texture: {file.name}")
            loaded_image = load_image(file_path)
            if loaded_image:
                imported_files.append(file.name)
            else:
                logger.warning(f"Failed to load texture: {file.name}")

        mesh_name = context.active_object.name.split(".")[0]
        data = get_mesh_data(context, mesh_name)
        data.textures = ",".join(imported_files)
        logger.info(f"Imported {len(imported_files)} textures for {mesh_name}")
        logger.info(f"Texture list: {data.textures}")

    def assign_textures(self, context):
        mesh_name = context.active_object.name.split(".")[0]
        data = get_mesh_data(context, mesh_name)
        logger.info(
            f"Assigning textures to {mesh_name} with mode: {data.tex_mode}")

        for slot in context.active_object.material_slots:
            if (
                slot.material
                and slot.material.use_nodes
                and (
                    match := re.search(
                        r"WW - ([A-Za-z]+)(_?\d+|(?:_[^_]+)*)?", slot.material.name
                    )
                )
            ):
                base, version = match.group(1), match.group(2) or ""
                logger.info(
                    f"Processing material: {slot.material.name} (base: {base}, version: {version})"
                )

                original_name = self.get_original_material_name(
                    context, base, version)
                logger.info(f"Original material name: {original_name}")

                material_info = MaterialDetails(base, version, original_name)
                mat_tex_data = MaterialTextureData(
                    slot.material,
                    material_info,
                    TEXTURE_TYPE_MAPPINGS,
                    self.files,
                    self.directory,
                    data.tex_mode,
                )

                apply_textures(mat_tex_data)

    def get_original_material_name(self, context, base: str, version: str):
        return next(
            (
                slot.material.name
                for slot in context.active_object.material_slots
                if slot.material
                and re.match(rf"MI_.*?{base}{version}$", slot.material.name)
            ),
            None,
        )


class WW_OT_Rigify(bpy.types.Operator):
    bl_idname = "shader.rigify_armature"
    bl_label = "Rigify"
    bl_description = "Rigify the selected armature with optimized bone structure and collections"
    bl_options = {"REGISTER", "UNDO"}

    left_bone_pairs = [
        ("Bip001LFinger1", "Bip001LFinger11"),
        ("Bip001LFinger11", "Bip001LFinger12"),
        ("Bip001LFinger12", "Bip001LFinger13"),
        ("Bip001LFinger2", "Bip001LFinger21"),
        ("Bip001LFinger21", "Bip001LFinger22"),
        ("Bip001LFinger22", "Bip001LFinger23"),
        ("Bip001LFinger3", "Bip001LFinger31"),
        ("Bip001LFinger31", "Bip001LFinger32"),
        ("Bip001LFinger32", "Bip001LFinger33"),
        ("Bip001LFinger4", "Bip001LFinger41"),
        ("Bip001LFinger41", "Bip001LFinger42"),
        ("Bip001LFinger42", "Bip001LFinger43"),
    ]

    right_bone_pairs = [
        ("Bip001RFinger1", "Bip001RFinger11"),
        ("Bip001RFinger11", "Bip001RFinger12"),
        ("Bip001RFinger12", "Bip001RFinger13"),
        ("Bip001RFinger2", "Bip001RFinger21"),
        ("Bip001RFinger21", "Bip001RFinger22"),
        ("Bip001RFinger22", "Bip001RFinger23"),
        ("Bip001RFinger3", "Bip001RFinger31"),
        ("Bip001RFinger31", "Bip001RFinger32"),
        ("Bip001RFinger32", "Bip001RFinger33"),
        ("Bip001RFinger4", "Bip001RFinger41"),
        ("Bip001RFinger41", "Bip001RFinger42"),
        ("Bip001RFinger42", "Bip001RFinger43"),
    ]

    skip_if_finger13 = {
        ("Bip001LFinger1", "Bip001LFinger11"),
        ("Bip001LFinger2", "Bip001LFinger21"),
        ("Bip001LFinger3", "Bip001LFinger31"),
        ("Bip001LFinger4", "Bip001LFinger41"),
        ("Bip001RFinger1", "Bip001RFinger11"),
        ("Bip001RFinger2", "Bip001RFinger21"),
        ("Bip001RFinger3", "Bip001RFinger31"),
        ("Bip001RFinger4", "Bip001RFinger41"),
    }

    ALIGN_THRESHOLD = math.radians(5)
    TARGET_ANGLE = math.radians(5)
    STEP_SIZE = 0.001
    MAX_ITER = 50
    move_amount = 0.0001

    @classmethod
    def poll(cls, context):
        if not context.active_object:
            return False
        return context.active_object.type in {'MESH', 'ARMATURE'}

    def validate_scene_objects(self, context):
        if not context.scene.objects:
            logger.error("Scene contains no objects")
            return False
        valid_objects = [
            obj for obj in context.scene.objects if obj.type in {'MESH', 'ARMATURE'}]
        if not valid_objects:
            logger.error("Scene contains no mesh or armature objects")
            return False
        logger.info(
            f"Scene validation passed - found {len(valid_objects)} valid objects")
        return True

    def check_if_already_rigged(self, obj):
        if obj.type == 'MESH':
            for modifier in obj.modifiers:
                if modifier.type == 'ARMATURE' and modifier.object:
                    armature_obj = modifier.object
                    if armature_obj.name.startswith("RIG-"):
                        logger.warning(
                            f"Mesh '{obj.name}' is already using a rigified armature '{armature_obj.name}'")
                        return True
                    if armature_obj.get('rigify_generated') == True:
                        logger.warning(
                            f"Mesh '{obj.name}' is using a Rigify-generated armature '{armature_obj.name}'")
                        return True
        elif obj.type == 'ARMATURE':
            if obj.name.startswith("RIG-"):
                logger.warning(
                    f"Armature '{obj.name}' appears to be already rigified")
                return True
            if obj.get('rigify_generated') == True:
                logger.warning(
                    f"Armature '{obj.name}' is already Rigify-generated")
                return True
        return False

    def validate_armature_structure(self, armature_obj):
        if not armature_obj.data.bones:
            logger.error(f"Armature '{armature_obj.name}' contains no bones")
            return False
        required_bones = ['Bip001Pelvis', 'Bip001Spine']
        missing_bones = [
            bone for bone in required_bones if bone not in armature_obj.data.bones]
        if missing_bones:
            logger.error(
                f"Armature '{armature_obj.name}' missing required bones: {missing_bones}")
            return False
        logger.info(
            f"Armature structure validation passed for '{armature_obj.name}' - {len(armature_obj.data.bones)} bones found")
        return True

    def validate_mesh_vertex_groups(self, mesh_obj):
        if not mesh_obj.vertex_groups:
            logger.warning(f"Mesh '{mesh_obj.name}' has no vertex groups")
            return False
        bip_groups = [
            vg for vg in mesh_obj.vertex_groups if vg.name.startswith('Bip001')]
        if not bip_groups:
            logger.warning(
                f"Mesh '{mesh_obj.name}' has no Bip001 vertex groups")
            return False
        logger.info(
            f"Mesh vertex groups validation passed for '{mesh_obj.name}' - {len(bip_groups)} Bip001 groups found")
        return True

    def get_local_x(self, bone):
        return bone.matrix.to_3x3().col[0].normalized()

    def angle_between(self, v1, v2):
        if v1.length == 0 or v2.length == 0:
            return math.pi
        return v1.angle(v2)

    def all_bone_pairs(self):
        return self.left_bone_pairs + self.right_bone_pairs

    def check_alignment(self, edit_bones, finger13_exists_left, finger13_exists_right):
        for name1, name2 in self.all_bone_pairs():
            if (finger13_exists_left and (name1, name2) in self.skip_if_finger13) or \
               (finger13_exists_right and (name1, name2) in self.skip_if_finger13):
                continue
            b1 = edit_bones.get(name1)
            b2 = edit_bones.get(name2)
            if b1 and b2:
                x1 = self.get_local_x(b1)
                x2 = self.get_local_x(b2)
                angle = self.angle_between(x1, x2)
                if angle < self.ALIGN_THRESHOLD:
                    return True
        return False

    def apply_adjustment(self, edit_bones, finger13_exists_left, finger13_exists_right):
        if finger13_exists_left or finger13_exists_right:
            outward_bones = [
                "Bip001LFinger11", "Bip001LFinger21", "Bip001LFinger31", "Bip001LFinger41",
                "Bip001RFinger11", "Bip001RFinger21", "Bip001RFinger31", "Bip001RFinger41"
            ]
            inward_bones = [
                "Bip001LFinger13", "Bip001LFinger23", "Bip001LFinger33", "Bip001LFinger43",
                "Bip001RFinger13", "Bip001RFinger23", "Bip001RFinger33", "Bip001RFinger43"
            ]
        else:
            outward_bones = [
                "Bip001LFinger1", "Bip001LFinger2", "Bip001LFinger3", "Bip001LFinger4",
                "Bip001RFinger1", "Bip001RFinger2", "Bip001RFinger3", "Bip001RFinger4"
            ]
            inward_bones = [
                "Bip001LFinger12", "Bip001LFinger22", "Bip001LFinger32", "Bip001LFinger42",
                "Bip001RFinger12", "Bip001RFinger22", "Bip001RFinger32", "Bip001RFinger42"
            ]
        for bone_name in outward_bones:
            bone = edit_bones.get(bone_name)
            if bone:
                x_axis = self.get_local_x(bone)
                bone.tail += x_axis * self.move_amount
        for bone_name in inward_bones:
            bone = edit_bones.get(bone_name)
            if bone:
                x_axis = self.get_local_x(bone)
                bone.tail -= x_axis * self.move_amount

    def remove_bone_collections(self, armature):
        if armature.data.collections:
            for collection in armature.data.collections[:]:
                armature.data.collections.remove(collection)

    def adjust_bone_positions(self, armature, bone_pairs):
        for bone1_name, bone2_name in bone_pairs:
            if bone1_name in armature.data.edit_bones and bone2_name in armature.data.edit_bones:
                bone1 = armature.data.edit_bones[bone1_name]
                bone2 = armature.data.edit_bones[bone2_name]
                bone1.tail = bone2.head

    def set_bone_connect(self, armature, bone_names):
        for bone_name in bone_names:
            if bone_name in armature.data.edit_bones:
                armature.data.edit_bones[bone_name].use_connect = True

    def adjust_bone_roll(self, armature, bones_to_adjust_roll):
        for bone_name in bones_to_adjust_roll:
            if bone_name in armature.data.edit_bones:
                armature.data.edit_bones[bone_name].roll = 0

    def process_bone_collections_and_rigify(self, armature, bone_data):
        for collection_name, index, row in bone_data:
            bpy.ops.armature.collection_add()
            new_collection = armature.data.collections[-1]
            new_collection.name = collection_name
            bpy.ops.armature.rigify_collection_set_ui_row(index=index, row=row)

    def modify_bone_rig_type(self, armature, bones_and_rig_types):
        for bone_name, rig_type, widget_type in bones_and_rig_types:
            bone = armature.pose.bones.get(bone_name)
            if bone:
                armature.data.bones[bone_name].select = True
                armature.data.bones.active = armature.data.bones[bone_name]
                bone.rigify_type = rig_type
                if widget_type and bone.rigify_parameters:
                    bone.rigify_parameters.super_copy_widget_type = widget_type

    def duplicate_and_adjust_heel_bone(self, armature, foot_bone_name, toe_bone_name, heel_bone_name, rotation_angle=1.5708):
        if toe_bone_name in armature.data.edit_bones:
            toe_bone = armature.data.edit_bones[toe_bone_name]
            heel_bone = armature.data.edit_bones.new(name=heel_bone_name)
            heel_bone.head = toe_bone.head
            heel_bone.tail = toe_bone.tail
            heel_bone.roll = toe_bone.roll
            rotation_matrix = mathutils.Matrix.Rotation(rotation_angle, 4, 'Y')
            heel_bone.tail = heel_bone.head + \
                rotation_matrix @ (heel_bone.tail - heel_bone.head)
            if foot_bone_name in armature.data.edit_bones:
                foot_bone = armature.data.edit_bones[foot_bone_name]
                foot_head_y = foot_bone.head[1]
                heel_bone.head[1] = foot_head_y
                heel_bone.tail[1] = foot_head_y
            heel_bone.parent = armature.data.edit_bones[foot_bone_name]

    def transfer_and_remove_vertex_weights(self, weight_mappings, obj):
        if obj is None or obj.type != 'MESH':
            return
        vgroups = obj.vertex_groups
        bpy.ops.object.mode_set(mode='OBJECT')
        for source_group_name, target_group_name in weight_mappings.items():
            if source_group_name not in vgroups:
                continue
            source_group = vgroups[source_group_name]
            if target_group_name not in vgroups:
                target_group = vgroups.new(name=target_group_name)
            else:
                target_group = vgroups[target_group_name]
            for vert in obj.data.vertices:
                new_weight = 0.0
                has_source_weight = False
                for group in vert.groups:
                    if group.group == source_group.index:
                        new_weight += group.weight
                        has_source_weight = True
                        break
                if has_source_weight:
                    for group in vert.groups:
                        if group.group == target_group.index:
                            new_weight += group.weight
                            break
                    target_group.add([vert.index], new_weight, 'REPLACE')
                    source_group.remove([vert.index])

    def create_circle_widget(self, name, radius=0.1, location=(0, 0, 0)):
        if name in bpy.data.objects:
            return bpy.data.objects[name]
        mesh = bpy.data.meshes.new(name + "_Mesh")
        obj = bpy.data.objects.new(name, mesh)
        bpy.context.collection.objects.link(obj)
        bm = bmesh.new()
        segments = 32
        verts = []
        for i in range(segments):
            angle = 2 * pi * i / segments
            x = cos(angle) * radius
            y = sin(angle) * radius
            verts.append(bm.verts.new((x, y, 0)))
        for i in range(segments):
            bm.edges.new((verts[i], verts[(i + 1) % segments]))
        bm.to_mesh(mesh)
        bm.free()
        obj.location = location
        obj.rotation_euler[0] = pi / 2
        obj.name = name
        return obj

    def create_capsule_path(self, bm, radius=0.14, spacing=0.6):
        segments = 16
        left_x = -spacing / 2
        right_x = spacing / 2
        verts = []
        for i in range(segments + 1):
            angle = pi / 2 + pi * i / segments
            x = left_x + cos(angle) * radius
            y = sin(angle) * radius
            verts.append(bm.verts.new((x, y, 0)))
        for i in range(segments + 1):
            angle = -pi / 2 + pi * i / segments
            x = right_x + cos(angle) * radius
            y = sin(angle) * radius
            verts.append(bm.verts.new((x, y, 0)))
        return verts

    def create_double_capsule_widget(self, name, inner_radius=0.14, outer_radius=0.17, spacing=0.6):
        if name in bpy.data.objects:
            return bpy.data.objects[name]
        mesh = bpy.data.meshes.new(name + "_Mesh")
        obj = bpy.data.objects.new(name, mesh)
        bpy.context.collection.objects.link(obj)
        bm = bmesh.new()
        verts_inner = self.create_capsule_path(bm, inner_radius, spacing)
        for i in range(len(verts_inner)):
            bm.edges.new(
                (verts_inner[i], verts_inner[(i + 1) % len(verts_inner)]))
        verts_outer = self.create_capsule_path(bm, outer_radius, spacing)
        for i in range(len(verts_outer)):
            bm.edges.new(
                (verts_outer[i], verts_outer[(i + 1) % len(verts_outer)]))
        bm.to_mesh(mesh)
        bm.free()
        obj.rotation_euler[0] = pi / 2
        obj.name = name
        return obj

    def lock_bone_transformations(self, bone):
        bone.lock_location[0] = False
        bone.lock_location[1] = False
        bone.lock_location[2] = False
        bone.lock_rotation_w = False
        bone.lock_rotation[0] = False
        bone.lock_rotation[1] = False
        bone.lock_rotation[2] = False
        bone.lock_scale[0] = False
        bone.lock_scale[1] = False
        bone.lock_scale[2] = False

    def select_and_move_bones(self, armature, keyword, collection_index):
        bpy.ops.pose.select_all(action='DESELECT')
        selected_bones = []
        for bone in armature.pose.bones:
            if keyword in bone.name:
                selected_bones.append(bone)
                bone.bone.select = True
                self.lock_bone_transformations(bone)
        if selected_bones:
            bpy.ops.armature.move_to_collection(
                collection_index=collection_index)
        return len(selected_bones)

    def execute(self, context):
        logger.info("Starting Rigify process")
        if not self.validate_scene_objects(context):
            self.report(
                {"ERROR"}, "Scene validation failed. Please check console for details.")
            return {"CANCELLED"}
        selected_object = context.active_object
        if not selected_object:
            logger.error("No active object selected")
            self.report(
                {"ERROR"}, "No active object selected. Please select a mesh or armature.")
            return {"CANCELLED"}
        logger.info(
            f"Processing object: '{selected_object.name}' (Type: {selected_object.type})")
        if self.check_if_already_rigged(selected_object):
            self.report(
                {"ERROR"}, "Rigify failed: Object already rigged. Select an unrigged object to create a new rig.")
            return {"CANCELLED"}
        original_selected_object_type = selected_object.type
        original_selected_object_name = selected_object.name
        if selected_object.type == 'MESH':
            logger.info(
                "Processing mesh object - searching for associated armature")
            for modifier in selected_object.modifiers:
                if modifier.type == 'ARMATURE' and modifier.object:
                    armature_obj = modifier.object
                    if self.check_if_already_rigged(armature_obj):
                        self.report(
                            {"ERROR"}, f"Associated armature '{armature_obj.name}' is already rigged.")
                        return {"CANCELLED"}
                    if not self.validate_armature_structure(armature_obj):
                        self.report(
                            {"ERROR"}, f"Armature '{armature_obj.name}' structure validation failed.")
                        return {"CANCELLED"}
                    if armature_obj.hide_get():
                        armature_obj.hide_set(False)
                    context.view_layer.objects.active = armature_obj
                    selected_object = armature_obj
                    logger.info(
                        f"Found and switched to armature: '{armature_obj.name}'")
                    break
        elif selected_object.type == 'ARMATURE':
            if not self.validate_armature_structure(selected_object):
                self.report(
                    {"ERROR"}, f"Armature '{selected_object.name}' structure validation failed.")
                return {"CANCELLED"}
        else:
            logger.info(
                "Object is not mesh or armature - searching for armature in scene")
            for obj in context.scene.objects:
                if obj.type == 'ARMATURE':
                    if self.check_if_already_rigged(obj):
                        continue
                    if not self.validate_armature_structure(obj):
                        continue
                    if obj.hide_get():
                        obj.hide_set(False)
                    context.view_layer.objects.active = obj
                    selected_object = obj
                    logger.info(f"Found suitable armature: '{obj.name}'")
                    break
        if selected_object.type != 'ARMATURE':
            logger.error("No suitable armature found for rigify process")
            self.report(
                {"ERROR"}, "No suitable armature found. Please ensure scene contains an unrigged armature with proper bone structure.")
            return {"CANCELLED"}
        OrigArmature = selected_object.name
        RigArmature = "RIG-" + OrigArmature
        if RigArmature in bpy.data.objects:
            logger.error(f"Rigified armature '{RigArmature}' already exists")
            self.report(
                {"ERROR"}, f"This armature has already been rigified as '{RigArmature}'. Please use the existing rig.")
            return {"CANCELLED"}
        if selected_object.get('rigify_generated') == True:
            logger.error(
                f"Armature '{selected_object.name}' is already Rigify-generated")
            self.report(
                {"ERROR"}, "This armature is already rigged with Rigify. Please use a different armature.")
            return {"CANCELLED"}
        logger.info("Searching for associated character mesh")
        CharacterMesh = None
        for obj in context.scene.objects:
            if obj.type == 'MESH':
                for modifier in obj.modifiers:
                    if modifier.type == 'ARMATURE' and modifier.object and modifier.object.name == OrigArmature:
                        if not self.validate_mesh_vertex_groups(obj):
                            logger.warning(
                                f"Mesh '{obj.name}' vertex groups validation failed")
                        CharacterMesh = obj
                        logger.info(f"Found character mesh: '{obj.name}'")
                        break
                if CharacterMesh:
                    break
        if not CharacterMesh:
            logger.error("No mesh found associated with the armature")
            self.report(
                {"ERROR"}, "No mesh found associated with the armature. Please ensure a mesh with armature modifier is present.")
            return {"CANCELLED"}
        logger.info("Preparing armature for rigify process")
        if selected_object.hide_viewport:
            selected_object.hide_viewport = False
        if selected_object.hide_get():
            selected_object.hide_set(False)
        original_location = selected_object.location.copy()
        original_rotation = selected_object.rotation_euler.copy()
        original_scale = selected_object.scale.copy()
        selected_object.location = (0, 0, 0)
        selected_object.rotation_euler = (0, 0, 0)
        selected_object.scale = (1, 1, 1)
        logger.info("Starting bone alignment adjustments")
        bpy.ops.object.mode_set(mode='EDIT')
        edit_bones = selected_object.data.edit_bones
        finger13_exists_left = "Bip001LFinger13" in edit_bones
        finger13_exists_right = "Bip001RFinger13" in edit_bones
        alignment_iterations = 0
        while self.check_alignment(edit_bones, finger13_exists_left, finger13_exists_right):
            self.apply_adjustment(
                edit_bones, finger13_exists_left, finger13_exists_right)
            alignment_iterations += 1
            if alignment_iterations > self.MAX_ITER:
                logger.warning(
                    f"Bone alignment reached maximum iterations ({self.MAX_ITER})")
                break
        if alignment_iterations > 0:
            logger.info(
                f"Completed bone alignment adjustments in {alignment_iterations} iterations")
        bpy.ops.object.mode_set(mode='OBJECT')
        bpy.ops.object.transform_apply(scale=True)
        logger.info("Processing spine bone adjustments")
        rig_armature_object = context.view_layer.objects.active
        if rig_armature_object and rig_armature_object.type == 'ARMATURE':
            bpy.ops.object.mode_set(mode='EDIT')
            spine_bone = rig_armature_object.data.edit_bones.get(
                "Bip001Spine2")
            if spine_bone:
                bone_length = (spine_bone.tail - spine_bone.head).length
                if bone_length < 0.06:
                    direction = spine_bone.tail - spine_bone.head
                    direction.normalize()
                    spine_bone.tail = spine_bone.head + direction * 0.15
                    spine_bone.tail.y = spine_bone.head.y
                    spine_bone.head.z += 0.03
                    spine_bone.tail.z += 0.03
                    logger.info("Applied spine bone length adjustments")
            bpy.ops.object.mode_set(mode='OBJECT')
        logger.info("Setting up bone collections and connections")
        armature = context.object
        self.remove_bone_collections(armature)
        bpy.ops.object.mode_set(mode='EDIT')
        bone_pairs = [
            ('Bip001Spine1', 'Bip001Spine2'),
            ('Bip001Pelvis', 'Bip001Spine'),
            ('Bip001RThigh', 'Bip001RCalf'),
            ('Bip001LThigh', 'Bip001LCalf'),
            ('Bip001RCalf', 'Bip001RFoot'),
            ('Bip001LCalf', 'Bip001LFoot'),
            ('Bip001LUpperArm', 'Bip001LForearm'),
            ('Bip001RUpperArm', 'Bip001RForearm'),
            ('Bip001LForearm', 'Bip001LHand'),
            ('Bip001RForearm', 'Bip001RHand'),
            ('Bip001LThigh', 'Bip001LCalf'),
            ('Bip001LCalf', 'Bip001LFoot'),
            ('Bip001LFoot', 'Bip001LToe0'),
            ('Bip001RThigh', 'Bip001RCalf'),
            ('Bip001RCalf', 'Bip001RFoot'),
            ('Bip001RFoot', 'Bip001RToe0'),
        ]
        self.adjust_bone_positions(armature, bone_pairs)
        twist_bones = {
            'Bip001RForeTwist': 'Bip001RForearm',
            'Bip001LForeTwist': 'Bip001LForearm'
        }
        for twist_bone, correct_parent in twist_bones.items():
            if twist_bone in armature.data.edit_bones and correct_parent in armature.data.edit_bones:
                bone = armature.data.edit_bones[twist_bone]
                if bone.parent != armature.data.edit_bones[correct_parent]:
                    bone.parent = armature.data.edit_bones[correct_parent]
        spine_bones = [
            'Bip001Spine', 'Bip001Spine1', 'Bip001Spine2',
            'Bip001LForearm', 'Bip001LHand', 'Bip001LFinger01',
            'Bip001LFinger02', 'Bip001LFinger11', 'Bip001LFinger12',
            'Bip001LFinger21', 'Bip001LFinger22', 'Bip001LFinger31',
            'Bip001LFinger32', 'Bip001LFinger41', 'Bip001LFinger42',
            'Bip001RForearm', 'Bip001RHand', 'Bip001RFinger01',
            'Bip001RFinger02', 'Bip001RFinger11', 'Bip001RFinger12',
            'Bip001RFinger21', 'Bip001RFinger22', 'Bip001RFinger31',
            'Bip001RFinger32', 'Bip001RFinger41', 'Bip001RFinger42',
            'Bip001LCalf', 'Bip001LFoot', 'Bip001LToe0',
            'Bip001RCalf', 'Bip001RFoot', 'Bip001RToe0',
            'Bip001Head',
            'Bip001LFinger13', 'Bip001LFinger23', 'Bip001LFinger33', 'Bip001LFinger43',
            'Bip001RFinger13', 'Bip001RFinger23', 'Bip001RFinger33', 'Bip001RFinger43',
        ]
        self.set_bone_connect(armature, spine_bones)
        bones_to_adjust_roll = [
            'Bip001Pelvis', 'Bip001Spine', 'Bip001Spine1',
            'Bip001Spine2', 'Bip001LClavicle', 'Bip001RClavicle'
        ]
        self.adjust_bone_roll(armature, bones_to_adjust_roll)
        logger.info(
            "Creating bone collections and setting up Rigify parameters")
        bpy.ops.object.mode_set(mode='OBJECT')
        bpy.ops.object.mode_set(mode='POSE')
        bone_data = [
            ('Torso', 0, 1),
            ('Torso (Tweak)', 1, 2),
            ('Fingers', 2, 3),
            ('Fingers (Details)', 3, 4),
            ('Arm.L (IK)', 4, 5),
            ('Arm.R (IK)', 5, 5),
            ('Arm.L (FK)', 6, 6),
            ('Arm.R (FK)', 7, 6),
            ('Arm.L (Tweak)', 8, 7),
            ('Arm.R (Tweak)', 9, 7),
            ('Leg.L (IK)', 10, 8),
            ('Leg.R (IK)', 11, 8),
            ('Leg.L (FK)', 12, 9),
            ('Leg.R (FK)', 13, 9),
            ('Leg.L (Tweak)', 14, 10),
            ('Leg.R (Tweak)', 15, 10),
            ('Hair', 16, 11),
            ('Cloth', 17, 11),
            ('Skirt', 18, 11),
            ('Tail', 19, 11),
            ('Root', 20, 12),
        ]
        self.process_bone_collections_and_rigify(armature, bone_data)
        bpy.ops.armature.collection_add()
        new_collection = armature.data.collections[-1]
        new_collection.name = 'Others'
        bpy.ops.armature.rigify_collection_add_ui_row(row=3, add=True)
        bpy.ops.armature.rigify_collection_add_ui_row(row=6, add=True)
        bpy.ops.armature.rigify_collection_add_ui_row(row=10, add=True)
        bpy.ops.armature.rigify_collection_add_ui_row(row=14, add=True)
        bpy.ops.armature.rigify_collection_add_ui_row(row=16, add=True)
        bones_and_rig_types = [
            ('Bip001Pelvis', 'spines.basic_spine', None),
            ('Bip001LClavicle', 'basic.super_copy', 'shoulder'),
            ('Bip001RClavicle', 'basic.super_copy', 'shoulder'),
            ('Bip001LUpperArm', 'limbs.arm', None),
            ('Bip001RUpperArm', 'limbs.arm', None),
            ('Bip001LThigh', 'limbs.leg', None),
            ('Bip001RThigh', 'limbs.leg', None),
            ('Bip001RFinger0', 'limbs.super_finger', None),
            ('Bip001LFinger0', 'limbs.super_finger', None),
            ('Bip001Neck', 'basic.super_copy', 'circle'),
            ('Bip001Head', 'basic.super_copy', 'circle'),
        ]
        if 'Bip001LFinger13' in armature.pose.bones:
            bones_and_rig_types.extend([
                ('Bip001LFinger11', 'limbs.super_finger', None),
                ('Bip001LFinger21', 'limbs.super_finger', None),
                ('Bip001LFinger31', 'limbs.super_finger', None),
                ('Bip001LFinger41', 'limbs.super_finger', None),
                ('Bip001RFinger11', 'limbs.super_finger', None),
                ('Bip001RFinger21', 'limbs.super_finger', None),
                ('Bip001RFinger31', 'limbs.super_finger', None),
                ('Bip001RFinger41', 'limbs.super_finger', None),
            ])
            logger.info("Applied finger13 bone configuration")
        else:
            bones_and_rig_types.extend([
                ('Bip001LFinger1', 'limbs.super_finger', None),
                ('Bip001LFinger2', 'limbs.super_finger', None),
                ('Bip001LFinger3', 'limbs.super_finger', None),
                ('Bip001LFinger4', 'limbs.super_finger', None),
                ('Bip001RFinger1', 'limbs.super_finger', None),
                ('Bip001RFinger2', 'limbs.super_finger', None),
                ('Bip001RFinger3', 'limbs.super_finger', None),
                ('Bip001RFinger4', 'limbs.super_finger', None),
            ])
            logger.info("Applied standard finger bone configuration")
        self.modify_bone_rig_type(armature, bones_and_rig_types)
        logger.info("Creating heel bones and preparing for Rigify generation")
        bpy.ops.object.mode_set(mode='EDIT')
        self.duplicate_and_adjust_heel_bone(
            armature, 'Bip001LFoot', 'Bip001LToe0', 'Bip001LHeel0', rotation_angle=1.5708)
        self.duplicate_and_adjust_heel_bone(
            armature, 'Bip001RFoot', 'Bip001RToe0', 'Bip001RHeel0', rotation_angle=-1.5708)
        bpy.ops.object.mode_set(mode='OBJECT')
        if context.object and context.object.type == 'ARMATURE':
            armature = context.object
            bpy.ops.object.mode_set(mode='EDIT')
            for bone in armature.data.edit_bones:
                if bone.name.startswith("Bip001R") and not bone.name.endswith(".R"):
                    bone.name += ".R"
                elif bone.name.startswith("Bip001L") and not bone.name.endswith(".L"):
                    bone.name += ".L"
            for bone in armature.data.edit_bones:
                if bone.name.startswith("Bip001R"):
                    bone.name = bone.name.replace("Bip001R", "Bip001", 1)
                elif bone.name.startswith("Bip001L"):
                    bone.name = bone.name.replace("Bip001L", "Bip001", 1)
        logger.info("Generating Rigify armature")
        try:
            bpy.ops.pose.rigify_generate()
            logger.info("Rigify generation completed successfully")
        except Exception as e:
            logger.error(f"Rigify generation failed: {str(e)}")
            self.report({"ERROR"}, f"Rigify generation failed: {str(e)}")
            return {"CANCELLED"}
        rig_obj = context.scene.objects.get(RigArmature)
        if rig_obj:
            rig_obj.location = original_location
            rig_obj.rotation_euler = original_rotation
            rig_obj.scale = original_scale
            logger.info(f"Applied original transforms to rig '{RigArmature}'")
        logger.info("Configuring bone properties and custom shapes")
        bpy.ops.object.mode_set(mode='POSE')
        armature = context.object
        pose_bone_neck = armature.pose.bones.get("Bip001Neck")
        if pose_bone_neck:
            bpy.ops.object.mode_set(mode='EDIT')
            edit_bone_neck = armature.data.edit_bones.get("Bip001Neck")
            if edit_bone_neck:
                neck_length = (edit_bone_neck.tail -
                               edit_bone_neck.head).length / 2
            bpy.ops.object.mode_set(mode='POSE')
            pose_bone_neck.custom_shape_translation.y = neck_length
            pose_bone_neck.custom_shape_scale_xyz = (1.5, 1.5, 1.5)
        pose_bone_head = armature.pose.bones.get("Bip001Head")
        if pose_bone_head:
            bpy.ops.object.mode_set(mode='EDIT')
            edit_bone_head = armature.data.edit_bones.get("Bip001Head")
            if edit_bone_head:
                head_length = (edit_bone_head.tail -
                               edit_bone_head.head).length
            bpy.ops.object.mode_set(mode='POSE')
            pose_bone_head.custom_shape_translation.y = head_length * 1.2
            pose_bone_head.custom_shape_scale_xyz = (2, 2, 2)
        bpy.ops.object.mode_set(mode='OBJECT')
        context.object.pose.bones["Bip001UpperArm_parent.L"]["IK_Stretch"] = 0.000
        context.object.pose.bones["Bip001UpperArm_parent.R"]["IK_Stretch"] = 0.000
        context.object.pose.bones["Bip001Thigh_parent.L"]["IK_Stretch"] = 0.000
        context.object.pose.bones["Bip001Thigh_parent.R"]["IK_Stretch"] = 0.000
        obj = context.active_object
        if obj and obj.type == 'ARMATURE':
            bpy.ops.object.mode_set(mode='EDIT')
            for bone in obj.data.edit_bones:
                if bone.name.startswith('ORG-'):
                    bone.use_deform = True
            bpy.ops.object.mode_set(mode='OBJECT')
        logger.info("Updating vertex groups and weight mappings")
        bpy.ops.object.select_all(action='DESELECT')
        context.view_layer.objects.active = CharacterMesh
        CharacterMesh.select_set(True)
        for obj in context.selected_objects:
            if obj.type == 'MESH':
                for group in obj.vertex_groups:
                    new_name = "ORG-" + group.name
                    group.name = new_name
        weight_mappings = {
            "ORG-Bip001UpArmTwist.L": "DEF-Bip001UpperArm.L",
            "ORG-Bip001UpArmTwist1.L": "DEF-Bip001UpperArm.L",
            "ORG-Bip001UpArmTwist2.L": "DEF-Bip001UpperArm.L.001",
            "ORG-Bip001UpperArm.L": "DEF-Bip001UpperArm.L.001",
            "ORG-Bip001Forearm.L": "DEF-Bip001Forearm.L",
            "ORG-Bip001ForeTwist.L": "DEF-Bip001Forearm.L.001",
            "ORG-Bip001ForeTwist1.L": "DEF-Bip001Forearm.L.001",
            "ORG-Bone_HandTwist_L": "DEF-Bip001Forearm.L.001",
            "ORG-Bip001ForeTwist2.L": "DEF-Bip001Forearm.L.001",
            "ORG-Bip001_L_Elbow_F": "DEF-Bip001UpperArm.L.001",
            "ORG-Bip001_L_Elbow_B": "DEF-Bip001UpperArm.L.001",
            "ORG-Bip001UpArmTwist.R": "DEF-Bip001UpperArm.R",
            "ORG-Bip001UpArmTwist1.R": "DEF-Bip001UpperArm.R",
            "ORG-Bip001UpArmTwist2.R": "DEF-Bip001UpperArm.R.001",
            "ORG-Bip001UpperArm.R": "DEF-Bip001UpperArm.R.001",
            "ORG-Bip001Forearm.R": "DEF-Bip001Forearm.R",
            "ORG-Bip001ForeTwist.R": "DEF-Bip001Forearm.R.001",
            "ORG-Bip001ForeTwist1.R": "DEF-Bip001Forearm.R.001",
            "ORG-Bone_HandTwist_R": "DEF-Bip001Forearm.R.001",
            "ORG-Bip001ForeTwist2.R": "DEF-Bip001Forearm.R.001",
            "ORG-Bip001_R_Elbow_F": "DEF-Bip001UpperArm.R.001",
            "ORG-Bip001_R_Elbow_B": "DEF-Bip001UpperArm.R.001",
            "ORG-Bip001ThighTwist.L": "DEF-Bip001Thigh.L",
            "ORG-Bip001Thigh.L": "DEF-Bip001Thigh.L.001",
            "ORG-Bip001_L_Calf": "DEF-Bip001Calf.L",
            "ORG-Bip001_L_Knee_B": "DEF-Bip001Thigh.L.001",
            "ORG-Bip001_L_Knee_F": "DEF-Bip001Thigh.L.001",
            "ORG-Bip001ThighTwist1.L": "DEF-Bip001Thigh.L",
            "ORG-Bip001_L_CalfTwist": "DEF-Bip001Calf.L.001",
            "ORG-Bip001ThighTwist.R": "DEF-Bip001Thigh.R",
            "ORG-Bip001Thigh.R": "DEF-Bip001Thigh.R.001",
            "ORG-Bip001_R_Calf": "DEF-Bip001Calf.R",
            "ORG-Bip001_R_Knee_B": "DEF-Bip001Thigh.R.001",
            "ORG-Bip001_R_Knee_F": "DEF-Bip001Thigh.R.001",
            "ORG-Bip001ThighTwist1.R": "DEF-Bip001Thigh.R",
            "ORG-Bip001_R_CalfTwist": "DEF-Bip001Calf.R.001",
        }
        self.transfer_and_remove_vertex_weights(weight_mappings, CharacterMesh)
        bpy.ops.object.select_all(action='DESELECT')
        context.view_layer.objects.active = CharacterMesh
        CharacterMesh.select_set(True)
        modifier_found = False
        for modifier in CharacterMesh.modifiers:
            if modifier.type == 'ARMATURE' and modifier.object and modifier.object.name == OrigArmature:
                rig_armature_object = context.scene.objects.get(RigArmature)
                if rig_armature_object:
                    modifier.object = rig_armature_object
                    modifier_found = True
                    logger.info(
                        f"Updated armature modifier to use '{RigArmature}'")
                    break
        if not modifier_found:
            logger.warning("Could not find armature modifier to update")
        bpy.ops.object.select_all(action='DESELECT')
        context.view_layer.objects.active = CharacterMesh
        CharacterMesh.select_set(True)
        mesh_obj = context.active_object
        if mesh_obj and mesh_obj.type == 'MESH':
            armature_mod = next(
                (mod for mod in mesh_obj.modifiers if mod.type == 'ARMATURE'), None)
            if armature_mod and armature_mod.object:
                armature_obj = armature_mod.object
                mesh_obj.parent = armature_obj
                logger.info("Set mesh parent to rig armature")
        logger.info("Setting up eye tracking system")
        context.view_layer.objects.active = CharacterMesh
        CharacterMesh.select_set(True)
        obj = context.object
        source_shape_keys = ["Pupil_R", "Pupil_L", "Pupil_Up", "Pupil_Down"]
        target_material_name = None
        for slot in obj.material_slots:
            if "Eye" in slot.name:
                target_material_name = slot.name
                break
        offset_connected = Vector((0.0, -0.001, 0.0))
        offset_unconnected = Vector((0.0, 0.001, 0.0))
        NEIGHBOR_DEPTH = 4
        if obj and obj.type == 'MESH' and obj.data.shape_keys:
            keys = obj.data.shape_keys.key_blocks
            basis = obj.data.shape_keys.reference_key
            bpy.ops.object.mode_set(mode='OBJECT')
            mat_slots = obj.material_slots
            relevant_face_vert_indices = set()
            relevant_edges = set()
            for poly in obj.data.polygons:
                if poly.material_index < len(mat_slots) and mat_slots[poly.material_index].name == target_material_name:
                    relevant_face_vert_indices.update(poly.vertices)
                    relevant_edges.update(tuple(sorted((poly.vertices[i], poly.vertices[j])))
                                          for i in range(len(poly.vertices)) for j in range(i + 1, len(poly.vertices)))
            if relevant_face_vert_indices:
                connectivity = defaultdict(set)
                edge_faces = defaultdict(int)
                for poly in obj.data.polygons:
                    if poly.material_index < len(mat_slots) and mat_slots[poly.material_index].name == target_material_name:
                        verts = poly.vertices
                        for i in range(len(verts)):
                            for j in range(i + 1, len(verts)):
                                vi, vj = verts[i], verts[j]
                                edge = tuple(sorted((vi, vj)))
                                if vi in relevant_face_vert_indices and vj in relevant_face_vert_indices:
                                    connectivity[vi].add(vj)
                                    connectivity[vj].add(vi)
                                    edge_faces[edge] += 1
                seed_vertices = {
                    v for v, linked in connectivity.items() if len(linked) > 10}
                connected_vertices = set()
                visited = set()
                for seed in seed_vertices:
                    queue = deque()
                    queue.append((seed, 0))
                    visited.add(seed)
                    connected_vertices.add(seed)
                    while queue:
                        current, depth = queue.popleft()
                        if depth >= NEIGHBOR_DEPTH:
                            continue
                        for neighbor in connectivity[current]:
                            if neighbor not in visited:
                                visited.add(neighbor)
                                connected_vertices.add(neighbor)
                                queue.append((neighbor, depth + 1))
                unconnected_vertices = relevant_face_vert_indices - connected_vertices
                border_vertices = set()
                for edge, count in edge_faces.items():
                    if count == 1:
                        border_vertices.update(edge)
                movable_unconnected = unconnected_vertices - border_vertices
                for source_name in source_shape_keys:
                    if source_name not in keys:
                        continue
                    source_key = keys[source_name]
                    index = next(i for i, k in enumerate(keys)
                                 if k.name == source_key.name)
                    obj.active_shape_key_index = index
                    bpy.ops.object.shape_key_add(from_mix=False)
                    key_L = obj.data.shape_keys.key_blocks[-1]
                    key_L.name = f"{source_name}.L"
                    bpy.ops.object.shape_key_add(from_mix=False)
                    key_R = obj.data.shape_keys.key_blocks[-1]
                    key_R.name = f"{source_name}.R"
                    for i in relevant_face_vert_indices:
                        base_co = basis.data[i].co
                        source_co = source_key.data[i].co
                        delta = source_co - base_co
                        if i in connected_vertices:
                            offset = offset_connected
                        elif i in movable_unconnected:
                            offset = offset_unconnected
                        else:
                            offset = Vector((0.0, 0.0, 0.0))
                        if base_co.x >= 0:
                            key_L.data[i].co = base_co + delta * 2 + offset
                        else:
                            key_R.data[i].co = base_co + delta * 2 + offset
                logger.info("Created eye shape keys")
                bpy.ops.object.select_all(action='DESELECT')
        rig_armature_object = context.scene.objects.get(RigArmature)
        if rig_armature_object:
            bpy.ops.object.select_all(action='DESELECT')
            context.view_layer.objects.active = rig_armature_object
            rig_armature_object.select_set(True)
            eye_tracker_location = rig_armature_object.location.copy()
            eye_tracker_rotation = rig_armature_object.rotation_euler.copy()
            eye_tracker_scale = rig_armature_object.scale.copy()
            rig_armature_object.location = (0, 0, 0)
            rig_armature_object.rotation_euler = (0, 0, 0)
            rig_armature_object.scale = (1, 1, 1)
            bpy.ops.object.mode_set(mode='EDIT')
            target_bone = rig_armature_object.data.edit_bones.get(
                "ORG-Bip001Head")
            if target_bone:
                context.scene.cursor.location = target_bone.head
            bpy.ops.object.mode_set(mode='OBJECT')
        logger.info("Creating eye tracker bones")
        bpy.ops.object.mode_set(mode='EDIT')
        bpy.ops.armature.bone_primitive_add()
        new_bone = rig_armature_object.data.edit_bones[-1]
        new_bone.name = "EyeTracker"
        head = new_bone.head.copy()
        tail = new_bone.tail.copy()
        direction = tail - head
        if direction.length > 0:
            direction.normalize()
            new_bone.tail = head + direction * 0.03
        new_bone.head.y -= 0.15
        new_bone.tail.y -= 0.15
        new_bone.head.z += 0.03
        new_bone.tail.z += 0.03
        parent_bone = rig_armature_object.data.edit_bones.get("ORG-Bip001Head")
        if parent_bone:
            new_bone.parent = parent_bone
            new_bone.use_connect = False
        eye_tracker_head = new_bone.head
        eye_tracker_tail = new_bone.tail
        eye_tracker_y_offset = new_bone.tail.y - new_bone.head.y
        eye_tracker_z_offset = new_bone.tail.z - new_bone.head.z
        eye_l = rig_armature_object.data.edit_bones.new("Eye.L")
        eye_l.head = eye_tracker_head + mathutils.Vector((0.03, 0, 0))
        eye_l.tail = eye_l.head + \
            mathutils.Vector((0, eye_tracker_y_offset, eye_tracker_z_offset))
        eye_l.parent = new_bone
        eye_l.use_connect = False
        eye_r = rig_armature_object.data.edit_bones.new("Eye.R")
        eye_r.head = eye_tracker_head + mathutils.Vector((-0.03, 0, 0))
        eye_r.tail = eye_r.head + \
            mathutils.Vector((0, eye_tracker_y_offset, eye_tracker_z_offset))
        eye_r.parent = new_bone
        eye_r.use_connect = False
        context.scene.cursor.location = mathutils.Vector((0, 0, 0))
        bpy.ops.object.mode_set(mode='OBJECT')
        rig_armature_object.location = eye_tracker_location
        rig_armature_object.rotation_euler = eye_tracker_rotation
        rig_armature_object.scale = eye_tracker_scale
        logger.info("Creating eye control widgets")
        self.create_circle_widget(
            "WGT-rig_eye.L", radius=0.1, location=(-0.3, 0, 0))
        self.create_circle_widget(
            "WGT-rig_eye.R", radius=0.1, location=(0.3, 0, 0))
        self.create_double_capsule_widget(
            "WGT-rig_eyes", inner_radius=0.14, outer_radius=0.17, spacing=0.6)
        if CharacterMesh is None or rig_armature_object is None:
            pass
        else:
            bpy.ops.object.select_all(action='DESELECT')
            last_selected_mesh = None
            for obj in context.scene.objects:
                if obj.type == 'MESH':
                    for modifier in obj.modifiers:
                        if modifier.type == 'ARMATURE' and modifier.object == rig_armature_object:
                            obj.select_set(True)
                            last_selected_mesh = obj
            if last_selected_mesh:
                context.view_layer.objects.active = last_selected_mesh
                bpy.ops.object.mode_set(mode='OBJECT')
                last_selected_mesh.select_set(False)
                context.view_layer.objects.active = rig_armature_object
                eye_tracker_bone_name = "EyeTracker"
                shape_key_names = {
                    "Pupil_L": "LOC_X",
                    "Pupil_R": "LOC_X",
                    "Pupil_Up": "LOC_Y",
                    "Pupil_Down": "LOC_Y"
                }
                expressions = {
                    "Pupil_L": 'max(min((bone_x * 10), 1), 0) if bone_x > 0 else 0',
                    "Pupil_R": 'max(min((-bone_x * 10), 1), 0) if bone_x < 0 else 0',
                    "Pupil_Up": 'max(min((bone_y * 10), 1), 0) if bone_y > 0 else 0',
                    "Pupil_Down": 'max(min((-bone_y * 10), 1), 0) if bone_y < 0 else 0'
                }
                if last_selected_mesh.data.shape_keys:
                    for shape_key_name, transform_axis in shape_key_names.items():
                        if shape_key_name in last_selected_mesh.data.shape_keys.key_blocks:
                            shape_key = last_selected_mesh.data.shape_keys.key_blocks[shape_key_name]
                            driver = shape_key.driver_add('value').driver
                            driver.type = 'SCRIPTED'
                            var = driver.variables.new()
                            var.name = 'bone_' + transform_axis[-1].lower()
                            var.type = 'TRANSFORMS'
                            var.targets[0].id = rig_armature_object
                            var.targets[0].bone_target = eye_tracker_bone_name
                            var.targets[0].transform_type = transform_axis
                            var.targets[0].transform_space = 'LOCAL_SPACE'
                            driver.expression = expressions[shape_key_name]
                    for bone_suffix in ['.L', '.R']:
                        bone_name = "Eye" + bone_suffix
                        for shape_key_prefix, transform_axis in shape_key_names.items():
                            shape_key_name = shape_key_prefix + bone_suffix
                            if shape_key_name in last_selected_mesh.data.shape_keys.key_blocks:
                                shape_key = last_selected_mesh.data.shape_keys.key_blocks[shape_key_name]
                                driver = shape_key.driver_add('value').driver
                                driver.type = 'SCRIPTED'
                                var = driver.variables.new()
                                var.name = 'bone_' + transform_axis[-1].lower()
                                var.type = 'TRANSFORMS'
                                var.targets[0].id = rig_armature_object
                                var.targets[0].bone_target = bone_name
                                var.targets[0].transform_type = transform_axis
                                var.targets[0].transform_space = 'LOCAL_SPACE'
                                driver.expression = expressions[shape_key_prefix]
                    logger.info("Set up eye tracking drivers")
                bpy.ops.object.mode_set(mode='OBJECT')
        logger.info("Configuring eye bone constraints")
        context.view_layer.objects.active = rig_armature_object
        bpy.ops.object.mode_set(mode='POSE')
        bones_to_lock = ["EyeTracker", "Eye.L", "Eye.R"]
        for bone_name in bones_to_lock:
            if bone_name in rig_armature_object.pose.bones:
                pbone = rig_armature_object.pose.bones[bone_name]
                pbone.lock_location[0] = False
                pbone.lock_location[1] = False
                pbone.lock_location[2] = True
                pbone.lock_rotation[0] = True
                pbone.lock_rotation[1] = True
                pbone.lock_rotation[2] = True
                pbone.lock_rotations_4d = True
        bpy.ops.object.mode_set(mode='OBJECT')
        context.view_layer.objects.active = rig_armature_object
        bpy.ops.object.mode_set(mode='POSE')
        pose_bones = rig_armature_object.pose.bones
        custom_shapes = {
            "EyeTracker": "WGT-rig_eyes",
            "Eye.L": "WGT-rig_eye.L",
            "Eye.R": "WGT-rig_eye.R"
        }
        for bone_name, shape_name in custom_shapes.items():
            if bone_name in pose_bones and shape_name in bpy.data.objects:
                bone = pose_bones[bone_name]
                bone.custom_shape = bpy.data.objects[shape_name]
                bone.custom_shape_scale_xyz = (4.0, 4.0, 4.0)
        bpy.ops.object.mode_set(mode='OBJECT')
        mesh_names_to_delete = ["WGT-rig_eyes",
                                "WGT-rig_eye.R", "WGT-rig_eye.L"]
        bpy.ops.object.select_all(action='DESELECT')
        for name in mesh_names_to_delete:
            obj = bpy.data.objects.get(name)
            if obj and obj.type == 'MESH':
                if obj.name in context.view_layer.objects:
                    obj.select_set(True)
        bpy.ops.object.delete()
        logger.info("Organizing bones into collections and setting colors")
        bpy.ops.object.mode_set(mode='POSE')
        armature = context.view_layer.objects.active
        bones_to_move = {
            0: ["torso", "chest", "Bip001Clavicle.L", "Bip001Clavicle.R", "hips", "Bip001Neck", "Bip001Head"],
            1: ["Bip001Spine_fk", "Bip001Spine1_fk", "Bip001Spine2_fk", "tweak_Bip001Spine1",
                "tweak_Bip001Spine2", "tweak_Bip001Spine2.001", "tweak_Bip001Spine", "tweak_Bip001Pelvis", "Bip001Pelvis_fk", "tweak_Bip001Neck"],
            2: ["Bip001Finger0_master.L", "Bip001Finger1_master.L", "Bip001Finger2_master.L", "Bip001Finger3_master.L", "Bip001Finger4_master.L",
                "Bip001Finger0_master.R", "Bip001Finger1_master.R", "Bip001Finger2_master.R", "Bip001Finger3_master.R", "Bip001Finger4_master.R",
                "Bip001Finger11_master.L", "Bip001Finger21_master.L", "Bip001Finger21_master.L", "Bip001Finger31_master.L", "Bip001Finger41_master.L",
                "Bip001Finger11_master.R", "Bip001Finger21_master.R", "Bip001Finger21_master.R", "Bip001Finger31_master.R", "Bip001Finger41_master.R"],
            3: ["Bip001Finger01.L", "Bip001Finger02.L", "Bip001Finger0.L.001", "Bip001Finger1.L", "Bip001Finger11.L", "Bip001Finger12.L", "Bip001Finger1.L.001",
                "Bip001Finger2.L", "Bip001Finger21.L", "Bip001Finger22.L", "Bip001Finger2.L.001", "Bip001Finger3.L", "Bip001Finger31.L", "Bip001Finger32.L", "Bip001Finger3.L.001",
                "Bip001Finger4.L", "Bip001Finger41.L", "Bip001Finger42.L", "Bip001Finger4.L.001", "Bip001Finger0.L",
                "Bip001Finger01.R", "Bip001Finger02.R", "Bip001Finger0.R.001", "Bip001Finger1.R", "Bip001Finger11.R", "Bip001Finger12.R", "Bip001Finger1.R.001",
                "Bip001Finger2.R", "Bip001Finger21.R", "Bip001Finger22.R", "Bip001Finger2.R.001", "Bip001Finger3.R", "Bip001Finger31.R", "Bip001Finger32.R", "Bip001Finger3.R.001",
                "Bip001Finger4.R", "Bip001Finger41.R", "Bip001Finger42.R", "Bip001Finger4.R.001", "Bip001Finger0.R",
                "Bip001Finger13.L", "Bip001Finger11.L.001", "Bip001Finger23.L", "Bip001Finger21.L.001", "Bip001Finger33.L", "Bip001Finger31.L.001", "Bip001Finger43.L", "Bip001Finger41.L.001",
                "Bip001Finger13.R", "Bip001Finger11.R.001", "Bip001Finger23.R", "Bip001Finger21.R.001", "Bip001Finger33.R", "Bip001Finger31.R.001", "Bip001Finger43.R", "Bip001Finger41.R.001"],
            4: ["Bip001UpperArm_parent.L", "Bip001UpperArm_ik.L", "Bip001Hand_ik.L"],
            5: ["Bip001UpperArm_parent.R", "Bip001UpperArm_ik.R", "Bip001Hand_ik.R"],
            6: ["Bip001UpperArm_fk.L", "Bip001Forearm_fk.L", "Bip001Hand_fk.L"],
            7: ["Bip001UpperArm_fk.R", "Bip001Forearm_fk.R", "Bip001Hand_fk.R"],
            8: ["Bip001UpperArm_tweak.L", "Bip001UpperArm_tweak.L.001", "Bip001Forearm_tweak.L", "Bip001Forearm_tweak.L.001", "Bip001Hand_tweak.L"],
            9: ["Bip001UpperArm_tweak.R", "Bip001UpperArm_tweak.R.001", "Bip001Forearm_tweak.R", "Bip001Forearm_tweak.R.001", "Bip001Hand_tweak.R"],
            10: ["Bip001Thigh_parent.L", "Bip001Thigh_ik.L", "Bip001Foot_heel_ik.L", "Bip001Foot_spin_ik.L", "Bip001Toe0.L", "Bip001Foot_ik.L"],
            11: ["Bip001Thigh_parent.R", "Bip001Thigh_ik.R", "Bip001Foot_heel_ik.R", "Bip001Foot_spin_ik.R", "Bip001Toe0.R", "Bip001Foot_ik.R"],
            12: ["Bip001Thigh_fk.L", "Bip001Calf_fk.L", "Bip001Foot_fk.L"],
            13: ["Bip001Thigh_fk.R", "Bip001Calf_fk.R", "Bip001Foot_fk.R"],
            14: ["Bip001Thigh_tweak.L", "Bip001Thigh_tweak.L.001", "Bip001Calf_tweak.L", "Bip001Calf_tweak.L.001", "Bip001Foot_tweak.L"],
            15: ["Bip001Thigh_tweak.R", "Bip001Thigh_tweak.R.001", "Bip001Calf_tweak.R", "Bip001Calf_tweak.R.001", "Bip001Foot_tweak.R"],
        }
        theme_for_groups = {
            0: 'THEME09', 1: 'THEME04', 2: 'THEME14', 3: 'THEME03',
            4: 'THEME01', 5: 'THEME01', 6: 'THEME03', 7: 'THEME03',
            8: 'THEME04', 9: 'THEME04', 10: 'THEME01', 11: 'THEME01',
            12: 'THEME03', 13: 'THEME03', 14: 'THEME04', 15: 'THEME04',
        }
        for group_index, bone_names in bones_to_move.items():
            theme = theme_for_groups.get(group_index)
            for bone_name in bone_names:
                bone = armature.pose.bones.get(bone_name)
                if bone and theme:
                    bone.color.palette = theme
        for collection_index, bone_names in bones_to_move.items():
            bpy.ops.pose.select_all(action='DESELECT')
            bones_found = False
            for bone_name in bone_names:
                bone = armature.pose.bones.get(bone_name)
                if bone:
                    bone.bone.select = True
                    bones_found = True
            if bones_found:
                bpy.ops.armature.move_to_collection(
                    collection_index=collection_index)
        context.object.data.collections_all["ORG"].is_visible = True
        bpy.ops.object.mode_set(mode='POSE')
        armature = context.view_layer.objects.active
        keywords_and_collections = [
            ("Hair", 16), ("Earrings", 16),
            ("Piao", 17),
            ("Skirt", 18), ("Trousers", 18),
            ("Tail", 19),
            ("Other", 21), ("Weapon", 21), ("Prop", 21), ("Chibang", 21),
            ("Bip001Neck.001", 22), ("Bip001Head.001", 22),
            ("EyeTracker", 0), ("Chest", 0), ("Eye.L", 0), ("Eye.R", 0),
        ]
        for keyword, collection_index in keywords_and_collections:
            self.select_and_move_bones(armature, keyword, collection_index)
        context.object.data.collections_all["ORG"].is_visible = False
        context.object.data.collections_all["Torso (Tweak)"].is_visible = False
        context.object.data.collections_all["Arm.L (FK)"].is_visible = False
        context.object.data.collections_all["Arm.R (FK)"].is_visible = False
        context.object.data.collections_all["Leg.L (FK)"].is_visible = False
        context.object.data.collections_all["Leg.R (FK)"].is_visible = False
        context.object.data.collections_all["Arm.L (Tweak)"].is_visible = False
        context.object.data.collections_all["Arm.R (Tweak)"].is_visible = False
        context.object.data.collections_all["Leg.L (Tweak)"].is_visible = False
        context.object.data.collections_all["Leg.R (Tweak)"].is_visible = False
        context.object.data.collections_all["Hair"].is_visible = False
        context.object.data.collections_all["Cloth"].is_visible = False
        context.object.data.collections_all["Skirt"].is_visible = False
        context.object.data.collections_all["Tail"].is_visible = False
        bpy.ops.object.mode_set(mode='POSE')
        armature = context.view_layer.objects.active
        pose_bones = armature.pose.bones
        theme_assignments = {
            "EyeTracker": "THEME01", "Eye.L": "THEME09", "Eye.R": "THEME09"
        }
        for bone_name, theme in theme_assignments.items():
            bone = pose_bones.get(bone_name)
            if bone:
                bone.color.palette = theme
        logger.info("Creating neck and head FK controls")
        if rig_armature_object:
            bpy.ops.object.mode_set(mode='EDIT')
            arm = armature.data
            if 'ORG-Bip001Neck' in arm.edit_bones:
                arm.edit_bones['ORG-Bip001Neck'].name = 'Bip001Neck'
            if 'ORG-Bip001Head' in arm.edit_bones:
                arm.edit_bones['ORG-Bip001Head'].name = 'Bip001Head'
            if 'Bip001Neck' in arm.edit_bones:
                neck_bone = arm.edit_bones['Bip001Neck']
                new_bone = arm.edit_bones.new('Bip001Neck._fk')
                new_bone.head = neck_bone.head.copy()
                new_bone.tail = neck_bone.tail.copy()
                new_bone.roll = neck_bone.roll
                new_bone.parent = neck_bone.parent
                rotation_matrix = mathutils.Matrix.Rotation(-1.5708, 4, 'X')
                new_bone.tail = new_bone.head + \
                    rotation_matrix @ (new_bone.tail - new_bone.head)
                new_bone.tail.z = new_bone.head.z
                direction = (new_bone.tail - new_bone.head).normalized()
                new_bone.tail = new_bone.head + direction * 0.05
                neck_bone.use_connect = False
                neck_bone.parent = new_bone
            if 'Bip001Head' in arm.edit_bones:
                head_bone = arm.edit_bones['Bip001Head']
                new_bone = arm.edit_bones.new('Bip001Head._fk')
                new_bone.head = head_bone.head.copy()
                new_bone.tail = head_bone.tail.copy()
                new_bone.roll = head_bone.roll
                new_bone.parent = head_bone.parent
                new_bone.tail = new_bone.head + \
                    rotation_matrix @ (new_bone.tail - new_bone.head)
                new_bone.tail.z = new_bone.head.z
                direction = (new_bone.tail - new_bone.head).normalized()
                new_bone.tail = new_bone.head + direction * 0.05
                head_bone.use_connect = False
                head_bone.parent = new_bone
            bpy.ops.object.mode_set(mode='POSE')
            if "Bip001Neck._fk" in armature.pose.bones:
                tweak_bone = armature.pose.bones["Bip001Neck._fk"]
                spine2_fk = armature.pose.bones.get("Bip001Spine2_fk")
                if spine2_fk:
                    tweak_bone.custom_shape = spine2_fk.custom_shape
                tweak_bone.custom_shape_transform = armature.pose.bones["Bip001Neck"]
            if "Bip001Head._fk" in armature.pose.bones:
                tweak_bone = armature.pose.bones["Bip001Head._fk"]
                spine2_fk = armature.pose.bones.get("Bip001Spine2_fk")
                if spine2_fk:
                    tweak_bone.custom_shape = spine2_fk.custom_shape
                tweak_bone.custom_shape_transform = armature.pose.bones["Bip001Head"]
            context.object.data.collections_all["Torso (Tweak)"].is_visible = True
            armature = context.view_layer.objects.active
            bones_to_move = {
                0: ["Bip001Neck", "Bip001Head"],
                1: ["Bip001Neck._fk", "Bip001Head._fk"],
            }
            theme_for_groups = {0: 'THEME09', 1: 'THEME04', }
            for group_index, bone_names in bones_to_move.items():
                theme = theme_for_groups.get(group_index)
                for bone_name in bone_names:
                    bone = armature.pose.bones.get(bone_name)
                    if bone and theme:
                        bone.color.palette = theme
            for collection_index, bone_names in bones_to_move.items():
                bpy.ops.pose.select_all(action='DESELECT')
                bones_found = False
                for bone_name in bone_names:
                    bone = armature.pose.bones.get(bone_name)
                    if bone:
                        bone.bone.select = True
                        bones_found = True
                if bones_found:
                    bpy.ops.armature.move_to_collection(
                        collection_index=collection_index)
            context.object.data.collections_all["Torso (Tweak)"].is_visible = False
            bpy.ops.object.mode_set(mode='OBJECT')
            logger.info("Created neck and head FK controls")
        logger.info("Applying bone length constraints")
        if rig_armature_object:
            obj = rig_armature_object
            if obj and obj.type == 'ARMATURE':
                if bpy.context.mode != 'EDIT_ARMATURE':
                    bpy.ops.object.mode_set(mode='EDIT')
                armature = obj.data
                for bone in armature.edit_bones:
                    length = (bone.head - bone.tail).length
                    if length > 1.0:
                        direction = (bone.tail - bone.head).normalized()
                        bone.tail = bone.head + direction * 0.5
                bpy.ops.object.mode_set(mode='OBJECT')
        logger.info("Finalizing rig setup")
        bpy.ops.object.select_all(action='DESELECT')
        original_armature = bpy.data.objects.get(OrigArmature)
        if original_armature and original_armature.type == 'ARMATURE':
            bpy.ops.object.select_all(action='DESELECT')
            original_armature.select_set(True)
            context.view_layer.objects.active = original_armature
            original_armature.hide_set(True)
            logger.info(f"Hidden original armature '{OrigArmature}'")
        if original_selected_object_type == 'MESH':
            original_mesh = bpy.data.objects.get(original_selected_object_name)
            if original_mesh:
                context.view_layer.objects.active = original_mesh
                original_mesh.select_set(True)
            else:
                if CharacterMesh:
                    context.view_layer.objects.active = CharacterMesh
                    CharacterMesh.select_set(True)
        else:
            rig_armature_object = context.scene.objects.get(RigArmature)
            if rig_armature_object:
                context.view_layer.objects.active = rig_armature_object
                rig_armature_object.select_set(True)
        logger.info(f"Rigify completed for '{OrigArmature}'")
        self.report({"INFO"}, f"Rigify completed for '{OrigArmature}'")
        return {"FINISHED"}


class WW_OT_SetupHeadDriver(Operator):
    bl_idname = "shader.setup_head_driver"
    bl_label = "Set Up Head Driver"
    bl_description = "Reset head bone position and parent Head Origin and Light Direction to armature"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        active_obj = context.active_object
        if not active_obj or active_obj.type not in {"MESH", "ARMATURE"}:
            return False
        if active_obj.type == "ARMATURE":
            return True
        return get_armature_from_modifiers(active_obj) is not None

    def execute(self, context):
        previous_mode = context.mode
        active_obj = context.active_object

        if not active_obj or active_obj.type not in {"MESH", "ARMATURE"}:
            self.report(
                {"ERROR"}, "Please select a mesh or armature to set up head driver."
            )
            return {"CANCELLED"}

        if active_obj.type == "ARMATURE":
            armature = active_obj
            mesh = self.get_mesh_from_armature(armature)
            if not mesh:
                self.report(
                    {"ERROR"}, "No mesh found associated with the selected armature."
                )
                return {"CANCELLED"}
        else:
            mesh = active_obj
            armature = get_armature_from_modifiers(mesh)
            if not armature:
                self.report({"ERROR"}, "No armature found in mesh modifiers.")
                return {"CANCELLED"}

        mesh_name = mesh.name.split(".")[0]
        head_origin, light_direction = self.get_model_specific_objects(
            mesh, mesh_name)

        if not head_origin or not light_direction:
            self.report(
                {"ERROR"}, "Head Origin or Light Direction not found for this model."
            )
            return {"CANCELLED"}

        self.parent_objects(armature, head_origin, light_direction)

        head_bone = self.reset_head_driver(mesh_name, armature, head_origin)
        if not head_bone:
            self.report(
                {"WARNING"},
                "Head bone not found. Head Origin may not be properly positioned.",
            )

        pos_bone = self.reset_light_direction(armature, light_direction)
        if not pos_bone:
            self.report(
                {"WARNING"},
                "Position bone not found. Light Direction may not be properly positioned.",
            )

        self.report(
            {"INFO"}, "Head Origin and Light Direction set up successfully.")

        self.restore_initial_state(context, active_obj)

        return {"FINISHED"}

    def get_mesh_from_armature(self, armature):
        for obj in bpy.data.objects:
            if obj.type == "MESH" and get_armature_from_modifiers(obj) == armature:
                return obj
        return None

    def get_model_specific_objects(self, mesh, mesh_name):
        modifier = mesh.modifiers.get(f"Light Vectors {mesh_name}")
        if modifier and modifier.type == "NODES":
            light_direction = modifier.get("Input_3")
            head_origin = modifier.get("Input_4")
            if light_direction and head_origin:
                return head_origin, light_direction

        head_origin = None
        light_direction = None

        for obj in bpy.data.objects:
            if obj.name.startswith("Head Origin"):
                head_origin = obj
                break

        if head_origin:
            suffix = head_origin.name[len("Head Origin"):]
            light_direction = bpy.data.objects.get(f"Light Direction{suffix}")

        return head_origin, light_direction

    def parent_objects(self, armature, head_origin, light_direction):
        for obj in (head_origin, light_direction):
            if obj:
                obj.parent = armature
                obj.matrix_parent_inverse = armature.matrix_world.inverted()

    def reset_head_driver(self, mesh_name, armature, head_origin):
        head_bone_names = ["c_head.x", "Bip001Head"]
        head_bone = None

        for bone_name in head_bone_names:
            if bone_name in armature.data.bones:
                head_bone = bone_name
                break

        if not head_bone and armature.data.bones:
            head_bone = armature.data.bones[0].name

        if not head_bone:
            return None

        bone = armature.data.bones[head_bone]
        bone_world_pos = armature.matrix_world @ bone.head_local
        relative_position = Vector((0, 0, 0.2))
        head_origin.location = bone_world_pos + relative_position

        for const in head_origin.constraints:
            head_origin.constraints.remove(const)

        constraint = head_origin.constraints.new("CHILD_OF")
        constraint.target = armature
        constraint.subtarget = head_bone

        bpy.ops.object.mode_set(mode="OBJECT")
        bpy.ops.object.select_all(action="DESELECT")
        head_origin.select_set(True)
        bpy.context.view_layer.objects.active = head_origin

        context_override = {
            "object": head_origin,
            "active_object": head_origin,
            "selected_objects": [head_origin],
            "selected_editable_objects": [head_origin],
            "active_editable_object": head_origin,
            "constraint": constraint,
        }

        try:
            with bpy.context.temp_override(**context_override):
                bpy.ops.constraint.childof_set_inverse(
                    constraint=constraint.name, owner="OBJECT"
                )
        except Exception as e:
            self.report(
                {"WARNING"}, f"Failed to set constraint inverse: {str(e)}")
            try:
                constraint.inverse_matrix = (
                    armature.matrix_world.inverted() @ head_origin.matrix_world
                )
            except Exception as e2:
                self.report({"WARNING"}, f"Manual inverse failed: {str(e2)}")

        head_origin.select_set(False)
        return head_bone

    def reset_light_direction(self, armature, light_direction):
        pos_bone_names = ["c_pos", "Root"]
        pos_bone = None

        for bone_name in pos_bone_names:
            if bone_name in armature.data.bones:
                pos_bone = bone_name
                break

        if not pos_bone and armature.data.bones:
            pos_bone = armature.data.bones[0].name

        if not pos_bone:
            return None

        bone = armature.data.bones[pos_bone]
        bone_world_pos = armature.matrix_world @ bone.head_local
        light_direction.location = bone_world_pos
        light_direction.rotation_euler = (-1.5708, 0, 0)
        return pos_bone

    def restore_initial_state(self, context, active_obj):
        bpy.ops.object.mode_set(mode="OBJECT")
        bpy.ops.object.select_all(action="DESELECT")
        if active_obj:
            active_obj.select_set(True)
            context.view_layer.objects.active = active_obj


preserved_shape_keys = {"Pupil_Up", "Pupil_Down",
                        "Pupil_R", "Pupil_L", "Pupil_Scale"}


def delete_shape_key_drivers(mesh, preserved_shape_keys):
    if mesh.data.shape_keys:
        for key_block in mesh.data.shape_keys.key_blocks:
            if key_block.name not in preserved_shape_keys:
                key_block.driver_remove('value')


class WW_OT_CreateFacePanel(bpy.types.Operator):
    bl_idname = "shader.create_face_panel"
    bl_label = "Create Face Panel"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        if not context.active_object:
            return False
        obj = context.active_object
        if obj.type == 'MESH':
            for mod in obj.modifiers:
                if mod.type == 'ARMATURE' and mod.object:
                    return True
        elif obj.type == 'ARMATURE':
            return True
        return False

    def setup_create_panel_drivers(self, context, armature_obj, CharacterMesh):
        shape_key_mappings = {
            "Smile.L": {"shape_key": "E_Smile_L", "var_type": "LOC_Y"},
            "Smile.R": {"shape_key": "E_Smile_R", "var_type": "LOC_Y"},
            "Anger.L": {"shape_key": "E_Anger.L", "var_type": "LOC_Y"},
            "Sad.L": {"shape_key": "E_Sad.L", "var_type": "LOC_Y"},
            "Focus.L": {"shape_key": "E_Focus.L", "var_type": "LOC_Y"},
            "Insipid.L": {"shape_key": "E_Insipid.L", "var_type": "LOC_Y"},
            "Anger.R": {"shape_key": "E_Anger.R", "var_type": "LOC_Y"},
            "Sad.R": {"shape_key": "E_Sad.R", "var_type": "LOC_Y"},
            "Focus.R": {"shape_key": "E_Focus.R", "var_type": "LOC_Y"},
            "Insipid.R": {"shape_key": "E_Insipid.R", "var_type": "LOC_Y"},
            "B_Anger": {"shape_key": "B_Anger", "var_type": "LOC_Y"},
            "B_Happy": {"shape_key": "B_Happy", "var_type": "LOC_Y"},
            "B_Cheerful": {"shape_key": "B_Cheerful", "var_type": "LOC_Y"},
            "B_Sad": {"shape_key": "B_Sad", "var_type": "LOC_Y"},
            "B_Flat": {"shape_key": "B_Flat", "var_type": "LOC_Y"},
            "B_Inside_Add": {"shape_key": "B_Inside_Add", "var_type": "LOC_Y"},
            "EyeScale": {"shape_key": "E_Blephar", "var_type": "LOC_Y"}
        }
        mouth_mappings = {
            "Mouth.L": {
                "positive_shape": "M_Smile_L",
                "negative_shape": "M_Ennui_L",
                "multiplier": 50,
                "var_type": "LOC_Y"
            },
            "Mouth.R": {
                "positive_shape": "M_Smile_R",
                "negative_shape": "M_Ennui_R",
                "multiplier": 50,
                "var_type": "LOC_Y"
            }
        }
        mouth_x_mappings = {
            "Mouth.L": {
                "negative_shape": "P_M_Scale_Add.L",
                "positive_shape": "P_M_L_Add"
            },
            "Mouth.R": {
                "negative_shape": "P_M_R_Add",
                "positive_shape": "P_M_Scale_Add.R"
            }
        }
        for bone_name, mapping in shape_key_mappings.items():
            if bone_name not in armature_obj.pose.bones:
                continue
            bone = armature_obj.pose.bones[bone_name]
            if not CharacterMesh.data.shape_keys:
                continue
            shape_key = CharacterMesh.data.shape_keys.key_blocks.get(
                mapping["shape_key"])
            if not shape_key:
                continue
            driver = shape_key.driver_add('value').driver
            driver.type = 'SCRIPTED'
            var = driver.variables.new()
            var.name = 'bone_var'
            var.targets[0].id = armature_obj
            var.targets[0].data_path = (
                f'pose.bones["{bone.name}"].location.y' if mapping["var_type"] == "LOC_Y"
                else f'pose.bones["{bone.name}"].location.x'
            )
            driver.expression = "bone_var * 50"
        for bone_name, mapping in mouth_mappings.items():
            if bone_name not in armature_obj.pose.bones:
                continue
            bone = armature_obj.pose.bones[bone_name]
            multiplier = mapping["multiplier"]
            if mapping["positive_shape"]:
                shape_key = CharacterMesh.data.shape_keys.key_blocks.get(
                    mapping["positive_shape"])
                if shape_key:
                    driver = shape_key.driver_add('value').driver
                    driver.type = 'SCRIPTED'
                    var = driver.variables.new()
                    var.name = 'mouth_y'
                    var.targets[0].id = armature_obj
                    var.targets[0].data_path = f'pose.bones["{bone.name}"].location.y'
                    driver.expression = f'max(mouth_y * {multiplier}, 0)'
            if mapping["negative_shape"]:
                shape_key = CharacterMesh.data.shape_keys.key_blocks.get(
                    mapping["negative_shape"])
                if shape_key:
                    driver = shape_key.driver_add('value').driver
                    driver.type = 'SCRIPTED'
                    var = driver.variables.new()
                    var.name = 'mouth_y'
                    var.targets[0].id = armature_obj
                    var.targets[0].data_path = f'pose.bones["{bone.name}"].location.y'
                    driver.expression = f'max(-mouth_y * {multiplier}, 0)'
        for bone_name, mapping in mouth_x_mappings.items():
            if bone_name not in armature_obj.pose.bones:
                continue
            bone = armature_obj.pose.bones[bone_name]
            x_data_path = f'pose.bones["{bone.name}"].location.x'
            pos_shape = CharacterMesh.data.shape_keys.key_blocks.get(
                mapping["positive_shape"])
            if pos_shape:
                driver = pos_shape.driver_add('value').driver
                driver.type = 'SCRIPTED'
                var = driver.variables.new()
                var.name = 'x_pos'
                var.targets[0].id = armature_obj
                var.targets[0].data_path = x_data_path
                driver.expression = 'max(min(x_pos / 0.01, 1), 0)'
            neg_shape = CharacterMesh.data.shape_keys.key_blocks.get(
                mapping["negative_shape"])
            if neg_shape:
                driver = neg_shape.driver_add('value').driver
                driver.type = 'SCRIPTED'
                var = driver.variables.new()
                var.name = 'x_neg'
                var.targets[0].id = armature_obj
                var.targets[0].data_path = x_data_path
                driver.expression = 'max(min(-x_neg / 0.01, 1), 0)'
        eye_scale_mappings = {
            "EyeTracker": "E_Close",
            "Eye.L": "E_Close.L",
            "Eye.R": "E_Close.R",
            "EyeScale": "Pupil_Scale",
        }
        for bone_name, shape_key_name in eye_scale_mappings.items():
            if bone_name not in armature_obj.pose.bones:
                continue
            shape_key = CharacterMesh.data.shape_keys.key_blocks.get(
                shape_key_name)
            if not shape_key:
                continue
            driver = shape_key.driver_add('value').driver
            driver.type = 'SCRIPTED'
            var = driver.variables.new()
            var.name = 'scaleval'
            var.targets[0].id = armature_obj
            if bone_name == "EyeScale":
                var.targets[0].data_path = f'pose.bones["{bone_name}"].scale.x'
                driver.expression = '(1 - scaleval) * 2'
            else:
                var.targets[0].data_path = f'pose.bones["{bone_name}"].scale.y'
                driver.expression = '(1 - scaleval) * 2'
        bone_name = "EyeTracker"
        shape_key_name = "E_Stare"
        if bone_name in armature_obj.pose.bones:
            shape_key = CharacterMesh.data.shape_keys.key_blocks.get(
                shape_key_name)
            if shape_key:
                driver = shape_key.driver_add('value').driver
                driver.type = 'SCRIPTED'
                var = driver.variables.new()
                var.name = 'yscale'
                var.targets[0].id = armature_obj
                var.targets[0].data_path = f'pose.bones["{bone_name}"].scale.y'
                driver.expression = 'max(min((yscale - 1) * 2, 1), 0)'
        vowel_shapes = {
            "E": {"axis": "x", "direction": -1, "max_value": 0.02},
            "I": {"axis": "x", "direction": 1, "max_value": 0.02},
            "A": {"axis": "y", "direction": 1, "max_value": 0.02},
            "U": {"axis": "y", "direction": -1, "max_value": 0.02},
        }
        mouth_bone = armature_obj.pose.bones.get("Mouth")
        if mouth_bone:
            for shape_key_name, info in vowel_shapes.items():
                shape_key = CharacterMesh.data.shape_keys.key_blocks.get(
                    shape_key_name)
                if not shape_key:
                    continue
                driver = shape_key.driver_add('value').driver
                driver.type = 'SCRIPTED'
                var_main = driver.variables.new()
                var_main.name = 'coord'
                var_main.targets[0].id = armature_obj
                var_main.targets[0].data_path = f'pose.bones["Mouth"].location.{info["axis"]}'
                var_o = driver.variables.new()
                var_o.name = 'oval'
                var_o.targets[0].id_type = 'KEY'
                var_o.targets[0].id = CharacterMesh.data.shape_keys
                var_o.targets[0].data_path = 'key_blocks["O"].value'
                if shape_key_name in ["E", "I"]:
                    var_y = driver.variables.new()
                    var_y.name = 'yval'
                    var_y.targets[0].id = armature_obj
                    var_y.targets[0].data_path = 'pose.bones["Mouth"].location.y'
                    driver.expression = (
                        f"(1 - oval * 0.6) * "
                        f"(1 - min(abs(yval) / 0.02, 1)) * "
                        f"max(min(({info['direction']} * coord) / {info['max_value']}, 1), 0)"
                    )
                else:
                    driver.expression = (
                        f"(1 - oval * 0.6) * "
                        f"max(min(({info['direction']} * coord) / {info['max_value']}, 1), 0)"
                    )
            o_shape = CharacterMesh.data.shape_keys.key_blocks.get("O")
            if o_shape:
                driver = o_shape.driver_add('value').driver
                driver.type = 'SCRIPTED'
                for axis in ["x", "y", "z"]:
                    var = driver.variables.new()
                    var.name = f"s_{axis}"
                    var.targets[0].id = armature_obj
                    var.targets[0].data_path = f'pose.bones["Mouth"].scale.{axis}'
                driver.expression = (
                    "max(min(((abs(s_x) + abs(s_y) + abs(s_z)) / 3 - 1) / 0.5, 1), 0)"
                )
        shape_map = {
            "M_OpenSmall": "M_OpenSmall",
            "M_Laugh": "M_Laugh",
            "M_Scared": "M_Scared",
            "M_ScaredTooth": "M_ScaredTooth",
            "M_Anger": "M_Anger",
            "M_Trapezoid": "M_Trapezoid",
            "M_Nutcracker": "M_Nutcracker",
            "Aa": "Aa",
            "M_A": "M_A",
            "M_O": "M_O",
        }
        for bone_name, shape_name in shape_map.items():
            shape_key = CharacterMesh.data.shape_keys.key_blocks.get(
                shape_name)
            if not shape_key:
                continue
            driver = shape_key.driver_add('value').driver
            driver.type = 'SCRIPTED'
            var = driver.variables.new()
            var.name = 'yval'
            var.targets[0].id = armature_obj
            var.targets[0].data_path = f'pose.bones["{bone_name}"].location.y'
            driver.expression = "max(min(yval / 0.02, 1), 0)"
        bone_name = "Eyebrows"
        y_mappings = {
            "B_Up_Add": {"direction": 1, "shape_key": "B_Up_Add"},
            "B_Down_Add": {"direction": -1, "shape_key": "B_Down_Add"},
        }
        for key, data in y_mappings.items():
            shape_key = CharacterMesh.data.shape_keys.key_blocks.get(
                data["shape_key"])
            if not shape_key:
                continue
            driver = shape_key.driver_add('value').driver
            driver.type = 'SCRIPTED'
            var = driver.variables.new()
            var.name = 'yval'
            var.targets[0].id = armature_obj
            var.targets[0].data_path = f'pose.bones["{bone_name}"].location.y'
            dir = data["direction"]
            driver.expression = f"max(min(({dir} * yval) / 0.01, 1), 0)"
        z_mappings = {
            "B_AH_L": {"direction": -1, "angle_deg": 10},
            "B_AH_R": {"direction": 1, "angle_deg": 10}
        }
        for key, info in z_mappings.items():
            shape_key = CharacterMesh.data.shape_keys.key_blocks.get(key)
            if not shape_key:
                continue
            driver = shape_key.driver_add('value').driver
            driver.type = 'SCRIPTED'
            var = driver.variables.new()
            var.name = 'zrot'
            var.targets[0].id = armature_obj
            var.targets[0].data_path = f'pose.bones["{bone_name}"].rotation_euler.z'
            max_radians = math.radians(info["angle_deg"])
            direction = info["direction"]
            driver.expression = f"max(min(({direction} * zrot) / {max_radians:.5f}, 1), 0)"

    def execute(self, context):
        initial_active_object = context.active_object
        initial_selected_objects = context.selected_objects[:]
        obj = context.active_object
        if obj.type == 'MESH':
            for mod in obj.modifiers:
                if mod.type == 'ARMATURE' and mod.object:
                    armature_obj = mod.object
                    break
            else:
                self.report(
                    {'ERROR'}, "No armature modifier found on the selected mesh.")
                return {'CANCELLED'}
        elif obj.type == 'ARMATURE':
            armature_obj = obj
        else:
            self.report(
                {'ERROR'}, "Selected object is neither a mesh nor an armature.")
            return {'CANCELLED'}
        if not armature_obj.name.startswith("RIG-"):
            self.report(
                {'ERROR'}, "Please use the Rigify function for the armature to continue.")
            return {'CANCELLED'}
        CharacterMesh = None
        for obj in bpy.data.objects:
            if obj.type == 'MESH':
                for mod in obj.modifiers:
                    if mod.type == 'ARMATURE' and mod.object == armature_obj:
                        CharacterMesh = obj
                        break
                if CharacterMesh:
                    break
        if not CharacterMesh:
            self.report(
                {'ERROR'}, "No mesh found with an Armature modifier using the selected armature.")
            return {'CANCELLED'}
        try:
            if armature_obj.get('face_panel_created', False):
                delete_shape_key_drivers(CharacterMesh, preserved_shape_keys)
                self.setup_create_panel_drivers(
                    context, armature_obj, CharacterMesh)
                self.report(
                    {'INFO'}, "Drivers reset successfully for existing face panel.")
            else:
                def get_or_create_collection(name):
                    if name in bpy.data.collections:
                        return bpy.data.collections[name]
                    else:
                        coll = bpy.data.collections.new(name)
                        bpy.context.scene.collection.children.link(coll)
                        coll.hide_viewport = True
                        return coll
                custom_shapes_coll = get_or_create_collection("CustomShapes")
                bpy.context.view_layer.objects.active = armature_obj
                bpy.ops.object.mode_set(mode='EDIT')
                edit_bones = armature_obj.data.edit_bones
                eye_tracker_bone = edit_bones.get("EyeTracker")
                if not eye_tracker_bone:
                    raise Exception("Bone 'EyeTracker' not found.")
                eye_tracker_pos = eye_tracker_bone.head.copy()
                face_panel_root = edit_bones.new("FacePanelRoot")
                face_panel_root.head = eye_tracker_pos
                face_panel_root.tail = eye_tracker_pos + \
                    mathutils.Vector((0.0, 0.0, 0.02))
                face_panel_root.use_connect = False
                parent_bone = edit_bones.get("Bip001Head.001")
                if parent_bone:
                    face_panel_root.parent = parent_bone
                face_panel = edit_bones.new("FacePanel")
                face_panel.head = eye_tracker_pos
                face_panel.tail = eye_tracker_pos + \
                    mathutils.Vector((0.0, 0.0, 0.01))
                face_panel.use_connect = False
                face_panel.parent = face_panel_root
                eye_scale = edit_bones.new("EyeScale")
                eye_scale.head = face_panel.head - \
                    mathutils.Vector((0.0, 0.0, 0.01))
                eye_scale.tail = eye_scale.head + \
                    mathutils.Vector((0.0, 0.0, 0.01))
                eye_scale.use_connect = False
                eye_scale.parent = face_panel
                for bone_name in ["Eye.L", "Eye.R"]:
                    bone = edit_bones.get(bone_name)
                    if bone:
                        bone.parent = face_panel
                eye_tracker_bone.parent = face_panel_root
                bpy.ops.object.mode_set(mode='OBJECT')
                pose_bones = armature_obj.pose.bones
                if "FacePanel" in pose_bones and "EyeTracker" in pose_bones:
                    face_panel_pose = pose_bones["FacePanel"]
                    constraint = face_panel_pose.constraints.new(
                        type='COPY_LOCATION')
                    constraint.name = "FollowEyeTracker"
                    constraint.target = armature_obj
                    constraint.subtarget = "EyeTracker"
                bpy.ops.object.mode_set(mode='EDIT')

                def create_fan_bones(base_bone_name, custom_bone_names, side_suffix):
                    base_bone = edit_bones.get(base_bone_name)
                    if not base_bone:
                        raise Exception(
                            f"Base bone '{base_bone_name}' not found.")
                    fan_center = base_bone.head
                    radius = 0.035
                    bone_length = 0.02
                    num_bones = len(custom_bone_names)
                    arc_angle = math.radians(120)
                    angle_start = -arc_angle / 2
                    for i in range(num_bones):
                        angle = angle_start + i * (arc_angle / (num_bones - 1))
                        direction_multiplier = -1 if side_suffix == ".R" else 1
                        head_x = math.cos(angle) * radius * \
                            direction_multiplier
                        head_z = math.sin(angle) * radius
                        head = fan_center + \
                            mathutils.Vector((head_x, 0, head_z))
                        tail = head + \
                            (head - fan_center).normalized() * bone_length
                        bone_name = custom_bone_names[i].replace(
                            ".L", side_suffix)
                        fan_bone = edit_bones.new(bone_name)
                        fan_bone.head = head
                        fan_bone.tail = tail
                        fan_bone.parent = edit_bones["FacePanel"]
                        fan_bone.use_connect = False

                def adjust_bone_roll():
                    bone_rolls = {
                        "Smile.L": 30,
                        "Anger.L": 60,
                        "Sad.L": 90,
                        "Focus.L": 120,
                        "Insipid.L": 150,
                    }
                    for bone_name, roll_deg in bone_rolls.items():
                        bone = edit_bones.get(bone_name)
                        if bone:
                            bone.roll = math.radians(roll_deg)
                        bone_R = edit_bones.get(bone_name.replace(".L", ".R"))
                        if bone_R:
                            bone_R.roll = -math.radians(roll_deg)
                custom_bone_names_L = ["Insipid.L",
                                       "Focus.L", "Sad.L", "Anger.L", "Smile.L"]
                custom_bone_names_R = [name.replace(
                    ".L", ".R") for name in custom_bone_names_L]
                create_fan_bones("Eye.L", custom_bone_names_L, ".L")
                create_fan_bones("Eye.R", custom_bone_names_R, ".R")
                adjust_bone_roll()
                bpy.ops.object.mode_set(mode='OBJECT')
                bpy.ops.object.mode_set(mode='EDIT')
                edit_bones = bpy.context.object.data.edit_bones
                face_panel = edit_bones.get("FacePanel")
                if not face_panel:
                    raise Exception("FacePanel bone not found.")
                eyebrows_bone = edit_bones.new("Eyebrows")
                eyebrows_head = face_panel.head + \
                    mathutils.Vector((0, 0, 0.06))
                eyebrows_bone.head = eyebrows_head
                eyebrows_bone.tail = eyebrows_head + \
                    mathutils.Vector((0, 0, 0.01))
                eyebrows_bone.parent = face_panel
                eyebrows_bone.use_connect = False
                b_names = ["B_Anger", "B_Happy", "B_Cheerful",
                           "B_Sad", "B_Flat", "B_Inside_Add"]
                spacing = 0.015
                start_x = -spacing * (len(b_names) - 1) / 2
                y = eyebrows_head.y
                z = eyebrows_bone.tail.z
                for i, name in enumerate(b_names):
                    b = edit_bones.new(name)
                    head = mathutils.Vector((start_x + i * spacing, y, z))
                    tail = head + mathutils.Vector((0, 0, 0.02))
                    b.head = head
                    b.tail = tail
                    b.parent = eyebrows_bone
                    b.use_connect = False
                mouth_panel_bone = edit_bones.new("MouthPanel")
                mouth_panel_head = face_panel.head - \
                    mathutils.Vector((0, 0, 0.055))
                mouth_panel_bone.head = mouth_panel_head
                mouth_panel_bone.tail = mouth_panel_head + \
                    mathutils.Vector((0, 0, 0.01))
                mouth_panel_bone.parent = face_panel
                mouth_panel_bone.use_connect = False
                mouth_bone = edit_bones.new("Mouth")
                mouth_bone.head = mouth_panel_head
                mouth_bone.tail = mouth_bone.head + \
                    mathutils.Vector((0, 0, 0.02))
                mouth_bone.parent = mouth_panel_bone
                mouth_bone.use_connect = False
                offset_x = 0.045
                y = mouth_bone.head.y
                z = mouth_bone.head.z
                length = 0.02
                for side in [("Mouth.L", offset_x), ("Mouth.R", -offset_x)]:
                    name, x_offset = side
                    b = edit_bones.new(name)
                    head = mathutils.Vector(
                        (mouth_bone.head.x + x_offset, y, z))
                    tail = head + mathutils.Vector((0, 0, length))
                    b.head = head
                    b.tail = tail
                    b.parent = mouth_panel_bone
                    b.use_connect = False
                expressions = ["Aa", "M_OpenSmall", "M_Laugh", "M_Scared", "M_ScaredTooth",
                               "M_Anger", "M_Trapezoid", "M_Nutcracker", "M_O", "M_A"]
                num = len(expressions)
                spacing = 0.01
                total_width = (num - 1) * spacing
                start_x = mouth_panel_head.x - total_width / 2
                y = mouth_panel_head.y
                z = mouth_panel_head.z - 0.035
                for i, name in enumerate(expressions):
                    b = edit_bones.new(name)
                    head = mathutils.Vector((start_x + i * spacing, y, z))
                    tail = head - mathutils.Vector((0, 0, 0.02))
                    b.head = head
                    b.tail = tail
                    b.parent = mouth_panel_bone
                    b.use_connect = False
                bpy.ops.object.mode_set(mode='OBJECT')

                def create_outline(name, verts_2d):
                    full_name = f"Custom{name}"
                    if full_name in bpy.data.objects:
                        return bpy.data.objects[full_name]
                    mesh = bpy.data.meshes.new(full_name)
                    obj = bpy.data.objects.new(full_name, mesh)
                    custom_shapes_coll.objects.link(obj)
                    obj.hide_viewport = True
                    obj.hide_render = True
                    bm = bmesh.new()
                    verts = []
                    for x, y in verts_2d:
                        verts.append(bm.verts.new((x, y, 0)))
                    bm.verts.ensure_lookup_table()
                    for i in range(len(verts)):
                        bm.edges.new((verts[i], verts[(i + 1) % len(verts)]))
                    bm.to_mesh(mesh)
                    bm.free()
                    return obj

                def create_lines(name, line_pairs):
                    full_name = f"Custom{name}"
                    if full_name in bpy.data.objects:
                        return bpy.data.objects[full_name]
                    mesh = bpy.data.meshes.new(full_name)
                    obj = bpy.data.objects.new(full_name, mesh)
                    custom_shapes_coll.objects.link(obj)
                    obj.hide_viewport = True
                    obj.hide_render = True
                    bm = bmesh.new()
                    for (x1, y1), (x2, y2) in line_pairs:
                        v1 = bm.verts.new((x1, y1, 0))
                        v2 = bm.verts.new((x2, y2, 0))
                        bm.edges.new((v1, v2))
                    bm.to_mesh(mesh)
                    bm.free()
                    return obj
                triangle_points = [(0, 1), (-1, -1), (1, -1)]
                diamond_points = [(0, 1), (-1, 0), (0, -1), (1, 0)]
                square_points = [(-1, 1), (1, 1), (1, -1), (-1, -1)]
                plus_cross_lines = [[(-1, 0), (1, 0)], [(0, -1), (0, 1)]]
                create_outline("Triangle", triangle_points)
                create_outline("Diamond", diamond_points)
                create_outline("Square", square_points)
                create_lines("Cross", plus_cross_lines)
                bpy.ops.object.mode_set(mode='POSE')
                bone_shape_map = {
                    "Eyebrows": "CustomSquare",
                    "Mouth": "CustomDiamond",
                    "Mouth.L": "CustomDiamond",
                    "Mouth.R": "CustomDiamond",
                    "Smile.L": "CustomTriangle",
                    "Anger.L": "CustomTriangle",
                    "Sad.L": "CustomTriangle",
                    "Focus.L": "CustomTriangle",
                    "Insipid.L": "CustomTriangle",
                    "Smile.R": "CustomTriangle",
                    "Anger.R": "CustomTriangle",
                    "Sad.R": "CustomTriangle",
                    "Focus.R": "CustomTriangle",
                    "Insipid.R": "CustomTriangle",
                    "B_Anger": "CustomTriangle",
                    "B_Happy": "CustomTriangle",
                    "B_Cheerful": "CustomTriangle",
                    "B_Sad": "CustomTriangle",
                    "B_Flat": "CustomTriangle",
                    "B_Inside_Add": "CustomTriangle",
                    "Mouth": "WGT-rig_eyes",
                    "M_OpenSmall": "CustomTriangle",
                    "M_Laugh": "CustomTriangle",
                    "M_Scared": "CustomTriangle",
                    "M_ScaredTooth": "CustomTriangle",
                    "M_Anger": "CustomTriangle",
                    "M_Trapezoid": "CustomTriangle",
                    "M_Nutcracker": "CustomTriangle",
                    "MouthPanel": "CustomCross",
                    "EyeScale": "CustomSquare",
                    "Aa": "CustomTriangle",
                    "M_A": "CustomTriangle",
                    "M_O": "CustomTriangle",
                }
                for bone_name, shape_name in bone_shape_map.items():
                    pbone = armature_obj.pose.bones.get(bone_name)
                    shape_obj = bpy.data.objects.get(shape_name)
                    if not pbone or not shape_obj:
                        continue
                    pbone.custom_shape = shape_obj
                    if bone_name == "EyeScale":
                        pbone.custom_shape_scale_xyz = (1.0, 0.1, 1.0)
                    elif shape_name == "CustomTriangle":
                        pbone.custom_shape_scale_xyz = (0.2, 0.2, 1.0)
                    elif shape_name == "CustomSquare":
                        pbone.custom_shape_scale_xyz = (4.5, 0.2, 1.0)
                    elif shape_name == "CustomDiamond":
                        if bone_name in ["Mouth.L", "Mouth.R"]:
                            pbone.custom_shape_scale_xyz = (0.2, 0.2, 1.0)
                        else:
                            pbone.custom_shape_scale_xyz = (0.5, 0.5, 1.0)
                    elif shape_name == "WGT-rig_eyes":
                        pbone.custom_shape_scale_xyz = (2.0, 2.0, 1.0)
                    elif shape_name == "CustomCross":
                        pbone.custom_shape_scale_xyz = (4.0, 2.5, 1.0)
                bpy.ops.object.mode_set(mode='OBJECT')
                bpy.ops.object.mode_set(mode='POSE')
                for pbone in armature_obj.pose.bones:
                    shape = pbone.custom_shape
                    pbone.lock_location = (False, False, False)
                    pbone.lock_rotation = (False, False, False)
                    pbone.lock_scale = (False, False, False)
                    for con in list(pbone.constraints):
                        if con.type in {'LIMIT_LOCATION', 'LIMIT_ROTATION', 'LIMIT_SCALE'}:
                            pbone.constraints.remove(con)
                    shape_name = shape.name if shape else ""
                    if shape_name == "CustomTriangle":
                        pbone.lock_location = (True, False, True)
                        pbone.lock_rotation = (True, True, True)
                        pbone.lock_scale = (True, True, True)
                        con = pbone.constraints.new(type='LIMIT_LOCATION')
                        con.use_min_y = True
                        con.use_max_y = True
                        con.min_y = 0.0
                        con.max_y = 0.02
                        con.owner_space = 'LOCAL'
                        con.use_transform_limit = True
                    elif pbone.name == "EyeScale":
                        pbone.lock_location = (True, False, True)
                        pbone.lock_rotation = (True, True, True)
                        pbone.lock_scale = (False, True, True)
                        con_loc = pbone.constraints.new(type='LIMIT_LOCATION')
                        con_loc.use_min_y = con_loc.use_max_y = True
                        con_loc.min_y = 0.0
                        con_loc.max_y = 0.01
                        con_loc.owner_space = 'LOCAL'
                        con_loc.use_transform_limit = True
                        con_scale = pbone.constraints.new(type='LIMIT_SCALE')
                        con_scale.use_min_x = con_scale.use_max_x = True
                        con_scale.min_x = 0.5
                        con_scale.max_x = 1
                        con_scale.owner_space = 'LOCAL'
                        con_scale.use_transform_limit = True
                    elif shape_name == "CustomSquare":
                        pbone.lock_location = (True, False, True)
                        pbone.lock_rotation = (True, True, False)
                        pbone.lock_scale = (True, True, True)
                        pbone.rotation_mode = 'XYZ'
                        con_loc = pbone.constraints.new(type='LIMIT_LOCATION')
                        con_loc.use_min_y = True
                        con_loc.use_max_y = True
                        con_loc.min_y = -0.01
                        con_loc.max_y = 0.01
                        con_loc.owner_space = 'LOCAL'
                        con_loc.use_transform_limit = True
                        con_rot = pbone.constraints.new(type='LIMIT_ROTATION')
                        con_rot.use_limit_z = True
                        con_rot.min_z = -math.radians(10)
                        con_rot.max_z = math.radians(10)
                        con_rot.owner_space = 'LOCAL'
                        con_rot.use_transform_limit = True
                    elif pbone.name == "Mouth":
                        pbone.lock_location = (False, False, True)
                        pbone.lock_rotation = (True, True, True)
                        con = pbone.constraints.new(type='LIMIT_LOCATION')
                        con.use_min_x = con.use_max_x = True
                        con.use_min_y = con.use_max_y = True
                        con.min_x = -0.02
                        con.max_x = 0.02
                        con.min_y = -0.02
                        con.max_y = 0.02
                        con.owner_space = 'LOCAL'
                        con.use_transform_limit = True
                        con_scale = pbone.constraints.new(type='LIMIT_SCALE')
                        con_scale.use_min_x = con_scale.use_max_x = True
                        con_scale.use_min_y = con_scale.use_max_y = True
                        con_scale.use_min_z = con_scale.use_max_z = True
                        con_scale.min_x = con_scale.min_y = con_scale.min_z = 1.0
                        con_scale.max_x = con_scale.max_y = con_scale.max_z = 1.5
                        con_scale.owner_space = 'LOCAL'
                        con_scale.use_transform_limit = True
                    elif pbone.name in {"Mouth.L", "Mouth.R"}:
                        pbone.lock_location = (False, False, True)
                        pbone.lock_rotation = (True, True, True)
                        pbone.lock_scale = (True, True, True)
                        con = pbone.constraints.new(type='LIMIT_LOCATION')
                        con.use_min_x = con.use_max_x = True
                        con.use_min_y = con.use_max_y = True
                        con.min_x = -0.01
                        con.max_x = 0.01
                        con.min_y = -0.01
                        con.max_y = 0.01
                        con.owner_space = 'LOCAL'
                        con.use_transform_limit = True
                    elif pbone.name == "EyeTracker":
                        pbone.lock_rotation = (True, True, True)
                        pbone.lock_scale = (True, False, True)
                        pbone.lock_location[2] = True
                        con = pbone.constraints.new(type='LIMIT_SCALE')
                        con.use_min_y = True
                        con.use_max_y = True
                        con.min_y = 0.5
                        con.max_y = 1.5
                        con.owner_space = 'LOCAL'
                        con.use_transform_limit = True
                    elif pbone.name in {"Eye.L", "Eye.R"}:
                        pbone.lock_rotation = (True, True, True)
                        pbone.lock_scale = (True, False, True)
                        pbone.lock_location[2] = True
                        con = pbone.constraints.new(type='LIMIT_SCALE')
                        con.use_min_y = True
                        con.use_max_y = True
                        con.min_y = 0.5
                        con.max_y = 1.0
                        con.owner_space = 'LOCAL'
                        con.use_transform_limit = True
                bpy.ops.object.mode_set(mode='OBJECT')
                bpy.ops.object.select_all(action='DESELECT')
                CharacterMesh.select_set(True)
                bpy.context.view_layer.objects.active = CharacterMesh
                obj = CharacterMesh
                shape_keys_to_split = [
                    "E_Close", "E_Anger", "E_Sad", "E_Focus", "E_Insipid", "P_M_Scale_Add"]
                if obj.data.shape_keys:
                    keys = obj.data.shape_keys.key_blocks
                    basis = obj.data.shape_keys.reference_key
                    for source_name in shape_keys_to_split:
                        if source_name not in keys:
                            continue
                        source_key = keys[source_name]
                        obj.active_shape_key_index = list(
                            keys).index(source_key)
                        bpy.ops.object.shape_key_add(from_mix=False)
                        key_L = obj.data.shape_keys.key_blocks[-1]
                        key_L.name = f"{source_name}.L"
                        bpy.ops.object.shape_key_add(from_mix=False)
                        key_R = obj.data.shape_keys.key_blocks[-1]
                        key_R.name = f"{source_name}.R"
                        for i, vert in enumerate(basis.data):
                            base_co = vert.co
                            delta = source_key.data[i].co - base_co
                            if base_co.x >= 0:
                                key_L.data[i].co = base_co + delta
                                key_R.data[i].co = base_co
                            else:
                                key_R.data[i].co = base_co + delta
                                key_L.data[i].co = base_co
                CharacterMesh.select_set(False)
                bpy.context.view_layer.objects.active = armature_obj
                armature_obj.select_set(True)
                delete_shape_key_drivers(CharacterMesh, preserved_shape_keys)
                self.setup_create_panel_drivers(
                    context, armature_obj, CharacterMesh)
                bpy.ops.object.mode_set(mode='POSE')
                if 'FacePanel' not in armature_obj.data.collections:
                    armature_obj.data.collections.new(name='FacePanel')
                theme_bones = {
                    "THEME01": [
                        "MouthPanel", "Mouth", "Eyebrows",
                        "B_Anger", "B_Happy", "B_Cheerful", "B_Sad", "B_Flat", "B_Inside_Add"
                    ],
                    "THEME09": [
                        "EyeScale", "Eye.L", "Eye.R",
                        "Smile.L", "Anger.L", "Sad.L", "Focus.L", "Insipid.L",
                        "Smile.R", "Anger.R", "Sad.R", "Focus.R", "Insipid.R"
                    ],
                    "THEME03": [
                        "Mouth.R", "Mouth.L", "M_OpenSmall", "M_Laugh",
                        "M_Scared", "M_ScaredTooth", "M_Anger", "M_Trapezoid", "M_Nutcracker", "Aa", "M_A", "M_O"
                    ],
                }
                exclude_from_facepanel = {"Eye.L", "Eye.R"}
                for theme_name, bone_names in theme_bones.items():
                    for bone_name in bone_names:
                        pbone = armature_obj.pose.bones.get(bone_name)
                        if pbone:
                            pbone.color.palette = theme_name
                            if bone_name not in exclude_from_facepanel:
                                pbone.bone.select = True
                facepanel_index = armature_obj.data.collections.find(
                    'FacePanel')
                bpy.ops.armature.move_to_collection(
                    collection_index=facepanel_index)
                bpy.ops.pose.select_all(action='DESELECT')
                for bone_name in ["FacePanel", "FacePanelRoot"]:
                    pbone = armature_obj.pose.bones.get(bone_name)
                    if pbone:
                        pbone.bone.select = True
                if 'Others' not in armature_obj.data.collections:
                    armature_obj.data.collections.new(name='Others')
                others_index = armature_obj.data.collections.find('Others')
                bpy.ops.armature.move_to_collection(
                    collection_index=others_index)
                bpy.ops.pose.select_all(action='DESELECT')
                bpy.ops.object.mode_set(mode='OBJECT')
                bone = armature_obj.data.bones.get("MouthPanel")
                if bone:
                    bone.hide_select = True
                armature_obj.data.collections_all["FacePanel"].is_visible = True
                bpy.ops.object.select_all(action='DESELECT')
                view_layer = bpy.context.view_layer
                collection = bpy.context.scene.collection
                mesh_names_to_delete = [
                    "CustomTriangle", "CustomSquare", "CustomDiamond", "CustomCross"]
                for name in mesh_names_to_delete:
                    obj = bpy.data.objects.get(name)
                    if obj and obj.type == 'MESH':
                        if obj.name not in view_layer.objects:
                            collection.objects.link(obj)
                        obj.select_set(True)
                        view_layer.objects.active = obj
                        bpy.ops.object.delete()
                armature_obj['face_panel_created'] = True
                self.report(
                    {'INFO'}, "Face panel created and drivers set up successfully.")
            bpy.context.view_layer.objects.active = initial_active_object
            for obj in bpy.data.objects:
                obj.select_set(obj in initial_selected_objects)
            return {'FINISHED'}
        except Exception as e:
            self.report({'ERROR'}, f"Failed to process face panel: {str(e)}")
            bpy.context.view_layer.objects.active = initial_active_object
            for obj in bpy.data.objects:
                obj.select_set(obj in initial_selected_objects)
            return {'CANCELLED'}


class WW_OT_ImportFacePanel(Operator, ImportHelper):
    bl_idname = "shader.import_face_panel"
    bl_label = "Import Face Panel"
    filename_ext = ".blend"
    filter_glob: StringProperty(
        default="*.blend",
        options={"HIDDEN"},
    )

    @classmethod
    def poll(cls, context):
        active_obj = context.active_object
        if not active_obj or active_obj.type not in {"MESH", "ARMATURE"}:
            return False
        if active_obj.type == "ARMATURE":
            return True
        return get_armature_from_modifiers(active_obj) is not None

    def invoke(self, context, event):
        if hasattr(context.scene, "face_panel_file_path") and os.path.exists(context.scene.face_panel_file_path):
            self.filepath = context.scene.face_panel_file_path
            if self.is_valid_blend_file(self.filepath):
                return self.execute(context)
        return ImportHelper.invoke(self, context, event)

    def is_valid_blend_file(self, filepath):
        try:
            with bpy.data.libraries.load(filepath, link=False) as (src_data, _):
                return "Face panel" in src_data.collections
        except Exception as e:
            logger.error(f"Error checking blend file: {str(e)}")
            return False

    def execute(self, context):
        active_obj = context.active_object
        selected_obj = context.active_object
        if not self.validate_selection(context, selected_obj, active_obj):
            return {"CANCELLED"}
        if selected_obj.type == "ARMATURE":
            armature = selected_obj
            mesh = self.get_mesh_from_armature(armature)
            if not mesh:
                self.report(
                    {"ERROR"}, "No mesh found associated with the selected armature.")
                self.restore_initial_state(context, active_obj)
                return {"CANCELLED"}
        else:
            mesh = selected_obj
            armature = get_armature_from_modifiers(mesh)
        if not self.validate_mesh_and_armature(context, mesh, armature, active_obj):
            return {"CANCELLED"}
        if "rig" not in armature.name.lower():
            self.report(
                {"ERROR"}, "Please use the Rigify function for the armature to continue.")
            self.restore_initial_state(context, active_obj)
            return {"CANCELLED"}
        if mesh.get("face_panel_assigned", False):
            panel_armature_name = mesh.get("face_panel_armature")
            if panel_armature_name:
                panel_armature = bpy.data.objects.get(panel_armature_name)
                if panel_armature:
                    delete_shape_key_drivers(mesh, preserved_shape_keys)
                    self.setup_drivers(context, mesh, panel_armature)
                    self.report(
                        {'INFO'}, "Drivers reset successfully for existing face panel.")
                    self.restore_initial_state(context, active_obj)
                    return {'FINISHED'}
                else:
                    self.report({'ERROR'}, "Face panel armature not found.")
                    self.restore_initial_state(context, active_obj)
                    return {'CANCELLED'}
            else:
                self.report(
                    {'ERROR'}, "Face panel assigned but armature not specified.")
                self.restore_initial_state(context, active_obj)
                return {'CANCELLED'}
        armature_was_hidden = armature.hide_get()
        if armature_was_hidden:
            armature.hide_set(False)
        bpy.ops.object.mode_set(mode="OBJECT")
        head_pos = self.get_bone_world_position(armature, "c_head.x")
        if not head_pos:
            head_pos = self.get_bone_world_position(armature, "Bip001Head")
            if not head_pos:
                self.report(
                    {"ERROR"}, "Bones 'c_head.x' or 'Bip001Head' not found. Face panel import cancelled.")
                if armature_was_hidden:
                    armature.hide_set(True)
                self.restore_initial_state(context, active_obj)
                return {"CANCELLED"}
        face_panel, panel_armature = self.import_collection(context)
        if not face_panel or not panel_armature:
            if armature_was_hidden:
                armature.hide_set(True)
            self.restore_initial_state(context, active_obj)
            return {"CANCELLED"}
        self.position_panel(face_panel, armature, head_pos)
        child_of = face_panel.constraints.new("CHILD_OF")
        child_of.target = armature
        child_of.subtarget = "c_head.x" if "c_head.x" in armature.pose.bones else "Bip001Head"
        bpy.ops.object.mode_set(mode="OBJECT")
        context.view_layer.objects.active = face_panel
        bpy.ops.constraint.childof_set_inverse(
            constraint=child_of.name, owner="OBJECT")
        mesh["face_panel_armature"] = panel_armature.name
        delete_shape_key_drivers(mesh, preserved_shape_keys)
        self.setup_drivers(context, mesh, panel_armature)
        if armature_was_hidden:
            armature.hide_set(True)
        mesh["face_panel_assigned"] = True
        self.report(
            {"INFO"}, "Face panel imported and drivers set up successfully.")
        self.restore_initial_state(context, active_obj)
        return {"FINISHED"}

    def validate_selection(self, context, selected_obj, active_obj):
        if not selected_obj:
            self.report(
                {"ERROR"}, "Please select a mesh or armature to import the face panel.")
            self.restore_initial_state(context, active_obj)
            return False
        if selected_obj.type not in {"MESH", "ARMATURE"}:
            self.report(
                {"ERROR"}, "Selected object must be a mesh or armature.")
            self.restore_initial_state(context, active_obj)
            return False
        return True

    def validate_mesh_and_armature(self, context, mesh, armature, active_obj):
        if not mesh.data.shape_keys:
            self.report(
                {"ERROR"}, "Mesh has no shape keys. Face panel requires a mesh with shape keys.")
            self.restore_initial_state(context, active_obj)
            return False
        if not armature:
            self.report(
                {"ERROR"}, "No armature found in mesh modifiers. Face panel requires an armature.")
            self.restore_initial_state(context, active_obj)
            return False
        return True

    def restore_initial_state(self, context, active_obj):
        bpy.ops.object.mode_set(mode="OBJECT")
        bpy.ops.object.select_all(action="DESELECT")
        if active_obj:
            active_obj.select_set(True)
            context.view_layer.objects.active = active_obj

    def import_collection(self, context):
        collection_name = "Face panel"
        filepath = self.filepath
        if not filepath or not os.path.exists(filepath):
            self.report(
                {"ERROR"}, "Please select a valid .blend file containing a face panel.")
            return None, None
        with bpy.data.libraries.load(filepath, link=False) as (src_data, tgt_data):
            if collection_name in src_data.collections:
                tgt_data.collections = [collection_name]
            else:
                self.report(
                    {"ERROR"}, "Face panel not found in the selected .blend file.")
                return None, None
        imported_collection = next(
            (coll for coll in tgt_data.collections if coll), None)
        if not imported_collection:
            self.report({"ERROR"}, "Failed to import face panel collection.")
            return None, None
        bpy.context.scene.collection.children.link(imported_collection)
        face_panel = next(
            (obj for obj in imported_collection.objects if obj.name.startswith("Face Pannel")), None)
        if not face_panel:
            self.report(
                {"ERROR"}, "Face panel object not found in the imported collection.")
            return None, None
        bpy.context.view_layer.objects.active = face_panel
        face_panel.select_set(True)
        face_panel.lock_scale = [False, False, False]
        face_panel.scale = (0.2, 0.2, 0.2)
        face_panel.lock_scale = [True, True, True]
        panel_armature = next(
            (obj for obj in imported_collection.objects if obj.type == "ARMATURE"), None)
        if not panel_armature:
            self.report(
                {"ERROR"}, "Armature missing in face panel collection.")
            return None, None
        context.scene.face_panel_file_path = filepath
        return face_panel, panel_armature

    def get_armature_from_modifiers(self, mesh):
        for modifier in mesh.modifiers:
            if modifier.type == "ARMATURE" and modifier.object:
                return modifier.object
        return None

    def get_mesh_from_armature(self, armature):
        for obj in bpy.data.objects:
            if obj.type == "MESH" and self.get_armature_from_modifiers(obj) == armature:
                return obj
        return None

    def position_panel(self, panel, armature, head_pos):
        y = 0.0
        x = head_pos.x + 0.1
        z = head_pos.z - 0.1
        bpy.ops.object.mode_set(mode="OBJECT")
        bpy.context.view_layer.objects.active = panel
        panel.select_set(True)
        panel.location = (x, y, z)

    def get_bone_world_position(self, armature, bone_name):
        bpy.ops.object.mode_set(mode="OBJECT")
        bpy.context.view_layer.objects.active = armature
        armature.select_set(True)
        bpy.ops.object.mode_set(mode="POSE")
        bone = armature.pose.bones.get(bone_name)
        if bone:
            pos = (armature.matrix_world @ bone.matrix).to_translation()
            bpy.ops.object.mode_set(mode="OBJECT")
            return pos
        bpy.ops.object.mode_set(mode="OBJECT")
        return None

    def setup_drivers(self, context, mesh, armature):
        if not armature:
            return
        bpy.context.view_layer.objects.active = mesh
        driver_configs = [
            ("Aa", "m.A", "bone * 5", "LOC_X"),
            ("A", "m.AA", "bone * 5", "LOC_X"),
            ("E", "m.E", "bone * 5", "LOC_X"),
            ("I", "m.I", "bone * 5", "LOC_X"),
            ("O", "m.O", "bone * 5", "LOC_X"),
            ("U", "m.U", "bone * 5", "LOC_X"),
            ("P_M_Up_Add", "fp.m.pos.sel", "bone * 5", "LOC_Y"),
            ("P_M_Down_Add", "fp.m.pos.sel", "bone * -5", "LOC_Y"),
            ("P_M_RMove_Add", "fp.m.pos.sel", "bone * -5", "LOC_X"),
            ("P_M_LMove_Add", "fp.m.pos.sel", "bone * 5", "LOC_X"),
            ("P_M_L_Add", "lip.cor.pos.sel.r", "bone * 5", "LOC_X"),
            ("P_M_R_Add", "lip.cor.pos.sel.l", "bone * -5", "LOC_X"),
            ("M_Smile_L", "lip.cor.pos.sel.r", "bone * 5", "LOC_Y"),
            ("M_Smile_R", "lip.cor.pos.sel.l", "bone * 5", "LOC_Y"),
            ("M_Ennui_L", "lip.cor.pos.sel.r", "bone * -5", "LOC_Y"),
            ("M_Ennui_R", "lip.cor.pos.sel.l", "bone * -5", "LOC_Y"),
            ("M_Laugh", "x1", "bone * 5", "LOC_X"),
            ("M_Scared", "x2", "bone * 5", "LOC_X"),
            ("M_ScaredTooth", "x3", "bone * 5", "LOC_X"),
            ("M_Anger", "x4", "bone * 5", "LOC_X"),
            ("M_Nutcracker", "x5", "bone * 5", "LOC_X"),
            ("M_O", "x6", "bone * 5", "LOC_X"),
            ("B_AH_R", "doubt.1", "bone * 5", "LOC_X"),
            ("B_AH_L", "doubt.2", "bone * 5", "LOC_X"),
            ("B_Cheerful", "b.happy", "bone * 5", "LOC_X"),
            ("B_Flat", "b.flat", "bone * 5", "LOC_X"),
            ("B_Inside_Add", "b.close", "bone * 5", "LOC_X"),
            ("B_Anger", "fp.brow.sel", "bone * -5", "LOC_X"),
            ("B_Sad", "fp.brow.sel", "bone * 5", "LOC_X"),
            ("B_Up_Add", "fp.brow.sel", "bone * 5", "LOC_Y"),
            ("B_Down_Add", "fp.brow.sel", "bone * -5", "LOC_Y"),
            ("E_Insipid", "e.ji", "bone * 5", "LOC_X"),
            ("E_Blephar", "e.lowlid", "bone * 5", "LOC_X"),
            ("E_Focus", "e.focus", "bone * 5", "LOC_X"),
            ("E_Stare", "e.wide", "bone * 5", "LOC_X"),
            ("E_Smile_R", "e.wink.up.r", "bone * 5", "LOC_X"),
            ("E_Smile_L", "e.wink.up.l", "bone * 5", "LOC_X"),
            ("E_Anger", "eye.pos", "bone * -5", "LOC_X"),
            ("E_Sad", "eye.pos", "bone * 5", "LOC_X"),
            ("E_Close", "eye.pos", "bone * -5", "LOC_Y"),
        ]
        dual_driver_configs = [
            ("E_Smile_L", "eye.pos", "e.wink.up.l",
             "max(bone_001 * 5, bone * 5)", "LOC_Y"),
            ("E_Smile_R", "eye.pos", "e.wink.up.r",
             "max(bone_001 * 5, bone * 5)", "LOC_Y"),
        ]
        try:
            for shape_key, bone_name, expression, transform_type in driver_configs:
                self.add_driver(mesh, armature, shape_key,
                                bone_name, expression, transform_type)
            for shape_key, bone1, bone2, expression, transform_type in dual_driver_configs:
                self.add_dual_driver(
                    mesh, armature, shape_key, bone1, bone2, expression, transform_type)
            context.evaluated_depsgraph_get().update()
        except Exception as e:
            logger.error(f"Error setting up face panel drivers: {str(e)}")

    def add_driver(self, mesh, armature, shape_key, bone_name, expression, transform_type):
        shape_key_block = mesh.data.shape_keys.key_blocks.get(shape_key)
        if not shape_key_block or (hasattr(shape_key_block, "driver") and shape_key_block.driver):
            return
        driver = shape_key_block.driver_add("value").driver
        variable = driver.variables.new()
        variable.name = "bone"
        variable.type = "TRANSFORMS"
        variable.targets[0].id = armature
        variable.targets[0].bone_target = bone_name
        variable.targets[0].transform_space = "LOCAL_SPACE"
        variable.targets[0].transform_type = transform_type
        driver.type = "SCRIPTED"
        driver.expression = expression

    def add_dual_driver(self, mesh, armature, shape_key, bone1, bone2, expression, transform_type):
        shape_key_block = mesh.data.shape_keys.key_blocks.get(shape_key)
        if not shape_key_block or (hasattr(shape_key_block, "driver") and shape_key_block.driver):
            return
        driver = shape_key_block.driver_add("value").driver
        var1 = driver.variables.new()
        var1.name = "bone_001"
        var1.type = "TRANSFORMS"
        var1.targets[0].id = armature
        var1.targets[0].bone_target = bone1
        var1.targets[0].transform_space = "LOCAL_SPACE"
        var1.targets[0].transform_type = transform_type
        var2 = driver.variables.new()
        var2.name = "bone"
        var2.type = "TRANSFORMS"
        var2.targets[0].id = armature
        var2.targets[0].bone_target = bone2
        var2.targets[0].transform_space = "LOCAL_SPACE"
        var2.targets[0].transform_type = transform_type
        driver.type = "SCRIPTED"
        driver.expression = expression


class WW_OT_SetPerformanceMode(Operator):
    bl_idname = "shader.set_performance"
    bl_label = "Set Performance Mode"
    bl_description = "Configure render settings for better performance"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        context.scene.eevee.taa_samples = 1
        context.scene.eevee.taa_render_samples = 64
        context.scene.eevee.use_gtao = False
        context.scene.eevee.use_bloom = False
        context.scene.eevee.use_ssr = True
        context.scene.eevee.use_ssr_refraction = True
        context.scene.view_settings.view_transform = "Standard"
        self.report(
            {"INFO"}, "Performance mode enabled. Viewport settings optimized for speed."
        )
        logger.info("Set performance mode")
        return {"FINISHED"}


class WW_OT_SetQualityMode(Operator):
    bl_idname = "shader.set_quality"
    bl_label = "Set Quality Mode"
    bl_description = "Configure render settings for better visual quality"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        context.scene.eevee.taa_samples = 16
        context.scene.eevee.taa_render_samples = 64
        context.scene.eevee.use_gtao = True
        context.scene.eevee.use_bloom = True
        context.scene.eevee.bloom_threshold = 0.5
        context.scene.eevee.use_ssr = True
        context.scene.eevee.use_ssr_refraction = True
        context.scene.view_settings.view_transform = "Standard"
        self.report(
            {"INFO"},
            "Quality mode enabled. Viewport settings optimized for visual quality.",
        )
        logger.info("Set quality mode")
        return {"FINISHED"}


class WW_OT_SetLightMode(Operator):
    bl_idname = "shader.set_light_mode"
    bl_label = "Set Light Mode"
    bl_description = "Set the lighting mode for character materials"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        value = context.scene.light_mode_value
        shadow_value = context.scene.shadow_position

        if not (0 <= value <= 6):
            self.report({"ERROR"}, "Light mode value must be between 0 and 6.")
            return {"CANCELLED"}

        if not (0.0 <= shadow_value <= 2.0):
            self.report(
                {"ERROR"}, "Shadow position must be between 0.0 and 2.0.")
            return {"CANCELLED"}

        updated = self.update_light_nodes(context, value, shadow_value)

        if updated:
            self.report(
                {"INFO"}, f"Light mode set to '{self.get_mode_name(value)}'.")
            return {"FINISHED"}
        else:
            self.report(
                {"WARNING"},
                "Global Material Properties node group not found. Light mode could not be set.",
            )
            return {"CANCELLED"}

    def update_light_nodes(self, context, value, shadow_value):
        updated = False
        if node_group := bpy.data.node_groups.get("Global Material Properties"):
            for node in node_group.nodes:
                if (
                    node.type == "GROUP"
                    and node.node_tree
                    and node.node_tree.name == "Color Palette"
                ):
                    for input in node.inputs:
                        if input.type == "VALUE" and input.name == "Value":
                            input.default_value = float(value)
                            updated = True
                if node.name == "Global Properties" and node.type == "GROUP_OUTPUT":
                    if "Shadow Position" in node.inputs:
                        node.inputs["Shadow Position"].default_value = shadow_value
                        updated = True
            if updated:
                logger.info(
                    f"Updated light mode to {value} and shadow position to {shadow_value}"
                )
        return updated

    def get_mode_name(self, value: int):
        return LIGHT_MODES.get(value, "Unknown")


class WW_OT_ToggleTexMode(Operator):
    bl_idname = "shader.toggle_texture_mode"
    bl_label = "Toggle Texture Mode"
    bl_description = "Switch between Default and Version texture priority modes"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        if not context.active_object or context.active_object.type != "MESH":
            self.report(
                {"ERROR"}, "Please select a mesh to toggle texture mode.")
            return {"CANCELLED"}

        mesh_name = context.active_object.name.split(".")[0]
        data = get_mesh_data(context, mesh_name)
        data.tex_mode = not data.tex_mode

        if not os.path.exists(context.scene.tex_dir):
            self.report({"WARNING"}, "Texture directory not set or invalid.")
            return {"CANCELLED"}

        textures = [
            type("File", (), {"name": f})() for f in data.textures.split(",") if f
        ]
        if not textures:
            self.report({"WARNING"}, "No textures found for this mesh.")
            return {"CANCELLED"}

        self.reassign_textures(context, textures, data.tex_mode)
        mode_name = "Default" if data.tex_mode else "Version"
        self.report({"INFO"}, f"Texture mode switched to {mode_name}.")
        logger.info(f"Toggled texture mode to {mode_name} for {mesh_name}")
        return {"FINISHED"}

    def reassign_textures(self, context, textures: List[Any], tex_mode: bool):
        assigned_count = 0
        for slot in context.active_object.material_slots:
            if not slot.material or not (
                match := re.search(
                    r"WW - ([A-Za-z]+)(_?\d+|(?:_[^_]+)*)?", slot.material.name
                )
            ):
                continue

            base, version = match.group(1), match.group(2) or ""
            original_name = next(
                (
                    s.material.name
                    for s in context.active_object.material_slots
                    if s.material
                    and re.match(rf"MI_.*?{base}{version}$", s.material.name)
                ),
                None,
            )
            material_info = MaterialDetails(base, version, original_name)
            mat_tex_data = MaterialTextureData(
                slot.material,
                material_info,
                TEXTURE_TYPE_MAPPINGS,
                textures,
                context.scene.tex_dir,
                tex_mode,
            )
            apply_textures(mat_tex_data)
            assigned_count += 1

        logger.info(f"Reassigned textures to {assigned_count} materials")


class WW_OT_ToggleOutlines(Operator):
    bl_idname = "shader.toggle_outlines"
    bl_label = "Toggle Outlines"
    bl_description = "Toggle visibility of character outline effects"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        context.scene.outlines_enabled = not context.scene.outlines_enabled
        toggled_count = 0
        for obj in (o for o in bpy.data.objects if o.type == "MESH"):
            for modifier in obj.modifiers:
                if "outlines" in modifier.name.lower():
                    if modifier.type == "NODES":
                        modifier.show_viewport = context.scene.outlines_enabled
                        toggled_count += 1

        state = "on" if context.scene.outlines_enabled else "off"
        self.report(
            {"INFO"}, f"Outlines turned {state} for {toggled_count} objects.")
        logger.info(f"Toggled outlines {state}")
        return {"FINISHED"}


class WW_OT_ToggleStarMotion(Operator):
    bl_idname = "shader.toggle_star_motion"
    bl_label = "Toggle Star Motion"
    bl_description = "Toggle the animated star motion effect"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        if not context.active_object or context.active_object.type != "MESH":
            self.report(
                {"ERROR"}, "Please select a mesh to toggle star motion.")
            return {"CANCELLED"}

        mesh_name = context.active_object.name.split(".")[0]
        data = get_mesh_data(context, mesh_name)
        data.star_move = not data.star_move

        self.update_star_motion(context, mesh_name, data.star_move)
        state = "on" if data.star_move else "off"
        self.report(
            {"INFO"}, f"Star motion turned {state} for mesh {mesh_name}.")
        logger.info(f"Toggled star motion {state} for {mesh_name}")
        return {"FINISHED"}

    def update_star_motion(self, context, mesh_name, star_move):
        modifier = context.active_object.modifiers.get(
            f"ResonatorStar Move {mesh_name}")
        if modifier and modifier.type == "NODES":
            modifier.show_viewport = star_move

        for slot in context.active_object.material_slots:
            if slot.material and slot.material.use_nodes and "WW - ResonatorStar" in slot.material.name:
                for node in slot.material.node_tree.nodes:
                    if node.type == "GROUP":
                        for input in node.inputs:
                            if "Moving" in input.name:
                                input.default_value = 1.0 if star_move else 0.0


class WW_OT_ToggleHairTrans(Operator):
    bl_idname = "shader.toggle_transparent_hair"
    bl_label = "Toggle Transparent Hair"
    bl_description = "Toggle transparency effect for hair materials"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        if not context.active_object or context.active_object.type != "MESH":
            self.report(
                {"ERROR"}, "Please select a mesh to toggle hair transparency.")
            return {"CANCELLED"}

        mesh_name = context.active_object.name.split(".")[0]
        data = get_mesh_data(context, mesh_name)
        data.hair_trans = not data.hair_trans

        self.update_hair_transparency(context, data.hair_trans)
        state = "on" if data.hair_trans else "off"
        self.report(
            {"INFO"}, f"Transparent hair turned {state} for mesh {mesh_name}.")
        logger.info(f"Toggled hair transparency {state} for {mesh_name}")
        return {"FINISHED"}

    def update_hair_transparency(self, context, hair_trans):
        for slot in context.active_object.material_slots:
            if slot.material and slot.material.use_nodes and slot.material.name.startswith("WW - "):
                for node in slot.material.node_tree.nodes:
                    if node.type == "GROUP" and node.node_tree and "See Through" in node.node_tree.name:
                        node.mute = not hair_trans


class WW_OT_FixEyeUV(Operator):
    bl_idname = "shader.fix_eye_uv"
    bl_label = "Fix Eye UV"
    bl_description = "Fix UV mapping for eye materials"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        if not context.active_object or context.active_object.type != "MESH":
            self.report(
                {"ERROR"}, "Please select a mesh to fix eye UV mapping.")
            return {"CANCELLED"}

        original_outlines = context.scene.outlines_enabled
        if original_outlines:
            bpy.ops.shader.toggle_outlines()

        fixed_count = self.fix_eye_uvs(context)
        if original_outlines:
            bpy.ops.shader.toggle_outlines()

        self.report(
            {"INFO"}, f"Fixed UV maps for {fixed_count} eye shader nodes.")
        logger.info(f"Fixed {fixed_count} eye UV maps")
        return {"FINISHED"}

    def fix_eye_uvs(self, context):
        fixed_count = 0
        eye_materials = [
            slot.material
            for slot in context.active_object.material_slots
            if slot.material and "WW - Eye" in slot.material.name
        ]

        for material in eye_materials:
            if not material.use_nodes:
                continue
            for node in material.node_tree.nodes:
                if (
                    node.type == "GROUP"
                    and node.node_tree
                    and "Eye Depth" in node.node_tree.name
                ):
                    new_tree = node.node_tree.copy()
                    node.node_tree = new_tree
                    for sub_node in new_tree.nodes:
                        if sub_node.type == "UVMAP":
                            sub_node.uv_map = "UV2"
                            fixed_count += 1
        return fixed_count


class WW_OT_SeparateMesh(Operator):
    bl_idname = "shader.separate_by_vertex_group"
    bl_label = "Separate Mesh"
    bl_description = "Separate mesh into parts based on vertex groups"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        objects = [obj for obj in context.selected_objects if obj.type == "MESH"]
        if not objects:
            self.report(
                {"ERROR"}, "Please select one or more meshes to continue."
            )
            return {"CANCELLED"}

        total_separated = 0
        for obj in objects:
            parts_separated = self.separate_mesh(obj)
            total_separated += parts_separated

        self.report(
            {"INFO"}, f"Successfully separated all selected meshes."
        )
        return {"FINISHED"}

    def separate_mesh(self, obj: bpy.types.Object):
        original_name = obj.name
        separated_objects = []
        bpy.context.view_layer.objects.active = obj
        bpy.ops.object.mode_set(mode="EDIT")
        bpy.ops.mesh.select_all(action="DESELECT")
        bpy.ops.object.mode_set(mode="OBJECT")

        patterns = {" Hair": "Hair", " Cloth": "Piao", " Skirt": "Skirt"}
        parts_found = False
        parts_separated = 0

        for suffix, pattern in patterns.items():
            self.deselect_vertices(obj)
            has_group = self.select_vertices_by_group(obj, pattern)
            if has_group:
                parts_found = True
                bpy.ops.object.mode_set(mode="EDIT")
                bpy.ops.mesh.separate(type="SELECTED")
                bpy.ops.object.mode_set(mode="OBJECT")
                new_objs = [
                    o
                    for o in bpy.context.selected_objects
                    if o != obj and o not in separated_objects
                ]
                separated_objects.extend(new_objs)
                if new_objs:
                    new_objs[-1].name = original_name + suffix
                    parts_separated += 1
                    logger.info(f"Separated {pattern} as {new_objs[-1].name}")

        obj.name = original_name + " Body"
        if parts_found:
            self.clean_vertex_groups(obj, separated_objects)

        return parts_separated

    def deselect_vertices(self, obj: bpy.types.Object):
        for v in obj.data.vertices:
            v.select = False

    def select_vertices_by_group(self, obj: bpy.types.Object, pattern: str):
        has_group = False
        for vg in obj.vertex_groups:
            if pattern in vg.name:
                has_group = True
                for v in obj.data.vertices:
                    try:
                        vg.weight(v.index)
                        v.select = True
                    except RuntimeError:
                        continue
        return has_group

    def clean_vertex_groups(
        self, obj: bpy.types.Object, separated_objects: List[bpy.types.Object]
    ):
        bpy.ops.object.select_all(action="DESELECT")
        obj.select_set(True)
        bpy.context.view_layer.objects.active = obj
        for o in [obj] + separated_objects:
            self.remove_unused_vertex_groups(o)

    def remove_unused_vertex_groups(self, obj: bpy.types.Object):
        vgroups = obj.vertex_groups[:]
        removed_count = 0
        for vg in vgroups:
            has_weights = False
            for v in obj.data.vertices:
                try:
                    if vg.weight(v.index) > 0:
                        has_weights = True
                        break
                except RuntimeError:
                    continue
            if not has_weights:
                obj.vertex_groups.remove(vg)
                removed_count += 1

        if removed_count > 0:
            logger.info(
                f"Removed {removed_count} unused vertex groups from {obj.name}")


class WW_OT_FixNPCMats(Operator):
    bl_idname = "shader.fix_npc"
    bl_label = "Fix NPC Materials"
    bl_description = "Fix naming issues in NPC materials"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        if not obj or obj.type != "MESH":
            return False
        return any(
            slot.material and slot.material.name.startswith("MI_")
            for slot in obj.material_slots
        )

    def execute(self, context):
        fixed_count = 0
        for slot in context.active_object.material_slots:
            if slot.material and slot.material.name.startswith("MI_"):
                parts = slot.material.name.split("_", 2)
                if len(parts) > 2:
                    old_name = slot.material.name
                    slot.material.name = parts[0] + "_" + parts[1] + parts[2]
                    if old_name != slot.material.name:
                        fixed_count += 1

        self.report({"INFO"}, f"Fixed {fixed_count} NPC material names.")
        logger.info(f"Fixed {fixed_count} NPC material names")
        return {"FINISHED"}


class WW_OT_SetOptimize(Operator):
    bl_idname = "shader.set_optimize"
    bl_label = "Optimize Armature"
    bl_description = "Organize bones into collections and optimize armature display"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        armatures = [
            obj for obj in context.selected_objects if obj.type == "ARMATURE"]
        if not armatures:
            self.report(
                {"ERROR"}, "Please select one or more armatures to optimize.")
            return {"CANCELLED"}

        for armature in armatures:
            self.organize_bones(armature)
            armature.data.display_type = "STICK"
            for coll in armature.data.collections:
                coll.is_visible = coll.name == "Main"

        bpy.data.use_autopack = True

        self.report(
            {"INFO"}, f"Optimization completed for {len(armatures)} armatures.")
        logger.info(f"Optimized {len(armatures)} armatures")
        return {"FINISHED"}

    def organize_bones(self, armature: bpy.types.Object):
        data = armature.data
        bone_cols = data.collections
        collections = ["Skirt", "Hair", "Cloth", "Chest"]
        for name in collections:
            if name not in bone_cols:
                bone_cols.new(name=name)
                logger.info(f"Created bone collection: {name}")

        organized_count = 0
        for bone in data.bones:
            name = bone.name.lower()
            new_collection = self.get_bone_collection(name, bone_cols)
            if new_collection:
                self.assign_bone_to_collection(bone, new_collection, bone_cols)
                organized_count += 1

        logger.info(f"Organized {organized_count} bones in {armature.name}")

    def get_bone_collection(self, name: str, bone_cols):
        if "skirt" in name:
            return bone_cols["Skirt"]
        elif "hair" in name:
            return bone_cols["Hair"]
        elif "piao" in name:
            return bone_cols["Cloth"]
        elif "chest" in name:
            return bone_cols["Chest"]
        return None

    def assign_bone_to_collection(
        self, bone: bpy.types.Bone, collection: bpy.types.BoneCollection, bone_cols
    ):
        for col in bone_cols:
            if bone.name in col.bones:
                col.unassign(bone)
        collection.assign(bone)


class VIEW3D_PT_WutheringWaves(Panel):
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Wuthering Waves"
    bl_label = "Wuthering Waves"

    def draw(self, context):
        layout = self.layout

        active_obj = context.active_object
        mesh_name = (
            active_obj.name.split(".")[0]
            if active_obj and active_obj.type == "MESH"
            else ""
        )
        data = next(
            (
                m
                for m in context.scene.mesh_texture_mappings
                if m.mesh_name == mesh_name
            ),
            None,
        )

        box = layout.box()
        row = box.row()
        row.label(text="Character Setup", icon="ARMATURE_DATA")

        col = box.column(align=True)
        col.operator("shader.import_shader", icon="IMPORT")
        col.operator("shader.rigify_armature", icon="BONE_DATA")
        col.operator("shader.setup_head_driver", icon="DRIVER")
        col.operator("shader.create_face_panel", icon="FACESEL")
        col.operator("shader.import_face_panel", icon="FACESEL")

        box = layout.box()
        row = box.row()
        row.label(text="Viewport Settings", icon="RESTRICT_VIEW_OFF")

        col = box.column(align=True)
        col.operator("shader.set_performance", icon="SHADING_RENDERED")
        col.operator("shader.set_quality", icon="MATERIAL")
        col.prop(data if data else context.scene, "blush_value", text="Blush")
        col.prop(data if data else context.scene,
                 "disgust_value", text="Disgust")


class VIEW3D_PT_WutheringWaves_Appearance(Panel):
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Wuthering Waves"
    bl_label = "Appearance Settings"
    bl_parent_id = "VIEW3D_PT_WutheringWaves"

    def draw(self, context):
        layout = self.layout

        active_obj = context.active_object
        mesh_name = (
            active_obj.name.split(".")[0]
            if active_obj and active_obj.type == "MESH"
            else ""
        )
        data = next(
            (
                m
                for m in context.scene.mesh_texture_mappings
                if m.mesh_name == mesh_name
            ),
            None,
        )

        box = layout.box()
        row = box.row()
        row.label(text="Material Settings", icon="MATERIAL")

        col = box.column(align=True)
        col.prop(
            data if data else context.scene, "metallic_value", text="Enable Metallics"
        )
        col.prop(
            data if data else context.scene,
            "specular_value",
            text="Specular Multiplier",
        )

        box = layout.box()
        row = box.row()
        row.label(text="Texture Settings", icon="TEXTURE")

        col = box.column(align=True)
        texture_mode_state = (
            "Default" if data is None or (
                data and data.tex_mode) else "Version"
        )
        col.operator(
            "shader.toggle_texture_mode",
            text=f"Texture Mode: {texture_mode_state}",
            icon="IMAGE_DATA",
        )
        col.operator("shader.fix_eye_uv", icon="UV")

        box = layout.box()
        row = box.row()
        row.label(text="Visual Effects", icon="SHADERFX")

        col = box.column(align=True)
        outline_state = "On" if context.scene.outlines_enabled else "Off"
        col.operator(
            "shader.toggle_outlines",
            text=f"Outlines: {outline_state}",
            icon="MOD_WIREFRAME",
        )

        transparent_hair_state = (
            "On" if data is None or (data and data.hair_trans) else "Off"
        )
        col.operator(
            "shader.toggle_transparent_hair",
            text=f"Transparent Hair: {transparent_hair_state}",
            icon="STRANDS",
        )

        star_move_state = "On" if data and data.star_move else "Off"
        col.operator(
            "shader.toggle_star_motion",
            text=f"Star Motion: {star_move_state}",
            icon="LIGHT_SUN",
        )


class VIEW3D_PT_WutheringWaves_Light(Panel):
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Wuthering Waves"
    bl_label = "Lighting"
    bl_parent_id = "VIEW3D_PT_WutheringWaves"

    def draw(self, context):
        layout = self.layout

        box = layout.box()
        row = box.row()
        row.label(text="Light Mode Settings", icon="LIGHT")

        col = box.column(align=True)
        col.prop(context.scene, "light_mode_value", text="Light Mode")

        mode_name = LIGHT_MODES.get(context.scene.light_mode_value, "Unknown")
        mode_box = box.box()
        mode_row = mode_box.row(align=True)
        mode_row.alignment = "CENTER"
        mode_row.label(icon="LIGHT_SUN")
        mode_row.label(text=f"{mode_name}")

        col = box.column(align=True)
        col.prop(context.scene, "shadow_transition_range_value",
                 text="Shadow Range")
        col.prop(context.scene, "face_shadow_softness_value",
                 text="Face Softness")
        col.prop(context.scene, "shadow_position", text="Shadow Position")
        col.prop(context.scene, "catch_shadows", text="Catch Shadows")

        if context.scene.light_mode_value == 6:
            box = layout.box()
            row = box.row()
            row.label(text="Custom Colors", icon="COLOR")

            col = box.column(align=True)
            col.prop(context.scene, "amb_color", text="Ambient")
            col.prop(context.scene, "light_color", text="Light")
            col.prop(context.scene, "shadow_color", text="Shadow")
            col.prop(context.scene, "rim_color", text="Rim Tint")


class VIEW3D_PT_WutheringWaves_Tools(Panel):
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Wuthering Waves"
    bl_label = "Tools"
    bl_parent_id = "VIEW3D_PT_WutheringWaves"

    def draw(self, context):
        layout = self.layout

        box = layout.box()
        row = box.row()
        row.label(text="Mesh Tools", icon="MESH_DATA")

        col = box.column(align=True)
        col.operator("shader.separate_by_vertex_group", icon="MOD_EDGESPLIT")
        col.operator("shader.fix_npc", icon="MATERIAL")

        box = layout.box()
        row = box.row()
        row.label(text="Armature Tools", icon="ARMATURE_DATA")

        col = box.column(align=True)
        col.operator("shader.set_optimize", icon="OUTLINER_OB_ARMATURE")


classes = [
    MeshTextureData,
    WW_OT_ImportShader,
    WW_OT_ImportTextures,
    WW_OT_SetupHeadDriver,
    WW_OT_Rigify,
    WW_OT_CreateFacePanel,
    WW_OT_ImportFacePanel,
    WW_OT_SetLightMode,
    WW_OT_ToggleTexMode,
    WW_OT_ToggleOutlines,
    WW_OT_SetPerformanceMode,
    WW_OT_SetQualityMode,
    WW_OT_ToggleStarMotion,
    WW_OT_ToggleHairTrans,
    WW_OT_FixEyeUV,
    WW_OT_SeparateMesh,
    WW_OT_SetOptimize,
    WW_OT_FixNPCMats,
    VIEW3D_PT_WutheringWaves,
    VIEW3D_PT_WutheringWaves_Appearance,
    VIEW3D_PT_WutheringWaves_Light,
    VIEW3D_PT_WutheringWaves_Tools,
]


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    add_scene_props()
    logger.info("Shader (.fbx / .uemodel) registered")


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)

    for prop in [
        "rim_color",
        "shadow_color",
        "light_color",
        "amb_color",
        "specular_value",
        "metallic_value",
        "mesh_texture_mappings",
        "texture_priority_mode",
        "outlines_enabled",
        "is_first_use",
        "tex_dir",
        "original_textures",
        "original_materials",
        "shader_file_path",
        "face_panel_file_path",
        "light_mode_value",
        "shadow_position",
        "catch_shadows",
        "shadow_transition_range_value",
        "face_shadow_softness_value",
    ]:
        if hasattr(Scene, prop):
            delattr(Scene, prop)

    logger.info("Shader (.fbx / .uemodel) unregistered")


if __name__ == "__main__":
    register()
