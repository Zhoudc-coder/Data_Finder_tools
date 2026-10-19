# -*- coding: utf-8 -*-
"""
主窗口。
负责 UI 布局、后台线程调度、结果预览和导出交互。

【安全约定】
本工具对“待出库”文件夹中的原始出货数据文件仅做只读访问：
1. 所有原始 CSV 读取均使用 "r" 只读模式；
2. 导出 Excel 时强制禁止保存到数据文件夹及其子目录；
3. 若程序被放在数据文件夹内导致索引库路径落入数据文件夹，
   建立索引前会检测并阻止，避免在数据文件夹写入任何文件。

【网络共享盘适配】
1. 目录遍历改为 os.scandir + 边遍历边回调进度；
2. UI 线程不做任何可能阻塞的网络路径检查；
3. 进度条在“不确定进度”阶段显示忙碌动画；
4. 网络路径检测与日志提示。
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QSettings, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QFontMetrics
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from core.exporter import Exporter
from core.indexer import Indexer
from core.searcher import SearchResult, Searcher
from core.utils import (
    CancelledError,
    PROCESS_HEADER_VALUES,
    SN_HEADER_VALUES,
    get_index_db_path,
    is_network_path,
    is_path_inside,
    load_first_column_from_file,
    parse_input_tokens,
    sanitize_filename,
)
from ui.styles import APP_QSS


class IndexWorker(QThread):
    """后台索引线程。"""
    progress = Signal(int, str)
    log = Signal(str)
    finished_ok = Signal(dict)
    failed = Signal(str)

    def __init__(self, db_path: Path, folder: Path, rebuild: bool, parent=None):
        super().__init__(parent)
        self.db_path = db_path
        self.folder = folder
        self.rebuild = rebuild
        self.cancel_event = threading.Event()

    def cancel(self) -> None:
        self.cancel_event.set()

    def run(self) -> None:
        try:
            indexer = Indexer(self.db_path)
            stats = indexer.build_index(
                self.folder,
                incremental=not self.rebuild,
                progress_cb=lambda p, m: self.progress.emit(p, m),
                log_cb=lambda m: self.log.emit(m),
                cancel_event=self.cancel_event,
            )
            self.finished_ok.emit(stats)
        except Exception as e:
            logging.exception("建立索引失败")
            self.failed.emit(str(e))


class SearchWorker(QThread):
    """后台查找线程。"""
    progress = Signal(int, str)
    log = Signal(str)
    finished_ok = Signal(object)
    failed = Signal(str)

    def __init__(self, db_path: Path, kind: str, tokens: list[str], parent=None):
        super().__init__(parent)
        self.db_path = db_path
        self.kind = kind
        self.tokens = tokens
        self.cancel_event = threading.Event()

    def cancel(self) -> None:
        self.cancel_event.set()

    def run(self) -> None:
        try:
            searcher = Searcher(self.db_path)
            progress_cb = lambda p, m: self.progress.emit(p, m)
            log_cb = lambda m: self.log.emit(m)

            if self.kind == "summary":
                result = searcher.search_sn_summary(
                    self.tokens, progress_cb=progress_cb, log_cb=log_cb,
                    cancel_event=self.cancel_event,
                )
            elif self.kind == "full":
                result = searcher.search_sn_full(
                    self.tokens, progress_cb=progress_cb, log_cb=log_cb,
                    cancel_event=self.cancel_event,
                )
            elif self.kind == "both":
                result = searcher.search_sn_both(
                    self.tokens, progress_cb=progress_cb, log_cb=log_cb,
                    cancel_event=self.cancel_event,
                )
            elif self.kind == "file_all":
                result = searcher.search_sn_files_all(
                    self.tokens, progress_cb=progress_cb, log_cb=log_cb,
                    cancel_event=self.cancel_event,
                )
            elif self.kind == "process_all":
                result = searcher.search_process_all(
                    self.tokens, progress_cb=progress_cb, log_cb=log_cb,
                    cancel_event=self.cancel_event,
                )
            elif self.kind == "duplicate":
                result = searcher.check_duplicate_sn(
                    self.tokens, progress_cb=progress_cb, log_cb=log_cb,
                    cancel_event=self.cancel_event,
                )
            else:
                raise ValueError(f"未知查找类型：{self.kind}")

            for msg in result.logs:
                self.log.emit(msg)

            self.finished_ok.emit(result)

        except CancelledError:
            self.failed.emit("操作已取消")
        except Exception as e:
            logging.exception("查找失败")
            self.failed.emit(str(e))


class ExportWorker(QThread):
    """后台导出线程。"""
    progress = Signal(int, str)
    finished_ok = Signal(str)
    failed = Signal(str)

    def __init__(self, result: SearchResult, output_path: Path, parent=None):
        super().__init__(parent)
        self.result = result
        self.output_path = output_path

    def run(self) -> None:
        try:
            exporter = Exporter()
            exporter.export(
                self.result,
                self.output_path,
                progress_cb=lambda p, m: self.progress.emit(p, m),
            )
            self.finished_ok.emit(str(self.output_path))
        except Exception as e:
            logging.exception("导出失败")
            self.failed.emit(str(e))


class MainWindow(QMainWindow):
    FUNCTIONS = [
        ("summary", "1. 按 SN 查找摘要信息", "查找每个 SN 对应的过程码和上传时间", "SN"),
        ("full", "2. 按 SN 查找完整数据", "导出 SN 所在行的全部列数据", "SN"),
        ("both", "3. 按 SN 查找摘要+完整数据", "同一个 Excel 两个工作表", "SN"),
        ("file_all", "4. 按 SN 查找所在文件全部数据", "导出 SN 所在文件的所有产品", "SN"),
        ("process_all", "5. 按过程码查找文件全部数据", "过程码为文件名去掉后缀", "过程码"),
        ("duplicate", "6. 重复 SN 检查", "列出 SN 所有出现位置与次数", "SN"),
    ]

    def __init__(self):
        super().__init__()
        self.setWindowTitle("物流出货数据查找工具")
        self.resize(1280, 820)
        self.setStyleSheet(APP_QSS)

        self.settings = QSettings("LogisticsDataFinder", "LogisticsDataFinder")
        self.db_path, self.db_msg = get_index_db_path()

        self.worker = None
        self.result: SearchResult | None = None
        self.busy = False

        self._build_ui()
        self._load_settings()

        self._center_on_screen()
        QTimer.singleShot(0, self._center_on_screen)

        self._append_log(self.db_msg)
        self._append_log("原始数据只读保护：本工具对“待出库”文件夹内的 CSV 只做读取，不会修改或覆盖任何原始文件。")
        self._append_log("请选择或输入“待出库”文件夹（支持本地路径和网络共享盘 UNC 路径）。")

    # ------------------------------------------------------------------
    # 窗口居中
    # ------------------------------------------------------------------
    def _center_on_screen(self) -> None:
        try:
            screen = self.screen() or QApplication.primaryScreen()
            if screen is None:
                return
            available = screen.availableGeometry()
            frame = self.frameGeometry()
            frame.moveCenter(available.center())
            self.move(frame.topLeft())
        except Exception:
            logging.exception("窗口居中失败（不影响正常使用）")

    # ------------------------------------------------------------------
    # UI 构建
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)

        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(16, 16, 16, 16)
        main_layout.setSpacing(12)

        header_card = QFrame()
        header_card.setObjectName("Card")
        header_layout = QHBoxLayout(header_card)
        header_layout.setContentsMargins(18, 14, 18, 14)

        title_box = QVBoxLayout()
        title = QLabel("物流出货数据查找工具")
        title.setObjectName("Title")
        subtitle = QLabel("高效索引 CSV 装箱文件，支持按 SN / 过程码查找并导出 Excel（原始数据只读，支持网络共享盘）")
        subtitle.setObjectName("Subtitle")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)

        header_layout.addLayout(title_box)
        header_layout.addStretch()
        main_layout.addWidget(header_card)

        body_layout = QHBoxLayout()
        body_layout.setSpacing(12)
        main_layout.addLayout(body_layout, 1)

        # 左侧功能列表
        left_card = QFrame()
        left_card.setObjectName("Card")
        left_card.setFixedWidth(260)
        left_layout = QVBoxLayout(left_card)
        left_layout.setContentsMargins(14, 14, 14, 14)

        left_title = QLabel("功能选择")
        left_title.setStyleSheet("font-weight: 600; color: #0b63ce;")
        left_layout.addWidget(left_title)

        self.func_list = QListWidget()
        self.func_list.setWordWrap(False)
        for key, label, desc, input_type in self.FUNCTIONS:
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, key)
            item.setToolTip(desc)
            self.func_list.addItem(item)
        self.func_list.currentRowChanged.connect(self._on_func_changed)
        left_layout.addWidget(self.func_list, 1)

        left_hint = QLabel("提示：索引只保存 SN 与文件位置，不保存完整数据，查询速度快。")
        left_hint.setObjectName("Hint")
        left_hint.setWordWrap(True)
        left_layout.addWidget(left_hint)

        self.func_list.ensurePolished()
        fm = QFontMetrics(self.func_list.font())
        max_text_width = 0
        for i in range(self.func_list.count()):
            w = fm.horizontalAdvance(self.func_list.item(i).text())
            if w > max_text_width:
                max_text_width = w
        left_card.setFixedWidth(max_text_width + 70)

        body_layout.addWidget(left_card)

        right_layout = QVBoxLayout()
        right_layout.setSpacing(12)
        body_layout.addLayout(right_layout, 1)

        # 文件夹选择卡片
        folder_card = QFrame()
        folder_card.setObjectName("Card")
        folder_layout = QHBoxLayout(folder_card)
        folder_layout.setContentsMargins(14, 12, 14, 12)

        folder_layout.addWidget(QLabel("待出库文件夹："))
        self.folder_edit = QLineEdit()
        self.folder_edit.setPlaceholderText(
            "请选择或粘贴“待出库”文件夹路径，支持 UNC，如 \\\\server\\share\\待出库"
        )
        folder_layout.addWidget(self.folder_edit, 1)

        self.select_btn = QPushButton("选择文件夹")
        self.select_btn.setObjectName("Secondary")
        self.select_btn.clicked.connect(self._select_folder)
        folder_layout.addWidget(self.select_btn)

        self.open_btn = QPushButton("打开文件夹")
        self.open_btn.setObjectName("Secondary")
        self.open_btn.clicked.connect(self._open_folder)
        folder_layout.addWidget(self.open_btn)

        self.update_index_btn = QPushButton("更新索引(增量)")
        self.update_index_btn.clicked.connect(lambda: self._start_index(rebuild=False))
        folder_layout.addWidget(self.update_index_btn)

        self.rebuild_index_btn = QPushButton("重建索引(全量)")
        self.rebuild_index_btn.clicked.connect(lambda: self._start_index(rebuild=True))
        folder_layout.addWidget(self.rebuild_index_btn)

        right_layout.addWidget(folder_card)

        # 输入卡片
        input_card = QFrame()
        input_card.setObjectName("Card")
        input_layout = QVBoxLayout(input_card)
        input_layout.setContentsMargins(14, 12, 14, 12)

        self.input_label = QLabel("输入 SN（每行一个，也可用逗号/空格分隔）")
        input_layout.addWidget(self.input_label)

        self.input_edit = QTextEdit()
        self.input_edit.setPlaceholderText(
            "例如：\nSN1001\nSN1002, SN1003\n\n也可点击「导入查找」从 xlsx/csv 文件第一列导入 SN 并自动查找。"
        )
        self.input_edit.setFixedHeight(110)
        input_layout.addWidget(self.input_edit)

        input_hint = QLabel(
            "支持多行、英文/中文逗号、空格、分号分隔；SN 去除首尾空格后精确匹配，区分大小写。"
        )
        input_hint.setObjectName("Hint")
        input_hint.setWordWrap(True)
        input_layout.addWidget(input_hint)

        right_layout.addWidget(input_card)

        # 操作按钮 + 进度条
        action_layout = QHBoxLayout()
        self.start_btn = QPushButton("开始查找")
        self.start_btn.clicked.connect(self._on_start_search_clicked)
        action_layout.addWidget(self.start_btn)

        self.import_btn = QPushButton("导入查找")
        self.import_btn.setObjectName("Secondary")
        self.import_btn.setToolTip("从 xlsx/csv 文件第一列导入并自动开始查找")
        self.import_btn.clicked.connect(self._import_and_search)
        action_layout.addWidget(self.import_btn)

        self.export_btn = QPushButton("导出结果")
        self.export_btn.setObjectName("Secondary")
        self.export_btn.setEnabled(False)
        self.export_btn.clicked.connect(self._export_result)
        action_layout.addWidget(self.export_btn)

        self.clear_btn = QPushButton("清空")
        self.clear_btn.setObjectName("Secondary")
        self.clear_btn.clicked.connect(self._clear_all)
        action_layout.addWidget(self.clear_btn)

        action_layout.addStretch()

        self.cancel_btn = QPushButton("取消")
        self.cancel_btn.setObjectName("Danger")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self._cancel_worker)
        action_layout.addWidget(self.cancel_btn)

        right_layout.addLayout(action_layout)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        right_layout.addWidget(self.progress_bar)

        # 结果预览卡片
        result_card = QFrame()
        result_card.setObjectName("Card")
        result_layout = QVBoxLayout(result_card)
        result_layout.setContentsMargins(14, 12, 14, 12)

        result_title = QLabel("结果预览")
        result_title.setStyleSheet("font-weight: 600; color: #0b63ce;")
        result_layout.addWidget(result_title)

        self.table = QTableWidget()
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        result_layout.addWidget(self.table, 1)

        right_layout.addWidget(result_card, 1)

        # 日志卡片
        log_card = QFrame()
        log_card.setObjectName("Card")
        log_layout = QVBoxLayout(log_card)
        log_layout.setContentsMargins(14, 12, 14, 12)

        log_title = QLabel("运行日志")
        log_title.setStyleSheet("font-weight: 600; color: #0b63ce;")
        log_layout.addWidget(log_title)

        self.log_edit = QTextEdit()
        self.log_edit.setReadOnly(True)
        self.log_edit.setFixedHeight(130)
        log_layout.addWidget(self.log_edit)

        right_layout.addWidget(log_card)

        self.statusBar().showMessage("就绪")

        self.action_buttons = [
            self.select_btn,
            self.open_btn,
            self.update_index_btn,
            self.rebuild_index_btn,
            self.start_btn,
            self.import_btn,
            self.clear_btn,
        ]

        self.func_list.setCurrentRow(0)

    # ------------------------------------------------------------------
    # 设置与日志
    # ------------------------------------------------------------------
    def _load_settings(self) -> None:
        folder = self.settings.value("last_folder", "", type=str)
        if folder:
            self.folder_edit.setText(folder)

        last_func = self.settings.value("last_func", 0, type=int)
        try:
            self.func_list.setCurrentRow(int(last_func))
        except Exception:
            self.func_list.setCurrentRow(0)

    def _save_settings(self) -> None:
        self.settings.setValue("last_folder", self.folder_edit.text().strip())
        self.settings.setValue("last_func", self.func_list.currentRow())

    def _append_log(self, message: str) -> None:
        message = str(message)
        self.log_edit.append(f"[{datetime.now():%H:%M:%S}] {message}")
        logging.info(message)

    # ------------------------------------------------------------------
    # 功能切换与文件夹操作
    # ------------------------------------------------------------------
    def _on_func_changed(self, row: int) -> None:
        if row < 0:
            return
        key, label, desc, input_type = self.FUNCTIONS[row]
        if input_type == "SN":
            self.input_label.setText("输入 SN（每行一个，也可用逗号/空格分隔）")
            self.input_edit.setPlaceholderText(
                "例如：\nSN1001\nSN1002, SN1003\n\n也可点击「导入查找」从 xlsx/csv 文件第一列导入 SN 并自动查找。"
            )
            self.import_btn.setToolTip("从 xlsx/csv 文件第一列导入 SN 并自动开始查找")
        else:
            self.input_label.setText("输入过程码（文件名去掉 .csv 后缀）")
            self.input_edit.setPlaceholderText(
                "例如：\nP001\nP002, P003\n\n也可点击「导入查找」从 xlsx/csv 文件第一列导入过程码并自动查找。"
            )
            self.import_btn.setToolTip("从 xlsx/csv 文件第一列导入过程码并自动开始查找")

        self.import_btn.setEnabled(not self.busy)
        self.statusBar().showMessage(desc)

    def _select_folder(self) -> None:
        start_dir = self.folder_edit.text().strip() or str(Path.home())
        folder = QFileDialog.getExistingDirectory(self, "选择待出库文件夹", start_dir)
        if folder:
            self.folder_edit.setText(folder)
            self.settings.setValue("last_folder", folder)
            self._append_log(f"已选择数据文件夹：{folder}")
            if is_network_path(folder):
                self._append_log("检测到网络路径，首次访问可能需要认证或较长时间，请耐心等待。")
            if is_path_inside(self.db_path, Path(folder)):
                self._append_log(
                    "【警告】当前索引库路径位于所选数据文件夹内，"
                    "建立索引将被阻止。请将程序移动到数据文件夹之外运行。"
                )

    def _open_folder(self) -> None:
        folder = self.folder_edit.text().strip()
        if not folder:
            QMessageBox.warning(self, "提示", "请先选择或输入待出库文件夹路径。")
            return
        # openUrl 为异步调用，不会阻塞 UI；路径无效时由系统提示
        QDesktopServices.openUrl(QUrl.fromLocalFile(folder))

    def _clear_all(self) -> None:
        self.input_edit.clear()
        self.table.clear()
        self.table.setRowCount(0)
        self.table.setColumnCount(0)
        self.result = None
        self.export_btn.setEnabled(False)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.statusBar().showMessage("已清空")
        self._append_log("已清空输入和结果预览。")

    # ------------------------------------------------------------------
    # 导入并自动开始查找
    # ------------------------------------------------------------------
    def _import_and_search(self) -> None:
        if self.busy:
            return

        row = self.func_list.currentRow()
        if row < 0:
            return
        key, label, desc, input_type = self.FUNCTIONS[row]

        if input_type == "SN":
            header_values = SN_HEADER_VALUES
            unit = "SN"
            dlg_title = "导入 SN 文件（第一列为 SN）"
        else:
            header_values = PROCESS_HEADER_VALUES
            unit = "过程码"
            dlg_title = "导入过程码文件（第一列为过程码）"

        start_dir = self.settings.value("last_import_dir", "", type=str)
        if not start_dir or not Path(start_dir).is_dir():
            start_dir = self.folder_edit.text().strip() or str(Path.home())

        path, _ = QFileDialog.getOpenFileName(
            self, dlg_title, start_dir,
            "支持的文件 (*.xlsx *.csv);;Excel 文件 (*.xlsx);;CSV 文件 (*.csv)",
        )
        if not path:
            return

        try:
            self.settings.setValue("last_import_dir", str(Path(path).parent))
        except Exception:
            pass

        try:
            values = load_first_column_from_file(
                Path(path), header_values=header_values, log_cb=self._append_log,
            )
        except Exception as e:
            logging.exception("导入文件失败")
            self._append_log(f"导入文件失败：{e}")
            QMessageBox.warning(self, "导入失败", str(e))
            return

        if not values:
            self._append_log(f"文件中未读取到有效的{unit}：{path}")
            QMessageBox.warning(self, "提示", f"文件中未读取到有效的{unit}，请检查第一列内容。")
            return

        self.input_edit.setPlainText("\n".join(values))
        self._append_log(f"已从文件导入 {len(values)} 个{unit}：{path}，正在自动开始查找...")
        self.statusBar().showMessage(f"已导入 {len(values)} 个{unit}，正在自动开始查找...")
        self._start_search()

    # ------------------------------------------------------------------
    # 线程状态控制
    # ------------------------------------------------------------------
    def _set_busy(self, busy: bool, allow_cancel: bool = True) -> None:
        self.busy = busy
        for btn in self.action_buttons:
            btn.setEnabled(not busy)
        self.func_list.setEnabled(not busy)
        self.cancel_btn.setEnabled(busy and allow_cancel)

        if busy:
            self.export_btn.setEnabled(False)
        else:
            self.export_btn.setEnabled(self.result is not None and not self.result.is_empty)
            self.import_btn.setEnabled(True)

    def _on_progress(self, value: int, message: str) -> None:
        """value < 0 表示不确定进度（busy 动画）；value >= 0 表示具体百分比。"""
        if value < 0:
            if self.progress_bar.maximum() != 0:
                self.progress_bar.setRange(0, 0)
        else:
            if self.progress_bar.maximum() == 0:
                self.progress_bar.setRange(0, 100)
            self.progress_bar.setValue(max(0, min(100, int(value))))
        if message:
            self.statusBar().showMessage(message)

    def _cancel_worker(self) -> None:
        if self.worker and hasattr(self.worker, "cancel"):
            self.worker.cancel()
            self._append_log("已发送取消请求，请稍候...（网络盘上的单个系统调用可能需等待其返回）")
        else:
            self._append_log("当前操作不可取消。")

    # ------------------------------------------------------------------
    # 索引
    # ------------------------------------------------------------------
    def _start_index(self, rebuild: bool) -> None:
        if self.busy:
            return

        folder = self.folder_edit.text().strip()
        if not folder:
            QMessageBox.warning(self, "提示", "请先选择或输入待出库文件夹路径。")
            return

        folder_path = Path(folder)

        # 只读保护：使用不触网的 is_path_inside
        if is_path_inside(self.db_path, folder_path):
            QMessageBox.critical(
                self,
                "已阻止：索引库位于数据文件夹内",
                "为保护“待出库”原始数据文件夹不被写入，本工具禁止在数据文件夹内"
                "创建或更新索引库。\n\n"
                f"当前索引库路径：\n{self.db_path}\n\n"
                "请将本程序移动到“待出库”文件夹之外的位置运行。",
            )
            self._append_log(f"已阻止在数据文件夹内建立索引：{self.db_path}")
            return

        if rebuild:
            ret = QMessageBox.question(
                self, "确认重建",
                "将清空现有索引并全量重建，是否继续？",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if ret != QMessageBox.Yes:
                return

        self.settings.setValue("last_folder", folder)
        self._set_busy(True, allow_cancel=True)

        # 不确定进度：显示忙碌动画
        self.progress_bar.setRange(0, 0)
        self.statusBar().showMessage("正在初始化索引（网络路径访问可能较慢）...")

        self._append_log(("开始全量重建索引：" if rebuild else "开始增量更新索引：") + folder)
        if is_network_path(folder_path):
            self._append_log("检测到网络路径，索引构建可能需要较长时间，请耐心等待。")

        # 实际存在性检查与目录遍历均在后台线程执行，避免阻塞 UI
        self.worker = IndexWorker(self.db_path, folder_path, rebuild)
        self.worker.progress.connect(self._on_progress)
        self.worker.log.connect(self._append_log)
        self.worker.finished_ok.connect(self._on_index_finished)
        self.worker.failed.connect(self._on_worker_failed)
        self.worker.start()

    def _on_index_finished(self, stats: dict) -> None:
        self._set_busy(False)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(100)
        self.worker = None

        if stats.get("cancelled"):
            msg = "索引构建已取消。"
            self._append_log(msg)
            self.statusBar().showMessage(msg)
            QMessageBox.information(self, "提示", msg)
            return

        msg = (
            f"索引完成：文件 {stats.get('total_files', 0)} 个，"
            f"扫描 {stats.get('scanned_files', 0)} 个，"
            f"跳过 {stats.get('skipped_files', 0)} 个，"
            f"删除 {stats.get('removed_files', 0)} 个，"
            f"SN 记录 {stats.get('sn_count', 0)} 条。"
        )
        self._append_log(msg)
        self.statusBar().showMessage("索引完成")
        QMessageBox.information(self, "索引完成", msg)

    # ------------------------------------------------------------------
    # 查找
    # ------------------------------------------------------------------
    def _on_start_search_clicked(self) -> None:
        self._start_search()

    def _start_search(self) -> None:
        if self.busy:
            return

        try:
            indexer = Indexer(self.db_path)
            file_count = indexer.count_files()
        except Exception:
            file_count = 0

        if file_count <= 0:
            QMessageBox.warning(
                self, "提示",
                "索引为空。请先选择待出库文件夹，并点击“重建索引(全量)”或“更新索引(增量)”。",
            )
            return

        tokens = parse_input_tokens(self.input_edit.toPlainText())
        if not tokens:
            QMessageBox.warning(self, "提示", "请输入至少一个 SN 或过程码。")
            return

        row = self.func_list.currentRow()
        if row < 0:
            return
        kind, label, desc, input_type = self.FUNCTIONS[row]

        self.result = None
        self.table.clear()
        self.table.setRowCount(0)
        self.table.setColumnCount(0)
        self.export_btn.setEnabled(False)

        self._set_busy(True, allow_cancel=True)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.statusBar().showMessage("正在查找...")

        self.worker = SearchWorker(self.db_path, kind, tokens)
        self.worker.progress.connect(self._on_progress)
        self.worker.log.connect(self._append_log)
        self.worker.finished_ok.connect(self._on_search_finished)
        self.worker.failed.connect(self._on_worker_failed)
        self.worker.start()

        self._append_log(f"开始查找：{label}，输入 {len(tokens)} 个。")

    def _on_search_finished(self, result: SearchResult) -> None:
        self._set_busy(False)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(100)
        self.worker = None
        self.result = result

        if result.is_empty:
            self._append_log("查找完成：未找到匹配数据。")
            self.statusBar().showMessage("未找到匹配数据")
            self.table.clear()
            self.table.setRowCount(0)
            self.table.setColumnCount(0)
            QMessageBox.information(self, "提示", "未找到匹配数据。")
            return

        if result.kind == "both":
            detail = f"摘要 {len(result.summary_rows)} 行，完整数据 {len(result.full_rows)} 行"
        elif result.kind == "summary":
            detail = f"摘要 {len(result.summary_rows)} 行"
        else:
            detail = f"数据 {len(result.full_rows)} 行"

        self._fill_table(result.preview_headers, result.preview_rows)
        self.export_btn.setEnabled(True)

        self._append_log(
            f"查找完成：{detail}。预览显示 {len(result.preview_rows)} 行 / {len(result.preview_headers)} 列。"
        )
        self.statusBar().showMessage(f"查找完成：{detail}")

        if result.kind in ("full", "file_all", "process_all"):
            self._append_log("提示：结果表列数较多时，界面仅预览前 30 列，完整数据请导出 Excel 查看。")

    def _fill_table(self, headers: list[str], rows: list[list]) -> None:
        self.table.clear()
        self.table.setColumnCount(len(headers))
        self.table.setRowCount(len(rows))
        self.table.setHorizontalHeaderLabels(headers)

        for r, row in enumerate(rows):
            for c, value in enumerate(row):
                text = str(value)
                if len(text) > 200:
                    text = text[:200] + "..."
                self.table.setItem(r, c, QTableWidgetItem(text))

        self.table.resizeColumnsToContents()

    # ------------------------------------------------------------------
    # 导出
    # ------------------------------------------------------------------
    def _export_result(self) -> None:
        if not self.result or self.result.is_empty:
            QMessageBox.warning(self, "提示", "当前没有可导出的结果。")
            return

        kind_label = {
            "summary": "SN摘要",
            "full": "SN完整数据",
            "both": "SN摘要和完整数据",
            "file_all": "SN所在文件全部数据",
            "process_all": "过程码文件全部数据",
            "duplicate": "重复SN检查",
        }.get(self.result.kind, "查询结果")

        default_name = sanitize_filename(
            f"物流查找_{kind_label}_{datetime.now():%Y%m%d_%H%M%S}.xlsx"
        )

        default_export_dir = self.settings.value("last_export_dir", "", type=str)
        if not default_export_dir or not Path(default_export_dir).is_dir():
            docs_dir = Path.home() / "Documents"
            default_export_dir = str(docs_dir) if docs_dir.is_dir() else str(Path.home())

        data_folder = self.folder_edit.text().strip()
        # 使用不触网的 is_path_inside，避免网络盘检查阻塞 UI
        if data_folder and is_path_inside(Path(default_export_dir), Path(data_folder)):
            docs_dir = Path.home() / "Documents"
            default_export_dir = str(docs_dir) if docs_dir.is_dir() else str(Path.home())

        default_path = str(Path(default_export_dir) / default_name)

        while True:
            path, _ = QFileDialog.getSaveFileName(
                self, "导出 Excel", default_path, "Excel 文件 (*.xlsx)",
            )
            if not path:
                return
            if not path.lower().endswith(".xlsx"):
                path += ".xlsx"

            if data_folder and is_path_inside(Path(path), Path(data_folder)):
                QMessageBox.warning(
                    self, "已阻止保存到数据文件夹",
                    "为保护“待出库”原始出货数据，导出文件不能保存到数据文件夹"
                    "或其子目录。\n\n请重新选择其他保存位置（例如“文档”或桌面）。",
                )
                self._append_log(f"已阻止导出到数据文件夹：{path}（原始数据只读保护）")
                default_path = str(Path(default_export_dir) / default_name)
                continue
            break

        try:
            self.settings.setValue("last_export_dir", str(Path(path).parent))
        except Exception:
            pass

        self._set_busy(True, allow_cancel=False)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.statusBar().showMessage("正在导出 Excel...")

        self.worker = ExportWorker(self.result, Path(path))
        self.worker.progress.connect(self._on_progress)
        self.worker.finished_ok.connect(self._on_export_finished)
        self.worker.failed.connect(self._on_worker_failed)
        self.worker.start()

        self._append_log(f"开始导出：{path}")

    def _on_export_finished(self, path: str) -> None:
        self._set_busy(False)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(100)
        self.worker = None

        self._append_log(f"导出完成：{path}")
        self.statusBar().showMessage("导出完成")
        QMessageBox.information(self, "导出完成", f"已导出到：\n{path}")

    # ------------------------------------------------------------------
    # 失败处理与关闭
    # ------------------------------------------------------------------
    def _on_worker_failed(self, message: str) -> None:
        self._set_busy(False)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.worker = None

        self._append_log(f"操作失败：{message}")
        self.statusBar().showMessage("操作失败")
        QMessageBox.warning(self, "操作失败", message)

    def closeEvent(self, event) -> None:
        if self.worker and self.worker.isRunning():
            ret = QMessageBox.question(
                self, "退出",
                "后台任务正在运行，是否取消任务并退出？",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if ret != QMessageBox.Yes:
                event.ignore()
                return

            if hasattr(self.worker, "cancel"):
                self.worker.cancel()
            self.worker.wait(5000)

        self._save_settings()
        event.accept()