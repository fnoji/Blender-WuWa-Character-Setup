import bpy
import logging
from typing import Optional
from .utils import extract_character_name, logger

def set_animate_mode(context, mesh_data, state: bool):
    """
    Toggles Animate Mode for a specific character mesh.
    
    Args:
        context: Blender context
        mesh_data: Object containing mesh_name
        state: True to enable Animate Mode (High -> Low), False to disable (Low -> High)
    """
    obj = bpy.data.objects.get(mesh_data.mesh_name)
    if not obj or obj.type != 'MESH':
        logger.warning(f"Mesh {mesh_data.mesh_name} not found or not a mesh.")
        return

    char_name = extract_character_name(obj.name)
    logger.info(f"Setting Animate Mode to {state} for {obj.name} (Char: {char_name})")

    # Force Outlines OFF if enabling Animate Mode
    if state:
        if context.scene.outlines_enabled:
            context.scene.outlines_enabled = False
            # Update modifiers to reflect the change (logic copied from WW_OT_ToggleOutlines)
            for o in bpy.data.objects:
                if o.type == 'MESH':
                    for modifier in o.modifiers:
                        if "outlines" in modifier.name.lower() and modifier.type == "NODES":
                            modifier.show_viewport = False
            logger.info("Forced Outlines OFF for Animate Mode")

    swapped_count = 0
    
    for slot in obj.material_slots:
        if not slot.material:
            continue
        
        current_mat = slot.material
        
        if state: # Enable Animate Mode (High Quality -> Simple Low)
            # Skip if already a Low material
            if current_mat.name.endswith("_Low"):
                continue

            target_mat_name = f"{current_mat.name}_Low"
            target_mat = bpy.data.materials.get(target_mat_name)

            if target_mat:
                # Use existing Low material
                slot.material = target_mat
                swapped_count += 1
            else:
                # Create new Low material
                target_mat = current_mat.copy()
                target_mat.name = target_mat_name
                
                # Simplify the new material
                simplify_material_nodes(target_mat, current_mat)
                
                # Assign
                slot.material = target_mat
                swapped_count += 1
            
            # Ensure permanence for both
            if target_mat:
                target_mat.use_fake_user = True
            current_mat.use_fake_user = True

        else: # Disable Animate Mode (Simple Low -> High Quality)
            # Skip if not a Low material
            if not current_mat.name.endswith("_Low"):
                continue

            # Original name is current name without "_Low" suffix
            original_mat_name = current_mat.name[:-4] 
            target_mat = bpy.data.materials.get(original_mat_name)

            if target_mat:
                slot.material = target_mat
                swapped_count += 1
            else:
                logger.warning(f"Original material '{original_mat_name}' not found for '{current_mat.name}'. Keeping Low material.")

    logger.info(f"Swapped {swapped_count} materials for {obj.name}")


def simplify_material_nodes(low_mat: bpy.types.Material, original_mat: bpy.types.Material):
    """
    Replaces the node tree of low_mat with a simple Texture -> Output setup.
    Prioritizes finding the main diffuse/albedo texture.
    """
    low_mat.use_nodes = True
    tree = low_mat.node_tree
    tree.nodes.clear()

    # Create Nodes
    output_node = tree.nodes.new('ShaderNodeOutputMaterial')
    output_node.location = (300, 0)
    
    tex_node = tree.nodes.new('ShaderNodeTexImage')
    tex_node.location = (0, 0)

    # Find source image from original material
    source_image = None
    use_uv2 = False # Flag for odd-eye/fix-eye-uv support

    if original_mat.use_nodes and original_mat.node_tree:
        orig_tree = original_mat.node_tree
        
        # Check for Fix Eye UV modification (UV2 usage)
        for node in orig_tree.nodes:
            # JaredNyts style (Eye Depth group)
            if node.type == 'GROUP' and node.node_tree and "Eye Depth" in node.node_tree.name:
                # Inspect inside the group for UV Map usage
                for sub_node in node.node_tree.nodes:
                    if sub_node.type == 'UVMAP' and sub_node.uv_map == "UV2":
                        use_uv2 = True
                        break
            
            # Jonn style (Top level UV Map node)
            if node.type == 'UVMAP' and node.uv_map == "UV2":
                use_uv2 = True

            if use_uv2:
                break
        
        # Collect all image nodes
        image_nodes = [n for n in orig_tree.nodes if isinstance(n, bpy.types.ShaderNodeTexImage) and n.image]
        
        # Priority 1: Connected to Principled BSDF "Base Color"
        for node in orig_tree.nodes:
            if isinstance(node, bpy.types.ShaderNodeBsdfPrincipled):
                if "Base Color" in node.inputs and node.inputs["Base Color"].is_linked:
                    link = node.inputs["Base Color"].links[0]
                    from_node = link.from_node
                    if isinstance(from_node, bpy.types.ShaderNodeTexImage) and from_node.image:
                        # Found explicitly linked base color
                        source_image = from_node.image
                        break
        
        if not source_image:
            # Priority 2: Image name ends with _D (e.g. T_..._D.png)
            # We check specific suffixes that denote Diffuse/Albedo
            for node in image_nodes:
                img_name = node.image.name.lower()
                # Check for _D before extension. 
                # Split by dot to handle extensions roughly
                base_name = img_name.rsplit('.', 1)[0]
                if base_name.endswith("_d") or base_name.endswith("_diff"):
                    source_image = node.image
                    break
        
        if not source_image:
             # Priority 3: Node name contains "Diffuse", "Albedo", "Base Color"
            for node in image_nodes:
                node_name = node.name.lower()
                if "diffuse" in node_name or "albedo" in node_name or "base color" in node_name:
                    source_image = node.image
                    break

        if not source_image and image_nodes:
            # Priority 4: Fallback to first image
            source_image = image_nodes[0].image

    # Assign image if found
    if source_image:
        tex_node.image = source_image
    
    # Handle UV2 if detected
    if use_uv2:
        uv_node = tree.nodes.new('ShaderNodeUVMap')
        uv_node.location = (-200, 0)
        uv_node.uv_map = "UV2"
        try:
            tree.links.new(uv_node.outputs['UV'], tex_node.inputs['Vector'])
        except Exception as e:
            logger.error(f"Failed to link UV node in simplified material {low_mat.name}: {e}")
    
    # Connect
    try:
        tree.links.new(tex_node.outputs['Color'], output_node.inputs['Surface'])
    except Exception as e:
        logger.error(f"Failed to link nodes in simplified material {low_mat.name}: {e}")
