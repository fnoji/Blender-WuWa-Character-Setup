
import bpy
import os
import math
import mathutils
import bmesh
from bpy.types import Operator
from mathutils import Vector
from math import pi, cos, sin
from collections import defaultdict, deque

# Here we are inside wuwa_add package.
from .utils import extract_character_name


# Global Constants / Configuration
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

# Parameters
ALIGN_THRESHOLD = math.radians(5)
move_amount = 0.0001
NEIGHBOR_DEPTH = 4


# --- Helper Functions ---

def get_local_x(bone):
    return bone.matrix.to_3x3().col[0].normalized()

def angle_between(v1, v2):
    if v1.length == 0 or v2.length == 0:
        return math.pi
    return v1.angle(v2)

def all_bone_pairs():
    return left_bone_pairs + right_bone_pairs

def remove_bone_collections(armature):
    if armature.data.collections:
        for collection in armature.data.collections[:]:
            armature.data.collections.remove(collection)

def process_bone_collections_and_rigify(armature, bone_data):
    for collection_name, index, row in bone_data:
        bpy.ops.armature.collection_add()
        new_collection = armature.data.collections[-1]
        new_collection.name = collection_name
        bpy.ops.armature.rigify_collection_set_ui_row(index=index, row=row)

def lock_bone_transformations(bone):
    bone.lock_location[:] = (False, False, False)
    bone.lock_rotation_w = False
    bone.lock_rotation[:] = (False, False, False)
    bone.lock_scale[:] = (False, False, False)

def select_and_move_bones(armature, keyword, collection_index):
    bpy.ops.pose.select_all(action='DESELECT')
    selected_bones = []
    
    for bone in armature.pose.bones:
        if keyword in bone.name:
            selected_bones.append(bone)
            bone.bone.select = True
            lock_bone_transformations(bone)
            
    if selected_bones:
        try:
            bpy.ops.armature.move_to_collection(collection_index=collection_index)
        except Exception as e:
            print(f"Error moving bones to collection {collection_index}: {e}")
            
    return len(selected_bones)

def get_hair_chain_length(bone):
    """Get total length of the Hair bone chain this bone belongs to."""
    # Find chain root (first Hair bone that has no Hair parent)
    root = bone
    while root.parent and "Hair" in root.parent.name:
        root = root.parent
    # Count from root to end following the chain
    length = 1
    current = root
    while current.children:
        hair_children = [c for c in current.children if "Hair" in c.name]
        if not hair_children:
            break
        current = hair_children[0]
        length += 1
    return length

def select_and_move_hair_bones(armature, hair1_index, hair2_index):
    """Move Hair bones to Hair 1 or Hair 2 based on chain length (≤3 = Hair 1, ≥4 = Hair 2)."""
    bpy.ops.pose.select_all(action='DESELECT')
    
    hair_bones = [bone for bone in armature.pose.bones if "Hair" in bone.name]
    hair1_bones = []
    hair2_bones = []
    
    for bone in hair_bones:
        chain_length = get_hair_chain_length(bone.bone)
        if chain_length >= 4:
            hair2_bones.append(bone)
        else:
            hair1_bones.append(bone)
        lock_bone_transformations(bone)
    
    # Move Hair 1 bones
    if hair1_bones:
        bpy.ops.pose.select_all(action='DESELECT')
        for bone in hair1_bones:
            bone.bone.select = True
        try:
            bpy.ops.armature.move_to_collection(collection_index=hair1_index)
        except Exception as e:
            print(f"Error moving Hair 1 bones: {e}")
    
    # Move Hair 2 bones
    if hair2_bones:
        bpy.ops.pose.select_all(action='DESELECT')
        for bone in hair2_bones:
            bone.bone.select = True
        try:
            bpy.ops.armature.move_to_collection(collection_index=hair2_index)
        except Exception as e:
            print(f"Error moving Hair 2 bones: {e}")

def create_circle_widget(name, radius=0.1, location=(0, 0, 0)):
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

def create_capsule_path(bm, radius=0.14, spacing=0.6):
    segments = 16
    left_x = -spacing / 2
    right_x = spacing / 2
    verts = []

    # Left semicircle
    for i in range(segments + 1):
        angle = pi / 2 + pi * i / segments
        x = left_x + cos(angle) * radius
        y = sin(angle) * radius
        verts.append(bm.verts.new((x, y, 0)))

    # Right semicircle
    for i in range(segments + 1):
        angle = -pi / 2 + pi * i / segments
        x = right_x + cos(angle) * radius
        y = sin(angle) * radius
        verts.append(bm.verts.new((x, y, 0)))

    return verts

def create_double_capsule_widget(name, inner_radius=0.14, outer_radius=0.17, spacing=0.6):
    if name in bpy.data.objects:
        return bpy.data.objects[name]

    mesh = bpy.data.meshes.new(name + "_Mesh")
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.collection.objects.link(obj)
    bm = bmesh.new()

    verts_inner = create_capsule_path(bm, inner_radius, spacing)
    for i in range(len(verts_inner)):
        bm.edges.new((verts_inner[i], verts_inner[(i + 1) % len(verts_inner)]))

    verts_outer = create_capsule_path(bm, outer_radius, spacing)
    for i in range(len(verts_outer)):
        bm.edges.new((verts_outer[i], verts_outer[(i + 1) % len(verts_outer)]))

    bm.to_mesh(mesh)
    bm.free()

    obj.rotation_euler[0] = pi / 2
    obj.name = name
    return obj


class WW_OT_Rigify(Operator):
    bl_idname = "shader.rigify_armature"
    bl_label = "Generate Rigify Rig"
    bl_description = "Generates a Rigify rig from the selected armature"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        obj = context.object
        if not obj or obj.type != 'ARMATURE':
            self.report({'ERROR'}, "Select an armature first.")
            return {'CANCELLED'}

        # --------------- Fix Bone Rotation --------------- #
        bpy.ops.object.mode_set(mode='EDIT')
        edit_bones = obj.data.edit_bones
        
        finger13_exists_left = "Bip001LFinger13" in edit_bones
        finger13_exists_right = "Bip001RFinger13" in edit_bones
        
        def check_alignment():
            for name1, name2 in all_bone_pairs():
                if (finger13_exists_left and (name1, name2) in skip_if_finger13) or \
                   (finger13_exists_right and (name1, name2) in skip_if_finger13):
                    continue
                b1 = edit_bones.get(name1)
                b2 = edit_bones.get(name2)
                if b1 and b2:
                    x1 = get_local_x(b1)
                    x2 = get_local_x(b2)
                    angle = angle_between(x1, x2)
                    if angle < ALIGN_THRESHOLD:
                        return True
            return False

        def apply_adjustment():
            if "Bip001LFinger13" in edit_bones:
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
                    x_axis = get_local_x(bone)
                    bone.tail += x_axis * move_amount

            for bone_name in inward_bones:
                bone = edit_bones.get(bone_name)
                if bone:
                    x_axis = get_local_x(bone)
                    bone.tail -= x_axis * move_amount

        while check_alignment():
            apply_adjustment()
            
        bpy.ops.object.mode_set(mode='OBJECT')

        # --------------- Main Rigify Code --------------- #
        selected_object = context.active_object
        OrigArmature = selected_object.name
        RigArmature = extract_character_name(OrigArmature) + "Rig"
        CharacterMesh = None

        for o in context.scene.objects:
            if o.type == 'MESH':
                for modifier in o.modifiers:
                    if modifier.type == 'ARMATURE' and modifier.object and modifier.object.name == OrigArmature:
                        CharacterMesh = o
                        break
                if CharacterMesh:
                    break
        
        # Apply Scale
        try:
            bpy.ops.object.transform_apply(scale=True)
        except Exception as e:
            print(f"Failed to apply scale: {e}")

        rig_armature_object = context.view_layer.objects.active
        if rig_armature_object and rig_armature_object.type == 'ARMATURE':
            bpy.ops.object.mode_set(mode='EDIT')
            spine_bone = rig_armature_object.data.edit_bones.get("Bip001Spine2")
            if spine_bone:
                 bone_length = (spine_bone.tail - spine_bone.head).length
                 if bone_length < 0.06:
                     direction = spine_bone.tail - spine_bone.head
                     direction.normalize()
                     spine_bone.tail = spine_bone.head + direction * 0.15
                     spine_bone.tail.y = spine_bone.head.y
                     spine_bone.head.z += 0.03
                     spine_bone.tail.z += 0.03
            bpy.ops.object.mode_set(mode='OBJECT')

        if context.object and context.object.type == 'ARMATURE':
            armature = context.object
            remove_bone_collections(armature)

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

            for bone1_name, bone2_name in bone_pairs:
                if bone1_name in armature.data.edit_bones and bone2_name in armature.data.edit_bones:
                    bone1 = armature.data.edit_bones[bone1_name]
                    bone2 = armature.data.edit_bones[bone2_name]
                    bone1.tail = bone2.head

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
            for bone_name in spine_bones:
                if bone_name in armature.data.edit_bones:
                    armature.data.edit_bones[bone_name].use_connect = True

            bones_to_adjust_roll = [
                'Bip001Pelvis', 'Bip001Spine', 'Bip001Spine1',
                'Bip001Spine2', 'Bip001LClavicle', 'Bip001RClavicle'
            ]
            for bone_name in bones_to_adjust_roll:
                if bone_name in armature.data.edit_bones:
                    armature.data.edit_bones[bone_name].roll = 0

            bpy.ops.object.mode_set(mode='OBJECT')
            bpy.ops.object.mode_set(mode='POSE')

            bone_data = [
                ('Torso', 0, 1), ('Torso (Tweak)', 1, 2), ('Fingers', 2, 3), ('Fingers (Details)', 3, 4),
                ('Arm.L (IK)', 4, 5), ('Arm.R (IK)', 5, 5), ('Arm.L (FK)', 6, 6), ('Arm.R (FK)', 7, 6),
                ('Arm.L (Tweak)', 8, 7), ('Arm.R (Tweak)', 9, 7), ('Leg.L (IK)', 10, 8), ('Leg.R (IK)', 11, 8),
                ('Leg.L (FK)', 12, 9), ('Leg.R (FK)', 13, 9), ('Leg.L (Tweak)', 14, 10), ('Leg.R (Tweak)', 15, 10),
                ('Hair 1', 16, 11), ('Hair 2', 17, 11),
                ('Cloth', 18, 12), ('Skirt', 19, 12),
                ('Breast / Tail', 20, 13),
                ('Root', 21, 14),     
            ]
            process_bone_collections_and_rigify(armature, bone_data)
            
            bpy.ops.armature.collection_add()
            armature.data.collections[-1].name = 'Others'
            
            for row in [3, 6, 10, 14, 17]:
                bpy.ops.armature.rigify_collection_add_ui_row(row=row, add=True)

            bpy.ops.object.mode_set(mode='POSE')
            # Hair bones handled separately with chain length logic
            select_and_move_hair_bones(armature, 16, 17)  # Hair 1 = 16, Hair 2 = 17
            
            keywords_and_collections = [
                ("Earrings", 16),
                ("Piao", 18),
                ("Skirt", 19), ("Trousers", 19),
                ("Tail", 20), ("Chest", 20),
                ("Other", 22), ("Weapon", 22), ("Prop", 22), ("Chibang", 22),
                ("Bip001Neck.001", 23), ("Bip001Head.001", 23),
                ("EyeTracker", 0), ("Eye.L", 0), ("Eye.R", 0),
            ]
            for keyword, collection_index in keywords_and_collections:
                select_and_move_bones(armature, keyword, collection_index)

            if "ORG" in armature.data.collections_all:
                armature.data.collections_all["ORG"].is_visible = False
            
            collections_to_hide = [
                "Torso (Tweak)", "Arm.L (FK)", "Arm.R (FK)", "Leg.L (FK)", "Leg.R (FK)",
                "Arm.L (Tweak)", "Arm.R (Tweak)", "Leg.L (Tweak)", "Leg.R (Tweak)",
                "Hair 1", "Hair 2", "Cloth", "Skirt", "Breast / Tail"
            ]
            for col_name in collections_to_hide:
                if col_name in armature.data.collections_all:
                    armature.data.collections_all[col_name].is_visible = False

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
                    ('Bip001LFinger11', 'limbs.super_finger', None), ('Bip001LFinger21', 'limbs.super_finger', None),
                    ('Bip001LFinger31', 'limbs.super_finger', None), ('Bip001LFinger41', 'limbs.super_finger', None),
                    ('Bip001RFinger11', 'limbs.super_finger', None), ('Bip001RFinger21', 'limbs.super_finger', None),
                    ('Bip001RFinger31', 'limbs.super_finger', None), ('Bip001RFinger41', 'limbs.super_finger', None),
                ])
            else:
                bones_and_rig_types.extend([
                    ('Bip001LFinger1', 'limbs.super_finger', None), ('Bip001LFinger2', 'limbs.super_finger', None),
                    ('Bip001LFinger3', 'limbs.super_finger', None), ('Bip001LFinger4', 'limbs.super_finger', None),
                    ('Bip001RFinger1', 'limbs.super_finger', None), ('Bip001RFinger2', 'limbs.super_finger', None),
                    ('Bip001RFinger3', 'limbs.super_finger', None), ('Bip001RFinger4', 'limbs.super_finger', None),
                ])

            for bone_name, rig_type, widget_type in bones_and_rig_types:
                bone = armature.pose.bones.get(bone_name)
                if bone:
                    armature.data.bones[bone_name].select = True
                    armature.data.bones.active = armature.data.bones[bone_name]
                    bone.rigify_type = rig_type
                    if widget_type and bone.rigify_parameters:
                        bone.rigify_parameters.super_copy_widget_type = widget_type

            bpy.ops.object.mode_set(mode='EDIT')

            def duplicate_and_adjust_heel_bone(foot_bone_name, toe_bone_name, heel_bone_name, rotation_angle=1.5708):
                if toe_bone_name in armature.data.edit_bones:
                    toe_bone = armature.data.edit_bones[toe_bone_name]
                    heel_bone = armature.data.edit_bones.new(name=heel_bone_name)
                    heel_bone.head = toe_bone.head
                    heel_bone.tail = toe_bone.tail
                    heel_bone.roll = toe_bone.roll
                    rotation_matrix = mathutils.Matrix.Rotation(rotation_angle, 4, 'Y')
                    heel_bone.tail = heel_bone.head + rotation_matrix @ (heel_bone.tail - heel_bone.head)
                    if foot_bone_name in armature.data.edit_bones:
                        foot_bone = armature.data.edit_bones[foot_bone_name]
                        foot_head_y = foot_bone.head[1]
                        heel_bone.head[1] = foot_head_y
                        heel_bone.tail[1] = foot_head_y
                    heel_bone.parent = armature.data.edit_bones[foot_bone_name]

            duplicate_and_adjust_heel_bone('Bip001LFoot', 'Bip001LToe0', 'Bip001LHeel0', rotation_angle=1.5708)
            duplicate_and_adjust_heel_bone('Bip001RFoot', 'Bip001RToe0', 'Bip001RHeel0', rotation_angle=-1.5708)

            bpy.ops.object.mode_set(mode='OBJECT')

            # Rename bones
            print("DEBUG: Starting bone renaming process...")
            armature = context.object
            bpy.ops.object.mode_set(mode='EDIT')
            
            name_mapping = {
                "Bip001Neck": "neck", "Bip001Head": "head", "Bip001Clavicle": "shoulder",
                "Bip001UpperArm": "upper_arm", "Bip001Forearm": "forearm", "Bip001Hand": "hand",
                "Bip001Thigh": "thigh", "Bip001Calf": "shin", "Bip001Foot": "foot", "Bip001Toe0": "toe_ik",
                "Bip001Spine": "Spine", "Bip001Spine1": "Spine1", "Bip001Spine2": "Spine2", "Bip001Pelvis": "Pelvis",
                "Bip001Finger0": "thumb.01", "Bip001Finger01": "thumb.02", "Bip001Finger02": "thumb.03",
            }
            
            # Add finger mappings based on whether Finger13 exists
            # For models WITH Finger13: Finger11→01, Finger12→02, Finger13→03
            # For models WITHOUT Finger13: Finger1→01, Finger11→02, Finger12→03
            if finger13_exists_left or finger13_exists_right:
                name_mapping.update({
                    "Bip001Finger11": "f_index.01", "Bip001Finger12": "f_index.02", "Bip001Finger13": "f_index.03",
                    "Bip001Finger21": "f_middle.01", "Bip001Finger22": "f_middle.02", "Bip001Finger23": "f_middle.03",
                    "Bip001Finger31": "f_ring.01", "Bip001Finger32": "f_ring.02", "Bip001Finger33": "f_ring.03",
                    "Bip001Finger41": "f_pinky.01", "Bip001Finger42": "f_pinky.02", "Bip001Finger43": "f_pinky.03",
                })
            else:
                name_mapping.update({
                    "Bip001Finger1": "f_index.01", "Bip001Finger11": "f_index.02", "Bip001Finger12": "f_index.03",
                    "Bip001Finger2": "f_middle.01", "Bip001Finger21": "f_middle.02", "Bip001Finger22": "f_middle.03",
                    "Bip001Finger3": "f_ring.01", "Bip001Finger31": "f_ring.02", "Bip001Finger32": "f_ring.03",
                    "Bip001Finger4": "f_pinky.01", "Bip001Finger41": "f_pinky.02", "Bip001Finger42": "f_pinky.03",
                })
            
            final_renames = {}
            current_bones = list(armature.data.edit_bones)
            
            for bone in current_bones:
                original_name = bone.name
                new_name = original_name
                
                if new_name.startswith("Bip001R") and not new_name.endswith(".R"):
                    new_name += ".R"
                elif new_name.startswith("Bip001L") and not new_name.endswith(".L"):
                    new_name += ".L"
                
                base_name_check = new_name
                suffix = ""
                if base_name_check.endswith(".L"):
                    base_name_check = base_name_check[:-2]; suffix = ".L"
                elif base_name_check.endswith(".R"):
                    base_name_check = base_name_check[:-2]; suffix = ".R"
                
                if base_name_check.startswith("Bip001R"):
                    base_name_check = base_name_check.replace("Bip001R", "Bip001", 1)
                elif base_name_check.startswith("Bip001L"):
                    base_name_check = base_name_check.replace("Bip001L", "Bip001", 1)
                
                if base_name_check in name_mapping:
                    new_name = name_mapping[base_name_check] + suffix
                else:
                    if base_name_check != original_name:
                         if new_name.startswith("Bip001L") or new_name.startswith("Bip001R"):
                             new_name = base_name_check + suffix

                if new_name != original_name:
                    final_renames[original_name] = new_name

            for old_name, new_name in final_renames.items():
                if old_name in armature.data.edit_bones:
                    armature.data.edit_bones[old_name].name = new_name
            
            bpy.ops.object.mode_set(mode='OBJECT')
            bpy.ops.object.select_all(action='DESELECT')
            armature.select_set(True)
            context.view_layer.objects.active = armature
            context.view_layer.update()
            
            bpy.ops.object.mode_set(mode='POSE')
            for bone in armature.pose.bones:
                for key in bone.keys():
                    if key.startswith("_"): continue
                    val = bone[key]
                    if isinstance(val, str) and val in final_renames:
                        bone[key] = final_renames[val]

            bpy.ops.pose.rigify_generate()

        # --------------- Post Generation Logic --------------- #
        bpy.ops.object.mode_set(mode='POSE')
        armature = context.object # This might be the rig now if rigify_generate switched context? No, usually it makes new rig active.
        
        # NOTE: Rigify generate usually creates a NEW armature object active. 
        # We need to find "RIG-" + OrigArmature if successfully created.
        try:
             RigArmatureObj = bpy.data.objects.get(RigArmature)
        except:
             RigArmatureObj = None
             
        if not RigArmatureObj:
             # Fallback if name is different
             RigArmatureObj = context.active_object
        
        if RigArmatureObj:
             context.view_layer.objects.active = RigArmatureObj
             
             # Adjust Neck/Head Custom Shapes
             pose_bone_neck = RigArmatureObj.pose.bones.get("neck")
             if pose_bone_neck:
                 bpy.ops.object.mode_set(mode='EDIT')
                 edit_bone_neck = RigArmatureObj.data.edit_bones.get("neck")
                 if edit_bone_neck:
                     neck_length = (edit_bone_neck.tail - edit_bone_neck.head).length / 2
                 bpy.ops.object.mode_set(mode='POSE')
                 pose_bone_neck.custom_shape_translation.y = neck_length
                 pose_bone_neck.custom_shape_scale_xyz = (1.5, 1.5, 1.5)

             pose_bone_head = RigArmatureObj.pose.bones.get("head")
             if pose_bone_head:
                 bpy.ops.object.mode_set(mode='EDIT')
                 edit_bone_head = RigArmatureObj.data.edit_bones.get("head")
                 if edit_bone_head:
                     head_length = (edit_bone_head.tail - edit_bone_head.head).length
                 bpy.ops.object.mode_set(mode='POSE')
                 pose_bone_head.custom_shape_translation.y = head_length * 1.2
                 pose_bone_head.custom_shape_scale_xyz = (2, 2, 2)
                 
             bpy.ops.object.mode_set(mode='OBJECT')
             
             # IK Stretch
             for b_name in ["upper_arm_parent.L", "upper_arm_parent.R", "thigh_parent.L", "thigh_parent.R"]:
                 if b_name in RigArmatureObj.pose.bones:
                     RigArmatureObj.pose.bones[b_name]["IK_Stretch"] = 0.000
             
             # ORG Deform
             bpy.context.view_layer.objects.active = RigArmatureObj
             bpy.ops.object.mode_set(mode='EDIT')
             for bone in RigArmatureObj.data.edit_bones:
                 if bone.name.startswith('ORG-'):
                     bone.use_deform = True
             bpy.ops.object.mode_set(mode='OBJECT')
             
             # Mesh updates
             bpy.ops.object.select_all(action='DESELECT')
             if CharacterMesh:
                 context.view_layer.objects.active = CharacterMesh
                 CharacterMesh.select_set(True)
                 
                 for group in CharacterMesh.vertex_groups:
                     if not group.name.startswith("ORG-"):
                         group.name = "ORG-" + group.name
                 
                 # Weight transfer
                 weight_mappings = {
                    "ORG-Bip001UpArmTwist.L": "DEF-upper_arm.L", "ORG-Bip001UpArmTwist1.L": "DEF-upper_arm.L",
                    "ORG-Bip001UpArmTwist2.L": "DEF-upper_arm.L.001", "ORG-upper_arm.L": "DEF-upper_arm.L.001",
                    "ORG-forearm.L": "DEF-forearm.L", "ORG-Bip001ForeTwist.L": "DEF-forearm.L.001",
                    "ORG-Bip001ForeTwist1.L": "DEF-forearm.L.001", "ORG-Bone_HandTwist_L": "DEF-forearm.L.001",
                    "ORG-Bip001ForeTwist2.L": "DEF-forearm.L.001", "ORG-Bip001_L_Elbow_F": "DEF-upper_arm.L.001",
                    "ORG-Bip001_L_Elbow_B": "DEF-upper_arm.L.001",
                    "ORG-Bip001UpArmTwist.R": "DEF-upper_arm.R", "ORG-Bip001UpArmTwist1.R": "DEF-upper_arm.R",
                    "ORG-Bip001UpArmTwist2.R": "DEF-upper_arm.R.001", "ORG-upper_arm.R": "DEF-upper_arm.R.001",
                    "ORG-forearm.R": "DEF-forearm.R", "ORG-Bip001ForeTwist.R": "DEF-forearm.R.001",
                    "ORG-Bip001ForeTwist1.R": "DEF-forearm.R.001", "ORG-Bone_HandTwist_R": "DEF-forearm.R.001",
                    "ORG-Bip001ForeTwist2.R": "DEF-forearm.R.001", "ORG-Bip001_R_Elbow_F": "DEF-upper_arm.R.001",
                    "ORG-Bip001_R_Elbow_B": "DEF-upper_arm.R.001",
                    "ORG-Bip001ThighTwist.L": "DEF-thigh.L", "ORG-thigh.L": "DEF-thigh.L.001",
                    "ORG-Bip001_L_Calf": "DEF-shin.L", "ORG-Bip001_L_Knee_B": "DEF-thigh.L.001",
                    "ORG-Bip001_L_Knee_F": "DEF-thigh.L.001", "ORG-Bip001ThighTwist1.L": "DEF-thigh.L",
                    "ORG-Bip001_L_CalfTwist": "DEF-shin.L.001",
                    "ORG-Bip001ThighTwist.R": "DEF-thigh.R", "ORG-thigh.R": "DEF-thigh.R.001",
                    "ORG-Bip001_R_Calf": "DEF-shin.R", "ORG-Bip001_R_Knee_B": "DEF-thigh.R.001",
                    "ORG-Bip001_R_Knee_F": "DEF-thigh.R.001", "ORG-Bip001ThighTwist1.R": "DEF-thigh.R",
                    "ORG-Bip001_R_CalfTwist": "DEF-shin.R.001",
                 }
                 
                 vgroups = CharacterMesh.vertex_groups
                 for source, target in weight_mappings.items():
                     if source in vgroups:
                         src_grp = vgroups[source]
                         if target not in vgroups: tgt_grp = vgroups.new(name=target)
                         else: tgt_grp = vgroups[target]
                         
                         for vert in CharacterMesh.data.vertices:
                             w = 0.0
                             has_w = False
                             for g in vert.groups:
                                 if g.group == src_grp.index:
                                     w += g.weight; has_w = True; break
                             if has_w:
                                 for g in vert.groups:
                                     if g.group == tgt_grp.index:
                                         w += g.weight; break
                                 tgt_grp.add([vert.index], w, 'REPLACE')
                                 src_grp.remove([vert.index])

                 # Armature Modifier update
                 for modifier in CharacterMesh.modifiers:
                     if modifier.type == 'ARMATURE' and modifier.object and modifier.object.name == OrigArmature:
                         modifier.object = RigArmatureObj
                 
                 CharacterMesh.parent = RigArmatureObj


                 # Secondary Shape Keys
                 source_shape_keys = ["Pupil_R", "Pupil_L", "Pupil_Up", "Pupil_Down"]
                 target_material_name = None
                 for slot in CharacterMesh.material_slots:
                     if "Eye" in slot.name:
                         target_material_name = slot.name
                         break
                 
                 offset_connected = Vector((0.0, -0.001, 0.0))
                 offset_unconnected = Vector((0.0, 0.001, 0.0))
                 NEIGHBOR_DEPTH = 4

                 if CharacterMesh.data.shape_keys and target_material_name:
                     keys = CharacterMesh.data.shape_keys.key_blocks
                     basis = CharacterMesh.data.shape_keys.reference_key
                     
                     bpy.ops.object.mode_set(mode='OBJECT')
                     
                     mat_slots = CharacterMesh.material_slots
                     relevant_face_vert_indices = set()
                     relevant_edges = set()
                     for poly in CharacterMesh.data.polygons:
                         if poly.material_index < len(mat_slots) and mat_slots[poly.material_index].name == target_material_name:
                             relevant_face_vert_indices.update(poly.vertices)
                             relevant_edges.update(tuple(sorted((poly.vertices[i], poly.vertices[j])))
                                                   for i in range(len(poly.vertices)) for j in range(i + 1, len(poly.vertices)))
                     
                     if relevant_face_vert_indices:
                         connectivity = defaultdict(set)
                         edge_faces = defaultdict(int)
                         for poly in CharacterMesh.data.polygons:
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
                         
                         seed_vertices = {v for v, linked in connectivity.items() if len(linked) > 10}
                         connected_vertices = set()
                         visited = set()
                         for seed in seed_vertices:
                             queue = deque()
                             queue.append((seed, 0))
                             visited.add(seed)
                             connected_vertices.add(seed)
                             while queue:
                                 current, depth = queue.popleft()
                                 if depth >= NEIGHBOR_DEPTH: continue
                                 for neighbor in connectivity[current]:
                                     if neighbor not in visited:
                                         visited.add(neighbor)
                                         connected_vertices.add(neighbor)
                                         queue.append((neighbor, depth + 1))
                         
                         unconnected_vertices = relevant_face_vert_indices - connected_vertices
                         border_vertices = set()
                         for edge, count in edge_faces.items():
                             if count == 1: border_vertices.update(edge)
                         
                         movable_unconnected = unconnected_vertices - border_vertices
                         
                         for source_name in source_shape_keys:
                             if source_name not in keys: continue
                             source_key = keys[source_name]
                             index = next(i for i, k in enumerate(keys) if k.name == source_key.name)
                             CharacterMesh.active_shape_key_index = index
                             
                             bpy.ops.object.shape_key_add(from_mix=False)
                             key_L = CharacterMesh.data.shape_keys.key_blocks[-1]
                             key_L.name = f"{source_name}.L"
                             
                             bpy.ops.object.shape_key_add(from_mix=False)
                             key_R = CharacterMesh.data.shape_keys.key_blocks[-1]
                             key_R.name = f"{source_name}.R"
                             
                             for i in relevant_face_vert_indices:
                                 base_co = basis.data[i].co
                                 source_co = source_key.data[i].co
                                 delta = source_co - base_co
                                 
                                 if i in connected_vertices: offset = offset_connected
                                 elif i in movable_unconnected: offset = offset_unconnected
                                 else: offset = Vector((0.0, 0.0, 0.0))
                                 
                                 if base_co.x >= 0: key_L.data[i].co = base_co + delta * 2 + offset
                                 else: key_R.data[i].co = base_co + delta * 2 + offset
                             
                             bpy.ops.object.select_all(action='DESELECT')
                     
                 # Eye Tracker
                 context.view_layer.objects.active = RigArmatureObj
                 bpy.ops.object.mode_set(mode='EDIT')
                 target_bone = RigArmatureObj.data.edit_bones.get("ORG-head")
                 if target_bone:
                     new_bone = RigArmatureObj.data.edit_bones.new("EyeTracker")
                     new_bone.head = target_bone.head.copy()
                     new_bone.head.y -= 0.15; new_bone.head.z += 0.03
                     new_bone.tail = new_bone.head + Vector((0, 0, 0.03))
                     new_bone.parent = target_bone
                     new_bone.use_connect = False
                     
                     et_head = new_bone.head
                     y_off = new_bone.tail.y - new_bone.head.y
                     z_off = new_bone.tail.z - new_bone.head.z
                     
                     eye_l = RigArmatureObj.data.edit_bones.new("Eye.L")
                     eye_l.head = et_head + Vector((0.03, 0, 0))
                     eye_l.tail = eye_l.head + Vector((0, y_off, z_off))
                     eye_l.parent = new_bone; eye_l.use_connect = False
                     
                     eye_r = RigArmatureObj.data.edit_bones.new("Eye.R")
                     eye_r.head = et_head + Vector((-0.03, 0, 0))
                     eye_r.tail = eye_r.head + Vector((0, y_off, z_off))
                     eye_r.parent = new_bone; eye_r.use_connect = False
                 
                 bpy.ops.object.mode_set(mode='OBJECT')
                 
                 # Widget Creation
                 create_circle_widget("WGT-rig_eye.L", radius=0.1, location=(-0.3, 0, 0))
                 create_circle_widget("WGT-rig_eye.R", radius=0.1, location=(0.3, 0, 0))
                 create_double_capsule_widget("WGT-rig_eyes", inner_radius=0.14, outer_radius=0.17, spacing=0.6)
                 
                 # Assign Widgets
                 context.view_layer.objects.active = RigArmatureObj
                 bpy.ops.object.mode_set(mode='POSE')
                 custom_shapes = {"EyeTracker": "WGT-rig_eyes", "Eye.L": "WGT-rig_eye.L", "Eye.R": "WGT-rig_eye.R"}
                 for b_name, s_name in custom_shapes.items():
                     if b_name in RigArmatureObj.pose.bones and s_name in bpy.data.objects:
                         RigArmatureObj.pose.bones[b_name].custom_shape = bpy.data.objects[s_name]
                         RigArmatureObj.pose.bones[b_name].custom_shape_scale_xyz = (4.0, 4.0, 4.0)


                 # Drivers
                 if CharacterMesh.data.shape_keys:
                     shape_key_names = {
                        "Pupil_L": "LOC_X", "Pupil_R": "LOC_X",
                        "Pupil_Up": "LOC_Y", "Pupil_Down": "LOC_Y"
                     }
                     expressions = {
                        "Pupil_L": 'max(min((bone_x * 10), 1), 0) if bone_x > 0 else 0',
                        "Pupil_R": 'max(min((-bone_x * 10), 1), 0) if bone_x < 0 else 0',
                        "Pupil_Up": 'max(min((bone_y * 10), 1), 0) if bone_y > 0 else 0',
                        "Pupil_Down": 'max(min((-bone_y * 10), 1), 0) if bone_y < 0 else 0'
                     }
                     
                     for shape_key_name, transform_axis in shape_key_names.items():
                         if shape_key_name in CharacterMesh.data.shape_keys.key_blocks:
                             shape_key = CharacterMesh.data.shape_keys.key_blocks[shape_key_name]
                             driver = shape_key.driver_add('value').driver
                             driver.type = 'SCRIPTED'
                             var = driver.variables.new()
                             var.name = 'bone_' + transform_axis[-1].lower()
                             var.type = 'TRANSFORMS'
                             var.targets[0].id = RigArmatureObj
                             var.targets[0].bone_target = "EyeTracker"
                             var.targets[0].transform_type = transform_axis
                             var.targets[0].transform_space = 'LOCAL_SPACE'
                             driver.expression = expressions[shape_key_name]

                     for bone_suffix in ['.L', '.R']:
                         bone_name = "Eye" + bone_suffix
                         for shape_key_prefix, transform_axis in shape_key_names.items():
                             shape_key_name = shape_key_prefix + bone_suffix
                             if shape_key_name in CharacterMesh.data.shape_keys.key_blocks:
                                 shape_key = CharacterMesh.data.shape_keys.key_blocks[shape_key_name]
                                 driver = shape_key.driver_add('value').driver
                                 driver.type = 'SCRIPTED'
                                 var = driver.variables.new()
                                 var.name = 'bone_' + transform_axis[-1].lower()
                                 var.type = 'TRANSFORMS'
                                 var.targets[0].id = RigArmatureObj
                                 var.targets[0].bone_target = bone_name
                                 var.targets[0].transform_type = transform_axis
                                 var.targets[0].transform_space = 'LOCAL_SPACE'
                                 driver.expression = expressions[shape_key_prefix]

                 bpy.ops.object.mode_set(mode='OBJECT')
                 

                 # Move Widgets to WGTS collection instead of deleting
                 wgts_collection = None
                 # Try to find existing Rigify widget collection first
                 for col in bpy.data.collections:
                     if col.name.startswith("WGTS_RIG-"):
                         wgts_collection = col
                         break
                 
                 # Fallback: check keys of collections
                 if not wgts_collection:
                      for key in bpy.data.collections.keys():
                           if key.startswith("WGTS"):
                                wgts_collection = bpy.data.collections[key]
                                break

                 if not wgts_collection:
                     wgts_collection = bpy.data.collections.new("WGTS_Custom")
                     context.scene.collection.children.link(wgts_collection)
                 
                 for n in ["WGT-rig_eyes", "WGT-rig_eye.R", "WGT-rig_eye.L"]:
                     o = bpy.data.objects.get(n)
                     if o:
                         # Ensure it's in the WGTS collection
                         if o.name not in wgts_collection.objects:
                             wgts_collection.objects.link(o)
                         # Remove from other collections (clean up scene)
                         for col in o.users_collection:
                             if col != wgts_collection:
                                 col.objects.unlink(o)
                 
                 if wgts_collection:
                      wgts_collection.hide_viewport = True



                 # Assign Collections, Themes, Skirt separation, etc.
                 bpy.ops.object.mode_set(mode='POSE')
                 bones_to_move = {
                    0: ["torso", "chest", "shoulder.L", "shoulder.R", "hips", "neck", "head"],
                    1: ["Spine_fk", "Spine1_fk", "Spine2_fk", "tweak_Spine1",
                        "tweak_Spine2", "tweak_Spine2.001", "tweak_Spine", "tweak_Pelvis", "Pelvis_fk", "tweak_neck"],
                    2: ["thumb.01_master.L", "f_index.01_master.L", "f_middle.01_master.L", "f_ring.01_master.L", "f_pinky.01_master.L",
                        "thumb.01_master.R", "f_index.01_master.R", "f_middle.01_master.R", "f_ring.01_master.R", "f_pinky.01_master.R",
                        "f_index.02_master.L", "f_middle.02_master.L", "f_middle.02_master.L", "f_ring.02_master.L", "f_pinky.02_master.L",
                        "f_index.02_master.R", "f_middle.02_master.R", "f_middle.02_master.R", "f_ring.02_master.R", "f_pinky.02_master.R"],
                    3: ["thumb.02.L", "thumb.03.L", "thumb.01.L.001", "f_index.01.L", "f_index.02.L", "f_index.03.L", "f_index.01.L.001",
                        "f_middle.01.L", "f_middle.02.L", "f_middle.03.L", "f_middle.01.L.001", "f_ring.01.L", "f_ring.02.L", "f_ring.03.L", "f_ring.01.L.001",
                        "f_pinky.01.L", "f_pinky.02.L", "f_pinky.03.L", "f_pinky.01.L.001", "thumb.01.L",
                        "thumb.02.R", "thumb.03.R", "thumb.01.R.001", "f_index.01.R", "f_index.02.R", "f_index.03.R", "f_index.01.R.001",
                        "f_middle.01.R", "f_middle.02.R", "f_middle.03.R", "f_middle.01.R.001", "f_ring.01.R", "f_ring.02.R", "f_ring.03.R", "f_ring.01.R.001",
                        "f_pinky.01.R", "f_pinky.02.R", "f_pinky.03.R", "f_pinky.01.R.001", "thumb.01.R",
                        "Bip001Finger13.L", "f_index.02.L.001", "Bip001Finger23.L", "f_middle.02.L.001", "Bip001Finger33.L", "f_ring.02.L.001", "Bip001Finger43.L", "f_pinky.02.L.001",
                        "Bip001Finger13.R", "f_index.02.R.001", "Bip001Finger23.R", "f_middle.02.R.001", "Bip001Finger33.R", "f_ring.02.R.001", "Bip001Finger43.R", "f_pinky.02.R.001"],
                    4: ["upper_arm_parent.L", "upper_arm_ik.L", "hand_ik.L", "upper_arm_ik_target.L"],
                    5: ["upper_arm_parent.R", "upper_arm_ik.R", "hand_ik.R", "upper_arm_ik_target.R"],
                    6: ["upper_arm_fk.L", "forearm_fk.L", "hand_fk.L"],
                    7: ["upper_arm_fk.R", "forearm_fk.R", "hand_fk.R"],
                    8: ["upper_arm_tweak.L", "upper_arm_tweak.L.001", "forearm_tweak.L", "forearm_tweak.L.001", "hand_tweak.L"],
                    9: ["upper_arm_tweak.R", "upper_arm_tweak.R.001", "forearm_tweak.R", "forearm_tweak.R.001", "hand_tweak.R"],
                    10: ["thigh_parent.L", "thigh_ik.L", "foot_heel_ik.L", "foot_spin_ik.L", "toe_ik.L", "foot_ik.L", "thigh_ik_target.L"],
                    11: ["thigh_parent.R", "thigh_ik.R", "foot_heel_ik.R", "foot_spin_ik.R", "toe_ik.R", "foot_ik.R", "thigh_ik_target.R"],
                    12: ["thigh_fk.L", "shin_fk.L", "foot_fk.L", "toe_fk.L"],
                    13: ["thigh_fk.R", "shin_fk.R", "foot_fk.R", "toe_fk.R"],
                    14: ["thigh_tweak.L", "thigh_tweak.L.001", "shin_tweak.L", "shin_tweak.L.001", "foot_tweak.L"],
                    15: ["thigh_tweak.R", "thigh_tweak.R.001", "shin_tweak.R", "shin_tweak.R.001", "foot_tweak.R"],
                 }
                 theme_for_groups = {
                    0: 'THEME09', 1: 'THEME04', 2: 'THEME14', 3: 'THEME03', 4: 'THEME01', 5: 'THEME01',
                    6: 'THEME03', 7: 'THEME03', 8: 'THEME04', 9: 'THEME04', 10: 'THEME01', 11: 'THEME01',
                    12: 'THEME03', 13: 'THEME03', 14: 'THEME04', 15: 'THEME04',
                 }
                 
                 for group_index, b_names in bones_to_move.items():
                     theme = theme_for_groups.get(group_index)
                     for b_name in b_names:
                         bone = RigArmatureObj.pose.bones.get(b_name)
                         if bone and theme: bone.color.palette = theme
                 
                 for collection_index, b_names in bones_to_move.items():
                     bpy.ops.pose.select_all(action='DESELECT')
                     for b_name in b_names:
                         bone = RigArmatureObj.pose.bones.get(b_name)
                         if bone: bone.bone.select = True
                     bpy.ops.armature.move_to_collection(collection_index=collection_index)
                 
                 # Set ORG visible
                 if "ORG" in RigArmatureObj.data.collections_all:
                     RigArmatureObj.data.collections_all["ORG"].is_visible = True
                 
                 bpy.ops.object.mode_set(mode='POSE')
                 # Helper loop for keywords and collections - updated for new layer structure
                 # Hair bones handled separately
                 select_and_move_hair_bones(RigArmatureObj, 16, 17)  # Hair 1 = 16, Hair 2 = 17
                 
                 keywords_and_collections = [
                    ("Earrings", 16), ("Piao", 18), ("Skirt", 19), ("Trousers", 19),
                    ("Tail", 20), ("Other", 22), ("Weapon", 22), ("Prop", 22), ("Chibang", 22),
                    ("EyeTracker", 0), ("neck.001", 22), ("head.001", 22), ("Chest", 20),
                    ("Eye.L", 0), ("Eye.R", 0), ("Bone_Chest001", 20), ("Bone_Chest002", 20),
                    ("Bone_Chest003", 20),
                    ("L_ChestBone01", 20), ("L_ChestBone02", 20),
                    ("R_ChestBone01", 20), ("R_ChestBone02", 20),
                 ]
                 for keyword, collection_index in keywords_and_collections:
                     select_and_move_bones(RigArmatureObj, keyword, collection_index)
                 
                 # Skirt separation
                 waist_bones = ["Bip001Pelvis", "Bip001Spine", "Bip001Spine1", "Bip001Spine2"]
                 bpy.ops.pose.select_all(action='DESELECT')
                 bones_piao_move = []
                 for bone in RigArmatureObj.pose.bones:
                     if "Piao" in bone.name:
                         parent = bone.parent
                         if parent:
                             is_skirt = any(waist_kw in parent.name for waist_kw in waist_bones)
                             if is_skirt:
                                 bones_piao_move.append(bone)
                                 bone.bone.select = True
                                 lock_bone_transformations(bone)
                 if bones_piao_move:
                     bpy.ops.armature.move_to_collection(collection_index=19)  # Skirt = 19
                 
                 # Visibility
                 cols_all = RigArmatureObj.data.collections_all
                 if "ORG" in cols_all: cols_all["ORG"].is_visible = False
                 for cname in ["Torso (Tweak)", "Fingers (Details)", "Arm.L (FK)", "Arm.R (FK)", "Arm.L (Tweak)", "Arm.R (Tweak)",
                               "Leg.L (FK)", "Leg.R (FK)", "Leg.L (Tweak)", "Leg.R (Tweak)", "Hair 1", "Hair 2", "Cloth", "Skirt", "Breast / Tail"]:
                     if cname in cols_all: cols_all[cname].is_visible = False
                 if "Root" in cols_all: cols_all["Root"].is_visible = True
                 
                 # IK Pole - set property and move to IK collections
                 ik_pole_targets = ["upper_arm_parent.L", "upper_arm_parent.R", "thigh_parent.L", "thigh_parent.R"]
                 for b_name in ik_pole_targets:
                     bone = RigArmatureObj.pose.bones.get(b_name)
                     if bone and "pole_vector" in bone: bone["pole_vector"] = True
                 
                 # Move IK pole bones to IK collections
                 pole_collection_map = {
                     "upper_arm_ik_target.L": 4,  # Arm.L (IK)
                     "upper_arm_ik_target.R": 5,  # Arm.R (IK)
                     "thigh_ik_target.L": 10,     # Leg.L (IK)
                     "thigh_ik_target.R": 11,     # Leg.R (IK)
                     "VIS_upper_arm_ik_pole.L": 4,  # Arm.L (IK)
                     "VIS_upper_arm_ik_pole.R": 5,  # Arm.R (IK)
                     "VIS_thigh_ik_pole.L": 10,     # Leg.L (IK)
                     "VIS_thigh_ik_pole.R": 11,     # Leg.R (IK)
                 }
                 for pole_name, col_idx in pole_collection_map.items():
                     bpy.ops.pose.select_all(action='DESELECT')
                     pole_bone = RigArmatureObj.pose.bones.get(pole_name)
                     if pole_bone:
                         pole_bone.bone.select = True
                         bpy.ops.armature.move_to_collection(collection_index=col_idx)
                 
                 # Theme Assignments
                 theme_assignments = {"EyeTracker": "THEME01", "Eye.L": "THEME09", "Eye.R": "THEME09"}
                 for b_name, theme in theme_assignments.items():
                     bone = RigArmatureObj.pose.bones.get(b_name)
                     if bone: bone.color.palette = theme
                 
                 # Create FK toe bones (toe_fk.L/R) from ORG-toe_ik bones
                 bpy.ops.object.mode_set(mode='EDIT')
                 arm = RigArmatureObj.data
                 for side in ['.L', '.R']:
                     org_toe = arm.edit_bones.get(f'ORG-toe_ik{side}')
                     foot_fk = arm.edit_bones.get(f'foot_fk{side}')
                     if org_toe and foot_fk:
                         new_bone = arm.edit_bones.new(f'toe_fk{side}')
                         new_bone.head = org_toe.head.copy()
                         new_bone.tail = org_toe.tail.copy()
                         new_bone.roll = org_toe.roll
                         new_bone.parent = foot_fk
                         new_bone.use_connect = True
                 bpy.ops.object.mode_set(mode='POSE')
                 
                 # Assign custom shape for FK toe bones
                 foot_fk_l = RigArmatureObj.pose.bones.get('foot_fk.L')
                 for side in ['.L', '.R']:
                     toe_fk = RigArmatureObj.pose.bones.get(f'toe_fk{side}')
                     if toe_fk and foot_fk_l:
                         toe_fk.custom_shape = foot_fk_l.custom_shape
                         toe_fk.color.palette = 'THEME03'
                 
                 # Move toe_fk bones to FK leg collections
                 bpy.ops.pose.select_all(action='DESELECT')
                 toe_fk_l = RigArmatureObj.pose.bones.get('toe_fk.L')
                 if toe_fk_l:
                     toe_fk_l.bone.select = True
                     bpy.ops.armature.move_to_collection(collection_index=12)
                 bpy.ops.pose.select_all(action='DESELECT')
                 toe_fk_r = RigArmatureObj.pose.bones.get('toe_fk.R')
                 if toe_fk_r:
                     toe_fk_r.bone.select = True
                     bpy.ops.armature.move_to_collection(collection_index=13)

                 # Neck Tweak
                 bpy.ops.object.mode_set(mode='EDIT')
                 arm = RigArmatureObj.data
                 if 'ORG-Bip001Neck' in arm.edit_bones: arm.edit_bones['ORG-Bip001Neck'].name = 'Bip001Neck'
                 if 'ORG-Bip001Head' in arm.edit_bones: arm.edit_bones['ORG-Bip001Head'].name = 'Bip001Head'
                 
                 if 'Bip001Neck' in arm.edit_bones:
                     neck_bone = arm.edit_bones['Bip001Neck']
                     new_bone = arm.edit_bones.new('Bip001Neck._fk')
                     new_bone.head = neck_bone.head.copy(); new_bone.tail = neck_bone.tail.copy(); new_bone.roll = neck_bone.roll
                     new_bone.parent = neck_bone.parent
                     rot_mat = mathutils.Matrix.Rotation(-1.5708, 4, 'X')
                     new_bone.tail = new_bone.head + rot_mat @ (new_bone.tail - new_bone.head)
                     new_bone.tail.z = new_bone.head.z
                     new_bone.tail = new_bone.head + (new_bone.tail - new_bone.head).normalized() * 0.05
                     neck_bone.use_connect = False; neck_bone.parent = new_bone
                 
                 if 'Bip001Head' in arm.edit_bones:
                     head_bone = arm.edit_bones['Bip001Head']
                     new_bone = arm.edit_bones.new('Bip001Head._fk')
                     new_bone.head = head_bone.head.copy(); new_bone.tail = head_bone.tail.copy(); new_bone.roll = head_bone.roll
                     new_bone.parent = head_bone.parent
                     rot_mat = mathutils.Matrix.Rotation(-1.5708, 4, 'X') # Re-using variable, safe
                     new_bone.tail = new_bone.head + rot_mat @ (new_bone.tail - new_bone.head)
                     new_bone.tail.z = new_bone.head.z
                     new_bone.tail = new_bone.head + (new_bone.tail - new_bone.head).normalized() * 0.05
                     head_bone.use_connect = False; head_bone.parent = new_bone
                 
                 bpy.ops.object.mode_set(mode='POSE')
                 spine2_fk = RigArmatureObj.pose.bones.get("Spine2_fk")
                 if "Bip001Neck._fk" in RigArmatureObj.pose.bones:
                     tb = RigArmatureObj.pose.bones["Bip001Neck._fk"]
                     if spine2_fk: tb.custom_shape = spine2_fk.custom_shape
                     tb.custom_shape_transform = RigArmatureObj.pose.bones["Bip001Neck"]
                 if "Bip001Head._fk" in RigArmatureObj.pose.bones:
                     tb = RigArmatureObj.pose.bones["Bip001Head._fk"]
                     if spine2_fk: tb.custom_shape = spine2_fk.custom_shape
                     tb.custom_shape_transform = RigArmatureObj.pose.bones["Bip001Head"]
                 
                 # Move tweak bones to collection (index 1)
                 bones_tweak_move = {0: ["Bip001Neck", "Bip001Head"], 1: ["Bip001Neck._fk", "Bip001Head._fk"]}
                 for cidx, bnames in bones_tweak_move.items():
                     bpy.ops.pose.select_all(action='DESELECT')
                     for b in bnames: 
                         pb = RigArmatureObj.pose.bones.get(b)
                         if pb: pb.bone.select = True
                     bpy.ops.armature.move_to_collection(collection_index=cidx)
                 
                 # Setup toe FK/IK switching
                 # toe_fk works in FK mode (IK_FK = 1), toe_ik works in IK mode (IK_FK = 0)
                 for side in ['.L', '.R']:
                     toe_fk_bone = RigArmatureObj.pose.bones.get(f'toe_fk{side}')
                     org_toe = RigArmatureObj.pose.bones.get(f'ORG-toe_ik{side}')
                     toe_ik_bone = RigArmatureObj.pose.bones.get(f'toe_ik{side}')
                     thigh_parent = RigArmatureObj.pose.bones.get(f'thigh_parent{side}')
                     
                     if toe_fk_bone and org_toe and thigh_parent and toe_ik_bone:
                         # Check for existing toe_ik copy constraint and add IK_FK driver
                         for con in org_toe.constraints:
                             if con.type == 'COPY_TRANSFORMS' and con.subtarget == f'toe_ik{side}':
                                 # Add driver for IK influence (1 - IK_FK)
                                 ik_driver = con.driver_add('influence').driver
                                 ik_driver.type = 'SCRIPTED'
                                 ik_var = ik_driver.variables.new()
                                 ik_var.name = 'ik_fk'
                                 ik_var.type = 'SINGLE_PROP'
                                 ik_var.targets[0].id = RigArmatureObj
                                 ik_var.targets[0].data_path = f'pose.bones["thigh_parent{side}"]["IK_FK"]'
                                 ik_driver.expression = '1 - ik_fk'
                                 break
                         
                         # Add copy rotation constraint to ORG-toe_ik from toe_fk for FK mode
                         fk_constraint = org_toe.constraints.new('COPY_ROTATION')
                         fk_constraint.name = 'Copy Rotation FK'
                         fk_constraint.target = RigArmatureObj
                         fk_constraint.subtarget = f'toe_fk{side}'
                         fk_constraint.target_space = 'LOCAL'
                         fk_constraint.owner_space = 'LOCAL'
                         
                         # Add driver for FK influence (equals IK_FK value - 1=FK mode, 0=IK mode)
                         driver = fk_constraint.driver_add('influence').driver
                         driver.type = 'SCRIPTED'
                         var = driver.variables.new()
                         var.name = 'ik_fk'
                         var.type = 'SINGLE_PROP'
                         var.targets[0].id = RigArmatureObj
                         var.targets[0].data_path = f'pose.bones["thigh_parent{side}"]["IK_FK"]'
                         driver.expression = 'ik_fk'
                 
                 # Bone Limit
                 bpy.ops.object.mode_set(mode='EDIT')
                 for bone in RigArmatureObj.data.edit_bones:
                     if (bone.head - bone.tail).length > 1.0:
                         bone.tail = bone.head + (bone.tail - bone.head).normalized() * 0.5
                 bpy.ops.object.mode_set(mode='OBJECT')
                 
                 RigArmatureObj.data.display_type = 'STICK'
                 RigArmatureObj.show_in_front = True
                 
                 # Delete Original Armature
                 orig_arm = bpy.data.objects.get(OrigArmature)
                 if orig_arm:
                     try: bpy.data.objects.remove(orig_arm, do_unlink=True)
                     except: pass

        self.report({'INFO'}, "Rigify generation complete.")
        return {'FINISHED'}

# Logic is massive, I will write the file with the FULL content now.
