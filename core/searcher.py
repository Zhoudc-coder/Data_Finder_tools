# -*- coding: utf-8 -*-
"""
查找逻辑：
- 按 SN 查摘要
- 按 SN 查完整数据
- 按 SN 同时查摘要和完整数据
- 按 SN 查所在文件全部数据
- 按过程码查文件全部数据
- 重复 SN 检查
"""
from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .utils import (
    CancelledError,
    format_mtime,
    read_all_csv_rows,
    read_csv_rows_by_indices,
)


@dataclass
class SearchResult:
    kind: str
    summary_headers: list[str] = field(default_factory=list)
    summary_rows: list[dict] = field(default_factory=list)
    full_headers: list[str] = field(default_factory=list)
    full_rows: list[dict] = field(default_factory=list)
    preview_headers: list[str] = field(default_factory=list)
    preview_rows: list[list] = field(default_factory=list)
    logs: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        if self.kind == "both":
            return not self.summary_rows and not self.full_rows
        if self.kind == "summary":
            return not self.summary_rows
        return not self.full_rows


class Searcher:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def _query_sn(self, sn: str) -> list[sqlite3.Row]:
        """SN 精确匹配，区分大小写。"""
        conn = self._connect()
        try:
            return conn.execute(
                """
                SELECT sn, file_path, row_index, process_code, file_mtime
                FROM sn_index
                WHERE sn = ?
                ORDER BY file_path, row_index
                """,
                (sn,),
            ).fetchall()
        finally:
            conn.close()

    def _query_process_code(self, process_code: str) -> list[sqlite3.Row]:
        conn = self._connect()
        try:
            return conn.execute(
                """
                SELECT path, process_code, mtime
                FROM files
                WHERE process_code = ?
                ORDER BY path
                """,
                (process_code,),
            ).fetchall()
        finally:
            conn.close()

    @staticmethod
    def _check_cancel(cancel_event: Optional[threading.Event]) -> None:
        if cancel_event and cancel_event.is_set():
            raise CancelledError("操作已取消")

    @staticmethod
    def _set_preview(result: SearchResult, max_rows: int = 200, max_cols: int = 30) -> None:
        """设置 UI 预览数据，避免完整大表直接塞进表格。"""
        if result.kind == "both":
            headers = result.summary_headers
            rows = result.summary_rows
            if not rows:
                headers = result.full_headers
                rows = result.full_rows
        elif result.kind == "summary":
            headers = result.summary_headers
            rows = result.summary_rows
        else:
            headers = result.full_headers
            rows = result.full_rows

        result.preview_headers = headers[:max_cols] if headers else []
        result.preview_rows = []
        for row_dict in rows[:max_rows]:
            result.preview_rows.append([
                str(row_dict.get(h, "")) for h in result.preview_headers
            ])

    def search_sn_summary(
        self,
        sns: list[str],
        progress_cb: Optional[Callable[[int, str], None]] = None,
        log_cb: Optional[Callable[[str], None]] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> SearchResult:
        """功能 1：按 SN 查找摘要信息。"""
        result = SearchResult(
            kind="summary",
            summary_headers=["查找序号", "SN", "过程码", "上传时间"],
        )

        total = len(sns)
        for i, sn in enumerate(sns, start=1):
            self._check_cancel(cancel_event)

            rows = self._query_sn(sn)
            if not rows:
                result.logs.append(f"未找到 SN：{sn}")

            for row in rows:
                result.summary_rows.append({
                    "查找序号": i,
                    "SN": sn,
                    "过程码": row["process_code"],
                    "上传时间": format_mtime(row["file_mtime"]),
                })

            if progress_cb:
                progress_cb(int(i / max(1, total) * 100), f"已查找 {i}/{total} 个 SN")

        self._set_preview(result)
        return result

    def search_sn_full(
        self,
        sns: list[str],
        progress_cb: Optional[Callable[[int, str], None]] = None,
        log_cb: Optional[Callable[[str], None]] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> SearchResult:
        """功能 2：按 SN 查找完整数据。"""
        result = SearchResult(kind="full")

        # 先查索引，得到所有目标位置
        refs: list[dict] = []
        refs_by_file: dict[str, list[dict]] = {}

        for i, sn in enumerate(sns, start=1):
            self._check_cancel(cancel_event)

            rows = self._query_sn(sn)
            if not rows:
                result.logs.append(f"未找到 SN：{sn}")

            for row in rows:
                ref = {
                    "查找序号": i,
                    "SN": sn,
                    "file_path": row["file_path"],
                    "row_index": int(row["row_index"]),
                    "process_code": row["process_code"],
                    "file_mtime": row["file_mtime"],
                }
                refs.append(ref)
                refs_by_file.setdefault(ref["file_path"], []).append(ref)

        # 按文件分组读取，避免同一文件反复打开
        file_data: dict[str, tuple[list[str], dict[int, list[str]]]] = {}
        all_headers: list[str] = []
        header_set: set[str] = set()

        total_files = len(refs_by_file)
        for j, (file_path, file_refs) in enumerate(refs_by_file.items(), start=1):
            self._check_cancel(cancel_event)

            if progress_cb:
                percent = int((j - 1) / max(1, total_files) * 80)
                progress_cb(percent, f"读取文件 {j}/{total_files}：{Path(file_path).name}")

            indices = {r["row_index"] for r in file_refs}
            header, row_map = read_csv_rows_by_indices(
                Path(file_path),
                indices,
                log_cb=result.logs.append,
            )

            if not header:
                result.logs.append(f"无法读取文件或文件为空：{file_path}")
                continue

            file_data[file_path] = (header, row_map)
            for h in header:
                if h not in header_set:
                    header_set.add(h)
                    all_headers.append(h)

        # 按查找序号、文件路径、行号输出
        refs.sort(key=lambda x: (x["查找序号"], x["file_path"], x["row_index"]))

        for ref in refs:
            self._check_cancel(cancel_event)

            data = file_data.get(ref["file_path"])
            if not data:
                continue

            header, row_map = data
            row = row_map.get(ref["row_index"])
            if row is None:
                result.logs.append(
                    f"行号不存在：{ref['file_path']}，数据行号 {ref['row_index']}"
                )
                continue

            row_dict: dict = {"查找序号": ref["查找序号"]}
            for col_idx, h in enumerate(header):
                row_dict[h] = row[col_idx] if col_idx < len(row) else ""
            result.full_rows.append(row_dict)

        # 不同文件标题可能不完全一致，按并集补齐
        for row_dict in result.full_rows:
            for h in all_headers:
                if h not in row_dict:
                    row_dict[h] = ""

        result.full_headers = ["查找序号"] + all_headers
        self._set_preview(result)

        if progress_cb:
            progress_cb(100, "完整数据查找完成")
        return result

    def search_sn_both(
        self,
        sns: list[str],
        progress_cb: Optional[Callable[[int, str], None]] = None,
        log_cb: Optional[Callable[[str], None]] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> SearchResult:
        """功能 3：按 SN 同时查找摘要 + 完整数据。"""
        summary_cb = None
        full_cb = None
        if progress_cb:
            summary_cb = lambda p, m: progress_cb(int(p * 0.4), m)
            full_cb = lambda p, m: progress_cb(40 + int(p * 0.6), m)

        summary = self.search_sn_summary(
            sns,
            progress_cb=summary_cb,
            log_cb=log_cb,
            cancel_event=cancel_event,
        )
        full = self.search_sn_full(
            sns,
            progress_cb=full_cb,
            log_cb=log_cb,
            cancel_event=cancel_event,
        )

        result = SearchResult(
            kind="both",
            summary_headers=summary.summary_headers,
            summary_rows=summary.summary_rows,
            full_headers=full.full_headers,
            full_rows=full.full_rows,
            logs=summary.logs + full.logs,
        )
        self._set_preview(result)
        return result

    def search_sn_files_all(
        self,
        sns: list[str],
        progress_cb: Optional[Callable[[int, str], None]] = None,
        log_cb: Optional[Callable[[str], None]] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> SearchResult:
        """
        功能 4：按 SN 查找其所在出货文件的全部产品数据。
        多个 SN 指向同一文件时，该文件只导出一次，辅助列列出相关查找序号/SN。
        """
        result = SearchResult(kind="file_all")

        file_map: dict[str, dict] = {}
        file_order: list[str] = []

        for i, sn in enumerate(sns, start=1):
            self._check_cancel(cancel_event)

            rows = self._query_sn(sn)
            if not rows:
                result.logs.append(f"未找到 SN：{sn}")

            for row in rows:
                file_path = row["file_path"]
                if file_path not in file_map:
                    file_map[file_path] = {
                        "indices": [],
                        "sns": [],
                        "process_code": row["process_code"],
                        "file_mtime": row["file_mtime"],
                    }
                    file_order.append(file_path)

                info = file_map[file_path]
                if i not in info["indices"]:
                    info["indices"].append(i)
                if sn not in info["sns"]:
                    info["sns"].append(sn)

        all_headers: list[str] = []
        header_set: set[str] = set()

        total_files = len(file_order)
        for j, file_path in enumerate(file_order, start=1):
            self._check_cancel(cancel_event)

            if progress_cb:
                percent = int((j - 1) / max(1, total_files) * 90)
                progress_cb(percent, f"读取文件 {j}/{total_files}：{Path(file_path).name}")

            info = file_map[file_path]
            header, rows = read_all_csv_rows(Path(file_path), log_cb=result.logs.append)

            if not header:
                result.logs.append(f"无法读取文件或文件为空：{file_path}")
                continue

            for h in header:
                if h not in header_set:
                    header_set.add(h)
                    all_headers.append(h)

            index_str = ",".join(str(x) for x in info["indices"])
            sn_str = ",".join(info["sns"])

            for row in rows:
                row_dict = {
                    "查找序号": index_str,
                    "来源过程码": info["process_code"],
                    "来源SN": sn_str,
                }
                for col_idx, h in enumerate(header):
                    row_dict[h] = row[col_idx] if col_idx < len(row) else ""
                result.full_rows.append(row_dict)

        for row_dict in result.full_rows:
            for h in all_headers:
                if h not in row_dict:
                    row_dict[h] = ""

        result.full_headers = ["查找序号", "来源过程码", "来源SN"] + all_headers
        self._set_preview(result)

        if progress_cb:
            progress_cb(100, "文件全部数据查找完成")
        return result

    def search_process_all(
        self,
        process_codes: list[str],
        progress_cb: Optional[Callable[[int, str], None]] = None,
        log_cb: Optional[Callable[[str], None]] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> SearchResult:
        """
        功能 5：按过程码查找文件全部数据。
        同一过程码若有重复文件，全部返回；同一输入过程码查找序号相同。
        """
        result = SearchResult(kind="process_all")

        file_map: dict[str, dict] = {}
        file_order: list[str] = []

        for i, code in enumerate(process_codes, start=1):
            self._check_cancel(cancel_event)

            rows = self._query_process_code(code)
            if not rows:
                result.logs.append(f"未找到过程码：{code}")

            for row in rows:
                file_path = row["path"]
                if file_path not in file_map:
                    file_map[file_path] = {
                        "index": i,
                        "process_code": row["process_code"],
                    }
                    file_order.append(file_path)

        all_headers: list[str] = []
        header_set: set[str] = set()

        total_files = len(file_order)
        for j, file_path in enumerate(file_order, start=1):
            self._check_cancel(cancel_event)

            if progress_cb:
                percent = int((j - 1) / max(1, total_files) * 90)
                progress_cb(percent, f"读取文件 {j}/{total_files}：{Path(file_path).name}")

            info = file_map[file_path]
            header, rows = read_all_csv_rows(Path(file_path), log_cb=result.logs.append)

            if not header:
                result.logs.append(f"无法读取文件或文件为空：{file_path}")
                continue

            for h in header:
                if h not in header_set:
                    header_set.add(h)
                    all_headers.append(h)

            for row in rows:
                row_dict = {
                    "查找序号": info["index"],
                    "来源过程码": info["process_code"],
                }
                for col_idx, h in enumerate(header):
                    row_dict[h] = row[col_idx] if col_idx < len(row) else ""
                result.full_rows.append(row_dict)

        for row_dict in result.full_rows:
            for h in all_headers:
                if h not in row_dict:
                    row_dict[h] = ""

        result.full_headers = ["查找序号", "来源过程码"] + all_headers
        self._set_preview(result)

        if progress_cb:
            progress_cb(100, "过程码文件数据查找完成")
        return result

    def check_duplicate_sn(
        self,
        sns: list[str],
        progress_cb: Optional[Callable[[int, str], None]] = None,
        log_cb: Optional[Callable[[str], None]] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> SearchResult:
        """
        功能 6：重复 SN 检查。
        列出每个 SN 的所有出现位置及出现次数。
        """
        result = SearchResult(
            kind="duplicate",
            full_headers=[
                "查找序号",
                "SN",
                "过程码",
                "上传时间",
                "数据行号",
                "出现次数",
                "文件路径",
            ],
        )

        total = len(sns)
        for i, sn in enumerate(sns, start=1):
            self._check_cancel(cancel_event)

            rows = self._query_sn(sn)
            count = len(rows)

            if count == 0:
                result.logs.append(f"未找到 SN：{sn}")
            elif count > 1:
                result.logs.append(f"发现重复 SN：{sn}，共出现 {count} 次。")

            for row in rows:
                result.full_rows.append({
                    "查找序号": i,
                    "SN": sn,
                    "过程码": row["process_code"],
                    "上传时间": format_mtime(row["file_mtime"]),
                    "数据行号": int(row["row_index"]) + 1,  # 展示为不含标题行的 1-based 行号
                    "出现次数": count,
                    "文件路径": row["file_path"],
                })

            if progress_cb:
                progress_cb(int(i / max(1, total) * 100), f"已检查 {i}/{total} 个 SN")

        self._set_preview(result)
        return result