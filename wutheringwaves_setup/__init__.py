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

from .utils import (
    TEXTURE_TYPE_MAPPINGS,
    LIGHT_MODES,
    get_armature_from_modifiers,
    load_image,
    split_material_name,
    MaterialDetails,
    MaterialTextureData,
    TextureSearchParameters,
    apply_textures,
    make_texture_patterns,
    logger,
    get_mesh_data,
    set_material_view,
    set_solid_view,
    find_texture,
    set_texture,
    set_node_input,
    darken_eye_colors,
    get_suffix,
    extract_character_name,
)
from .import_shader import WW_OT_ImportShader, WW_OT_ImportTextures
from .rigify import WW_OT_Rigify
from .create_face_panel import WW_OT_CreateFacePanel, WW_OT_ImportFacePanel, WW_OT_SetupHeadDriver
from .run_entire_setup import WW_OT_RunEntireSetup

bl_info = {
    "name": "WuWa Character Setup",
    "author": "Akatsuki, fnoji",
    "version": (1, 4, 1),
    "blender": (4, 1, 0),
    "location": "View3D > UI > Wuthering Waves",
    "description": "Import & Setup Wuthering Waves characters",
    "support": "COMMUNITY",
    "warning": "",
    "doc_url": "https://github.com/fnoji/Blender-WuWa-Character-Setup",
    "tracker_url": "https://github.com/fnoji/Blender-WuWa-Character-Setup",
    "category": "Wuthering Waves",
    "license": "GPL-3.0-or-later",
}



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
    Scene.ww_setup_status = StringProperty(default="IDLE")
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


class WW_OT_ImportUEModel(Operator):
    bl_idname = "shader.import_uemodel"
    bl_label = "Import Model"
    bl_description = "Import Model using UEFormat (requires UEFormat addon)"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        try:
            settings = context.scene.uf_settings
            
            # Set properties individually
            settings.import_collision = False
            settings.import_morph_targets = True
            settings.import_sockets = True
            settings.import_virtual_bones = False
            settings.reorient_bones = True
            settings.bone_length = 4.0
            
            # Call operator without arguments
            bpy.ops.uf.import_uemodel("INVOKE_DEFAULT")
            
            # Register delayed rename to run after import (attempting to catch it)
            bpy.app.timers.register(self.delayed_rename, first_interval=1.0)
            
        except AttributeError:
            self.report({"ERROR"}, "UEFormat addon (uf_settings) not found.")
            return {"CANCELLED"}
        except Exception as e:
            self.report({"ERROR"}, f"Error importing model: {str(e)}")
            return {"CANCELLED"}
        
        return {"FINISHED"}

    def delayed_rename(self):
        # Check selected objects for the pattern
        renamed_count = 0
        for obj in bpy.context.selected_objects:
            new_name = extract_character_name(obj.name)
            if new_name != obj.name:
                obj.name = new_name
                if obj.data:
                    obj.data.name = new_name
                renamed_count += 1
                logger.info(f"Renamed object to {new_name}")
        
        if renamed_count > 0:
            return None # Stop timer
        return 1.0 # Retry checking (simple heuristic, might need improvement)



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

        row = layout.row()
        row.scale_y = 1.5
        row.operator("shader.run_entire_setup", text="Run Entire Setup", icon="PLAY")

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
        col.operator("shader.import_uemodel", icon="IMPORT")
        col.operator("shader.import_shader", icon="IMPORT")
        col.operator("shader.rigify_armature", icon="BONE_DATA")
        col.operator("shader.setup_head_driver", icon="DRIVER")

        box = layout.box()
        row = box.row()
        row.label(text="Face Rig", icon="FACESEL")

        col = box.column(align=True)
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

        # box = layout.box()
        # row = box.row()
        # row.label(text="Armature Tools", icon="ARMATURE_DATA")

        # col = box.column(align=True)
        # col.operator("shader.set_optimize", icon="OUTLINER_OB_ARMATURE")


classes = [
    MeshTextureData,
    WW_OT_RunEntireSetup,
    WW_OT_ImportUEModel,
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
