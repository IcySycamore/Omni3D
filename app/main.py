"""Omni3D 桌面应用入口。

运行方式（在项目根目录）：
    python app/main.py
或：
    D:\\anaconda3\\envs\\Omni3D\\python.exe app/main.py
"""
import os
import sys

# 将项目根目录加入 sys.path，确保 fast3r 包与 app 包可导入
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from PyQt5.QtWidgets import QApplication  # noqa: E402

from app.ui.main_window import MainWindow  # noqa: E402


def main():
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
