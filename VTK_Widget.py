"""VTK 控件。"""

import sys
import vtk
import numpy as np
from PyQt5 import QtCore
from PyQt5.QtWidgets import QApplication, QMainWindow, QVBoxLayout, QWidget
from vtk.qt.QVTKRenderWindowInteractor import QVTKRenderWindowInteractor

class VTKWidget(QWidget):
    """基于 VTK 的 3D 渲染控件。"""

    def __init__(self, parent=None):
        """初始化 VTK 渲染窗口和交互器。"""
        super().__init__(parent)
        layout = QVBoxLayout(self)
        # 获取 QWidget 的位置（在窗口内的坐标）
        self.renderer = vtk.vtkRenderer()
        self.vtk_widget = QVTKRenderWindowInteractor(self)
        layout.addWidget(self.vtk_widget)
        self.render_window = self.vtk_widget.GetRenderWindow()
        self.render_window.AddRenderer(self.renderer)
        self.renderer.SetBackground(1.0, 1.0, 1.0)
        camera = self.renderer.GetActiveCamera()
        camera.SetPosition(-0.00141163, -0.01910395, -0.06794288)  # 设置相机的位置0 0 500
        camera.SetFocalPoint(-0.00352821, -0.01143425, 0.0154939)  # 设置相机焦点#200 200 0
        camera.SetViewUp(0.0, -1.0, 0.0)
        style = vtk.vtkInteractorStyleTrackballCamera()
        self.vtk_widget.SetInteractorStyle(style)
        # # 创建鼠标点击回调
        # self.vtk_widget.GetRenderWindow().GetInteractor().AddObserver("LeftButtonPressEvent", self.on_click)
        self.render_window.Render()

    # def on_left_click(self, obj, event,points):
    #     global closest_point
    #     # 获取鼠标点击位置的3D坐标
    #     click_pos = self.get_click_position()
    #
    #     # 在点击位置附近0.1的正方体内搜索最近的点
    #     closest_point = self.find_closest_point(click_pos,points)
    #
    #     print(f"点击位置: {click_pos}")
    #     print(f"最近的点坐标: {closest_point}")

    def get_click_position(self):
        """获取鼠标点击位置对应的三维世界坐标。"""
        # 获取鼠标点击的位置
        click_pos = self.vtk_widget.GetRenderWindow().GetInteractor().GetEventPosition()

        # 使用 vtkWorldPointPicker 来获取点击的世界坐标
        picker = vtk.vtkWorldPointPicker()
        picker.Pick(click_pos[0], click_pos[1], 0, self.renderer)

        # 获取点击位置的世界坐标
        world_pos = picker.GetPickPosition()
        # print(f"World coordinates of clicked point: {world_pos}")
        return world_pos

    def find_closest_point(self, click_pos, points, max_extent):
        """在点击位置附近搜索最近的点云点。

        Args:
            click_pos: 点击位置的三维坐标。
            points: 点云数据。
            max_extent: 点云最大范围，用于确定搜索半径。

        Returns:
            距离点击位置最近的点，若未找到则返回 None。
        """
        # 定义搜索范围
        cube_half_length = max_extent*0.02
        closest_point = None
        min_distance = float('inf')

        # 遍历点云数据，找到离点击点最近的点
        for i in range(points.GetNumberOfPoints()):
            point = points.GetPoint(i)
            if (abs(point[0] - click_pos[0]) <= cube_half_length[0] and
                    abs(point[1] - click_pos[1]) <= cube_half_length[1] and
                    abs(point[2] - click_pos[2]) <= cube_half_length[2]):

                # 计算距离
                distance = vtk.vtkMath.Distance2BetweenPoints(point, click_pos)
                if distance < min_distance:
                    min_distance = distance
                    closest_point = point
        return closest_point
