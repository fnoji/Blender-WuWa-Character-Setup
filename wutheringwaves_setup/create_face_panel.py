import bpy
import math
import mathutils
import bmesh
import os
import re
from typing import Set
from collections import defaultdict, deque

from bpy.types import Operator
from bpy.props import StringProperty
from bpy_extras.io_utils import ImportHelper
from mathutils import Vector

from .utils import get_armature_from_modifiers, logger

preserved_shape_keys = {"Pupil_Up", "Pupil_Down",
                        "Pupil_R", "Pupil_L", "Pupil_Scale"}


def delete_shape_key_drivers(mesh, preserved_shape_keys):
    if mesh.data.shape_keys:
        for key_block in mesh.data.shape_keys.key_blocks:
            if key_block.name not in preserved_shape_keys:
                key_block.driver_remove('value')


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
        head_bone_names = ["c_head.x", "Bip001Head", "head"]
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
                    self.report({'ERROR'}, "Bone 'EyeTracker' not found.")
                    return {'CANCELLED'}
                eye_tracker_pos = eye_tracker_bone.head.copy()
                face_panel_root = edit_bones.new("FacePanelRoot")
                face_panel_root.head = eye_tracker_pos
                face_panel_root.tail = eye_tracker_pos + \
                    mathutils.Vector((0.0, 0.0, 0.02))
                face_panel_root.use_connect = False
                parent_bone = edit_bones.get("ORG-head")
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
                    {'INFO'}, "Face panel created and drivers set up drivers.")
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
            head_pos = self.get_bone_world_position(armature, "Bip001Head")
            if not head_pos:
                head_pos = self.get_bone_world_position(armature, "head")
                if not head_pos:
                    self.report(
                        {"ERROR"}, "Bones 'c_head.x', 'Bip001Head' or 'head' not found. Face panel import cancelled.")
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
        if "c_head.x" in armature.pose.bones:
            child_of.subtarget = "c_head.x"
        elif "Bip001Head" in armature.pose.bones:
            child_of.subtarget = "Bip001Head"
        else:
            child_of.subtarget = "head"
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

    def get_mesh_from_armature(self, armature):
        for obj in bpy.data.objects:
            if obj.type == "MESH" and get_armature_from_modifiers(obj) == armature:
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
