import vtk
from tqdm.auto import tqdm
import numpy as np
from fast3r.dust3r.utils.device import to_numpy
import cv2
from matplotlib import cm
from scipy import ndimage
import torch
from concurrent.futures import ThreadPoolExecutor
import roma
from functools import partial

closest_point = None
last_sphere_actor = None
before_sphere_actor = None
last_line_actor = None
def is_outdoor_scene(frame_data_list):
    sky_ratios = []
    for fd in frame_data_list:
        mask = fd.get('sorted_not_sky_global', np.ones(1))
        sky_ratio = 1.0 - np.mean(mask)
        sky_ratios.append(float(sky_ratio))
    significant = sum(1 for ratio in sky_ratios if ratio > 0.2)
    return significant >= len(sky_ratios) / 4

def rainbow_color(n, total):
    import colorsys
    hue = n / total
    return colorsys.hsv_to_rgb(hue, 1.0, 1.0)

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

def align_local_pts3d_to_global(preds, views, min_conf_thr_percentile=0):
    """
    Aligns the local point clouds to the global coordinate frame.

    Args:
        preds (List[Dict]): A list of dictionaries containing predictions for each view.
        views (List[Dict]): A list of dictionaries containing ground truth data for each view.
        min_conf_thr_percentile (float): Minimum confidence percentile threshold (default is 0).

    Modifies:
        preds: Each pred dictionary in the list will have a new key 'pts3d_local_aligned_to_global',
            which contains the aligned local points.
    """
    # Check if required keys are present in preds
    for pred in preds:
        if 'pts3d_local' not in pred:
            raise ValueError("Key 'pts3d_local' not found in preds.")
        if 'conf_local' not in pred:
            raise ValueError("Key 'conf_local' not found in preds.")
        if 'pts3d_in_other_view' not in pred:
            raise ValueError("Key 'pts3d_in_other_view' not found in preds.")
        if 'conf' not in pred:
            raise ValueError("Key 'conf' (global head confidence) not found in preds.")

    num_views = len(preds)
    B, H, W, _ = preds[0]['pts3d_local'].shape  # Get batch size and dimensions

    # Function to process a single (view_index, batch_index) pair
    def process_view_batch(view_index, batch_index):
        pred = preds[view_index]
        view = views[view_index]

        # Get the predicted points from local and global heads for this sample
        pts3d_local = pred['pts3d_local'][batch_index]            # Shape: (H, W, 3)
        conf_local = pred['conf_local'][batch_index]              # Shape: (H, W)
        pts3d_global = pred['pts3d_in_other_view'][batch_index]   # Shape: (H, W, 3)
        conf_global = pred['conf'][batch_index]                   # Shape: (H, W)

        H_cur, W_cur, _ = pts3d_local.shape

        # Get valid_mask if it exists
        if 'valid_mask' in view:
            valid_mask = view['valid_mask'][batch_index]          # Shape: (H, W)
        else:
            valid_mask = torch.ones_like(conf_global, dtype=torch.bool)

        # Flatten the confidences to compute the threshold
        conf_global_flat = conf_global.reshape(-1)  # Shape: (N,)

        # Compute the confidence threshold
        conf_threshold_value = torch.quantile(conf_global_flat, min_conf_thr_percentile / 100.0)

        # Create a mask for high-confidence points
        conf_mask = conf_global >= conf_threshold_value

        # Combine masks
        final_mask = conf_mask & valid_mask  # Shape: (H, W)

        # Flatten the points and masks
        pts_local_flat = pts3d_local.view(-1, 3)   # Shape: (N, 3)
        pts_global_flat = pts3d_global.view(-1, 3) # Shape: (N, 3)
        final_mask_flat = final_mask.view(-1)      # Shape: (N,)

        # Select valid points
        x = pts_local_flat[final_mask_flat]    # Local points (M, 3)
        y = pts_global_flat[final_mask_flat]   # Global points (M, 3)
        # w = conf_global.view(-1)[final_mask_flat]  # Weights (M,)

        # Check if we have enough points after applying confidence threshold
        if x.shape[0] < 3:
            # Not enough points after applying confidence threshold
            # Use only valid_mask
            final_mask = valid_mask
            final_mask_flat = final_mask.view(-1)

            # Re-select points without confidence threshold
            x = pts_local_flat[final_mask_flat]    # Local points (M, 3)
            y = pts_global_flat[final_mask_flat]   # Global points (M, 3)
            # w = conf_global.view(-1)[final_mask_flat]  # Weights (M,)

        # Check again if we have enough points
        if x.shape[0] < 3:
            # Not enough points even after using valid_mask only
            # Use identity transformation
            R = torch.eye(3, device=pts_local_flat.device, dtype=pts_local_flat.dtype)
            t = torch.zeros(3, device=pts_local_flat.device, dtype=pts_local_flat.dtype)
            s = 1.0
        else:
            # Compute the rigid transformation with scaling
            R, t, s = roma.rigid_points_registration(
                x, y, compute_scaling=True
            )

        # Apply the transformation to all local points (including invalid ones)
        pts_local_aligned_flat = s * (pts_local_flat @ R.T) + t  # Shape: (N, 3)

        # Reshape back to (H, W, 3)
        pts_local_aligned = pts_local_aligned_flat.view(H_cur, W_cur, 3)

        return (view_index, batch_index, pts_local_aligned)

    # Create a list of all tasks (view_index, batch_index) pairs
    tasks = [(view_idx, batch_idx) for view_idx in range(num_views) for batch_idx in range(B)]

    # Use ThreadPoolExecutor to parallelize across tasks
    with ThreadPoolExecutor() as executor:
        futures = [executor.submit(process_view_batch, view_idx, batch_idx) for view_idx, batch_idx in tasks]

        # Collect the results
        results = [future.result() for future in futures]

    # Organize the results and update preds
    # Create a dictionary to store aligned points for each view
    aligned_pts_dict = {view_idx: [None] * B for view_idx in range(num_views)}

    for view_index, batch_index, pts_local_aligned in results:
        aligned_pts_dict[view_index][batch_index] = pts_local_aligned

    # Update preds with the aligned points
    for view_index in range(num_views):
        pred = preds[view_index]
        # Stack the aligned points back into a tensor of shape (B, H, W, 3)
        pred['pts3d_local_aligned_to_global'] = torch.stack(aligned_pts_dict[view_index], dim=0)

def start_visualization(window,output_dict, min_conf_thr_percentile=10,
                        global_conf_thr_value_to_drop_view=1.5,point_size=0.0004):
    gui_global_conf_threshold=global_conf_thr_value_to_drop_view    #gui_global_conf_threshold根据用户选择
    gui_point_size=point_size
    gui_show_confidence_color = False#用户可选
    gui_rainbow_color_option = False
    gui_show_global = False
    gui_show_local = True
    frame_data_list,max_extent=frame_processing(output_dict,gui_global_conf_threshold)
    reconstruction(window,frame_data_list,max_extent,gui_show_confidence_color,gui_rainbow_color_option,gui_show_global,gui_show_local,gui_point_size)


def frame_processing(output,gui_global_conf_threshold):
    num_frames = len(output['preds'])
    frame_data_list = []
    cumulative_pts = []

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

        rainbow_color_for_frame = rainbow_color(i, num_frames)
        colors_rainbow_global = np.tile(rainbow_color_for_frame, (sorted_pts3d_global.shape[0], 1))
        colors_rainbow_local = np.tile(rainbow_color_for_frame, (sorted_pts3d_local.shape[0], 1))

        max_conf_global = conf_global.max()
        is_high_confidence = max_conf_global >= gui_global_conf_threshold#后面可能会改

        height, width = view['img'].shape[2], view['img'].shape[3]
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
            'height': height,
            'width': width,
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

    return frame_data_list,scene_extent

def reconstruction(window,frame_data_list,max_extent,gui_show_confidence_color,gui_rainbow_color_option,gui_show_global,gui_show_local,gui_point_size):
    print(len(frame_data_list))
    # Scene type detection and sky masking initialization
    is_outdoor = is_outdoor_scene(frame_data_list)

    for i in tqdm(range(len(frame_data_list))):
        fd = frame_data_list[i]

        pts3d_global = fd['sorted_pts3d_global']
        pts3d_local = fd['sorted_pts3d_local']

        # Select appropriate colors based on active color option
        if gui_show_confidence_color:
            colors_global = fd['colors_confidence_global']
            colors_local = fd['colors_confidence_local']
        elif gui_rainbow_color_option:
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


        # 全局坐标系的点云
        vpoints_global = vtk.vtkPoints()
        vcolors_global = vtk.vtkUnsignedCharArray()
        vcolors_global.SetNumberOfComponents(3)
        for pt, color in zip(pts3d_global, colors_global):
            if color.dtype == np.float64:
                color = np.clip((color + 1) * 127.5, 0, 255).astype(np.uint8)
            vpoints_global.InsertNextPoint(pt)
            vcolors_global.InsertNextTuple3(color[0],color[1],color[2])

        # 局部坐标系的点云
        vpoints_local = vtk.vtkPoints()
        vcolors_local = vtk.vtkUnsignedCharArray()
        vcolors_local.SetNumberOfComponents(3)
        for pt, color in zip(pts3d_local, colors_local):
            if color.dtype == np.float64:
                color = np.clip((color + 1) * 127.5, 0, 255).astype(np.uint8)
            vpoints_local.InsertNextPoint(pt)
            vcolors_local.InsertNextTuple3(color[0],color[1],color[2])

        #全局
        polydata_global = vtk.vtkPolyData()
        polydata_global.SetPoints(vpoints_global)
        polydata_global.GetPointData().SetScalars(vcolors_global)
        # 顶点相关的 filter
        vertex = vtk.vtkVertexGlyphFilter()
        vertex.SetInputData(polydata_global)
        mapper_global = vtk.vtkPolyDataMapper()
        mapper_global.SetInputConnection(vertex.GetOutputPort())
        actor_global = vtk.vtkActor()
        actor_global.SetMapper(mapper_global)
        actor_global.GetProperty().SetPointSize(gui_point_size)

        #局部
        polydata_local = vtk.vtkPolyData()
        polydata_local.SetPoints(vpoints_local)
        polydata_local.GetPointData().SetScalars(vcolors_local)
        # 顶点相关的 filter
        vertex = vtk.vtkVertexGlyphFilter()
        vertex.SetInputData(polydata_local)
        mapper_local = vtk.vtkPolyDataMapper()
        mapper_local.SetInputConnection(vertex.GetOutputPort())
        actor_local = vtk.vtkActor()
        actor_local.SetMapper(mapper_local)
        actor_local.GetProperty().SetPointSize(gui_point_size)

        if gui_show_global:
            window.widget_3.renderer.AddActor(actor_global)
            callback = partial(on_left_click, window=window,points=vpoints_global,max_extent=max_extent)
        if gui_show_local:
            window.widget_3.renderer.AddActor(actor_local)
            callback = partial(on_left_click, window=window,points=vpoints_local,max_extent=max_extent)

        print("\nScene type detection:")
        sky_ratios = [1.0 - np.mean(fd['sorted_not_sky_global']) for fd in frame_data_list]
        significant = sum(1 for r in sky_ratios if r > 0.2)
        print(f"- Found {significant}/{len(sky_ratios)} frames with significant sky presence (>20% sky pixels)")
        print(f"- Scene classified as: {'outdoor' if is_outdoor else 'indoor'}, setting mask_sky to {is_outdoor}")

    # 将这个新函数作为回调
    window.widget_3.render_window.GetInteractor().AddObserver(
        "LeftButtonPressEvent",
        callback
    )

    # # 创建鼠标点击回调
    # window.widget_3.render_window.GetInteractor().AddObserver("LeftButtonPressEvent", window.widget_3.on_left_click)
    window.widget_3.render_window.Render()

def on_left_click(obj, event,window,points,max_extent):
    global closest_point,last_sphere_actor,last_line_actor,before_sphere_actor
    # 获取鼠标点击位置的3D坐标
    click_pos = window.widget_3.get_click_position()
    # 在点击位置附近0.1的正方体内搜索最近的点
    closest_point = window.widget_3.find_closest_point(click_pos,points,max_extent)
    print(f"点击位置: {click_pos}")
    print(f"最近的点坐标: {closest_point}")

    if closest_point!=None:
        if window.radioButton_3.isChecked() == True:
            if last_sphere_actor is not None:
                window.widget_3.renderer.RemoveActor(last_sphere_actor)
        sphere = vtk.vtkSphereSource()
        sphere.SetRadius(np.max(max_extent)*0.01)
        sphere.SetCenter(closest_point[0],closest_point[1],closest_point[2])
        mapper = vtk.vtkPolyDataMapper()
        mapper.SetInputConnection(sphere.GetOutputPort())
        sphere_actor = vtk.vtkActor()
        sphere_actor.SetMapper(mapper)
        sphere_actor.GetProperty().SetColor(1.0, 0.0, 0.0)
        window.widget_3.renderer.AddActor(sphere_actor)
        if window.radioButton_4.isChecked() and last_sphere_actor is not None:#两点画线
            if before_sphere_actor is not None:
                window.widget_3.renderer.RemoveActor(before_sphere_actor)
            if last_line_actor is not None:
                window.widget_3.renderer.RemoveActor(last_line_actor)
            # 画一条从之前的点到当前点的线
            line = vtk.vtkLineSource()
            line.SetPoint1(last_sphere_actor.GetCenter())
            line.SetPoint2(closest_point)
            line_mapper = vtk.vtkPolyDataMapper()
            line_mapper.SetInputConnection(line.GetOutputPort())
            line_actor = vtk.vtkActor()
            line_actor.SetMapper(line_mapper)
            line_actor.GetProperty().SetColor(1.0, 0.0, 0.0)  # 设置线的颜色
            line_actor.GetProperty().SetLineWidth(5)
            window.widget_3.renderer.AddActor(line_actor)
            last_line_actor=line_actor
        before_sphere_actor=last_sphere_actor
        last_sphere_actor = sphere_actor
        window.widget_3.render_window.Render()