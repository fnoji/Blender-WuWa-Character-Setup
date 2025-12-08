
<img width="1913" height="1000" alt="スクリーンショット 2025-12-08 001448" src="https://github.com/user-attachments/assets/4c202b14-4e40-4988-9e35-5ed10831c15d" />

[ 🇯🇵 **日本語** ](README_JA.md)

# WuWa Character Setup for Blender

**WuWa Character Setup** is a Blender addon designed to easily configure model and shader settings.
It automates the entire process of importing Wuthering Waves character UEmodels into Blender, setting up shading, and rigging. It provides automatic shading setup, rigging with Rigify, and facial expression control setups.

This addon was developed by user Akatsuki on Discord. Since I joined as a maintainer from version 1.4, we are publishing it on GitHub starting from the initial version.
The shader used is the [shader optimized for Wuthering Waves models](https://discord.com/channels/894925535870865498/1213552094678614038/1272958039221338114) created by JaredNyts.
The original scripts for the rig and face rig were [created by Scheinze](https://discord.com/channels/894925535870865498/1320434868806615122/1320434868806615122), and I have optimized them from the final version.
The references used for the face panel were [created by Micchi](https://discord.com/channels/894925535870865498/1216442545782132746/1216442545782132746).

> [!IMPORTANT]
> To import `.uemodel` files, the **UEFormat** addon is typically required. Please ensure it is installed if you intend to use the "Import Model" feature.

## Features

- **One-Click Setup**: "Run Entire Setup" automates the entire workflow from import to rigging.
- **Model Import**: Uses UEFormat to import models, automatically correcting bone orientation.
- **Shader Import**: Automatically sets up materials and textures.
- **Rigify Generation**: Generates a Rigify control rig tailored for the characters.
- **Face Panel**: Creates a UI panel and drivers for operating facial expressions (Blush, Disgust, etc.).
- **Global Rendering Properties**:
  - Switch between various Light Modes.
  - Customize Ambient, Light, Shadow, and Rim colors.
  - Adjust Shadow Position.
  - Toggle Outlines and specific effects like "Star Motion" or "Transparent Hair".

## Requirements

- **Blender**: Version 4.1 or later (Note: The shader itself requires Goo Engine).
- **Dependencies**: [UEFormat](https://github.com/Start5132/Blender-UEFormat) (Required for importing `.uemodel` files).

## Installation

1. Download the latest release `.zip` file.
2. Open Blender.
3. Go to **Edit > Preferences > Add-ons**.
4. Click **Install...** and select the downloaded zip file.
5. Enable the addon by checking the box next to **"WuWa Character Setup"**.

## Usage

The addon panel can be found in the 3D Viewport Sidebar (N-Panel) under the **Wuthering Waves** tab.

### Basic Workflow
1. **Import Model**: Load the character `.uemodel` file.
2. **Import Shader**: Select the mesh and run the shader setup.
3. **Rigify**: Generate the control rig.
4. **Face Panel**: Setup the face drivers and panel.

*Alternatively, use **Run Entire Setup** to perform these steps all at once.*

### Global Properties
You can adjust the character's appearance using settings in the panel:
- **Light Mode**: Changes the lighting calculation mode.
- **Colors**: Customize the character's shading colors.
- **Shadow Position**: Rotates the direction of the cast shadow.

## Credits

- **Authors**: Akatsuki, fnoji
- **Version**: 1.4.0

## Notes

This addon has been a topic of discussion in the English-speaking community server Omatsuri. As I have become a maintainer, Japanese support is now available.
However, I have not localized the addon UI itself into Japanese as it would be complicated and I do not personally need it.

If you have any questions, please create an issue on GitHub, ask in English within Omatsuri, or contact the following:

- [Discord server](https://discord.gg/3p9cT4ajqy) / Hoyo Animation Creator [JP/EN]
- [Twitter / fnoji](https://twitter.com/fnoji) / [JP/EN]
