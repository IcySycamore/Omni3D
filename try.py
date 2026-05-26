import vtk

# 创建点云数据
points = vtk.vtkPoints()
# 示例数据：3个点云
points.InsertNextPoint(0.1, 0.1, 0.1)
points.InsertNextPoint(0.2, 0.2, 0.2)
points.InsertNextPoint(0.3, 0.3, 0.3)

colors = vtk.vtkUnsignedCharArray()
colors.SetNumberOfComponents(3)
colors.InsertNextTuple3(255,0,0)
colors.InsertNextTuple3(255,0,0)
colors.InsertNextTuple3(255,0,0)

# 创建点集数据对象
poly_data = vtk.vtkPolyData()
poly_data.SetPoints(points)
poly_data.GetPointData().SetScalars(colors)

# 顶点相关的 filter
vertex = vtk.vtkVertexGlyphFilter()
vertex.SetInputData(poly_data)

# 创建点云的可视化器
point_mapper = vtk.vtkPolyDataMapper()
point_mapper.SetInputConnection(vertex.GetOutputPort())
# point_mapper.SetInputData(poly_data)

# 创建演员对象
point_actor = vtk.vtkActor()
point_actor.SetMapper(point_mapper)

# 设置点的大小
point_actor.GetProperty().SetPointSize(10)  # 设置点的大小

# 创建渲染器、渲染窗口、渲染窗口交互器
renderer = vtk.vtkRenderer()
render_window = vtk.vtkRenderWindow()
render_window.AddRenderer(renderer)

render_window_interactor = vtk.vtkRenderWindowInteractor()
render_window_interactor.SetRenderWindow(render_window)

# 将点云添加到渲染器
renderer.AddActor(point_actor)

# 设置背景色
renderer.SetBackground(1.0, 1.0, 1.0)

# 设置相机视角
camera = renderer.GetActiveCamera()
camera.SetPosition(0.5, 0.5, 1.0)  # 设置相机位置
camera.SetFocalPoint(0.2, 0.2, 0.2)  # 设置相机焦点

# 启动渲染并开始交互
render_window.Render()
render_window_interactor.Start()
