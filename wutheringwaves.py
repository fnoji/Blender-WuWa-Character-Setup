import bpy
import re
import os
import math
import logging
import mathutils
from mathutils import Vector
from bpy.types import Panel, Operator, Scene, PropertyGroup
from bpy.props import (
    StringProperty,
    CollectionProperty,
    BoolProperty,
    FloatProperty,
    IntProperty,
    FloatVectorProperty,
)
from bpy_extras.io_utils import ImportHelper
from collections import namedtuple
from typing import Optional, Dict, List, Set, Tuple, Any

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
    "version": (1, 0),
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
    """Get the armature from a mesh's modifiers."""
    for modifier in mesh.modifiers:
        if modifier.type == "ARMATURE" and modifier.object:
            return modifier.object
    return None


def load_image(path: str) -> Optional[bpy.types.Image]:
    """Load an image or get it from bpy.data if already loaded."""
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
    """Find a texture node by name in a material."""
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
    """Find a texture that matches any of the provided patterns."""
    for pattern in patterns:
        for file in textures:
            fname = file.name if hasattr(file, "name") else file
            if re.match(pattern, fname):
                return load_image(os.path.join(tex_dir, fname))
    return None


def set_texture(
    material: bpy.types.Material, image: bpy.types.Image, nodes: Tuple[str]
):
    """Set an image texture to all matching nodes in a material."""
    for node_name in nodes:
        if node := find_texture_node(material, node_name):
            node.image = image


def set_node_input(material: bpy.types.Material, input_name: str, value: float):
    """Set a node input value in a material."""
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
    """Darken the eye vertex colors for proper shader functioning."""
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
    """Split a material name into base part and version."""
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
    """Get or create mesh texture data for a mesh."""
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
    """Set viewport to solid view mode."""
    for area in bpy.context.screen.areas:
        if area.type == "VIEW_3D":
            area.spaces.active.shading.type = "SOLID"
            break


def set_material_view():
    """Set viewport to material preview mode."""
    for area in bpy.context.screen.areas:
        if area.type == "VIEW_3D":
            area.spaces.active.shading.type = "MATERIAL"
            break


def get_suffix():
    """Get the suffix from light direction objects."""
    base_objects = [
        o for o in bpy.data.objects if o.name.startswith("Light Direction")]
    return (
        "." + base_objects[-1].name.split(".")[-1]
        if len(base_objects) > 1 and "." in base_objects[-1].name
        else ""
    )


def update_light(self, context):
    """Update light mode in shader node groups."""
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
    """Update shadow position in shader node groups."""
    value = context.scene.shadow_position
    if not (0.0 <= value <= 2.0):
        return
    if node_group := bpy.data.node_groups.get("Global Material Properties"):
        for node in node_group.nodes:
            if node.name == "Global Properties" and node.type == "GROUP_OUTPUT":
                if "Shadow Position" in node.inputs:
                    node.inputs["Shadow Position"].default_value = value


def update_catch_shadows(self, context):
    """Update shadow catching in shader node groups."""
    value = context.scene.catch_shadows
    if not (0 <= value <= 1):
        return
    if node_group := bpy.data.node_groups.get("Global Material Properties"):
        for node in node_group.nodes:
            if node.name == "Global Properties" and node.type == "GROUP_OUTPUT":
                if "Catch Shadows" in node.inputs:
                    node.inputs["Catch Shadows"].default_value = value


def update_colors(self, context):
    """Update custom color values in shader node groups."""
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
    """Update blush value in active object face materials."""
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
    """Update disgust value in active object face materials."""
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
    """Update metallic value in active object materials."""
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
    """Update specular value in active object materials."""
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
    """Add required properties to the Scene class."""
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
    """Initialize scene settings for first use."""
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
    """Import required node groups and objects from the shader file."""
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
    """Initialize geometry nodes modifiers for the active mesh."""
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
    """Set up control objects for the character."""
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
    """Set up geometry nodes modifiers for the mesh."""
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
    """Add a head lock constraint to align Head Origin with the head bone."""
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
    """Apply the head lock constraint."""
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
    """Configure the star shader settings."""
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
    """Generate texture filename patterns for matching."""
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
    """Apply textures to a material using the material texture data."""
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
    """Property group for storing mesh texture data."""

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


class WW_OT_Rigify(Operator):
    bl_idname = "shader.rigify_armature"
    bl_label = "Rigify"
    bl_description = "Rigify the selected armature with optimized bone structure and collections"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return context.active_object and context.active_object.type in {'MESH', 'ARMATURE'}

    def execute(self, context):
        selected_object = context.active_object
        original_selected_object_type = selected_object.type
        original_selected_object_name = selected_object.name
        logger.info(f"Starting Rigify process for {selected_object.name}")

        if selected_object.type == 'MESH':
            for modifier in selected_object.modifiers:
                if modifier.type == 'ARMATURE' and modifier.object:
                    armature_obj = modifier.object

                    if armature_obj.hide_get():
                        armature_obj.hide_set(False)
                        logger.info(f"Unhiding armature: {armature_obj.name}")

                    context.view_layer.objects.active = armature_obj
                    selected_object = armature_obj
                    logger.info(
                        f"Found armature modifier, switching to {selected_object.name}")
                    break
        elif selected_object.type != 'ARMATURE':
            logger.warning(
                "No direct armature selected, searching scene for armatures")
            for obj in context.scene.objects:
                if obj.type == 'ARMATURE':
                    if obj.hide_get():
                        obj.hide_set(False)
                        logger.info(f"Unhiding armature: {obj.name}")

                    context.view_layer.objects.active = obj
                    selected_object = obj
                    logger.info(
                        f"Found armature in scene: {selected_object.name}")
                    break

        if selected_object.type != 'ARMATURE':
            self.report({"ERROR"}, "No armature found to rigify.")
            logger.error("No armature found for Rigify process")
            return {"CANCELLED"}

        OrigArmature = selected_object.name
        RigArmatureName = "RIG-" + OrigArmature

        if RigArmatureName in bpy.data.objects:
            self.report(
                {"ERROR"}, f"This armature has already been rigified as '{RigArmatureName}'. Please use the existing rig.")
            logger.error(
                f"Rigify process cancelled - {RigArmatureName} already exists")
            return {"CANCELLED"}

        if selected_object.get('rigify_generated') == True:
            self.report(
                {"ERROR"}, "This armature is already rigged with Rigify. Please use a different armature.")
            logger.error(
                f"Rigify process cancelled - {selected_object.name} is already a Rigify rig")
            return {"CANCELLED"}

        CharacterMesh = None
        logger.info("Searching for mesh associated with the armature")
        for obj in context.scene.objects:
            if obj.type == 'MESH':
                for modifier in obj.modifiers:
                    if modifier.type == 'ARMATURE' and modifier.object and modifier.object.name == OrigArmature:
                        CharacterMesh = obj
                        logger.info(
                            f"Found associated mesh: {CharacterMesh.name}")
                        break
                if CharacterMesh:
                    break

        if not CharacterMesh:
            self.report(
                {"ERROR"}, "No mesh found associated with the armature.")
            logger.error(f"No mesh found for armature {OrigArmature}")
            return {"CANCELLED"}

        if selected_object.hide_viewport:
            selected_object.hide_viewport = False
            logger.info(
                f"Unhiding viewport visibility for {selected_object.name}")

        if selected_object.hide_get():
            selected_object.hide_set(False)
            logger.info(f"Unhiding {selected_object.name} in view layer")

        original_armature_location = selected_object.location.copy()
        logger.info(
            f"Original armature location: {original_armature_location}")

        selected_object.location = (0, 0, 0)
        logger.info("Moved armature to origin (0,0,0)")

        logger.info("Applying scale transformations to armature")
        bpy.ops.object.transform_apply(scale=True)
        rig_armature_object = context.view_layer.objects.active
        if rig_armature_object and rig_armature_object.type == 'ARMATURE':
            logger.info("Adjusting spine bones")
            bpy.ops.object.mode_set(mode='EDIT')
            spine_bone = rig_armature_object.data.edit_bones.get(
                "Bip001Spine2")
            if spine_bone:
                bone_length = (spine_bone.tail - spine_bone.head).length
                if bone_length < 0.06:
                    logger.info(
                        f"Adjusting spine bone length from {bone_length:.4f} to 0.15")
                    direction = spine_bone.tail - spine_bone.head
                    direction.normalize()
                    spine_bone.tail = spine_bone.head + direction * 0.15
                    spine_bone.tail.y = spine_bone.head.y
                    spine_bone.head.z += 0.03
                    spine_bone.tail.z += 0.03
            bpy.ops.object.mode_set(mode='OBJECT')

        armature = context.object
        logger.info("Starting finger bone adjustments")
        bpy.ops.object.mode_set(mode='EDIT')
        if "Bip001LFinger13" in armature.data.edit_bones:
            logger.info("Using 3-part finger bone structure")
            outward_bones = [
                "Bip001LFinger11", "Bip001LFinger21", "Bip001LFinger31", "Bip001LFinger41",
                "Bip001RFinger11", "Bip001RFinger21", "Bip001RFinger31", "Bip001RFinger41"
            ]
            inward_bones = [
                "Bip001LFinger13", "Bip001LFinger23", "Bip001LFinger33", "Bip001LFinger43",
                "Bip001RFinger13", "Bip001RFinger23", "Bip001RFinger33", "Bip001RFinger43"
            ]
        else:
            logger.info("Using 2-part finger bone structure")
            outward_bones = [
                "Bip001LFinger1", "Bip001LFinger2", "Bip001LFinger3", "Bip001LFinger4",
                "Bip001RFinger1", "Bip001RFinger2", "Bip001RFinger3", "Bip001RFinger4"
            ]
            inward_bones = [
                "Bip001LFinger12", "Bip001LFinger22", "Bip001LFinger32", "Bip001LFinger42",
                "Bip001RFinger12", "Bip001RFinger22", "Bip001RFinger32", "Bip001RFinger42"
            ]
        move_amount = 0.0014
        logger.info(f"Adjusting finger bones by {move_amount}")
        for bone_name in outward_bones:
            if bone_name in armature.data.edit_bones:
                bone = armature.data.edit_bones[bone_name]
                local_x_axis = bone.matrix.to_3x3().col[0].normalized()
                bone.tail += local_x_axis * move_amount
        for bone_name in inward_bones:
            if bone_name in armature.data.edit_bones:
                bone = armature.data.edit_bones[bone_name]
                local_x_axis = bone.matrix.to_3x3().col[0].normalized()
                bone.tail -= local_x_axis * move_amount
        bpy.ops.object.mode_set(mode='OBJECT')

        logger.info("Clearing existing bone collections")
        if armature.data.collections:
            for collection in armature.data.collections[:]:
                armature.data.collections.remove(collection)

        logger.info("Calculating bone rolls")
        bpy.ops.object.mode_set(mode='EDIT')
        bpy.ops.armature.select_all(action='SELECT')
        bpy.ops.armature.calculate_roll(type='CURSOR')
        bpy.ops.armature.select_all(action='DESELECT')

        logger.info("Adjusting bone connections")
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
        for bone1_name, bone2_name in bone_pairs:
            if bone1_name in armature.data.edit_bones and bone2_name in armature.data.edit_bones:
                bone1 = armature.data.edit_bones[bone1_name]
                bone2 = armature.data.edit_bones[bone2_name]
                bone1.tail = bone2.head

        logger.info("Fixing twist bone parents")
        twist_bones = {
            'Bip001RForeTwist': 'Bip001RForearm',
            'Bip001LForeTwist': 'Bip001LForearm'
        }
        for twist_bone, correct_parent in twist_bones.items():
            if twist_bone in armature.data.edit_bones and correct_parent in armature.data.edit_bones:
                bone = armature.data.edit_bones[twist_bone]
                if bone.parent != armature.data.edit_bones[correct_parent]:
                    logger.info(
                        f"Correcting parent of {twist_bone} to {correct_parent}")
                    bone.parent = armature.data.edit_bones[correct_parent]

        logger.info("Setting bone connections")
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
        connected_count = 0
        for bone_name in spine_bones:
            if bone_name in armature.data.edit_bones:
                armature.data.edit_bones[bone_name].use_connect = True
                connected_count += 1
        logger.info(f"Connected {connected_count} bones")

        logger.info("Adjusting bone rolls")
        bones_to_adjust_roll = [
            'Bip001Pelvis', 'Bip001Spine', 'Bip001Spine1',
            'Bip001Spine2', 'Bip001LClavicle', 'Bip001RClavicle'
        ]
        for bone_name in bones_to_adjust_roll:
            if bone_name in armature.data.edit_bones:
                armature.data.edit_bones[bone_name].roll = 0
        bpy.ops.object.mode_set(mode='OBJECT')

        logger.info("Creating bone collections")
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
            ('Root', 16, 11),
        ]
        for collection_name, index, row in bone_data:
            bpy.ops.armature.collection_add()
            new_collection = armature.data.collections[-1]
            new_collection.name = collection_name
            bpy.ops.armature.rigify_collection_set_ui_row(index=index, row=row)

        logger.info("Adding additional bone collections")
        collections_to_add = ['Hair', 'Cloth', 'Skirt', 'Others']
        for collection_name in collections_to_add:
            bpy.ops.armature.collection_add()
            new_collection = armature.data.collections[-1]
            new_collection.name = collection_name
        bpy.ops.armature.rigify_collection_add_ui_row(row=3, add=True)
        bpy.ops.armature.rigify_collection_add_ui_row(row=6, add=True)
        bpy.ops.armature.rigify_collection_add_ui_row(row=10, add=True)
        bpy.ops.armature.rigify_collection_add_ui_row(row=13, add=True)
        bpy.ops.armature.rigify_collection_add_ui_row(row=15, add=True)

        logger.info("Assigning rigify types to bones")
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
            logger.info("Adding 3-part finger bone structure rigify types")
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
        else:
            logger.info("Adding 2-part finger bone structure rigify types")
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

        assigned_count = 0
        for bone_name, rig_type, widget_type in bones_and_rig_types:
            bone = armature.pose.bones.get(bone_name)
            if bone:
                armature.data.bones[bone_name].select = True
                armature.data.bones.active = armature.data.bones[bone_name]
                bone.rigify_type = rig_type
                if widget_type and bone.rigify_parameters:
                    bone.rigify_parameters.super_copy_widget_type = widget_type
                assigned_count += 1
        logger.info(f"Assigned rigify types to {assigned_count} bones")

        logger.info("Creating heel bones")
        bpy.ops.object.mode_set(mode='EDIT')

        def duplicate_and_adjust_heel_bone(foot_bone_name, toe_bone_name, heel_bone_name, rotation_angle=1.5708):
            if toe_bone_name in armature.data.edit_bones:
                toe_bone = armature.data.edit_bones[toe_bone_name]
                heel_bone = armature.data.edit_bones.new(name=heel_bone_name)
                heel_bone.head = toe_bone.head
                heel_bone.tail = toe_bone.tail
                heel_bone.roll = toe_bone.roll
                rotation_matrix = mathutils.Matrix.Rotation(
                    rotation_angle, 4, 'Y')
                heel_bone.tail = heel_bone.head + \
                    rotation_matrix @ (heel_bone.tail - heel_bone.head)
                if foot_bone_name in armature.data.edit_bones:
                    foot_bone = armature.data.edit_bones[foot_bone_name]
                    foot_head_y = foot_bone.head[1]
                    heel_bone.head[1] = foot_head_y
                    heel_bone.tail[1] = foot_head_y
                heel_bone.parent = armature.data.edit_bones[foot_bone_name]
                logger.info(f"Created heel bone: {heel_bone_name}")
                return True
            return False

        heel_created_left = duplicate_and_adjust_heel_bone(
            'Bip001LFoot', 'Bip001LToe0', 'Bip001LHeel0', rotation_angle=1.5708)
        heel_created_right = duplicate_and_adjust_heel_bone(
            'Bip001RFoot', 'Bip001RToe0', 'Bip001RHeel0', rotation_angle=-1.5708)
        logger.info(
            f"Heel bones created: Left = {heel_created_left}, Right = {heel_created_right}")

        bpy.ops.object.mode_set(mode='OBJECT')

        logger.info("Renaming bones with left/right side suffixes")
        bpy.ops.object.mode_set(mode='EDIT')
        renamed_count = 0
        for bone in armature.data.edit_bones:
            if bone.name.startswith("Bip001R") and not bone.name.endswith(".R"):
                old_name = bone.name
                bone.name += ".R"
                renamed_count += 1
                logger.debug(f"Renamed {old_name} to {bone.name}")
            elif bone.name.startswith("Bip001L") and not bone.name.endswith(".L"):
                old_name = bone.name
                bone.name += ".L"
                renamed_count += 1
                logger.debug(f"Renamed {old_name} to {bone.name}")
        logger.info(f"Renamed {renamed_count} bones with side suffixes")

        logger.info("Normalizing bone naming")
        side_prefix_count = 0
        for bone in armature.data.edit_bones:
            if bone.name.startswith("Bip001R"):
                old_name = bone.name
                bone.name = bone.name.replace("Bip001R", "Bip001", 1)
                side_prefix_count += 1
                logger.debug(f"Normalized {old_name} to {bone.name}")
            elif bone.name.startswith("Bip001L"):
                old_name = bone.name
                bone.name = bone.name.replace("Bip001L", "Bip001", 1)
                side_prefix_count += 1
                logger.debug(f"Normalized {old_name} to {bone.name}")
        logger.info(f"Normalized {side_prefix_count} bone names")

        logger.info("Generating Rigify rig")
        bpy.ops.pose.rigify_generate()

        logger.info("Adjusting neck and head bone controllers")
        bpy.ops.object.mode_set(mode='POSE')
        armature = context.object
        pose_bone_neck = armature.pose.bones.get("Bip001Neck")
        if pose_bone_neck:
            bpy.ops.object.mode_set(mode='EDIT')
            edit_bone_neck = armature.data.edit_bones.get("Bip001Neck")
            if edit_bone_neck:
                neck_length = (edit_bone_neck.tail -
                               edit_bone_neck.head).length / 2
                logger.info(f"Neck length: {neck_length:.4f}")
            bpy.ops.object.mode_set(mode='POSE')
            pose_bone_neck.custom_shape_translation.y = neck_length
            pose_bone_neck.custom_shape_scale_xyz = (1.5, 1.5, 1.5)
            logger.info("Adjusted neck bone controller shape and position")

        pose_bone_head = armature.pose.bones.get("Bip001Head")
        if pose_bone_head:
            bpy.ops.object.mode_set(mode='EDIT')
            edit_bone_head = armature.data.edit_bones.get("Bip001Head")
            if edit_bone_head:
                head_length = (edit_bone_head.tail -
                               edit_bone_head.head).length
                logger.info(f"Head length: {head_length:.4f}")
            bpy.ops.object.mode_set(mode='POSE')
            pose_bone_head.custom_shape_translation.y = head_length * 1.2
            pose_bone_head.custom_shape_scale_xyz = (2, 2, 2)
            logger.info("Adjusted head bone controller shape and position")

        logger.info("Disabling IK Stretch on limbs")
        bpy.ops.object.mode_set(mode='OBJECT')
        context.object.pose.bones["Bip001UpperArm_parent.L"]["IK_Stretch"] = 0.000
        context.object.pose.bones["Bip001UpperArm_parent.R"]["IK_Stretch"] = 0.000
        context.object.pose.bones["Bip001Thigh_parent.L"]["IK_Stretch"] = 0.000
        context.object.pose.bones["Bip001Thigh_parent.R"]["IK_Stretch"] = 0.000

        logger.info("Setting up bone collections")
        bpy.ops.object.mode_set(mode='POSE')
        armature = context.view_layer.objects.active
        bones_to_move = {
            0: ["torso", "chest", "Bip001Clavicle.L", "Bip001Clavicle.R", "hips", "Bip001Neck", "Bip001Head", "EyeTracker"],
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
            0: 'THEME09',
            1: 'THEME04',
            2: 'THEME14',
            3: 'THEME03',
            4: 'THEME01',
            5: 'THEME01',
            6: 'THEME03',
            7: 'THEME03',
            8: 'THEME04',
            9: 'THEME04',
            10: 'THEME01',
            11: 'THEME01',
            12: 'THEME03',
            13: 'THEME03',
            14: 'THEME04',
            15: 'THEME04',
        }

        logger.info("Setting bone colors")
        for group_index, bone_names in bones_to_move.items():
            theme = theme_for_groups.get(group_index)
            bones_colored = 0
            for bone_name in bone_names:
                bone = armature.pose.bones.get(bone_name)
                if bone and theme:
                    bone.color.palette = theme
                    bones_colored += 1
            logger.debug(
                f"Colored {bones_colored} bones in group {group_index} with theme {theme}")

        logger.info("Organizing bones into collections")
        for collection_index, bone_names in bones_to_move.items():
            bpy.ops.pose.select_all(action='DESELECT')
            bones_moved = 0
            for bone_name in bone_names:
                bone = armature.pose.bones.get(bone_name)
                if bone:
                    bone.bone.select = True
                    bones_moved += 1
            logger.debug(
                f"Moving {bones_moved} bones to collection {collection_index}")
            bpy.ops.armature.move_to_collection(
                collection_index=collection_index)

        logger.info("Setting collection visibility")
        bpy.context.object.data.collections_all["ORG"].is_visible = True

        logger.info("Configuring bone transformations")
        bpy.ops.object.mode_set(mode='POSE')
        armature = context.view_layer.objects.active

        def lock_bone_transformations(bone):
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

        logger.info("Organizing special bone categories")

        def select_and_move_bones(keyword, collection_index):
            bpy.ops.pose.select_all(action='DESELECT')
            selected_bones = []
            for bone in armature.pose.bones:
                if keyword in bone.name:
                    selected_bones.append(bone)
                    bone.bone.select = True
                    lock_bone_transformations(bone)
            logger.debug(
                f"Found {len(selected_bones)} bones with keyword '{keyword}' for collection {collection_index}")
            bpy.ops.armature.move_to_collection(
                collection_index=collection_index)
            return len(selected_bones)

        keywords_and_collections = [
            ("Hair", 17),
            ("Earrings", 17),
            ("Piao", 18),
            ("Skirt", 19),
            ("Trousers", 19),
            ("Other", 20),
            ("Weapon", 20),
            ("Prop", 20),
            ("Chibang", 20),
            ("EyeTracker", 0),
            ("Bip001Neck.001", 21),
            ("Bip001Head.001", 21),
            ("Chest", 0),
        ]

        for keyword, collection_index in keywords_and_collections:
            count = select_and_move_bones(keyword, collection_index)
            logger.info(
                f"Moved {count} bones with keyword '{keyword}' to collection {collection_index}")

        logger.info("Configuring collection visibility settings")
        bpy.context.object.data.collections_all["ORG"].is_visible = False
        bpy.context.object.data.collections_all["Torso (Tweak)"].is_visible = False
        bpy.context.object.data.collections_all["Arm.L (FK)"].is_visible = False
        bpy.context.object.data.collections_all["Arm.R (FK)"].is_visible = False
        bpy.context.object.data.collections_all["Leg.L (FK)"].is_visible = False
        bpy.context.object.data.collections_all["Leg.R (FK)"].is_visible = False
        bpy.context.object.data.collections_all["Hair"].is_visible = False
        bpy.context.object.data.collections_all["Cloth"].is_visible = False
        bpy.context.object.data.collections_all["Skirt"].is_visible = False
        bpy.context.object.data.collections_all["Arm.L (Tweak)"].is_visible = False
        bpy.context.object.data.collections_all["Arm.R (Tweak)"].is_visible = False
        bpy.context.object.data.collections_all["Leg.L (Tweak)"].is_visible = False
        bpy.context.object.data.collections_all["Leg.R (Tweak)"].is_visible = False

        logger.info("Setting ORG bones to deform")
        obj = context.active_object
        if obj and obj.type == 'ARMATURE':
            bpy.ops.object.mode_set(mode='EDIT')
            deform_count = 0
            for bone in obj.data.edit_bones:
                if bone.name.startswith('ORG-'):
                    bone.use_deform = True
                    deform_count += 1
            logger.info(f"Set {deform_count} ORG bones to deform")
            bpy.ops.object.mode_set(mode='OBJECT')

        logger.info("Updating mesh vertex groups for rig compatibility")
        bpy.ops.object.select_all(action='DESELECT')
        context.view_layer.objects.active = CharacterMesh
        CharacterMesh.select_set(True)

        group_rename_count = 0
        for obj in context.selected_objects:
            if obj.type == 'MESH':
                for group in obj.vertex_groups:
                    new_name = "ORG-" + group.name
                    group.name = new_name
                    group_rename_count += 1
        logger.info(
            f"Renamed {group_rename_count} vertex groups with ORG- prefix")

        logger.info("Transferring and remapping vertex weights")

        def transfer_and_remove_vertex_weights(weight_mappings):
            obj = context.object
            if obj is None or obj.type != 'MESH':
                logger.warning("No mesh object selected for weight transfer")
                return

            vgroups = obj.vertex_groups
            bpy.ops.object.mode_set(mode='OBJECT')
            transfers_completed = 0

            for source_group_name, target_group_name in weight_mappings.items():
                if source_group_name not in vgroups:
                    logger.debug(
                        f"Source group {source_group_name} not found, skipping")
                    continue

                source_group = vgroups[source_group_name]
                if target_group_name not in vgroups:
                    logger.debug(
                        f"Creating new target group {target_group_name}")
                    target_group = vgroups.new(name=target_group_name)
                else:
                    target_group = vgroups[target_group_name]

                verts_affected = 0
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
                        verts_affected += 1

                transfers_completed += 1
                logger.debug(
                    f"Transferred weights from {source_group_name} to {target_group_name}, affected {verts_affected} vertices")

            logger.info(f"Completed {transfers_completed} weight transfers")

        weight_mappings = {
            "ORG-Bip001UpArmTwist.L": "DEF-Bip001UpperArm.L",
            "ORG-Bip001UpArmTwist1.L": "DEF-Bip001UpperArm.L",
            "ORG-Bip001UpArmTwist2.L": "DEF-Bip001UpperArm.L.001",
            "ORG-Bip001UpperArm.L": "DEF-Bip001UpperArm.L.001",
            "ORG-Bip001Forearm.L": "DEF-Bip001Forearm.L",
            "ORG-Bip001ForeTwist.L": "DEF-Bip001Forearm.L.001",
            "ORG-Bip001ForeTwist1.L": "DEF-Bip001Forearm.L.001",
            "ORG-Bip001_L_Elbow_F": "DEF-Bip001UpperArm.L.001",
            "ORG-Bip001_L_Elbow_B": "DEF-Bip001UpperArm.L.001",
            "ORG-Bip001UpArmTwist.R": "DEF-Bip001UpperArm.R",
            "ORG-Bip001UpArmTwist1.R": "DEF-Bip001UpperArm.R",
            "ORG-Bip001UpArmTwist2.R": "DEF-Bip001UpperArm.R.001",
            "ORG-Bip001UpperArm.R": "DEF-Bip001UpperArm.R.001",
            "ORG-Bip001Forearm.R": "DEF-Bip001Forearm.R",
            "ORG-Bip001ForeTwist.R": "DEF-Bip001Forearm.R.001",
            "ORG-Bip001ForeTwist1.R": "DEF-Bip001Forearm.R.001",
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

        transfer_and_remove_vertex_weights(weight_mappings)

        logger.info("Updating armature modifier")
        bpy.ops.object.select_all(action='DESELECT')
        context.view_layer.objects.active = CharacterMesh
        CharacterMesh.select_set(True)
        modifier_found = False
        for modifier in CharacterMesh.modifiers:
            if modifier.type == 'ARMATURE' and modifier.object and modifier.object.name == OrigArmature:
                rig_armature_object = context.scene.objects.get(
                    RigArmatureName)
                if rig_armature_object:
                    logger.info(
                        f"Updating armature modifier to use {RigArmatureName}")
                    modifier.object = rig_armature_object
                    modifier_found = True
                    break

        if not modifier_found:
            logger.warning(f"No armature modifier found for {OrigArmature}")

        logger.info("Creating eye tracker bone")
        bpy.ops.object.select_all(action='DESELECT')
        rig_armature_object = context.scene.objects.get(RigArmatureName)
        if rig_armature_object:
            bpy.ops.object.select_all(action='DESELECT')
            context.view_layer.objects.active = rig_armature_object
            rig_armature_object.select_set(True)
            bpy.ops.object.mode_set(mode='EDIT')
            target_bone = rig_armature_object.data.edit_bones.get(
                "ORG-Bip001Head")
            if target_bone:
                logger.info("Positioning cursor at head bone")
                context.scene.cursor.location = target_bone.head
            bpy.ops.object.mode_set(mode='OBJECT')

        bpy.ops.object.mode_set(mode='EDIT')
        bpy.ops.armature.bone_primitive_add()
        new_bone = rig_armature_object.data.edit_bones[-1]
        new_bone.name = "EyeTracker"
        head = new_bone.head
        tail = new_bone.tail
        direction = tail - head
        direction_length = direction.length

        if direction_length > 0:
            direction.normalize()
            new_bone.tail = head + direction * 0.03

        new_bone.head.y -= 0.15
        new_bone.tail.y -= 0.15
        new_bone.head.z += 0.03
        new_bone.tail.z += 0.03
        logger.info("Created EyeTracker bone")

        parent_bone = rig_armature_object.data.edit_bones.get("ORG-Bip001Head")
        if parent_bone:
            new_bone.parent = parent_bone
            new_bone.use_connect = False
            logger.info("Parented EyeTracker to head bone")

        context.scene.cursor.location = mathutils.Vector((0, 0, 0))
        bpy.ops.object.mode_set(mode='OBJECT')

        CharacterMesh = context.view_layer.objects.active
        if CharacterMesh is None or rig_armature_object is None:
            logger.warning(
                "Character mesh or rig armature not found for eye tracking setup")
            pass
        else:
            logger.info("Setting up eye tracking shape key drivers")
            bpy.ops.object.select_all(action='DESELECT')
            last_selected_mesh = None
            for obj in context.scene.objects:
                if obj.type == 'MESH':
                    for modifier in obj.modifiers:
                        if modifier.type == 'ARMATURE' and modifier.object == rig_armature_object:
                            obj.select_set(True)
                            last_selected_mesh = obj
                            logger.info(
                                f"Found mesh for eye tracking: {obj.name}")

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

                drivers_added = 0
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
                            drivers_added += 1
                            logger.info(
                                f"Added driver for shape key {shape_key_name}")

                logger.info(f"Added {drivers_added} eye tracking drivers")
                bpy.ops.object.mode_set(mode='OBJECT')

        context.view_layer.objects.active = rig_armature_object
        bpy.ops.object.mode_set(mode='POSE')
        pose_bones = rig_armature_object.pose.bones

        if "root" in pose_bones and "EyeTracker" in pose_bones:
            root_bone = pose_bones["root"]
            eye_tracker_bone = pose_bones["EyeTracker"]
            if root_bone.custom_shape:
                eye_tracker_bone.custom_shape = root_bone.custom_shape
                logger.info(
                    "Assigned custom shape to EyeTracker from root bone")

        bpy.ops.object.mode_set(mode='OBJECT')

        if rig_armature_object is None or rig_armature_object.type != 'ARMATURE':
            logger.warning("No valid armature for neck/head FK setup")
            pass
        else:
            logger.info("Setting up FK controls for neck and head")
            bpy.ops.object.mode_set(mode='EDIT')
            arm = armature.data
            bone_renames = 0

            if 'ORG-Bip001Neck' in arm.edit_bones:
                arm.edit_bones['ORG-Bip001Neck'].name = 'Bip001Neck'
                bone_renames += 1

            if 'ORG-Bip001Head' in arm.edit_bones:
                arm.edit_bones['ORG-Bip001Head'].name = 'Bip001Head'
                bone_renames += 1

            logger.info(f"Renamed {bone_renames} ORG bones for FK control")

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
                logger.info("Created FK control for neck")

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
                logger.info("Created FK control for head")

            bpy.ops.object.mode_set(mode='POSE')

            if "Bip001Neck._fk" in armature.pose.bones:
                tweak_bone = armature.pose.bones["Bip001Neck._fk"]
                spine2_fk = armature.pose.bones.get("Bip001Spine2_fk")
                if spine2_fk:
                    tweak_bone.custom_shape = spine2_fk.custom_shape
                tweak_bone.custom_shape_transform = armature.pose.bones["Bip001Neck"]
                logger.info("Assigned custom shape to neck FK controller")

            if "Bip001Head._fk" in armature.pose.bones:
                tweak_bone = armature.pose.bones["Bip001Head._fk"]
                spine2_fk = armature.pose.bones.get("Bip001Spine2_fk")
                if spine2_fk:
                    tweak_bone.custom_shape = spine2_fk.custom_shape
                tweak_bone.custom_shape_transform = armature.pose.bones["Bip001Head"]
                logger.info("Assigned custom shape to head FK controller")

            bpy.context.object.data.collections_all["Torso (Tweak)"].is_visible = True
            armature = context.view_layer.objects.active
            bones_to_move = {
                0: ["Bip001Neck", "Bip001Head"],
                1: ["Bip001Neck._fk", "Bip001Head._fk"],
            }
            theme_for_groups = {
                0: 'THEME09',
                1: 'THEME04',
            }

            logger.info("Setting bone colors for neck and head controllers")
            for group_index, bone_names in bones_to_move.items():
                theme = theme_for_groups.get(group_index)
                bones_colored = 0
                for bone_name in bone_names:
                    bone = armature.pose.bones.get(bone_name)
                    if bone and theme:
                        bone.color.palette = theme
                        bones_colored += 1
                logger.debug(
                    f"Colored {bones_colored} neck/head bones in group {group_index}")

            logger.info("Moving neck and head bones to collections")
            for collection_index, bone_names in bones_to_move.items():
                bpy.ops.pose.select_all(action='DESELECT')
                bones_moved = 0
                for bone_name in bone_names:
                    bone = armature.pose.bones.get(bone_name)
                    if bone:
                        bone.bone.select = True
                        bones_moved += 1
                bpy.ops.armature.move_to_collection(
                    collection_index=collection_index)
                logger.debug(
                    f"Moved {bones_moved} neck/head bones to collection {collection_index}")

            bpy.context.object.data.collections_all["Torso (Tweak)"].is_visible = False
            bpy.ops.object.mode_set(mode='OBJECT')

        if rig_armature_object is None or rig_armature_object.type != 'ARMATURE':
            logger.warning("No valid armature for bone length check")
            pass
        else:
            logger.info("Checking for and fixing excessively long bones")
            obj = rig_armature_object
            if obj and obj.type == 'ARMATURE':
                if bpy.context.mode != 'EDIT_ARMATURE':
                    bpy.ops.object.mode_set(mode='EDIT')
                armature = obj.data
                fixed_count = 0
                for bone in armature.edit_bones:
                    length = (bone.head - bone.tail).length
                    if length > 1.0:
                        logger.warning(
                            f"Found excessively long bone: {bone.name} ({length:.2f} units)")
                        direction = (bone.tail - bone.head).normalized()
                        bone.tail = bone.head + direction * 0.5
                        fixed_count += 1
                logger.info(f"Fixed {fixed_count} excessively long bones")
                bpy.ops.object.mode_set(mode='OBJECT')

        bpy.ops.object.select_all(action='DESELECT')

        logger.info("Moving RIG armature to original location")
        rig_armature_object = context.scene.objects.get(RigArmatureName)
        if rig_armature_object:
            rig_armature_object.location = original_armature_location
            logger.info(f"Moved RIG armature to {original_armature_location}")

            original_armature = bpy.data.objects.get(OrigArmature)
            if original_armature:
                original_armature.location = original_armature_location
                logger.info(
                    f"Restored original armature location to {original_armature_location}")

        logger.info(
            "Setting up parent-child relationship between rig and meshes")
        meshes_with_rig_modifier = []
        rig_armature_object = context.scene.objects.get(RigArmatureName)

        if rig_armature_object:
            for obj in context.scene.objects:
                if obj.type == 'MESH':
                    for modifier in obj.modifiers:
                        if modifier.type == 'ARMATURE' and modifier.object == rig_armature_object:
                            meshes_with_rig_modifier.append(obj)
                            logger.info(
                                f"Found mesh using RIG armature in modifier: {obj.name}")

            parent_count = 0
            for mesh in meshes_with_rig_modifier:
                bpy.ops.object.select_all(action='DESELECT')
                mesh.select_set(True)
                rig_armature_object.select_set(True)
                context.view_layer.objects.active = rig_armature_object
                bpy.ops.object.parent_set(type='OBJECT', keep_transform=True)
                parent_count += 1
                logger.info(
                    f"Set {mesh.name} as child of {rig_armature_object.name} with keep_transform")

            logger.info(
                f"Set up parent-child relationships for {parent_count} meshes")

        if rig_armature_object:
            rig_armature_object['rigify_generated'] = True
            logger.info(
                f"Marked {RigArmatureName} as a rigify-generated armature")

        logger.info("Hiding original armature")
        original_armature = context.scene.objects.get(OrigArmature)
        if original_armature and original_armature.type == 'ARMATURE':
            bpy.ops.object.select_all(action='DESELECT')
            original_armature.select_set(True)
            context.view_layer.objects.active = original_armature
            original_armature.hide_set(True)
            logger.info(f"Original armature {OrigArmature} hidden")

        bpy.ops.object.select_all(action='DESELECT')

        if bpy.context.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')

        if original_selected_object_type == 'MESH':
            original_mesh = bpy.data.objects.get(original_selected_object_name)
            if original_mesh:
                context.view_layer.objects.active = original_mesh
                original_mesh.select_set(True)
                logger.info(f"Selected original mesh: {original_mesh.name}")
            else:
                if CharacterMesh:
                    context.view_layer.objects.active = CharacterMesh
                    CharacterMesh.select_set(True)
                    logger.info(
                        f"Selected character mesh: {CharacterMesh.name}")
        else:
            rig_armature_object = context.scene.objects.get(RigArmatureName)
            if rig_armature_object:
                context.view_layer.objects.active = rig_armature_object
                rig_armature_object.select_set(True)
                logger.info(f"Selected rig armature: {RigArmatureName}")

        self.report({"INFO"}, f"Rigify completed for {RigArmatureName}")
        logger.info(
            f"Rigify process completed successfully for {RigArmatureName}")

        return {"FINISHED"}


class WW_OT_SetupHeadDriver(Operator):
    """Reset head bone position and parent Head Origin and Light Direction to armature."""

    bl_idname = "shader.setup_head_driver"
    bl_label = "Set Up Head Driver"
    bl_description = "Reset head bone position and parent Head Origin and Light Direction to armature"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        """Check if the active object is a mesh with an armature or an armature itself."""
        active_obj = context.active_object
        if not active_obj or active_obj.type not in {"MESH", "ARMATURE"}:
            return False
        if active_obj.type == "ARMATURE":
            return True
        return get_armature_from_modifiers(active_obj) is not None

    def execute(self, context):
        """Execute the head driver setup process."""
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
        """Retrieve the mesh associated with the given armature."""
        for obj in bpy.data.objects:
            if obj.type == "MESH" and get_armature_from_modifiers(obj) == armature:
                return obj
        return None

    def get_model_specific_objects(self, mesh, mesh_name):
        """Get the Head Origin and Light Direction objects specific to the model."""
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
        """Parent the Head Origin and Light Direction objects to the armature."""
        for obj in (head_origin, light_direction):
            if obj:
                obj.parent = armature
                obj.matrix_parent_inverse = armature.matrix_world.inverted()

    def reset_head_driver(self, mesh_name, armature, head_origin):
        """Reset the head driver by aligning Head Origin with the head bone."""
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
        """Reset the Light Direction object position and rotation relative to the armature."""
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
        """Restore the initial selection state and mode."""
        bpy.ops.object.mode_set(mode="OBJECT")
        bpy.ops.object.select_all(action="DESELECT")
        if active_obj:
            active_obj.select_set(True)
            context.view_layer.objects.active = active_obj


class WW_OT_ImportFacePanel(Operator, ImportHelper):
    """Operator to import a face panel for a rigged character mesh."""

    bl_idname = "shader.import_face_panel"
    bl_label = "Import Face Panel"
    bl_description = "Import a face panel for the selected character"
    filename_ext = ".blend"
    filter_glob: StringProperty(
        default="*.blend",
        options={"HIDDEN"},
    )

    @classmethod
    def poll(cls, context):
        """Check if the active object is a mesh or armature for operator availability."""
        active_obj = context.active_object
        if not active_obj or active_obj.type not in {"MESH", "ARMATURE"}:
            return False
        if active_obj.type == "ARMATURE":
            return True
        return get_armature_from_modifiers(active_obj) is not None

    def invoke(self, context, event):
        """Invoke the operator, using stored file path if valid, otherwise open file dialog."""
        if hasattr(context.scene, "face_panel_file_path") and os.path.exists(
            context.scene.face_panel_file_path
        ):
            self.filepath = context.scene.face_panel_file_path
            if self.is_valid_blend_file(self.filepath):
                return self.execute(context)
        return ImportHelper.invoke(self, context, event)

    def is_valid_blend_file(self, filepath):
        """Verify if the .blend file contains a 'Face panel' collection."""
        try:
            with bpy.data.libraries.load(filepath, link=False) as (src_data, _):
                return "Face panel" in src_data.collections
        except Exception as e:
            logger.error(f"Error checking blend file: {str(e)}")
            return False

    def execute(self, context):
        """Execute the face panel import process for the selected character."""
        active_obj = context.active_object
        selected_obj = context.active_object

        if not self.validate_selection(context, selected_obj, active_obj):
            return {"CANCELLED"}

        if selected_obj.type == "ARMATURE":
            armature = selected_obj
            mesh = self.get_mesh_from_armature(armature)
            if not mesh:
                self.report(
                    {"ERROR"}, "No mesh found associated with the selected armature."
                )
                self.restore_initial_state(context, active_obj)
                return {"CANCELLED"}
        else:
            mesh = selected_obj
            armature = get_armature_from_modifiers(mesh)

        if not self.validate_mesh_and_armature(context, mesh, armature, active_obj):
            return {"CANCELLED"}

        if "rig" not in armature.name.lower():
            self.report(
                {"ERROR"}, "The armature must be pre-rigged for the face panel import to work correctly."
            )
            self.restore_initial_state(context, active_obj)
            return {"CANCELLED"}

        if mesh.get("face_panel_assigned", False):
            self.report(
                {"ERROR"}, "Mesh already has a face panel assigned. Operation cancelled."
            )
            self.restore_initial_state(context, active_obj)
            return {"CANCELLED"}

        armature_was_hidden = armature.hide_get()
        if armature_was_hidden:
            armature.hide_set(False)

        bpy.ops.object.mode_set(mode="OBJECT")
        head_pos = self.get_bone_world_position(armature, "c_head.x")
        if not head_pos:
            head_pos = self.get_bone_world_position(armature, "Bip001Head")
            if not head_pos:
                self.report(
                    {"ERROR"}, "Bones 'c_head.x' or 'Bip001Head' not found. Face panel import cancelled."
                )
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

        self.setup_drivers(context, mesh, panel_armature)

        if armature_was_hidden:
            armature.hide_set(True)

        mesh["face_panel_assigned"] = True

        self.report(
            {"INFO"}, "Face panel imported and configured successfully."
        )

        self.restore_initial_state(context, active_obj)

        return {"FINISHED"}

    def validate_selection(self, context, selected_obj, active_obj):
        """Ensure a valid mesh or armature is selected for import."""
        if not selected_obj:
            self.report(
                {"ERROR"}, "Please select a mesh or armature to import the face panel."
            )
            self.restore_initial_state(context, active_obj)
            return False
        if selected_obj.type not in {"MESH", "ARMATURE"}:
            self.report(
                {"ERROR"}, "Selected object must be a mesh or armature.")
            self.restore_initial_state(context, active_obj)
            return False
        return True

    def validate_mesh_and_armature(self, context, mesh, armature, active_obj):
        """Check if the mesh has shape keys and is linked to an armature."""
        if not mesh.data.shape_keys:
            self.report(
                {"ERROR"},
                "Mesh has no shape keys. Face panel requires a mesh with shape keys.",
            )
            self.restore_initial_state(context, active_obj)
            return False
        if not armature:
            self.report(
                {"ERROR"},
                "No armature found in mesh modifiers. Face panel requires an armature.",
            )
            self.restore_initial_state(context, active_obj)
            return False
        return True

    def restore_initial_state(self, context, active_obj):
        """Restore the context to its previous state after an error or completion."""
        bpy.ops.object.mode_set(mode="OBJECT")
        bpy.ops.object.select_all(action="DESELECT")
        if active_obj:
            active_obj.select_set(True)
            context.view_layer.objects.active = active_obj

    def import_collection(self, context):
        """Import the 'Face panel' collection from the specified .blend file."""
        collection_name = "Face panel"
        filepath = self.filepath

        if not filepath or not os.path.exists(filepath):
            self.report(
                {"ERROR"}, "Please select a valid .blend file containing a face panel."
            )
            return None, None

        with bpy.data.libraries.load(filepath, link=False) as (src_data, tgt_data):
            if collection_name in src_data.collections:
                tgt_data.collections = [collection_name]
            else:
                self.report(
                    {"ERROR"},
                    "Face panel collection not found in the selected .blend file.",
                )
                return None, None

        imported_collection = next(
            (coll for coll in tgt_data.collections if coll), None
        )
        if not imported_collection:
            self.report({"ERROR"}, "Failed to import face panel collection.")
            return None, None

        bpy.context.scene.collection.children.link(imported_collection)
        logger.info(
            f"Imported face panel collection: {imported_collection.name}")

        face_panel = next(
            (
                obj
                for obj in imported_collection.objects
                if obj.name.startswith("Face Pannel")
            ),
            None,
        )
        if not face_panel:
            self.report(
                {"ERROR"}, "Face panel object not found in the imported collection."
            )
            return None, None

        bpy.context.view_layer.objects.active = face_panel
        face_panel.select_set(True)
        face_panel.lock_scale = [False, False, False]
        face_panel.scale = (0.2, 0.2, 0.2)
        face_panel.lock_scale = [True, True, True]

        panel_armature = next(
            (obj for obj in imported_collection.objects if obj.type == "ARMATURE"), None
        )
        if not panel_armature:
            self.report(
                {"ERROR"}, "Armature missing in face panel collection.")
            return None, None

        context.scene.face_panel_file_path = filepath
        return face_panel, panel_armature

    def get_armature_from_modifiers(self, mesh):
        """Retrieve the armature linked to the mesh via modifiers."""
        for modifier in mesh.modifiers:
            if modifier.type == "ARMATURE" and modifier.object:
                return modifier.object
        return None

    def get_mesh_from_armature(self, armature):
        """Find a mesh associated with the given armature."""
        for obj in bpy.data.objects:
            if obj.type == "MESH" and self.get_armature_from_modifiers(obj) == armature:
                return obj
        return None

    def position_panel(self, panel, armature, head_pos):
        """Position the face panel relative to the head bone."""
        y = 0.0
        x = head_pos.x + 0.1
        z = head_pos.z - 0.1

        bpy.ops.object.mode_set(mode="OBJECT")
        bpy.context.view_layer.objects.active = panel
        panel.select_set(True)
        panel.location = (x, y, z)
        logger.info(f"Positioned face panel at ({x}, {y}, {z})")

    def get_bone_world_position(self, armature, bone_name):
        """Calculate the world position of a specified bone in the armature."""
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
        """Configure drivers for shape keys based on face panel armature movements."""
        if not armature:
            return

        bpy.context.view_layer.objects.active = mesh
        logger.info("Setting up face panel drivers")

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
            (
                "E_Smile_L",
                "eye.pos",
                "e.wink.up.l",
                "max(bone_001 * 5, bone * 5)",
                "LOC_Y",
            ),
            (
                "E_Smile_R",
                "eye.pos",
                "e.wink.up.r",
                "max(bone_001 * 5, bone * 5)",
                "LOC_Y",
            ),
        ]

        try:
            for shape_key, bone_name, expression, transform_type in driver_configs:
                self.add_driver(
                    mesh, armature, shape_key, bone_name, expression, transform_type
                )
            for (
                shape_key,
                bone1,
                bone2,
                expression,
                transform_type,
            ) in dual_driver_configs:
                self.add_dual_driver(
                    mesh, armature, shape_key, bone1, bone2, expression, transform_type
                )
            context.evaluated_depsgraph_get().update()
            logger.info("Face panel drivers set up successfully")
        except Exception as e:
            logger.error(f"Error setting up face panel drivers: {str(e)}")

    def add_driver(
        self, mesh, armature, shape_key, bone_name, expression, transform_type
    ):
        """Add a single-variable driver to a shape key based on bone transform."""
        shape_key_block = mesh.data.shape_keys.key_blocks.get(shape_key)
        if not shape_key_block or (
            hasattr(shape_key_block, "driver") and shape_key_block.driver
        ):
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

    def add_dual_driver(
        self, mesh, armature, shape_key, bone1, bone2, expression, transform_type
    ):
        """Add a dual-variable driver to a shape key based on two bone transforms."""
        shape_key_block = mesh.data.shape_keys.key_blocks.get(shape_key)
        if not shape_key_block or (
            hasattr(shape_key_block, "driver") and shape_key_block.driver
        ):
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


class WW_OT_SetLightMode(Operator):
    """Set light mode for characters."""

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
        """Update node values for light mode."""
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
        """Get the name of a light mode by value."""
        return LIGHT_MODES.get(value, "Unknown")


class WW_OT_ToggleTexMode(Operator):
    """Switch texture priority mode."""

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
        """Reassign textures with the new priority mode."""
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
    """Toggle character outlines."""

    bl_idname = "shader.toggle_outlines"
    bl_label = "Toggle Outlines"
    bl_description = "Toggle visibility of character outline effects"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        context.scene.outlines_enabled = not context.scene.outlines_enabled
        toggled_count = 0
        for obj in (o for o in bpy.data.objects if o.type == "MESH"):
            if modifier := obj.modifiers.get(f"WW - Outlines {obj.name.split('.')[0]}"):
                if modifier.type == "NODES":
                    modifier.show_viewport = context.scene.outlines_enabled
                    toggled_count += 1

        state = "on" if context.scene.outlines_enabled else "off"
        self.report(
            {"INFO"}, f"Outlines turned {state} for {toggled_count} objects.")
        logger.info(f"Toggled outlines {state}")
        return {"FINISHED"}


class WW_OT_SetPerformanceMode(Operator):
    """Optimize viewport performance."""

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
    """Set viewport to high quality."""

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


class WW_OT_ToggleStarMotion(Operator):
    """Toggle star motion effect."""

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
        self.report({"INFO"}, f"Star motion turned {state}.")
        logger.info(f"Toggled star motion {state} for {mesh_name}")
        return {"FINISHED"}

    def update_star_motion(self, context, mesh_name, star_move):
        """Update star motion in modifiers and materials."""
        modifier = context.active_object.modifiers.get(
            f"ResonatorStar Move {mesh_name}"
        )
        if modifier and modifier.type == "NODES":
            modifier.show_viewport = star_move

        modified_count = 0
        for slot in context.active_object.material_slots:
            if (
                slot.material
                and slot.material.use_nodes
                and "WW - ResonatorStar" in slot.material.name
            ):
                for node in slot.material.node_tree.nodes:
                    if node.type == "GROUP":
                        for input in node.inputs:
                            if "Moving" in input.name:
                                input.default_value = 1.0 if star_move else 0.0
                                modified_count += 1

        logger.info(f"Updated star motion in {modified_count} materials")


class WW_OT_ToggleHairTrans(Operator):
    """Toggle transparent hair effect."""

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

        modified_count = self.update_hair_transparency(
            context, data.hair_trans)
        state = "on" if data.hair_trans else "off"
        self.report(
            {"INFO"}, f"Transparent hair turned {state} for {modified_count} materials."
        )
        logger.info(f"Toggled hair transparency {state} for {mesh_name}")
        return {"FINISHED"}

    def update_hair_transparency(self, context, hair_trans):
        """Update hair transparency in materials."""
        modified_count = 0
        for slot in context.active_object.material_slots:
            if (
                slot.material
                and slot.material.use_nodes
                and slot.material.name.startswith("WW - ")
            ):
                for node in slot.material.node_tree.nodes:
                    if (
                        node.type == "GROUP"
                        and node.node_tree
                        and "See Through" in node.node_tree.name
                    ):
                        node.mute = not hair_trans
                        modified_count += 1
        return modified_count


class WW_OT_FixEyeUV(Operator):
    """Fix eye UV mapping."""

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
        """Fix UV map settings for eye materials."""
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
    """Separate mesh by vertex groups."""

    bl_idname = "shader.separate_by_vertex_group"
    bl_label = "Separate Mesh"
    bl_description = "Separate mesh into parts based on vertex groups"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        objects = [obj for obj in context.selected_objects if obj.type == "MESH"]
        if not objects:
            self.report(
                {"ERROR"}, "Please select one or more mesh objects to separate."
            )
            return {"CANCELLED"}

        total_separated = 0
        for obj in objects:
            parts_separated = self.separate_mesh(obj)
            total_separated += parts_separated

        self.report(
            {"INFO"}, f"Mesh separation complete. {total_separated} parts separated."
        )
        return {"FINISHED"}

    def separate_mesh(self, obj: bpy.types.Object):
        """Separate a mesh by vertex groups."""
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
        """Deselect all vertices in a mesh."""
        for v in obj.data.vertices:
            v.select = False

    def select_vertices_by_group(self, obj: bpy.types.Object, pattern: str):
        """Select vertices that belong to groups matching the pattern."""
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
        """Remove unused vertex groups from objects."""
        bpy.ops.object.select_all(action="DESELECT")
        obj.select_set(True)
        bpy.context.view_layer.objects.active = obj
        for o in [obj] + separated_objects:
            self.remove_unused_vertex_groups(o)

    def remove_unused_vertex_groups(self, obj: bpy.types.Object):
        """Remove vertex groups that have no vertices assigned."""
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


class WW_OT_SetOptimize(Operator):
    """Optimize armature and bones."""

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
        """Organize bones into collections by type."""
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
        """Get the appropriate collection for a bone based on its name."""
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
        """Assign a bone to a collection, removing it from others."""
        for col in bone_cols:
            if bone.name in col.bones:
                col.unassign(bone)
        collection.assign(bone)


class WW_OT_FixNPCMats(Operator):
    """Fix NPC material names."""

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


class VIEW3D_PT_WutheringWaves(Panel):
    """Main Wuthering Waves panel."""

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
    """Appearance panel for Wuthering Waves."""

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
    """Lighting panel for Wuthering Waves."""

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
    """Tools panel for Wuthering Waves."""

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
