"""VTK 渲染与测量模块（原 process.py 的渲染部分）。

职责：
- frame_processing: 将推理输出整理为可渲染的帧数据（排序/着色/天空掩膜/场景尺度）
- reconstruction / start_visualization: 把帧数据渲染进主窗口的 VTK 控件
- on_left_click: 鼠标点击选点打标记 + 两点连线测量

注意：本模块通过 ``window`` 对象访问 UI 控件（``window.widget_3``、
``window.radioButton_3`` 等），主窗口需提供这些属性（或转发自 ui）。
"""
from functools import partial

import cv2
import numpy as np
import vtk
from matplotlib import cm
from scipy import ndimage
from tqdm.auto import tqdm

from fast3r.dust3r.utils.device import to_numpy

# 测量交互的全局状态（球/线 actor）
closest_point = None
last_sphere_actor = None
before_sphere_actor = None
last_line_actor = None


def is_outdoor_scene(frame_data_list):
    """根据天空占比判断是否为户外场景。"""
    sky_ratios = []
    for fd in frame_data_list:
        mask = fd.get("sorted_not_sky_global", np.ones(1))
        sky_ratio = 1.0 - np.mean(mask)
        sky_ratios.append(float(sky_ratio))
    significant = sum(1 for ratio in sky_ratios if ratio > 0.2)
    return significant >= len(sky_ratios) / 4


def rainbow_color(n, total):
    """为第 n 帧生成彩虹色（hue 沿帧序号变化）。"""
    import colorsys

    hue = n / total
    return colorsys.hsv_to_rgb(hue, 1.0, 1.0)


def detect_sky_mask(img_rgb):
    """检测天空像素（HSV + 形态学）。

    Args:
        img_rgb: 归一化到 [-1, 1] 的 RGB 图像。

    Returns:
        int8 掩膜，True 表示非天空像素。
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
    mask[:upper_third, :] |= (upper_region[:, :, 1] < 50) & (
        upper_region[:, :, 2] > 150
    )

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


def frame_processing(output, gui_global_conf_threshold):
    """将推理输出整理为每帧的渲染数据。

    Returns:
        (frame_data_list, scene_extent)
    """
    num_frames = len(output["preds"])
    frame_data_list = []
    cumulative_pts = []

    for i in tqdm(range(num_frames)):
        pred = output["preds"][i]
        view = output["views"][i]

        img_rgb_orig = to_numpy(view["img"].cpu().squeeze().permute(1, 2, 0))
        not_sky_mask = detect_sky_mask(img_rgb_orig).flatten().astype(np.int8)

        pts3d_global = to_numpy(
            pred["pts3d_in_other_view"].cpu().squeeze()
        ).reshape(-1, 3)
        conf_global = to_numpy(pred["conf"].cpu().squeeze()).flatten()
        pts3d_local = to_numpy(
            pred["pts3d_local_aligned_to_global"].cpu().squeeze()
        ).reshape(-1, 3)
        conf_local = to_numpy(pred["conf_local"].cpu().squeeze()).flatten()
        img_rgb = to_numpy(view["img"].cpu().squeeze().permute(1, 2, 0))
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

        colors_rgb_global = (
            (sorted_img_rgb_global + 1) * 127.5
        ).astype(np.uint8) / 255.0
        colors_rgb_local = (
            (sorted_img_rgb_local + 1) * 127.5
        ).astype(np.uint8) / 255.0

        conf_norm_global = (sorted_conf_global - sorted_conf_global.min()) / (
            sorted_conf_global.max() - sorted_conf_global.min() + 1e-8
        )
        conf_norm_local = (sorted_conf_local - sorted_conf_local.min()) / (
            sorted_conf_local.max() - sorted_conf_local.min() + 1e-8
        )
        colormap = cm.turbo
        colors_confidence_global = colormap(conf_norm_global)[:, :3]
        colors_confidence_local = colormap(conf_norm_local)[:, :3]

        rainbow_color_for_frame = rainbow_color(i, num_frames)
        colors_rainbow_global = np.tile(
            rainbow_color_for_frame, (sorted_pts3d_global.shape[0], 1)
        )
        colors_rainbow_local = np.tile(
            rainbow_color_for_frame, (sorted_pts3d_local.shape[0], 1)
        )

        max_conf_global = conf_global.max()
        is_high_confidence = max_conf_global >= gui_global_conf_threshold

        height, width = view["img"].shape[2], view["img"].shape[3]
        img_rgb_reshaped = img_rgb.reshape(height, width, 3)
        img_rgb_normalized = ((img_rgb_reshaped + 1) * 127.5).astype(np.uint8)
        img_downsampled = img_rgb_normalized[::4, ::4]

        frame_data_list.append(
            {
                "sorted_pts3d_global": sorted_pts3d_global,
                "colors_rgb_global": colors_rgb_global,
                "colors_confidence_global": colors_confidence_global,
                "colors_rainbow_global": colors_rainbow_global,
                "sorted_pts3d_local": sorted_pts3d_local,
                "colors_rgb_local": colors_rgb_local,
                "colors_confidence_local": colors_confidence_local,
                "colors_rainbow_local": colors_rainbow_local,
                "sorted_not_sky_global": sorted_not_sky_global,
                "sorted_not_sky_local": sorted_not_sky_local,
                "max_conf_global": float(max_conf_global),
                "is_high_confidence": is_high_confidence,
                "height": height,
                "width": width,
                "img_downsampled": img_downsampled,
                "rainbow_color": rainbow_color_for_frame,
            }
        )

    # 场景尺度（20~80 百分位范围）
    extent_percentile = 80
    cumulative_pts_combined = np.concatenate(cumulative_pts, axis=0)
    min_coords = np.percentile(
        cumulative_pts_combined, 100 - extent_percentile, axis=0
    )
    max_coords = np.percentile(cumulative_pts_combined, extent_percentile, axis=0)
    scene_extent = max_coords - min_coords

    return frame_data_list, scene_extent


def start_visualization(
    window,
    output_dict,
    min_conf_thr_percentile=10,
    global_conf_thr_value_to_drop_view=1.5,
    point_size=0.0004,
):
    """启动 VTK 可视化（处理帧数据并渲染到主窗口 widget_3）。"""
    gui_global_conf_threshold = global_conf_thr_value_to_drop_view
    gui_point_size = point_size
    gui_show_confidence_color = False
    gui_rainbow_color_option = False
    gui_show_global = False
    gui_show_local = True

    frame_data_list, max_extent = frame_processing(
        output_dict, gui_global_conf_threshold
    )
    reconstruction(
        window,
        frame_data_list,
        max_extent,
        gui_show_confidence_color,
        gui_rainbow_color_option,
        gui_show_global,
        gui_show_local,
        gui_point_size,
    )


def reconstruction(
    window,
    frame_data_list,
    max_extent,
    gui_show_confidence_color,
    gui_rainbow_color_option,
    gui_show_global,
    gui_show_local,
    gui_point_size,
):
    """把每帧点云渲染进 window.widget_3，并注册鼠标测量回调。"""
    is_outdoor = is_outdoor_scene(frame_data_list)

    callback = None
    for i in tqdm(range(len(frame_data_list))):
        fd = frame_data_list[i]

        pts3d_global = fd["sorted_pts3d_global"]
        pts3d_local = fd["sorted_pts3d_local"]

        if gui_show_confidence_color:
            colors_global = fd["colors_confidence_global"]
            colors_local = fd["colors_confidence_local"]
        elif gui_rainbow_color_option:
            colors_global = fd["colors_rainbow_global"]
            colors_local = fd["colors_rainbow_local"]
        else:
            colors_global = fd["colors_rgb_global"]
            colors_local = fd["colors_rgb_local"]

        if is_outdoor:  # 户外场景滤除天空点
            mask_global = fd["sorted_not_sky_global"]
            mask_local = fd["sorted_not_sky_local"]
            pts3d_global = pts3d_global[mask_global > 0]
            pts3d_local = pts3d_local[mask_local > 0]
            colors_global = colors_global[mask_global > 0]
            colors_local = colors_local[mask_local > 0]

        vpoints_global = _make_vtk_points(pts3d_global, colors_global)
        vpoints_local = _make_vtk_points(pts3d_local, colors_local)

        if gui_show_global:
            actor = _make_actor(*vpoints_global, gui_point_size)
            window.widget_3.renderer.AddActor(actor)
            callback = partial(
                on_left_click, window=window, points=vpoints_global[0], max_extent=max_extent
            )
        if gui_show_local:
            actor = _make_actor(*vpoints_local, gui_point_size)
            window.widget_3.renderer.AddActor(actor)
            callback = partial(
                on_left_click, window=window, points=vpoints_local[0], max_extent=max_extent
            )

    if callback is not None:
        window.widget_3.render_window.GetInteractor().AddObserver(
            "LeftButtonPressEvent", callback
        )
    window.widget_3.render_window.Render()


def _make_vtk_points(points, colors):
    """把 numpy 点与颜色转换为 vtkPoints + vtkUnsignedCharArray。"""
    vpoints = vtk.vtkPoints()
    vcolors = vtk.vtkUnsignedCharArray()
    vcolors.SetNumberOfComponents(3)
    for pt, color in zip(points, colors):
        if color.dtype == np.float64:
            color = np.clip((color + 1) * 127.5, 0, 255).astype(np.uint8)
        vpoints.InsertNextPoint(pt)
        vcolors.InsertNextTuple3(color[0], color[1], color[2])
    return vpoints, vcolors


def _make_actor(vpoints, vcolors, point_size):
    """由点与颜色创建 vtkActor。"""
    polydata = vtk.vtkPolyData()
    polydata.SetPoints(vpoints)
    polydata.GetPointData().SetScalars(vcolors)
    vertex = vtk.vtkVertexGlyphFilter()
    vertex.SetInputData(polydata)
    mapper = vtk.vtkPolyDataMapper()
    mapper.SetInputConnection(vertex.GetOutputPort())
    actor = vtk.vtkActor()
    actor.SetMapper(mapper)
    actor.GetProperty().SetPointSize(point_size)
    return actor


def on_left_click(obj, event, window, points, max_extent):
    """鼠标左键：拾取最近点 → 选点模式打球标，两点模式画测量线。"""
    global closest_point, last_sphere_actor, last_line_actor, before_sphere_actor

    click_pos = window.widget_3.get_click_position()
    closest_point = window.widget_3.find_closest_point(click_pos, points, max_extent)
    print(f"点击位置: {click_pos}")
    print(f"最近的点坐标: {closest_point}")

    if closest_point is None:
        return

    # 选点模式：移除旧球，只保留最新
    if window.radioButton_3.isChecked():
        if last_sphere_actor is not None:
            window.widget_3.renderer.RemoveActor(last_sphere_actor)

    sphere = vtk.vtkSphereSource()
    sphere.SetRadius(np.max(max_extent) * 0.01)
    sphere.SetCenter(closest_point[0], closest_point[1], closest_point[2])
    mapper = vtk.vtkPolyDataMapper()
    mapper.SetInputConnection(sphere.GetOutputPort())
    sphere_actor = vtk.vtkActor()
    sphere_actor.SetMapper(mapper)
    sphere_actor.GetProperty().SetColor(1.0, 0.0, 0.0)
    window.widget_3.renderer.AddActor(sphere_actor)

    # 两点模式：从上一点画线到当前点
    if window.radioButton_4.isChecked() and last_sphere_actor is not None:
        if before_sphere_actor is not None:
            window.widget_3.renderer.RemoveActor(before_sphere_actor)
        if last_line_actor is not None:
            window.widget_3.renderer.RemoveActor(last_line_actor)
        line = vtk.vtkLineSource()
        line.SetPoint1(last_sphere_actor.GetCenter())
        line.SetPoint2(closest_point)
        line_mapper = vtk.vtkPolyDataMapper()
        line_mapper.SetInputConnection(line.GetOutputPort())
        line_actor = vtk.vtkActor()
        line_actor.SetMapper(line_mapper)
        line_actor.GetProperty().SetColor(1.0, 0.0, 0.0)
        line_actor.GetProperty().SetLineWidth(5)
        window.widget_3.renderer.AddActor(line_actor)
        last_line_actor = line_actor

    before_sphere_actor = last_sphere_actor
    last_sphere_actor = sphere_actor
    window.widget_3.render_window.Render()
