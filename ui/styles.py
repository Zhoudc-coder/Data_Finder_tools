# -*- coding: utf-8 -*-
"""界面 QSS 样式：蓝青主色、浅色背景、圆角卡片。"""

APP_QSS = """
QMainWindow, QWidget {
    background: #f4f7fb;
    color: #1f2d3d;
    font-size: 13px;
}

QFrame#Card {
    background: #ffffff;
    border: 1px solid #dbe7f3;
    border-radius: 12px;
}

QLabel#Title {
    font-size: 22px;
    font-weight: 700;
    color: #0b63ce;
}

QLabel#Subtitle {
    color: #5b6b7c;
}

QLabel#Hint {
    color: #6b7c8f;
    font-size: 12px;
}

QListWidget {
    background: #ffffff;
    border: 1px solid #dbe7f3;
    border-radius: 10px;
    padding: 6px;
    outline: none;
}

QListWidget::item {
    padding: 10px 8px;
    border-radius: 8px;
    margin: 2px;
}

QListWidget::item:selected {
    background: #e3f2fd;
    color: #0b63ce;
    font-weight: 600;
}

QListWidget::item:hover {
    background: #f0f7ff;
}

QPushButton {
    background: #0b63ce;
    color: white;
    border: none;
    border-radius: 8px;
    padding: 8px 14px;
    min-height: 18px;
}

QPushButton:hover {
    background: #0a56b3;
}

QPushButton:pressed {
    background: #084a99;
}

QPushButton:disabled {
    background: #b8c7d9;
    color: #f0f0f0;
}

QPushButton#Secondary {
    background: #e8f3ff;
    color: #0b63ce;
    border: 1px solid #b9d7ff;
}

QPushButton#Secondary:hover {
    background: #d6e9ff;
}

QPushButton#Danger {
    background: #e55353;
}

QPushButton#Danger:hover {
    background: #cc4444;
}

QLineEdit, QTextEdit {
    background: #ffffff;
    border: 1px solid #cfdceb;
    border-radius: 8px;
    padding: 6px;
    selection-background-color: #0b63ce;
}

QLineEdit:focus, QTextEdit:focus {
    border: 1px solid #0b63ce;
}

QProgressBar {
    border: none;
    border-radius: 7px;
    background: #e6eef8;
    text-align: center;
    height: 14px;
}

QProgressBar::chunk {
    background: #00a8a8;
    border-radius: 7px;
}

QTableWidget {
    background: white;
    border: 1px solid #dbe7f3;
    border-radius: 8px;
    gridline-color: #edf2f7;
    alternate-background-color: #f8fbff;
}

QHeaderView::section {
    background: #eef6ff;
    color: #1f2d3d;
    padding: 6px;
    border: none;
    border-right: 1px solid #dbe7f3;
    font-weight: 600;
}

QStatusBar {
    background: #eaf4ff;
    color: #0b63ce;
}

QScrollBar:vertical {
    background: #f4f7fb;
    width: 10px;
    margin: 0px;
}

QScrollBar::handle:vertical {
    background: #b9d7ff;
    border-radius: 5px;
    min-height: 24px;
}

QScrollBar::handle:vertical:hover {
    background: #8fc1ff;
}

QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0px;
}
"""