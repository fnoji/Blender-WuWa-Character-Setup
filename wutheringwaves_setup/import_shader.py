import bpy
import logging
import os
import re
from typing import Dict, List, Any
from bpy.props import StringProperty, CollectionProperty, BoolProperty
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
    SHADER_TYPE_JAREDNYTS,
    SHADER_TYPE_JONN,
    get_texture_mappings,
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


def import_node_groups(path: str, shader_type: str = SHADER_TYPE_JAREDNYTS):
    """Import node groups and objects based on shader type."""
    if shader_type == SHADER_TYPE_JONN:
        # Jonn Gathering Wives shader
        node_trees = [
            "[GW] Vectors",
            "[GW] Outlines",
            "Gathering Wives [Body]",
            "Gathering Wives [Face]",
            "Gathering Wives [Eye]",
            "Gathering Wives [Hair]",
            "Gathering Wives [Shader]",
        ]
        objects = {
            "Main Light": False,
            "Head Controller": False,
        }
    else:
        # JaredNyts shader (default)
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

def init_modifiers(shader_type: str = SHADER_TYPE_JAREDNYTS):
    """Initialize modifiers based on shader type."""
    ctx = bpy.context
    if not ctx.active_object or ctx.active_object.type != "MESH":
        return

    mesh_name = ctx.active_object.name.split(".")[0]
    suffix = get_suffix()
    
    if shader_type == SHADER_TYPE_JONN:
        # Jonn Gathering Wives modifiers
        setup_controls_jonn(ctx, mesh_name)
        set_modifiers_jonn(ctx, mesh_name)
    else:
        # JaredNyts modifiers (default)
        setup_controls(ctx, mesh_name, suffix)
        set_modifiers(ctx, mesh_name, suffix)
        add_head_lock(mesh_name)
        apply_head_lock()
    
    logger.info(f"Initialized modifiers for {mesh_name} (shader: {shader_type})")


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
                if i < len(materials):
                    source_mat = materials[i]

                    part_match = re.search(r"WW - ([A-Za-z0-9]+)", source_mat.name)
                    part_name = part_match.group(1) if part_match else "Main"

                    outline_mat_name = f"WW - Outlines {part_name} {mesh_name}"
                    outline_mat = (
                        bpy.data.materials.get(outline_mat_name)
                        or bpy.data.materials.get("WW - Outlines").copy()
                    )
                    outline_mat.name = outline_mat_name

                    modifier[f"Input_{mask}"] = source_mat
                    modifier[f"Input_{mat}"] = outline_mat
                else:
                    modifier[f"Input_{mask}"] = None
                    modifier[f"Input_{mat}"] = None
            modifier.show_viewport = ctx.scene.outlines_enabled

        elif base_name == "ResonatorStar Move":
            if circle := bpy.data.objects.get("Circle"):
                modifier["Input_2"] = circle
            modifier["Output_3_attribute_name"] = "move"

            data = get_mesh_data(ctx, mesh_name)
            modifier.show_viewport = data.star_move

        logger.info(f"Set up {base_name} modifier for {mesh_name}")


def setup_controls_jonn(ctx, mesh_name: str):
    """Setup control objects for Jonn Gathering Wives shader.
    Duplicates Main Light and Head Controller for each character."""
    control_objects = ["Main Light", "Head Controller"]
    
    # Determine if we need new control objects
    # Check how many [GW] Vectors node groups exist (one per character)
    # OR count existing Main Light objects that were already set up
    existing_gw_vectors = [ng for ng in bpy.data.node_groups if ng.name.startswith("[GW] Vectors ")]
    
    # If no character-specific vectors exist yet, this is the 1st character
    # If 1+ exist, this is the 2nd+ character and needs new suffixed controls
    if len(existing_gw_vectors) == 0:
        # First character - use original objects (no suffix)
        suffix = ""
    else:
        # 2nd+ character - create new suffixed copies
        # Suffix is .001 for 2nd, .002 for 3rd, etc.
        suffix = f".{len(existing_gw_vectors):03d}"
    
    # Duplicate control objects for this character if needed (2nd+ characters)
    if suffix:
        for obj_name in control_objects:
            suffixed_name = obj_name + suffix
            if suffixed_name not in bpy.data.objects and obj_name in bpy.data.objects:
                orig = bpy.data.objects[obj_name]
                new_obj = orig.copy()
                new_obj.name = suffixed_name
                new_obj.location = orig.location.copy()
                new_obj.rotation_euler = orig.rotation_euler.copy()
                new_obj.scale = orig.scale.copy()
                bpy.context.collection.objects.link(new_obj)
                logger.info(f"Created control object: {new_obj.name}")
    
    # Head Controller constraint setup
    head_controller = bpy.data.objects.get(f"Head Controller{suffix}")
    mesh = bpy.data.objects.get(mesh_name)
    armature = get_armature_from_modifiers(mesh) if mesh else None
    
    if not head_controller or not armature:
        logger.warning(f"Head Controller{suffix} or armature not found for Jonn setup")
        return
    
    # Find head bone (try common names)
    head_bone = None
    for bone_name in ["Bip001Head", "c_head.x", "Head"]:
        if bone_name in armature.data.bones:
            head_bone = bone_name
            break
    
    if not head_bone:
        logger.warning(f"Head bone not found in armature {armature.name}")
        return
    
    # Use existing Child Of constraint OR create new one
    # First look for existing "Child Of" (from blend file import)
    constraint = head_controller.constraints.get("Child Of")
    if not constraint:
        # Try "Head Lock" (if we created it before)
        constraint = head_controller.constraints.get("Head Lock")
    if not constraint:
        # Create new constraint
        constraint = head_controller.constraints.new("CHILD_OF")
    
    # Ensure constraint name and settings
    constraint.name = "Child Of"  # Keep original name to avoid duplicates
    constraint.target = armature
    constraint.subtarget = head_bone
    
    # Set Inverse
    ctx.view_layer.update()
    matrix = constraint.target.matrix_world @ constraint.target.pose.bones[head_bone].matrix
    constraint.inverse_matrix = matrix.inverted()
    
    logger.info(f"Set up Head Controller constraint for {mesh_name}")


def set_modifiers_jonn(ctx, mesh_name: str):
    """Set up Jonn Gathering Wives specific modifiers."""
    mesh_obj = ctx.active_object
    
    # Calculate suffix based on existing [GW] Vectors node groups
    # This is called AFTER setup_controls_jonn, so new control objects should exist
    # We need to find WHICH suffix was used for this character's controls
    existing_gw_vectors = [ng for ng in bpy.data.node_groups 
                          if ng.name.startswith("[GW] Vectors ") and ng.name != f"[GW] Vectors {mesh_name}"]
    
    # If no OTHER character-specific vectors exist, this is the 1st character (no suffix)
    # If 1+ OTHER exist, this character uses suffixed controls
    if len(existing_gw_vectors) == 0:
        suffix = ""
    else:
        suffix = f".{len(existing_gw_vectors):03d}"
    
    # LIGHT VECTOR modifier
    gw_vectors_group = bpy.data.node_groups.get("[GW] Vectors")
    if gw_vectors_group:
        new_group_name = f"[GW] Vectors {mesh_name}"
        new_group = bpy.data.node_groups.get(new_group_name) or gw_vectors_group.copy()
        new_group.name = new_group_name
        
        modifier = mesh_obj.modifiers.get(new_group_name) or mesh_obj.modifiers.new(new_group_name, "NODES")
        modifier.name = "LIGHT VECTOR"
        modifier.node_group = new_group
        
        # Set inputs - use suffixed object names
        if main_light := bpy.data.objects.get(f"Main Light{suffix}"):
            modifier["Input_2"] = main_light
        if head_controller := bpy.data.objects.get(f"Head Controller{suffix}"):
            modifier["Socket_1"] = head_controller
        
        # Set attribute names
        modifier["Output_6_attribute_name"] = "lightDirection"
        modifier["Output_7_attribute_name"] = "headForward"
        modifier["Output_8_attribute_name"] = "headUp"
        
        logger.info(f"Set up [GW] Vectors modifier for {mesh_name} (suffix: {suffix})")
    
    # OUTLINE modifier
    gw_outlines_group = bpy.data.node_groups.get("[GW] Outlines")
    if gw_outlines_group:
        new_group_name = f"[GW] Outlines {mesh_name}"
        new_group = bpy.data.node_groups.get(new_group_name) or gw_outlines_group.copy()
        new_group.name = new_group_name
        
        modifier = mesh_obj.modifiers.get(new_group_name) or mesh_obj.modifiers.new(new_group_name, "NODES")
        modifier.name = "OUTLINE"
        modifier.node_group = new_group
        
        # Find Face and Eye materials
        face_mat = None
        eye_mat = None
        for slot in mesh_obj.material_slots:
            if slot.material:
                if "Face" in slot.material.name and not face_mat:
                    face_mat = slot.material
                elif "Eye" in slot.material.name and not eye_mat:
                    eye_mat = slot.material
        
        # Set inputs
        if face_mat:
            modifier["Socket_1"] = face_mat
        if eye_mat:
            modifier["Socket_2"] = eye_mat
        modifier["Input_2_use_attribute"] = True
        modifier["Input_2_attribute_name"] = "COL0"
        modifier["Input_5"] = 0.1
        
        modifier.show_viewport = ctx.scene.outlines_enabled
        
        logger.info(f"Set up [GW] Outlines modifier for {mesh_name}")


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
    # bpy.ops.object.select_all(action="DESELECT")
    for obj in bpy.context.selected_objects:
        obj.select_set(False)
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
        # bpy.ops.object.select_all(action="DESELECT")
        for obj in bpy.context.selected_objects:
            obj.select_set(False)
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


def create_seethru_mesh(context, original_object, shader_filepath: str):
    """
    Create a SEETHRU mesh by duplicating only Face/Eye material mesh parts.
    This replicates the manual workflow: Edit Mode -> Select Face/Eye faces -> Shift+D -> P
    
    ONLY creates SEETHRU for Jonn shader (detected by LIGHT VECTOR modifier).
    JaredNyts shader does not need SEETHRU mesh.
    
    Args:
        context: Blender context
        original_object: The mesh object to process
        shader_filepath: Path to the shader .blend file for importing SEETHRU materials
        
    Returns:
        The newly created SEETHRU object, or None if no Face/Eye materials found or not Jonn shader
    """
    if not original_object or original_object.type != 'MESH':
        return None
    
    # Check if this is Jonn shader (has LIGHT VECTOR modifier)
    # JaredNyts uses "Light Vectors {name}" modifier instead
    is_jonn_shader = any(mod.name == "LIGHT VECTOR" for mod in original_object.modifiers)
    if not is_jonn_shader:
        logger.info("Not Jonn shader (no LIGHT VECTOR modifier) - skipping SEETHRU mesh creation")
        return None
    
    # 1. Find Face/Eye material slot indices and store references
    face_eye_indices = []
    face_materials = {}  # slot_index -> material
    eye_materials = {}   # slot_index -> material
    
    for i, slot in enumerate(original_object.material_slots):
        if slot.material:
            mat_name = slot.material.name
            # Check for WW - Face or WW - Eye materials
            if "Face" in mat_name and "WW -" in mat_name and "_SEETHRU" not in mat_name:
                face_eye_indices.append(i)
                face_materials[i] = slot.material
                logger.info(f"Found Face material at slot {i}: {mat_name}")
            elif "Eye" in mat_name and "WW -" in mat_name and "_SEETHRU" not in mat_name:
                face_eye_indices.append(i)
                eye_materials[i] = slot.material
                logger.info(f"Found Eye material at slot {i}: {mat_name}")
    
    if not face_eye_indices:
        logger.info("No Face/Eye materials found, skipping SEETHRU mesh creation")
        return None
    
    # Get armature from original object for later parenting
    armature = get_armature_from_modifiers(original_object)
    if armature:
        logger.info(f"Found armature for parenting: {armature.name} (type: {armature.type})")
    else:
        logger.warning("No armature found for SEETHRU mesh parenting")
    
    # Track existing mesh objects BEFORE the operation
    existing_mesh_objects = {obj.name for obj in bpy.data.objects if obj.type == 'MESH'}
    logger.info(f"Existing mesh objects before operation: {len(existing_mesh_objects)}")
    
    # Deselect ALL objects first to ensure clean state
    for obj in bpy.data.objects:
        obj.select_set(False)
    
    # Ensure we're working with the correct object
    context.view_layer.objects.active = original_object
    original_object.select_set(True)
    
    # 2. Switch to Object mode to manipulate polygon selection
    if context.object.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
    
    # 3. Select polygons with Face/Eye materials
    mesh = original_object.data
    
    # Deselect all mesh elements first
    for poly in mesh.polygons:
        poly.select = False
    for edge in mesh.edges:
        edge.select = False
    for vert in mesh.vertices:
        vert.select = False
    
    # Select Face/Eye polygons
    selected_count = 0
    for poly in mesh.polygons:
        if poly.material_index in face_eye_indices:
            poly.select = True
            selected_count += 1
    
    logger.info(f"Selected {selected_count} polygons with Face/Eye materials")
    
    if selected_count == 0:
        logger.warning("No polygons selected for SEETHRU mesh")
        return None
    
    # 4. Enter Edit mode, duplicate and separate
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.duplicate()
    bpy.ops.mesh.separate(type='SELECTED')
    bpy.ops.object.mode_set(mode='OBJECT')
    
    # 5. Find the newly created object by comparing with existing objects
    seethru_object = None
    for obj in bpy.data.objects:
        if obj.type == 'MESH' and obj.name not in existing_mesh_objects:
            seethru_object = obj
            logger.info(f"Found new mesh object: {obj.name}")
            break
    
    if not seethru_object:
        logger.error("Failed to create SEETHRU mesh - no new object found")
        return None
    
    # 6. Rename the SEETHRU object
    seethru_name = original_object.name + "_SEETHRU"
    seethru_object.name = seethru_name
    logger.info(f"Renamed SEETHRU mesh to: {seethru_object.name}")
    
    # 7. Import SEETHRU materials from shader .blend file if not already present
    seethru_mat_names = ["WW - Face_SEETHRU", "WW - Eye_SEETHRU"]
    if shader_filepath and os.path.exists(shader_filepath):
        existing_mats = {mat.name for mat in bpy.data.materials}
        mats_to_import = [name for name in seethru_mat_names if name not in existing_mats]
        
        if mats_to_import:
            try:
                with bpy.data.libraries.load(shader_filepath) as (data_from, data_to):
                    data_to.materials = [
                        mat_name for mat_name in data_from.materials
                        if mat_name in mats_to_import
                    ]
                logger.info(f"Imported SEETHRU materials: {[m.name for m in data_to.materials]}")
            except Exception as e:
                logger.warning(f"Failed to import SEETHRU materials: {str(e)}")
    
    # 8. Replace materials with SEETHRU variants and copy textures
    for slot in seethru_object.material_slots:
        if not slot.material:
            continue
        
        mat_name = slot.material.name
        original_mat = slot.material  # Reference to original Face/Eye material
        
        # Check if this is a Face material
        if "Face" in mat_name and "WW -" in mat_name and "_SEETHRU" not in mat_name:
            seethru_mat = bpy.data.materials.get("WW - Face_SEETHRU")
            
            if seethru_mat:
                # Copy to create character-specific version
                char_seethru_name = f"WW - Face_SEETHRU {extract_character_name(original_object.name)}"
                char_seethru_mat = bpy.data.materials.get(char_seethru_name)
                if not char_seethru_mat:
                    char_seethru_mat = seethru_mat.copy()
                    char_seethru_mat.name = char_seethru_name
                
                # Copy textures from original Face material
                copy_textures_between_materials(original_mat, char_seethru_mat)
                slot.material = char_seethru_mat
                logger.info(f"Applied SEETHRU material: {char_seethru_name}")
            else:
                logger.warning("WW - Face_SEETHRU not found in materials")
            
        # Check if this is an Eye material
        elif "Eye" in mat_name and "WW -" in mat_name and "_SEETHRU" not in mat_name:
            seethru_mat = bpy.data.materials.get("WW - Eye_SEETHRU")
            
            if seethru_mat:
                # Copy to create character-specific version
                char_seethru_name = f"WW - Eye_SEETHRU {extract_character_name(original_object.name)}"
                char_seethru_mat = bpy.data.materials.get(char_seethru_name)
                if not char_seethru_mat:
                    char_seethru_mat = seethru_mat.copy()
                    char_seethru_mat.name = char_seethru_name
                
                # Copy textures from original Eye material
                copy_textures_between_materials(original_mat, char_seethru_mat)
                slot.material = char_seethru_mat
                logger.info(f"Applied SEETHRU material: {char_seethru_name}")
            else:
                logger.warning("WW - Eye_SEETHRU not found in materials")
    
    # 8.5. Clean up material slots - remove all non-SEETHRU materials
    # Go through slots in reverse order to avoid index issues when removing
    bpy.context.view_layer.objects.active = seethru_object
    seethru_object.select_set(True)
    
    slots_to_remove = []
    for i, slot in enumerate(seethru_object.material_slots):
        if slot.material:
            if "_SEETHRU" not in slot.material.name:
                slots_to_remove.append(i)
        else:
            slots_to_remove.append(i)  # Remove empty slots too
    
    # Remove slots in reverse order to maintain correct indices
    for i in reversed(slots_to_remove):
        seethru_object.active_material_index = i
        bpy.ops.object.material_slot_remove()
    
    logger.info(f"Cleaned up material slots - kept {len(seethru_object.material_slots)} SEETHRU materials")
    
    # 9. Parent SEETHRU mesh to armature and add Armature modifier
    if armature:
        logger.info(f"Setting up parenting for {seethru_object.name} to {armature.name}")
        
        # Set parent directly via data (more reliable than operator)
        seethru_object.parent = armature
        seethru_object.matrix_parent_inverse = armature.matrix_world.inverted()
        
        # Verify parent was set
        if seethru_object.parent == armature:
            logger.info(f"SUCCESS: Parented {seethru_object.name} to {armature.name}")
        else:
            logger.error(f"FAILED: Parent is {seethru_object.parent}, expected {armature.name}")
        
        # Find and update existing Armature modifier (duplicated mesh has one already)
        armature_mod = None
        for mod in seethru_object.modifiers:
            if mod.type == 'ARMATURE':
                armature_mod = mod
                logger.info(f"Found existing Armature modifier: {mod.name}, current object: {mod.object}")
                break
        
        if not armature_mod:
            armature_mod = seethru_object.modifiers.new(name="Armature", type='ARMATURE')
            logger.info(f"Created new Armature modifier")
        
        armature_mod.object = armature
        armature_mod.use_vertex_groups = True
        
        # Verify modifier was set
        if armature_mod.object == armature:
            logger.info(f"SUCCESS: Armature modifier object set to {armature.name}")
        else:
            logger.error(f"FAILED: Armature modifier object is {armature_mod.object}")
        
        # Move SEETHRU into the same collection as original_object
        original_collections = list(original_object.users_collection)
        if original_collections:
            target_collection = original_collections[0]
            logger.info(f"Moving SEETHRU to collection: {target_collection.name}")
            # Unlink from current collections
            for coll in list(seethru_object.users_collection):
                if seethru_object.name in coll.objects:
                    coll.objects.unlink(seethru_object)
            # Link to original object's collection
            if seethru_object.name not in target_collection.objects:
                target_collection.objects.link(seethru_object)
            logger.info(f"Moved {seethru_object.name} to collection {target_collection.name}")
    else:
        logger.error("Armature is None - cannot parent SEETHRU mesh")
    
    # 10. Restore original object as active and deselect all
    for obj in bpy.data.objects:
        obj.select_set(False)
    
    original_object.select_set(True)
    context.view_layer.objects.active = original_object
    
    logger.info(f"SEETHRU mesh creation complete: {seethru_object.name}")
    return seethru_object


def copy_textures_between_materials(source_mat, target_mat):
    """
    Copy texture images from source material to target material.
    Matches nodes by name (e.g., 'Face Diffuse', 'Eye HET', etc.)
    """
    if not source_mat or not target_mat:
        return
    
    if not source_mat.use_nodes or not target_mat.use_nodes:
        return
    
    source_nodes = source_mat.node_tree.nodes
    target_nodes = target_mat.node_tree.nodes
    
    copied_count = 0
    for source_node in source_nodes:
        if source_node.type == 'TEX_IMAGE' and source_node.image:
            # Find matching node in target by name
            target_node = target_nodes.get(source_node.name)
            if target_node and target_node.type == 'TEX_IMAGE':
                target_node.image = source_node.image
                copied_count += 1
                logger.info(f"Copied texture '{source_node.image.name}' to node '{source_node.name}'")
    
    logger.info(f"Copied {copied_count} textures from {source_mat.name} to {target_mat.name}")


class WW_OT_ImportShader(Operator, ImportHelper):
    bl_idname = "shader.import_shader"
    bl_label = "Import Shader"
    bl_description = "Import and apply WW shaders to the selected mesh"
    bl_options = {"REGISTER", "UNDO"}
    filename_ext = ".blend"
    filter_glob: StringProperty(default="*.blend", options={"HIDDEN"})

    is_auto_run: BoolProperty(default=False, options={'HIDDEN'})

    def invoke(self, context, event):
        if hasattr(context.scene, "shader_file_path") and os.path.exists(
            context.scene.shader_file_path
        ):
            self.filepath = context.scene.shader_file_path
            return self.execute(context)
        return ImportHelper.invoke(self, context, event)

    def execute(self, context):
        if not self.is_auto_run:
            context.scene.ww_setup_status = "MANUAL_INTERRUPTION"

        if not self.validate_context(context):
            return {"CANCELLED"}

        active_obj = context.active_object
        mesh_name = active_obj.name.split(".")[0]
        has_shader = self.check_if_has_shader(context)
        shader_type = context.scene.shader_type  # Read shader type from scene

        logger.info(f"Starting shader import process for mesh: {mesh_name} (shader: {shader_type})")
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
                if not self.import_materials(context, shader_type):
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
                import_node_groups(self.filepath, shader_type)
                logger.info("Node groups imported")

            self.process_materials(context)
            context.scene.original_materials = str(orig_mats)
            logger.info("Original materials saved to scene")
            darken_eye_colors(context.active_object)
            logger.info("Eye colors adjusted")
            init_modifiers(shader_type)
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

    def import_materials(self, context, shader_type: str = SHADER_TYPE_JAREDNYTS):
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
            import_node_groups(self.filepath, shader_type)
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
        
        # NH Logic Check
        # Object name might be renamed (NH_ stripped), so check materials too
        is_nh = "NH_" in context.active_object.name or any("MI_NH_" in s.material.name for s in context.active_object.material_slots if s.material)
        has_bangs = any(
            slot.material and ("Bang" in slot.material.name or "Bangs" in slot.material.name)
            for slot in context.active_object.material_slots
        )

        logger.info(f"Processing materials for {mesh_name} (NH: {is_nh}, Has Bangs: {has_bangs})")
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

                source_override = None
                # Special logic for NH Hair without Bangs -> Use Main Shader for Hair
                if is_nh and not has_bangs and "Hair" in target_shader:
                    source_override = "WW - Main"
                    logger.info(f"NH Character without Bangs detected. Using WW - Main for {target_shader}")

                new_material = self.duplicate_material(
                    target_shader, mesh_name, source_override)
                
                # Lock original material
                # slot.material.use_fake_user = True
                # self.setup_original_material(context, slot.material)
                
                slot.material = new_material
                set_star_shader(new_material, mat_name, stars)
                processed_count += 1
            except Exception as e:
                self.report(
                    {"INFO"}, f"Error processing material {mat_name}: {str(e)}")
                logger.error(f"Error processing material {mat_name}: {str(e)}")

        logger.info(f"Processed {processed_count} materials")

    def setup_original_material(self, context, material: bpy.types.Material):
        """Ensure original material has a simple texture setup for Animate Mode"""
        if not material.use_nodes:
            material.use_nodes = True
        
        target_node = None
        output_node = None
        
        # 1. Try to find existing connected Image Texture
        if material.node_tree:
            for node in material.node_tree.nodes:
                if node.type == 'OUTPUT_MATERIAL':
                    output_node = node
                    if node.inputs['Surface'].is_linked:
                        link = node.inputs['Surface'].links[0]
                        if link.from_node.type == 'TEX_IMAGE':
                            target_node = link.from_node
                            break
        
        # 2. If found, ensure it is named correctly for apply_textures
        if target_node:
            logger.info(f"Normalizing existing node in {material.name} to 'Base Color'")
            target_node.name = "Base Color"
            target_node.label = "Base Color"
        else:
            # 3. If not found (or complex setup), rebuild simplest graph
            logger.info(f"Rebuilding simple graph for {material.name}")
            material.node_tree.nodes.clear()
            output_node = material.node_tree.nodes.new(type='ShaderNodeOutputMaterial')
            output_node.location = (300, 0)
            target_node = material.node_tree.nodes.new(type='ShaderNodeTexImage')
            target_node.location = (0, 0)
            target_node.name = "Base Color"
            target_node.label = "Base Color"
            material.node_tree.links.new(target_node.outputs['Color'], output_node.inputs['Surface'])

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

    def duplicate_material(self, shader_name: str, mesh_name: str, source_override: str = None):
        unique_name = f"{shader_name} {extract_character_name(mesh_name)}"
        if unique_name in bpy.data.materials:
            return bpy.data.materials[unique_name]
        
        source_name = source_override if source_override else shader_name

        if source_name in bpy.data.materials:
            material = bpy.data.materials[source_name].copy()
        elif base_match := re.match(r"WW - ([A-Za-z0-9]+)", shader_name): # fallback to shader_name base if source not found? or source base?
             # If source override matches existing, line above catches it.
             # If shader_name is derived (WW - Hair ...), try getting base from it?
             # Assuming standard logic for "WW - Main" fallback.
            base_name = base_match.group(0)
            material = bpy.data.materials.get(
                base_name, bpy.data.materials.get("WW - Main")
            ).copy()
        else:
            material = bpy.data.materials["WW - Main"].copy()

        material.name = unique_name
        # Lock character specific material
        material.use_fake_user = True
        
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
        self.assign_textures(context)
        
        return {"FINISHED"}
    
    def setup_original_material_helper(self, context, material: bpy.types.Material):
        """Helper to ensure original material has a simple texture setup"""
        if not material.use_nodes:
            material.use_nodes = True
        
        target_node = None
        
        if material.node_tree:
            for node in material.node_tree.nodes:
                if node.type == 'OUTPUT_MATERIAL':
                    if node.inputs['Surface'].is_linked:
                        link = node.inputs['Surface'].links[0]
                        if link.from_node.type == 'TEX_IMAGE':
                            target_node = link.from_node
                            break
        
        if target_node:
            target_node.name = "Base Color"
            target_node.label = "Base Color"
        else:
            material.node_tree.nodes.clear()
            output_node = material.node_tree.nodes.new(type='ShaderNodeOutputMaterial')
            output_node.location = (300, 0)
            target_node = material.node_tree.nodes.new(type='ShaderNodeTexImage')
            target_node.location = (0, 0)
            target_node.name = "Base Color"
            target_node.label = "Base Color"
            material.node_tree.links.new(target_node.outputs['Color'], output_node.inputs['Surface'])

    def assign_textures(self, context):
        active_obj = context.active_object
        mesh_name = active_obj.name.split(".")[0]
        data = get_mesh_data(context, mesh_name)
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
        has_het_anywhere = False
        assigned_count = 0
        for slot in active_obj.material_slots:
            if not slot.material or not slot.material.use_nodes:
                continue

            mat_name = slot.material.name
            base = ""
            version = ""
            original_name = ""

            # Check for WW materials
            if match := re.search(r"WW - ([A-Za-z0-9]+)(_?\d+|(?:_[^_]+)*)?", mat_name):
                base, version = match.group(1), match.group(2) or ""
                original_name = self.get_original_material_name(context, base, version)
            
            # Check for MI materials (Animate Mode)
            elif mat_name.startswith("MI_"):
                base, version = split_material_name(mat_name)
                # For MI materials, the "original name" IS the material name
                original_name = mat_name
                # Ensure node setup exists
                self.setup_original_material_helper(context, slot.material)

            if base:
                logger.info(
                    f"Processing material: {mat_name} (base: {base}, version: {version})"
                )

                # For finding textures, we use the original name logic if possible, 
                # or just the components.
                # If we are in MI_ mode, extracting "original material name" via get_original_material_name is tricky 
                # because that function expects WW naming conventions usually.
                # But for MI_, we just want to load its textures.
                
                # Material Details wrapper
                material_info = MaterialDetails(base, version, original_name)
                # Use dynamic texture mappings based on shader type
                texture_mappings = get_texture_mappings(context.scene.shader_type)
                mat_tex_data = MaterialTextureData(
                    slot.material,
                    material_info,
                    texture_mappings,
                    self.files,
                    self.directory,
                    data.tex_mode,
                    context.scene.shader_type,
                )

                apply_textures(mat_tex_data)
                assigned_count += 1
                logger.info(
                    f"Applied textures to material: {slot.material.name}")
                
                # Apply textures to Original Material (MI_) for Animate Mode logic REMOVED
                # because new Animate Mode uses _Low generated from WW materials.
                pass

                if slot.material.use_nodes and any(
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

        # Create SEETHRU mesh AFTER textures are applied (so textures can be copied)
        shader_filepath = context.scene.shader_file_path if hasattr(context.scene, "shader_file_path") else None
        seethru_obj = create_seethru_mesh(context, active_obj, shader_filepath)
        if seethru_obj:
            logger.info(f"SEETHRU mesh created with textures: {seethru_obj.name}")

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
            loaded_image = load_image(file_path, context.scene.shader_type)
            if loaded_image:
                imported_files.append(file.name)
            else:
                logger.warning(f"Failed to load texture: {file.name}")

        mesh_name = context.active_object.name.split(".")[0]
        data = get_mesh_data(context, mesh_name)
        data.textures = ",".join(imported_files)
        logger.info(f"Imported {len(imported_files)} textures for {mesh_name}")
        logger.info(f"Texture list: {data.textures}")



    def get_original_material_name(self, context, base: str, version: str):
        """Find original MI_ material in bpy.data.materials (not slots, as slots may have WW materials)"""
        # Extract character name from active object to match only relevant materials
        mesh_name = context.active_object.name.split(".")[0]
        char_name = extract_character_name(mesh_name)
        
        for mat in bpy.data.materials:
            if not mat.name.startswith("MI_"):
                continue
            # Check if this material belongs to the same character
            if char_name and char_name.lower() not in mat.name.lower():
                continue
            # Check if base part matches (case insensitive, handle Bangs/Bang variants)
            mat_base, mat_version = split_material_name(mat.name)
            if mat_base.lower() == base.lower() or \
               (mat_base.lower() == "bang" and base.lower() == "bangs") or \
               (mat_base.lower() == "bangs" and base.lower() == "bang"):
                return mat.name
        return None
