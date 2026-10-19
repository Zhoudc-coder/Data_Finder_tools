# -*- coding: utf-8 -*-
"""
程序入口。
"""
import sys

from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication

from core.utils import setup_logging
from ui.main_window import MainWindow
from ui.styles import APP_QSS


def main() -> None:
    setup_logging()

    app = QApplication(sys.argv)
    app.setApplicationName("出货数据查找工具")
    app.setOrganizationName("LogisticsDataFinder")
    app.setStyle("Fusion")
    app.setStyleSheet(APP_QSS)

    # 尽量保证 Windows 中文显示正常
    if sys.platform.startswith("win"):
        app.setFont(QFont("Microsoft YaHei", 10))
    elif sys.platform == "darwin":
        app.setFont(QFont("PingFang SC", 12))
    else:
        app.setFont(QFont("Noto Sans CJK SC", 10))

    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()