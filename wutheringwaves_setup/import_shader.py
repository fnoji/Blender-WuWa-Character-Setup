import bpy
import logging
import os
import re
from typing import Dict, List, Any
from bpy.props import StringProperty, CollectionProperty
from bpy.types import Operator
from bpy_extras.io_utils import ImportHelper
from mathutils import Vector

from .utils import (
    logger,
    get_suffix,
    get_armature_from_modifiers,
    load_image,
    find_texture,
    set_texture,
    set_node_input,
    darken_eye_colors,
    split_material_name,
    get_mesh_data,
    set_solid_view,
    set_material_view,
    TEXTURE_TYPE_MAPPINGS,
    TextureSearchParameters,
    MaterialDetails,
    MaterialTextureData,
    make_texture_patterns,
    apply_textures,
    extract_character_name,
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
            head_bone = "head"
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
        unique_name = f"{shader_name} {extract_character_name(mesh_name)}"
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
        context.scene.ww_setup_status = "TEXTURES_DONE"
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
