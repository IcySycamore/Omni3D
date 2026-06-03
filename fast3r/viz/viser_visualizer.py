# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.

# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

import time
import threading
import numpy as np
from tqdm.auto import tqdm
import imageio.v3 as iio
from matplotlib import cm
import cv2
from scipy import ndimage
import torch

import viser
import viser.transforms as tf
from fast3r.dust3r.utils.device import to_numpy
from fast3r.models.multiview_dust3r_module import MultiViewDUSt3RLitModule

# ----------------- Helper Functions -----------------

def detect_sky_mask(img_rgb):
    """
    Detect sky pixels using HSV color space and morphological operations.
    Args:
        img_rgb: RGB image normalized to [-1, 1]
    Returns:
        Boolean mask (as int8) where True indicates non-sky pixels.
    """
    img = ((img_rgb + 1) * 127.5).astype(np.uint8)
    img_bgr = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
    hsv = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2HSV)

    lower_blue = np.array([105, 50, 140])
    upper_blue = np.array([135, 255, 255])
    mask_blue = cv2.inRange(hsv, lower_blue, upper_blue)

    lower_light_blue = np.array([95, 5, 150])
    upper_light_blue = np.array([145, 100, 255])
    mask_light_blue = cv2.inRange(hsv, lower_light_blue, upper_light_blue)

    lower_white = np.array([0, 0, 235])
    upper_white = np.array([180, 10, 255])
    mask_white = cv2.inRange(hsv, lower_white, upper_white)

    mask = mask_blue | mask_light_blue | mask_white

    height = mask.shape[0]
    upper_third = int(height * 0.4)
    upper_region = hsv[:upper_third, :, :]
    mask[:upper_third, :] |= ((upper_region[:, :, 1] < 50) & (upper_region[:, :, 2] > 150))

    kernel = np.ones((7, 7), np.uint8)
    mask = cv2.dilate(mask, kernel, iterations=1)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)

    mask = mask.astype(bool)
    labels, num_labels = ndimage.label(mask)
    if num_labels > 0:
        top_row_labels = set(labels[0, :])
        top_row_labels.discard(0)
        if top_row_labels:
            mask = np.isin(labels, list(top_row_labels))
            labels, num_labels = ndimage.label(mask)
            if num_labels > 0:
                sizes = ndimage.sum(mask, labels, range(1, num_labels + 1))
                mask_size = mask.size
                big_enough = sizes > mask_size * 0.01
                mask = np.isin(labels, np.where(big_enough)[0] + 1)
    return (~mask).astype(np.int8)

def is_outdoor_scene(frame_data_list):
    """Detect whether the scene is outdoor by checking the proportion of sky pixels.

    Computes the sky ratio for each frame and returns ``True`` when at least
    25 % of the frames contain more than 20 % sky pixels.

    Args:
        frame_data_list (list of dict): Per-frame data dictionaries; each
            must contain the key ``'sorted_not_sky_global'`` (a boolean-like
            mask where 1 = not-sky, 0 = sky).

    Returns:
        bool: ``True`` if the scene is classified as outdoor, ``False`` otherwise.
    """
    sky_ratios = []
    for fd in frame_data_list:
        mask = fd.get('sorted_not_sky_global', np.ones(1))
        sky_ratio = 1.0 - np.mean(mask)
        sky_ratios.append(float(sky_ratio))
    significant = sum(1 for ratio in sky_ratios if ratio > 0.2)
    return significant >= len(sky_ratios) / 4

# ----------------- Update Handlers -----------------
# These functions are lightweight and respond only to their respective events.
def update_frame_visibility(server, frame_data_list, gui_timestep, num_frames, gui_show_global, gui_show_local, gui_show_high_conf, gui_show_low_conf):
    """Update the visibility of all frame nodes based on the current timestep.

    Shows frames with index ``<= gui_timestep.value``; the global and local
    point cloud nodes are shown only when their respective checkboxes are
    enabled.

    Args:
        server (viser.ViserServer): The running viser server.
        frame_data_list (list of dict): Per-frame scene data including
            ``'frame_node'``, ``'frustum_node'``, ``'point_node_global'``,
            and ``'point_node_local'``.
        gui_timestep (viser.GuiSliderHandle): Slider controlling the current
            timestep.
        num_frames (int): Total number of frames.
        gui_show_global (viser.GuiCheckboxHandle): Toggle for global point clouds.
        gui_show_local (viser.GuiCheckboxHandle): Toggle for local point clouds.
        gui_show_high_conf (viser.GuiCheckboxHandle): Toggle for high-confidence views.
        gui_show_low_conf (viser.GuiCheckboxHandle): Toggle for low-confidence views.
    """
    current = int(gui_timestep.value)
    with server.atomic():
        for i in range(num_frames):
            fd = frame_data_list[i]
            # For simplicity, we show the frame if i <= current.
            show_frame = (i <= current)
            fd['frame_node'].visible = show_frame
            fd['frustum_node'].visible = show_frame
            fd['point_node_global'].visible = show_frame and gui_show_global.value
            fd['point_node_local'].visible = show_frame and gui_show_local.value
        server.flush()

def update_point_cloud_colors(server, frame_data_list, gui_timestep, gui_show_confidence_color, gui_rainbow_color_option, gui_show_global, gui_show_local):
    """Swap point cloud colors for all frames based on the active color mode.

    Three mutually exclusive color modes are supported (evaluated in order):
    confidence heatmap, per-view rainbow color, and original RGB.

    Args:
        server (viser.ViserServer): The running viser server.
        frame_data_list (list of dict): Per-frame scene data.
        gui_timestep (viser.GuiSliderHandle): Current timestep slider (unused
            directly but kept for API consistency).
        gui_show_confidence_color (viser.GuiCheckboxHandle): Toggle confidence
            coloring.
        gui_rainbow_color_option (viser.GuiCheckboxHandle): Toggle rainbow
            per-view coloring.
        gui_show_global (viser.GuiCheckboxHandle): Toggle global point cloud
            visibility.
        gui_show_local (viser.GuiCheckboxHandle): Toggle local point cloud
            visibility.
    """
    with server.atomic():
        for i in range(len(frame_data_list)):
            fd = frame_data_list[i]
            if gui_show_confidence_color.value:
                colors_global = fd['colors_confidence_global']
                colors_local = fd['colors_confidence_local']
            elif gui_rainbow_color_option.value:
                colors_global = fd['colors_rainbow_global']
                colors_local = fd['colors_rainbow_local']
            else:
                colors_global = fd['colors_rgb_global']
                colors_local = fd['colors_rgb_local']
            fd['point_node_global'].colors = colors_global if gui_show_global.value else []
            fd['point_node_local'].colors = colors_local if gui_show_local.value else []
            server.flush()

def update_points_filtering(server, frame_data_list, gui_timestep, gui_min_conf_percentile, gui_mask_sky, gui_show_confidence_color, gui_rainbow_color_option):
    """Filter and update point clouds based on confidence percentile and sky masking.

    For each frame, the top ``(100 - gui_min_conf_percentile.value)`` percent
    of points (sorted by descending confidence) are kept.  If sky masking is
    enabled, sky pixels are additionally removed.

    Args:
        server (viser.ViserServer): The running viser server.
        frame_data_list (list of dict): Per-frame scene data containing sorted
            points, colors, and sky masks.
        gui_timestep (viser.GuiSliderHandle): Current timestep slider (unused
            directly but kept for API consistency).
        gui_min_conf_percentile (viser.GuiSliderHandle): Slider controlling
            the confidence percentile cut-off (0 = keep all, 100 = keep none).
        gui_mask_sky (viser.GuiCheckboxHandle): Toggle sky-pixel removal.
        gui_show_confidence_color (viser.GuiCheckboxHandle): Toggle confidence
            heatmap coloring.
        gui_rainbow_color_option (viser.GuiCheckboxHandle): Toggle per-view
            rainbow coloring.
    """
    for i in range(len(frame_data_list)):
        fd = frame_data_list[i]
        total_global = len(fd['sorted_pts3d_global'])
        total_local = len(fd['sorted_pts3d_local'])

        # Calculate number of points to show based on the percentile
        num_global = max(1, int(total_global * (100 - gui_min_conf_percentile.value) / 100))
        num_local = max(1, int(total_local * (100 - gui_min_conf_percentile.value) / 100))

        # Apply sky masking if enabled
        if gui_mask_sky.value:
            mask_global = fd['sorted_not_sky_global'][:num_global]
            mask_local = fd['sorted_not_sky_local'][:num_local]
            # Filter points based on the mask
            pts3d_global = fd['sorted_pts3d_global'][:num_global][mask_global > 0]
            pts3d_local = fd['sorted_pts3d_local'][:num_local][mask_local > 0]
            
            # Select the appropriate colors based on the active color option
            if gui_show_confidence_color.value:
                colors_global = fd['colors_confidence_global'][:num_global][mask_global > 0]
                colors_local = fd['colors_confidence_local'][:num_local][mask_local > 0]
            elif gui_rainbow_color_option.value:
                colors_global = fd['colors_rainbow_global'][:num_global][mask_global > 0]
                colors_local = fd['colors_rainbow_local'][:num_local][mask_local > 0]
            else:
                colors_global = fd['colors_rgb_global'][:num_global][mask_global > 0]
                colors_local = fd['colors_rgb_local'][:num_local][mask_local > 0]
        else:
            pts3d_global = fd['sorted_pts3d_global'][:num_global]
            pts3d_local = fd['sorted_pts3d_local'][:num_local]

            # Select the appropriate colors based on the active color option
            if gui_show_confidence_color.value:
                colors_global = fd['colors_confidence_global'][:num_global]
                colors_local = fd['colors_confidence_local'][:num_local]
            elif gui_rainbow_color_option.value:
                colors_global = fd['colors_rainbow_global'][:num_global]
                colors_local = fd['colors_rainbow_local'][:num_local]
            else:
                colors_global = fd['colors_rgb_global'][:num_global]
                colors_local = fd['colors_rgb_local'][:num_local]

        # Update point clouds
        fd['point_node_global'].points = pts3d_global
        fd['point_node_local'].points = pts3d_local
        # update colors
        fd['point_node_global'].colors = colors_global
        fd['point_node_local'].colors = colors_local
                
        server.flush()

# ---------------Helper Functions to save PLY--------------------
def collect_visible_points(frame_data_list, current_timestep):
    """Collect all currently visible 3D points and their colors up to *current_timestep*.

    Iterates over global and local point cloud nodes for frames
    ``0 .. current_timestep`` (inclusive) and concatenates the points/colors
    of any node that is currently visible and non-empty.

    Args:
        frame_data_list (list of dict): Per-frame scene data with
            ``'point_node_global'`` and ``'point_node_local'`` entries.
        current_timestep (int): Index of the last frame to include.

    Returns:
        tuple: ``(points, colors)`` where both are ``np.ndarray`` of shapes
        ``(N, 3)`` and ``(N, 3)`` respectively, or ``(None, None)`` if no
        visible points are found.
    """
    # collects all visible points up to the current timestep t
    points = []
    colors = []
    for i in range(current_timestep + 1):
        fd = frame_data_list[i]
        
        # Global points
        if fd['point_node_global'].visible and len(fd['point_node_global'].points) > 0:
            pts = fd['point_node_global'].points
            clr = fd['point_node_global'].colors
            
            # Ensure colors are correctly processed
            if clr.dtype == np.float32:
                # If colors are in float format (likely normalized)
                clr = np.clip((clr + 1) * 127.5, 0, 255).astype(np.uint8)
            
            points.append(pts)
            colors.append(clr)
        
        # Local points
        if fd['point_node_local'].visible and len(fd['point_node_local'].points) > 0:
            pts = fd['point_node_local'].points
            clr = fd['point_node_local'].colors
            
            # Ensure colors are correctly processed
            if clr.dtype == np.float32:
                # If colors are in float format (likely normalized)
                clr = np.clip((clr + 1) * 127.5, 0, 255).astype(np.uint8)
            
            points.append(pts)
            colors.append(clr)
    
    if not points:
        return None, None
    return np.concatenate(points), np.concatenate(colors)

def safe_color_conversion(colors):
    """Convert an array of colors to uint8 format, handling multiple input ranges.

    Supported input formats:

    * ``float`` in [0, 1] – multiplied by 255.
    * ``float`` in [-1, 1] – mapped from ``[-1, 1]`` to ``[0, 255]``.
    * ``float`` outside the above ranges – linearly rescaled to ``[0, 255]``.
    * Integer types – clipped to ``[0, 255]`` and cast to ``uint8``.

    Args:
        colors (np.ndarray): Color array of shape (N, 3) in any of the
            formats described above.

    Returns:
        np.ndarray: Color array of shape (N, 3) with ``dtype=uint8``.
    """
    # If colors are in float format (normalized)
    if colors.dtype in [np.float32, np.float64]:
        # Handle two common normalization ranges
        if colors.min() >= 0 and colors.max() <= 1:
            # 0 to 1 range
            colors_uint8 = np.clip(colors * 255, 0, 255).astype(np.uint8)
        elif colors.min() >= -1 and colors.max() <= 1:
            # -1 to 1 range (common in some frameworks)
            colors_uint8 = np.clip((colors + 1) * 127.5, 0, 255).astype(np.uint8)
        else:
            # Unexpected range, try linear scaling
            colors_min, colors_max = colors.min(), colors.max()
            colors_uint8 = np.clip(
                ((colors - colors_min) / (colors_max - colors_min)) * 255, 
                0, 255
            ).astype(np.uint8)
    else:
        # Already in uint8 or similar integer format
        colors_uint8 = np.clip(colors, 0, 255).astype(np.uint8)
    
    return colors_uint8

def generate_ply_bytes(points, colors):
    """Serialize a point cloud to binary PLY format.

    Builds a PLY header and packs 3D coordinates (float32) together with
    RGB colors (uint8) into a compact binary blob suitable for file download.

    Args:
        points (np.ndarray): Point coordinates of shape (N, 3), dtype float32.
        colors (np.ndarray): Point colors of shape (N, 3) in any format
            accepted by :func:`safe_color_conversion`.

    Returns:
        bytes: Binary PLY file content (header + vertex data).
    """
    # generate binary ply object bytes from the point cloud and their color
    header = [
        "ply",
        "format binary_little_endian 1.0",
        f"element vertex {len(points)}",
        "property float x",
        "property float y",
        "property float z",
        "property uchar red",
        "property uchar green",
        "property uchar blue",
        "end_header",
    ]
    header = "\n".join(header).encode("ascii") + b"\n"
    
    colors_uint8 = safe_color_conversion(colors)

    # Pack data into binary format
    data = np.empty(len(points), dtype=[
        ("xyz", np.float32, 3),
        ("rgb", np.uint8, 3),
    ])
    data["xyz"] = points
    data["rgb"] = colors_uint8
    
    return header + data.tobytes()

# ----------------- Playback Loop -----------------
def playback_loop(gui_playing, gui_timestep, num_frames, gui_framerate):
    """Continuously advance the timestep slider at the requested frame rate.

    Runs in an infinite loop (intended for a background thread): increments
    the timestep by one each tick when ``gui_playing`` is enabled, then
    sleeps for ``1 / gui_framerate`` seconds.

    Args:
        gui_playing (viser.GuiCheckboxHandle): Checkbox that controls
            whether playback is active.
        gui_timestep (viser.GuiSliderHandle): Slider whose value is
            incremented each frame.
        num_frames (int): Total number of frames (used for modular wrap-around).
        gui_framerate (viser.GuiSliderHandle): Slider that controls the
            target playback speed in frames per second.
    """
    while True:
        if gui_playing.value:
            gui_timestep.value = (int(gui_timestep.value) + 1) % num_frames
        time.sleep(1.0 / float(gui_framerate.value))

def bind_update(widget, update_func):
    """Attach an update callback to a GUI widget.

    A thin wrapper around ``widget.on_update`` that ignores the event
    argument and simply calls *update_func* with no arguments.

    Args:
        widget: Any viser GUI widget that supports the ``on_update`` method.
        update_func (Callable): Zero-argument callable to invoke on every
            widget update event.
    """
    widget.on_update(lambda _: update_func())

# ----------------- Main Visualization Function -----------------
def start_visualization(output, min_conf_thr_percentile=10, global_conf_thr_value_to_drop_view=1.5, port=8020, point_size=0.0004):
    """Launch an interactive viser-based 3D visualization server for Fast3R output.

    Estimates camera poses from the model predictions, constructs per-frame
    3D point clouds with RGB / confidence / rainbow coloring, registers all
    GUI controls (playback, filtering, export), and starts a background
    playback thread.

    Args:
        output (dict): Dictionary returned by the Fast3R pipeline, containing:

            * ``'preds'`` (list of dict) – per-frame model predictions with
              keys ``'pts3d_in_other_view'``, ``'conf'``,
              ``'pts3d_local_aligned_to_global'``, ``'conf_local'``.
            * ``'views'`` (list of dict) – per-frame view data with key
              ``'img'`` (shape ``(1, 3, H, W)``).

        min_conf_thr_percentile (int): Initial per-view confidence percentile
            cut-off (0 = keep all points, 100 = keep none).
            Defaults to ``10``.
        global_conf_thr_value_to_drop_view (float): Global confidence
            threshold used to separate high- and low-confidence views.
            Defaults to ``1.5``.
        port (int): TCP port for the viser server. Defaults to ``8020``.
        point_size (float): Initial rendered size of each point in world
            units. Defaults to ``0.0004``.

    Returns:
        viser.ViserServer: The running viser server instance (the visualization
        will keep serving until the process is terminated or the server is
        stopped).
    """
    server = viser.ViserServer(host='127.0.0.1', port=port)
    server.gui.set_panel_label("Show Controls")
    server.gui.configure_theme(control_layout="floating", control_width="medium", show_logo=False)

    @server.on_client_connect
    def on_client_connect(client: viser.ClientHandle) -> None:
        """客户端连接时设置初始相机位置。"""
        with client.atomic():
            client.camera.position = (-0.00141163, -0.01910395, -0.06794288)
            client.camera.look_at = (-0.00352821, -0.01143425, 0.0154939)
        client.flush()

    poses_c2w_batch, estimated_focals = MultiViewDUSt3RLitModule.estimate_camera_poses(
        output['preds'], niter_PnP=100, focal_length_estimation_method='first_view_from_global_head'
    )
    poses_c2w = poses_c2w_batch[0]

    server.scene.set_up_direction((0.0, -1.0, 0.0))
    server.scene.world_axes.visible = False

    num_frames = len(output['preds'])
    frame_data_list = []
    cumulative_pts = []

    # ----------------- Grouped GUI Controls -----------------
    with server.gui.add_folder("Point and Camera Options", expand_by_default=False):
        gui_point_size = server.gui.add_slider("Point Size", min=1e-6, max=0.002, step=1e-5, initial_value=point_size)
        gui_frustum_size_percent = server.gui.add_slider("Camera Size (%)", min=0.1, max=10.0, step=0.1, initial_value=2.0)
        gui_mask_sky = server.gui.add_checkbox("Mask Sky", True)
        gui_show_confidence_color = server.gui.add_checkbox("Show Confidence", False)
        gui_rainbow_color_option = server.gui.add_checkbox("Color by View", False)

    with server.gui.add_folder("Playback Options", expand_by_default=False):
        gui_timestep = server.gui.add_slider("Timestep", min=0, max=num_frames - 1, step=1, initial_value=0)
        gui_next_frame = server.gui.add_button("Next Frame")
        gui_prev_frame = server.gui.add_button("Prev Frame")
        gui_playing = server.gui.add_checkbox("Playing", False)
        gui_framerate = server.gui.add_slider("FPS", min=0.25, max=60, step=0.25, initial_value=10)
        gui_framerate_options = server.gui.add_button_group("FPS options", ("0.5", "1", "10", "20", "30", "60"))

    with server.gui.add_folder("Pointmap Head Options", expand_by_default=False):
        gui_show_global = server.gui.add_checkbox("Global", False)
        gui_show_local = server.gui.add_checkbox("Local", True)

    with server.gui.add_folder("Confidence Options", expand_by_default=False):
        gui_show_high_conf = server.gui.add_checkbox("Show High-Conf Views", True)
        gui_show_low_conf = server.gui.add_checkbox("Show Low-Conf Views", False)
        gui_global_conf_threshold = server.gui.add_slider("High/Low Conf Threshold", min=1.0, max=12.0, step=0.1, initial_value=global_conf_thr_value_to_drop_view)
        gui_min_conf_percentile = server.gui.add_slider("Per-View Conf Percentile", min=0, max=100, step=1, initial_value=min_conf_thr_percentile)

    with server.gui.add_folder("Export Options", expand_by_default=False):
        button_render_gif = server.gui.add_button("Render a GIF")
        button_download_ply = server.gui.add_button("Download PLY")  

    @gui_next_frame.on_click
    def next_frame(_):
        """切换到下一帧。"""
        gui_timestep.value = (gui_timestep.value + 1) % num_frames

    @gui_prev_frame.on_click
    def prev_frame(_):
        """切换到上一帧。"""
        gui_timestep.value = (gui_timestep.value - 1) % num_frames

    @gui_playing.on_update
    def playing_update(_):
        """播放状态切换时更新控件禁用状态。"""
        state = gui_playing.value
        gui_timestep.disabled = state
        gui_next_frame.disabled = state
        gui_prev_frame.disabled = state

    @gui_framerate_options.on_click
    def fps_options(_):
        """帧率选项点击回调。"""
        gui_framerate.value = float(gui_framerate_options.value)

    server.scene.add_frame("/cams", show_axes=False)

    # ----------------- Frame Processing -----------------
    for i in tqdm(range(num_frames)):
        pred = output['preds'][i]
        view = output['views'][i]

        img_rgb_orig = to_numpy(view['img'].cpu().squeeze().permute(1,2,0))
        not_sky_mask = detect_sky_mask(img_rgb_orig).flatten().astype(np.int8)

        pts3d_global = to_numpy(pred['pts3d_in_other_view'].cpu().squeeze()).reshape(-1, 3)
        conf_global = to_numpy(pred['conf'].cpu().squeeze()).flatten()
        pts3d_local = to_numpy(pred['pts3d_local_aligned_to_global'].cpu().squeeze()).reshape(-1, 3)
        conf_local = to_numpy(pred['conf_local'].cpu().squeeze()).flatten()
        img_rgb = to_numpy(view['img'].cpu().squeeze().permute(1,2,0))
        img_rgb_flat = img_rgb.reshape(-1, 3)

        cumulative_pts.append(pts3d_global)

        sort_idx_global = np.argsort(-conf_global)
        sorted_conf_global = conf_global[sort_idx_global]
        sorted_pts3d_global = pts3d_global[sort_idx_global]
        sorted_img_rgb_global = img_rgb_flat[sort_idx_global]
        sorted_not_sky_global = not_sky_mask[sort_idx_global]

        sort_idx_local = np.argsort(-conf_local)
        sorted_conf_local = conf_local[sort_idx_local]
        sorted_pts3d_local = pts3d_local[sort_idx_local]
        sorted_img_rgb_local = img_rgb_flat[sort_idx_local]
        sorted_not_sky_local = not_sky_mask[sort_idx_local]

        colors_rgb_global = ((sorted_img_rgb_global + 1) * 127.5).astype(np.uint8) / 255.0
        colors_rgb_local = ((sorted_img_rgb_local + 1) * 127.5).astype(np.uint8) / 255.0

        conf_norm_global = (sorted_conf_global - sorted_conf_global.min()) / (sorted_conf_global.max() - sorted_conf_global.min() + 1e-8)
        conf_norm_local = (sorted_conf_local - sorted_conf_local.min()) / (sorted_conf_local.max() - sorted_conf_local.min() + 1e-8)
        colormap = cm.turbo
        colors_confidence_global = colormap(conf_norm_global)[:, :3]
        colors_confidence_local = colormap(conf_norm_local)[:, :3]

        def rainbow_color(n, total):
            """生成彩虹色调颜色。"""
            import colorsys
            hue = n / total
            return colorsys.hsv_to_rgb(hue, 1.0, 1.0)
        rainbow_color_for_frame = rainbow_color(i, num_frames)
        colors_rainbow_global = np.tile(rainbow_color_for_frame, (sorted_pts3d_global.shape[0], 1))
        colors_rainbow_local = np.tile(rainbow_color_for_frame, (sorted_pts3d_local.shape[0], 1))

        max_conf_global = conf_global.max()
        is_high_confidence = max_conf_global >= gui_global_conf_threshold.value

        c2w = poses_c2w[i]
        height, width = view['img'].shape[2], view['img'].shape[3]
        focal_length = estimated_focals[0][i]
        img_rgb_reshaped = img_rgb.reshape(height, width, 3)
        img_rgb_normalized = ((img_rgb_reshaped + 1) * 127.5).astype(np.uint8)
        img_downsampled = img_rgb_normalized[::4, ::4]

        frame_data = {
            'sorted_pts3d_global': sorted_pts3d_global,
            'colors_rgb_global': colors_rgb_global,
            'colors_confidence_global': colors_confidence_global,
            'colors_rainbow_global': colors_rainbow_global,
            'sorted_pts3d_local': sorted_pts3d_local,
            'colors_rgb_local': colors_rgb_local,
            'colors_confidence_local': colors_confidence_local,
            'colors_rainbow_local': colors_rainbow_local,
            'sorted_not_sky_global': sorted_not_sky_global,
            'sorted_not_sky_local': sorted_not_sky_local,
            'max_conf_global': float(max_conf_global),
            'is_high_confidence': is_high_confidence,
            'c2w': c2w,
            'height': height,
            'width': width,
            'focal_length': focal_length,
            'img_downsampled': img_downsampled,
            'rainbow_color': rainbow_color_for_frame,
        }
        frame_data_list.append(frame_data)

    # Percentile for scene extent calculation (10th to 90th percentile by default)
    extent_percentile = 80
    cumulative_pts_combined = np.concatenate(cumulative_pts, axis=0)
    # Calculate percentiles for each coordinate
    min_coords = np.percentile(cumulative_pts_combined, 100 - extent_percentile, axis=0)
    max_coords = np.percentile(cumulative_pts_combined, extent_percentile, axis=0)
    scene_extent = max_coords - min_coords
    max_extent = np.max(scene_extent)

    # ----------------- Create Visualization Nodes -----------------
    for i in tqdm(range(num_frames)):
        fd = frame_data_list[i]
        frame_node = server.scene.add_frame(f"/cams/t{i}", show_axes=False)

        point_node_global = server.scene.add_point_cloud(
            name=f"/pts3d_global/t{i}",
            points=fd['sorted_pts3d_global'],
            colors=fd['colors_rgb_global'],
            point_size=gui_point_size.value,
            point_shape="rounded",
            visible=False,
        )
        point_node_local = server.scene.add_point_cloud(
            name=f"/pts3d_local/t{i}",
            points=fd['sorted_pts3d_local'],
            colors=fd['colors_rgb_local'],
            point_size=gui_point_size.value,
            point_shape="rounded",
            visible=True if fd['is_high_confidence'] else False,
        )

        rotation_matrix = fd['c2w'][:3, :3]
        position = fd['c2w'][:3, 3]
        rotation_quaternion = tf.SO3.from_matrix(rotation_matrix).wxyz
        fov = 2 * np.arctan2(fd['height'] / 2, fd['focal_length'])
        aspect_ratio = fd['width'] / fd['height']
        frustum_scale = max_extent * (gui_frustum_size_percent.value / 100.0)

        frustum_node = server.scene.add_camera_frustum(
            name=f"/cams/t{i}/frustum",
            fov=fov,
            aspect=aspect_ratio,
            scale=frustum_scale,
            color=fd['rainbow_color'],
            image=fd['img_downsampled'],
            wxyz=rotation_quaternion,
            position=position,
            visible=True if fd['is_high_confidence'] else False,
        )

        fd['frame_node'] = frame_node
        fd['point_node_global'] = point_node_global
        fd['point_node_local'] = point_node_local
        fd['frustum_node'] = frustum_node

    # Initially set all nodes hidden
    for fd in frame_data_list:
        fd['frame_node'].visible = False
        fd['point_node_global'].visible = False
        fd['point_node_local'].visible = False
        fd['frustum_node'].visible = False
    server.flush()

    # Initialize timestep to show all frames and disable playing
    gui_timestep.value = num_frames - 1
    gui_playing.value = False

    # Scene type detection and sky masking initialization
    is_outdoor = is_outdoor_scene(frame_data_list)
    gui_mask_sky.value = is_outdoor

    print("\nScene type detection:")
    sky_ratios = [1.0 - np.mean(fd['sorted_not_sky_global']) for fd in frame_data_list]
    significant = sum(1 for r in sky_ratios if r > 0.2)
    print(f"- Found {significant}/{len(sky_ratios)} frames with significant sky presence (>20% sky pixels)")
    print(f"- Scene classified as: {'outdoor' if is_outdoor else 'indoor'}, setting mask_sky to {is_outdoor}")

    # Initial visibility setup
    with server.atomic():
        for i in range(num_frames):
            fd = frame_data_list[i]
            fd['frame_node'].visible = True
            fd['frustum_node'].visible = True if fd['is_high_confidence'] else False
            
            # Set up initial points with sky masking if needed
            pts3d_global = fd['sorted_pts3d_global']
            pts3d_local = fd['sorted_pts3d_local']
            
            # Select appropriate colors based on active color option
            if gui_show_confidence_color.value:
                colors_global = fd['colors_confidence_global']
                colors_local = fd['colors_confidence_local']
            elif gui_rainbow_color_option.value:
                colors_global = fd['colors_rainbow_global']
                colors_local = fd['colors_rainbow_local']
            else:
                colors_global = fd['colors_rgb_global']
                colors_local = fd['colors_rgb_local']
            
            if is_outdoor:  # Apply sky masking if outdoor scene
                mask_global = fd['sorted_not_sky_global']
                mask_local = fd['sorted_not_sky_local']
                pts3d_global = pts3d_global[mask_global > 0]
                pts3d_local = pts3d_local[mask_local > 0]
                colors_global = colors_global[mask_global > 0]
                colors_local = colors_local[mask_local > 0]
            
            # Update point clouds
            fd['point_node_global'].points = pts3d_global
            fd['point_node_local'].points = pts3d_local
            fd['point_node_global'].colors = colors_global
            fd['point_node_local'].colors = colors_local
            fd['point_node_global'].visible = gui_show_global.value
            fd['point_node_local'].visible = gui_show_local.value

    server.flush()

    # ----------------- GUI Callback Updates -----------------
    @gui_timestep.on_update
    def _(_):
        """时间步滑块更新回调：控制帧/视锥/点云的可见性。"""
        current = int(gui_timestep.value)
        with server.atomic():
            for i in range(num_frames):
                fd = frame_data_list[i]
                if i <= current:
                    fd['frame_node'].visible = True
                    # Set frustum visibility based on confidence settings
                    if fd['is_high_confidence']:
                        fd['frustum_node'].visible = gui_show_high_conf.value
                    else:
                        fd['frustum_node'].visible = gui_show_low_conf.value
                    fd['point_node_global'].visible = gui_show_global.value
                    fd['point_node_local'].visible = gui_show_local.value
                else:
                    fd['frame_node'].visible = False
                    fd['frustum_node'].visible = False
                    fd['point_node_global'].visible = False
                    fd['point_node_local'].visible = False
        server.flush()

    @gui_point_size.on_update
    def _(_):
        """点大小滑块更新回调：调整全局和局部点云的渲染点尺寸。"""
        with server.atomic():
            for fd in frame_data_list:
                fd['point_node_global'].point_size = gui_point_size.value
                fd['point_node_local'].point_size = gui_point_size.value
        server.flush()

    @gui_frustum_size_percent.on_update
    def _(_):
        """视锥大小滑块更新回调：缩放所有相机视锥的显示尺寸。"""
        frustum_scale = max_extent * (gui_frustum_size_percent.value / 100.0)
        with server.atomic():
            for fd in frame_data_list:
                fd['frustum_node'].scale = frustum_scale
        server.flush()

    @gui_show_confidence_color.on_update
    def _(_):
        """置信度着色开关回调：切换置信度颜色模式并更新点云颜色。"""
        # Make options mutually exclusive
        if gui_show_confidence_color.value and gui_rainbow_color_option.value:
            gui_rainbow_color_option.value = False
        
        # Update colors for all visible points
        update_points_filtering(server, frame_data_list, gui_timestep, gui_min_conf_percentile, 
                               gui_mask_sky, gui_show_confidence_color, gui_rainbow_color_option)

    @gui_rainbow_color_option.on_update
    def _(_):
        """彩虹着色开关回调：切换彩虹颜色模式并更新点云颜色。"""
        # Make options mutually exclusive
        if gui_rainbow_color_option.value and gui_show_confidence_color.value:
            gui_show_confidence_color.value = False
            
        # Update colors for all visible points
        update_points_filtering(server, frame_data_list, gui_timestep, gui_min_conf_percentile, 
                               gui_mask_sky, gui_show_confidence_color, gui_rainbow_color_option)

    @gui_min_conf_percentile.on_update
    def _(_):
        """最小置信度分位数滑块回调：更新点云过滤阈值。"""
        update_points_filtering(server, frame_data_list, gui_timestep, gui_min_conf_percentile, 
                               gui_mask_sky, gui_show_confidence_color, gui_rainbow_color_option)

    @gui_mask_sky.on_update
    def _(_):
        """天空遮罩开关回调：切换天空区域过滤并更新点云显示。"""
        # For each visible frame, update filtering if mask sky changes.
        update_points_filtering(server, frame_data_list, gui_timestep, gui_min_conf_percentile, 
                               gui_mask_sky, gui_show_confidence_color, gui_rainbow_color_option)

    @gui_show_global.on_update
    def _(_):
        """全局点云可见性开关回调：控制当前时间步内全局点云的显示。"""
        with server.atomic():
            for i in range(int(gui_timestep.value)+1):
                frame_data_list[i]['point_node_global'].visible = gui_show_global.value
        server.flush()

    @gui_show_local.on_update
    def _(_):
        """局部点云可见性开关回调：控制当前时间步内局部点云的显示。"""
        with server.atomic():
            for i in range(int(gui_timestep.value)+1):
                frame_data_list[i]['point_node_local'].visible = gui_show_local.value
        server.flush()

    @gui_show_high_conf.on_update
    def _(_):
        """高置信度视图开关回调：控制高置信度视图的视锥和点云可见性。"""
        with server.atomic():
            for i in range(num_frames):
                fd = frame_data_list[i]
                if i <= int(gui_timestep.value):
                    # Hide frustum and points if high confidence views are disabled
                    if fd['is_high_confidence'] and gui_show_high_conf.value:
                        fd['frustum_node'].visible = gui_show_high_conf.value
                        fd['point_node_global'].visible = gui_show_global.value and gui_show_high_conf.value
                        fd['point_node_local'].visible = gui_show_local.value and gui_show_high_conf.value
                    else:
                        fd['frustum_node'].visible = False  # Hide if not high confidence
                        fd['point_node_global'].visible = False  # Hide if not high confidence
                        fd['point_node_local'].visible = False  # Hide if not high confidence
        server.flush()

    @gui_show_low_conf.on_update
    def _(_):
        """低置信度视图开关回调：控制低置信度视图的视锥和点云可见性。"""
        with server.atomic():
            for i in range(num_frames):
                fd = frame_data_list[i]
                if i <= int(gui_timestep.value):
                    # Hide frustum and points if low confidence views are disabled
                    if not fd['is_high_confidence'] and gui_show_low_conf.value:
                        fd['frustum_node'].visible = gui_show_low_conf.value
                        fd['point_node_global'].visible = gui_show_global.value and gui_show_low_conf.value
                        fd['point_node_local'].visible = gui_show_local.value and gui_show_low_conf.value
                    else:
                        fd['frustum_node'].visible = False  # Hide if high confidence
                        fd['point_node_global'].visible = False  # Hide if high confidence
                        fd['point_node_local'].visible = False  # Hide if high confidence
        server.flush()

    @gui_global_conf_threshold.on_update
    def _(_):
        """全局置信度阈值滑块回调：重新计算各帧的高低置信度标记。"""
        for fd in frame_data_list:
            fd['is_high_confidence'] = fd['max_conf_global'] >= gui_global_conf_threshold.value
        server.flush()

    # ----------------- Start Playback Loop -----------------
    def local_playback_loop():
        """本地回放循环：在后台线程中持续推进时间步。"""
        while True:
            if gui_playing.value:
                gui_timestep.value = (int(gui_timestep.value) + 1) % num_frames
            time.sleep(1.0 / float(gui_framerate.value))
    playback_thread = threading.Thread(target=local_playback_loop)
    playback_thread.start()

    @button_render_gif.on_click
    def _(event: viser.GuiEvent) -> None:
        """渲染 GIF 按钮回调：逐帧截图并生成 GIF 动画发送给客户端下载。"""
        client = event.client
        if client is None:
            print("Error: No client connected.")
            return
        try:
            images = []
            original_timestep = gui_timestep.value
            original_playing = gui_playing.value
            gui_playing.value = False
            fps = gui_framerate.value
            for i in range(num_frames):
                gui_timestep.value = i
                time.sleep(0.1)
                images.append(client.get_render(height=720, width=1280))
            gif_bytes = iio.imwrite("<bytes>", images, extension=".gif", fps=fps, loop=0)
            client.send_file_download("visualization.gif", gif_bytes)
            gui_timestep.value = original_timestep
            gui_playing.value = original_playing
        except Exception as e:
            print(f"Error while rendering GIF: {e}")

    @button_download_ply.on_click
    def _(event: viser.GuiEvent):
        """下载 PLY 按钮回调：收集可见点云并生成 PLY 文件发送给客户端下载。"""
        client = event.client
        if client is None:
            print("No client connected; skipping download.")
            return
        
        # Get current state
        current_timestep = int(gui_timestep.value)
        
        # Collect visible points
        points, colors = collect_visible_points(frame_data_list, current_timestep)
        if points is None:
            print("No points to save.")
            return
        
        # Generate PLY and send to client
        try:
            ply_bytes = generate_ply_bytes(points, colors)
            client.send_file_download("pointcloud.ply", ply_bytes)
        except Exception as e:
            print(f"Failed to generate PLY: {e}")

    public_url = server.request_share_url()
    return server

# Example usage:
# server = start_visualization(output=your_output_dict, min_conf_thr_percentile=10, global_conf_thr_value_to_drop_view=1.5, port=8020)