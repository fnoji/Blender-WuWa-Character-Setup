import bpy
import time
from bpy.types import Operator
from .utils import logger

class WW_OT_RunEntireSetup(Operator):
    bl_idname = "shader.run_entire_setup"
    bl_label = "Run Entire Setup"
    bl_description = "Run the entire setup process: Import Model -> Import Shader -> Generate Rig -> Setup Head"
    bl_options = {"REGISTER", "UNDO"}

    _timer = None
    _state = "IDLE"
    _start_time = 0
    _last_check_time = 0
    _target_armature = None
    _target_mesh = None

    def modal(self, context, event):
        if event.type == 'TIMER':
            # Run checks every 0.5 seconds to avoid spamming
            if time.time() - self._last_check_time < 0.5:
                return {'PASS_THROUGH'}
            self._last_check_time = time.time()

            if self._state == "WAIT_FOR_MODEL":
                # Check if a new armature is active
                obj = context.active_object
                # Ensure it's an armature AND it's a new object (not in the list of objects from before import)
                if obj and obj.type == 'ARMATURE' and obj not in self._initial_objects:
                    # Assuming the import selects the new armature
                    self._target_armature = obj
                    logger.info(f"Model imported: {obj.name}")
                    
                    # Transition to selecting mesh
                    self._state = "SELECT_MESH"
                    return {'PASS_THROUGH'}
                
                # Timeout check (e.g., 5 minutes for user file selection and import)
                if time.time() - self._start_time > 300:
                    self.report({'WARNING'}, "Run Entire Setup: Timed out waiting for model import.")
                    self.cancel(context)
                    return {'CANCELLED'}

            elif self._state == "SELECT_MESH":
                # Find the mesh child of the armature
                found_mesh = None
                for child in self._target_armature.children:
                    if child.type == 'MESH':
                        found_mesh = child
                        break
                
                if found_mesh:
                    self._target_mesh = found_mesh
                    # Deselect armature, select mesh
                    bpy.ops.object.select_all(action='DESELECT')
                    found_mesh.select_set(True)
                    context.view_layer.objects.active = found_mesh
                    logger.info(f"Selected mesh: {found_mesh.name}")
                    
                    # Clean up existing materials if needed (optional, but good for cleanliness)
                    # For now, just proceed to import shader
                    self._state = "IMPORT_SHADER"
                else:
                    # Keep waiting or fail? The import might take a moment to parent?
                    # Usually parenting happens immediately on import.
                    # Let's wait a bit more if not found immediately, or fail.
                    if time.time() - self._start_time > 310: # small buffer
                         self.report({'ERROR'}, "Run Entire Setup: Could not find mesh child of imported armature.")
                         self.cancel(context)
                         return {'CANCELLED'}

            elif self._state == "IMPORT_SHADER":
                # Trigger shader import
                # We assume the user wants to pick a file. 
                logger.info("Starting Import Shader...")
                context.scene.ww_setup_status = "IMPORTING_SHADER"
                bpy.ops.shader.import_shader('INVOKE_DEFAULT')
                self._state = "WAIT_FOR_SHADER"
                
            elif self._state == "WAIT_FOR_SHADER":
                # Check for the completion flag set by Import Textures
                if getattr(context.scene, "ww_setup_status", "") == "TEXTURES_DONE":
                    logger.info("Shader and Texture import completed.")
                    self._state = "SELECT_ARMATURE"
                    
                    # Timeout for shader step (e.g. 2 mins)
                    # We can use a separate variable for step start time if needed.

            elif self._state == "SELECT_ARMATURE":
                if self._target_armature:
                    bpy.ops.object.select_all(action='DESELECT')
                    self._target_armature.select_set(True)
                    context.view_layer.objects.active = self._target_armature
                    logger.info(f"Selected armature: {self._target_armature.name}")
                    self._state = "GENERATE_RIG"

            elif self._state == "GENERATE_RIG":
                logger.info("Generating Rig...")
                # This is usually synchronous
                # But Rigify might take a moment. 
                # The operator execution blocks Blender, so next timer tick happens after it's done.
                try:
                    bpy.ops.shader.rigify_armature()
                    self._state = "SETUP_HEAD"
                except Exception as e:
                    self.report({'ERROR'}, f"Rig generation failed: {e}")
                    self.cancel(context)
                    return {'CANCELLED'}

            elif self._state == "SETUP_HEAD":
                logger.info("Setting up Head Driver...")
                try:
                    bpy.ops.shader.setup_head_driver()
                except Exception as e:
                    self.report({'ERROR'}, f"Head setup failed: {e}")
                
                self.report({'INFO'}, "Run Entire Setup: Completed successfully.")
                self.cancel(context)
                return {'FINISHED'}

        return {'PASS_THROUGH'}

    def invoke(self, context, event):
        context.scene.ww_setup_status = "WAIT_FOR_MODEL"
        self._state = "WAIT_FOR_MODEL"
        self._start_time = time.time()
        self._last_check_time = time.time()
        
        # Capture existing objects
        self._initial_objects = set(context.scene.objects)
        
        # Start Import Model
        # This will likely open a file browser.
        # The user interacts with it, and eventually the model appears.
        logger.info("Starting Run Entire Setup sequence...")
        bpy.ops.shader.import_uemodel('INVOKE_DEFAULT')

        wm = context.window_manager
        self._timer = wm.event_timer_add(0.1, window=context.window)
        wm.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def cancel(self, context):
        wm = context.window_manager
        wm.event_timer_remove(self._timer)
