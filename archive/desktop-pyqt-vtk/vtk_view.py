"""VTK 点云视图（Qt + VTK 集成）。

- 渲染重建结果的点云：服务器给了真实 RGB 就用 RGB，否则回退按高度着色
- 「两点测距」模式：左键依次点选两点 → 发出信号（模型坐标）
"""
from __future__ import annotations

from typing import List, Optional

import vtk
from PyQt5.QtCore import pyqtSignal
from PyQt5.QtWidgets import QVBoxLayout, QWidget
from vtk.qt.QVTKRenderWindowInteractor import QVTKRenderWindowInteractor


class PointCloudView(QWidget):
    """点云渲染 + 两点拾取。"""

    pickedTwoPoints = pyqtSignal(list)  # [[x,y,z], [x,y,z]]

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.interactor = QVTKRenderWindowInteractor(self)
        layout.addWidget(self.interactor)

        self.renderer = vtk.vtkRenderer()
        self.renderer.SetBackground(0.043, 0.071, 0.125)   # 深空背景
        self.render_window = self.interactor.GetRenderWindow()
        self.render_window.AddRenderer(self.renderer)

        self._style = vtk.vtkInteractorStyleTrackballCamera()
        self.interactor.SetInteractorStyle(self._style)

        # 状态
        self._points: List[list] = []
        self._cloud_actor = None
        self._markers: List = []
        self._line_actor = None
        self._picked: List[list] = []
        self._measure_mode = False
        self._point_size = 3.0

        self._picker = vtk.vtkPointPicker()
        self.interactor.AddObserver("LeftButtonPressEvent", self._on_left_click, 1.0)

        self.interactor.Initialize()
        self.render_window.Render()

    # ---- 对外接口 ----
    def set_points(self, points: List[list], reset_camera: bool = True,
                   colors: List[list] | None = None) -> None:
        """设置点云。

        ``points`` 支持两种形式：

        - ``[[x, y, z], ...]`` —— 按高度着色（蓝→红），适合模型坐标系下看结构
        - ``[[x, y, z, r, g, b], ...]`` —— 用**真实 RGB** 渲染（服务器现在默认给这种）

        也可以把颜色单独放在 ``colors``（``[[r, g, b], ...]``）。
        """
        self.clear_cloud()
        self._points = [[float(p[0]), float(p[1]), float(p[2])] for p in points]
        if not self._points:
            self.render_window.Render()
            return

        rgb: List[list] | None = None
        if points and len(points[0]) >= 6:
            rgb = [[int(p[3]), int(p[4]), int(p[5])] for p in points]
        elif colors is not None and len(colors) == len(points):
            rgb = [[int(c[0]), int(c[1]), int(c[2])] for c in colors]

        vtk_points = vtk.vtkPoints()
        for x, y, z in self._points:
            vtk_points.InsertNextPoint(x, y, z)

        polydata = vtk.vtkPolyData()
        polydata.SetPoints(vtk_points)

        mapper = vtk.vtkPolyDataMapper()
        if rgb is not None:
            # 真实颜色：直接以 uchar RGB 作为标量
            cols = vtk.vtkUnsignedCharArray()
            cols.SetName("rgb")
            cols.SetNumberOfComponents(3)
            for r, g, b in rgb:
                cols.InsertNextTuple3(r, g, b)
            polydata.GetPointData().SetScalars(cols)
        else:
            heights = vtk.vtkFloatArray()
            heights.SetName("height")
            for _, _, z in self._points:
                heights.InsertNextValue(z)
            polydata.GetPointData().SetScalars(heights)

        glyph = vtk.vtkVertexGlyphFilter()
        glyph.SetInputData(polydata)

        mapper.SetInputConnection(glyph.GetOutputPort())
        if rgb is not None:
            mapper.SetColorModeToDirectScalars()
            mapper.ScalarVisibilityOn()
        else:
            lut = vtk.vtkLookupTable()
            lut.SetHueRange(0.66, 0.0)  # 蓝 → 红
            lut.SetTableRange(min(p[2] for p in self._points),
                              max(p[2] for p in self._points) or 1.0)
            lut.Build()
            mapper.SetLookupTable(lut)
            mapper.SetScalarRange(lut.GetTableRange())
            mapper.SetScalarModeToUsePointFieldData()
            mapper.SelectColorArray("height")

        actor = vtk.vtkActor()
        actor.SetMapper(mapper)
        actor.GetProperty().SetPointSize(self._point_size)
        actor.GetProperty().SetAmbient(0.35)
        actor.GetProperty().SetDiffuse(0.75)

        self.renderer.AddActor(actor)
        self._cloud_actor = actor

        if reset_camera:
            self.reset_view()
        else:
            self.render_window.Render()

    def set_point_size(self, size: float) -> None:
        self._point_size = float(size)
        if self._cloud_actor is not None:
            self._cloud_actor.GetProperty().SetPointSize(self._point_size)
        self.render_window.Render()

    def set_measure_mode(self, on: bool) -> None:
        self._measure_mode = bool(on)
        if not self._measure_mode:
            self._picked = []

    def reset_view(self) -> None:
        """重置相机到刚好容纳点云。"""
        self.renderer.ResetCamera()
        cam = self.renderer.GetActiveCamera()
        cam.Azimuth(30)
        cam.Elevation(20)
        cam.SetViewUp(0, 1, 0)
        self.renderer.ResetCameraClippingRange()
        self.render_window.Render()

    def clear_measurements(self) -> None:
        for actor in self._markers:
            self.renderer.RemoveActor(actor)
        self._markers = []
        if self._line_actor is not None:
            self.renderer.RemoveActor(self._line_actor)
            self._line_actor = None
        self._picked = []
        self.render_window.Render()

    def clear_cloud(self) -> None:
        self.clear_measurements()
        if self._cloud_actor is not None:
            self.renderer.RemoveActor(self._cloud_actor)
            self._cloud_actor = None
        self._points = []
        self.render_window.Render()

    def closeEvent(self, event):  # noqa: N802 - Qt 命名
        # 释放 VTK 资源，避免退出时崩溃
        self.interactor.Finalize()
        super().closeEvent(event)

    # ---- 内部 ----
    def _on_left_click(self, obj, event) -> None:  # noqa: ARG002
        if not self._measure_mode or not self._points:
            return
        pos = self.interactor.GetEventPosition()
        self._picker.Pick(pos[0], pos[1], 0, self.renderer)
        if self._picker.GetPointId() < 0:
            return
        point = list(self._picker.GetPickPosition())
        self._picked.append(point)
        self._add_marker(point, is_first=(len(self._picked) == 1))
        if len(self._picked) == 2:
            self._add_line(self._picked[0], self._picked[1])
            self.pickedTwoPoints.emit(list(self._picked))
            self._picked = []
        self.render_window.Render()

    def _add_marker(self, point, is_first: bool) -> None:
        source = vtk.vtkSphereSource()
        source.SetCenter(point)
        size = self._marker_size()
        source.SetRadius(size * 0.6)
        source.SetThetaResolution(16)
        source.SetPhiResolution(16)

        mapper = vtk.vtkPolyDataMapper()
        mapper.SetInputConnection(source.GetOutputPort())
        actor = vtk.vtkActor()
        actor.SetMapper(mapper)
        actor.GetProperty().SetColor(0.13, 0.85, 0.93) if is_first else \
            actor.GetProperty().SetColor(0.98, 0.57, 0.24)
        self.renderer.AddActor(actor)
        self._markers.append(actor)

    def _add_line(self, p0, p1) -> None:
        line = vtk.vtkLineSource()
        line.SetPoint1(p0)
        line.SetPoint2(p1)

        mapper = vtk.vtkPolyDataMapper()
        mapper.SetInputConnection(line.GetOutputPort())
        actor = vtk.vtkActor()
        actor.SetMapper(mapper)
        actor.GetProperty().SetColor(0.98, 0.75, 0.14)
        actor.GetProperty().SetLineWidth(2.0)
        self.renderer.AddActor(actor)
        self._line_actor = actor

    def _marker_size(self) -> float:
        if not self._points:
            return 0.01
        xs = [p[0] for p in self._points]
        ys = [p[1] for p in self._points]
        zs = [p[2] for p in self._points]
        extent = max(max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs))
        return max(extent * 0.012, 1e-6)
