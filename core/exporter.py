# -*- coding: utf-8 -*-
"""
Excel 导出器。
统一使用 .xlsx，依赖 pandas + openpyxl。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Optional

import pandas as pd

from .searcher import SearchResult


class Exporter:
    def export(
        self,
        result: SearchResult,
        output_path: Path,
        progress_cb: Optional[Callable[[int, str], None]] = None,
    ) -> Path:
        """导出查询结果到 Excel。"""
        if result.is_empty:
            raise ValueError("查询结果为空，未生成导出文件。")

        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        if progress_cb:
            progress_cb(10, "准备导出数据...")

        if result.kind == "summary":
            df = pd.DataFrame(result.summary_rows, columns=result.summary_headers)
            df.to_excel(output_path, index=False, engine="openpyxl")

        elif result.kind == "both":
            summary_df = pd.DataFrame(result.summary_rows, columns=result.summary_headers)
            full_df = pd.DataFrame(result.full_rows, columns=result.full_headers)

            with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
                summary_df.to_excel(writer, sheet_name="摘要", index=False)
                full_df.to_excel(writer, sheet_name="完整数据", index=False)

        else:
            # 功能 2、4、5、6 以及完整数据型结果
            df = pd.DataFrame(result.full_rows, columns=result.full_headers)
            df.to_excel(output_path, index=False, engine="openpyxl")

        if progress_cb:
            progress_cb(100, "导出完成")

        logging.info("Excel 导出完成：%s", output_path)
        return output_path