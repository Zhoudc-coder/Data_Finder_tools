# -*- coding: utf-8 -*-
"""
SQLite 索引构建器。
只保存 SN 与文件位置，不保存 1500 列完整数据。
支持全量重建和增量更新，适配网络共享盘（UNC/SMB）。

【增量更新原理】
1. 用 os.scandir 递归遍历待出库文件夹下所有 .csv 文件（网络盘友好，边遍历边回调进度）；
2. 读取 files 表中已有的 (path, mtime, size)；
3. 对每个当前文件：
   - 新文件（索引中不存在）→ 需要扫描；
   - mtime 或 size 变化 → 需要重新扫描；
   - 未变化 → 跳过，不打开 CSV；
4. 对需要扫描的文件：先删除旧索引，再读取 SN 列并写入新索引；
5. 对索引中存在但磁盘上已不存在的文件：删除其索引记录；
6. stat 失败的文件保留旧索引（避免误删，防止漏查）。

【只读约定】
本模块对原始 CSV 只调用 read_sn_column（内部使用 "r" 只读模式），
不写入、不修改、不删除原始数据文件。
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Callable, Optional

from .utils import (
    is_network_path,
    normalize_path_str,
    read_sn_column,
    scan_csv_files,
)


class Indexer:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        # 某些 SMB 不支持 WAL 共享内存，失败时回退到 DELETE 模式
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError:
            try:
                conn.execute("PRAGMA journal_mode=DELETE")
            except sqlite3.DatabaseError:
                pass
        return conn

    def _init_db(self) -> None:
        conn = self._connect()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS files (
                    path TEXT PRIMARY KEY,
                    process_code TEXT NOT NULL,
                    mtime REAL NOT NULL,
                    size INTEGER NOT NULL,
                    row_count INTEGER DEFAULT 0,
                    error TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sn_index (
                    sn TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    row_index INTEGER NOT NULL,
                    process_code TEXT NOT NULL,
                    file_mtime REAL NOT NULL,
                    file_size INTEGER NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_sn_index_sn ON sn_index(sn)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_sn_index_file ON sn_index(file_path)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_files_process ON files(process_code)")
            conn.commit()
        finally:
            conn.close()

    def _migrate_paths_if_needed(self, conn: sqlite3.Connection, log_cb=None) -> None:
        """
        将旧版本使用 Path.resolve() 存储的路径迁移到新的 normalize_path_str() 格式，
        避免新旧格式不一致导致增量时误判为“已删除 + 新增”而全量重扫。
        """
        try:
            rows = conn.execute("SELECT path FROM files").fetchall()
        except sqlite3.DatabaseError:
            return

        migrated = 0
        for (p,) in rows:
            np = normalize_path_str(p)
            if np == p:
                continue
            try:
                conn.execute("UPDATE files SET path=? WHERE path=?", (np, p))
            except sqlite3.IntegrityError:
                # 规范化后与已有记录冲突：删除旧记录，保留新记录
                conn.execute("DELETE FROM files WHERE path=?", (p,))
                conn.execute("DELETE FROM sn_index WHERE file_path=?", (p,))
                migrated += 1
                continue
            conn.execute("UPDATE sn_index SET file_path=? WHERE file_path=?", (np, p))
            migrated += 1

        if migrated:
            conn.commit()
            if log_cb:
                log_cb(f"已迁移 {migrated} 条旧版本索引路径到新格式。")

    def count_files(self) -> int:
        """索引中的文件数量。"""
        conn = self._connect()
        try:
            row = conn.execute("SELECT COUNT(*) FROM files").fetchone()
            return int(row[0]) if row else 0
        except sqlite3.OperationalError:
            return 0
        finally:
            conn.close()

    def build_index(
        self,
        folder: Path,
        incremental: bool = True,
        progress_cb: Optional[Callable[[int, str], None]] = None,
        log_cb: Optional[Callable[[str], None]] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> dict:
        """
        构建索引。
        incremental=True：根据路径、mtime、size 增量更新。
        incremental=False：清空后全量重建。
        progress_cb(value, message)：value < 0 表示不确定进度（UI 显示忙碌动画）。
        """
        folder = Path(folder)

        if progress_cb:
            progress_cb(-1, "正在检查数据文件夹...")
        if not folder.exists() or not folder.is_dir():
            raise FileNotFoundError(f"数据文件夹不存在或无法访问：{folder}")

        if is_network_path(folder) and log_cb:
            log_cb(f"检测到网络路径：{folder}，访问速度可能较慢，请耐心等待。")

        # 阶段 1：目录遍历（网络盘上无总数，使用不确定进度）
        if progress_cb:
            progress_cb(-1, "正在遍历目录（网络路径可能较慢）...")

        def _scan_progress(dirs_scanned: int, found: int, msg: str) -> None:
            if progress_cb:
                progress_cb(-1, msg)

        csv_files = scan_csv_files(
            folder,
            progress_cb=_scan_progress,
            log_cb=log_cb,
            cancel_event=cancel_event,
        )

        stats = {
            "total_files": len(csv_files),
            "scanned_files": 0,
            "skipped_files": 0,
            "removed_files": 0,
            "sn_count": 0,
            "cancelled": False,
        }

        if cancel_event is not None and cancel_event.is_set():
            stats["cancelled"] = True
            return stats

        conn = self._connect()
        try:
            if not incremental:
                conn.execute("DELETE FROM sn_index")
                conn.execute("DELETE FROM files")
                conn.commit()
            else:
                self._migrate_paths_if_needed(conn, log_cb)

            existing: dict[str, tuple[float, int]] = {}
            if incremental:
                for path, mtime, size in conn.execute("SELECT path, mtime, size FROM files"):
                    existing[str(path)] = (float(mtime), int(size))

            # 阶段 2：检查文件变化（10%–20%）
            current_paths: set[str] = set()
            to_scan: list[tuple[Path, str, float, int, str]] = []
            total_files = len(csv_files)

            for i, path in enumerate(csv_files, start=1):
                if cancel_event is not None and cancel_event.is_set():
                    stats["cancelled"] = True
                    return stats

                path_str = normalize_path_str(path)
                current_paths.add(path_str)

                try:
                    stat = path.stat()
                except Exception as e:
                    if log_cb:
                        log_cb(f"无法读取文件属性，保留旧索引：{path}，原因：{e}")
                    continue

                old = existing.get(path_str)
                changed = (
                    old is None
                    or abs(old[0] - stat.st_mtime) > 1e-6
                    or old[1] != stat.st_size
                )
                if (not incremental) or changed:
                    to_scan.append((
                        path,
                        path_str,
                        stat.st_mtime,
                        stat.st_size,
                        path.stem,
                    ))

                if progress_cb and (i % 50 == 0 or i == total_files):
                    progress_cb(
                        10 + int(i / max(1, total_files) * 10),
                        f"检查文件变化 {i}/{total_files}",
                    )

            # 清理磁盘上已不存在的文件索引
            removed_paths = [p for p in existing.keys() if p not in current_paths]
            for path_str in removed_paths:
                conn.execute("DELETE FROM sn_index WHERE file_path = ?", (path_str,))
                conn.execute("DELETE FROM files WHERE path = ?", (path_str,))
            conn.commit()
            stats["removed_files"] = len(removed_paths)

            # 阶段 3：扫描变化文件（20%–100%）
            total_to_scan = len(to_scan)
            for i, (path, path_str, mtime, size, process_code) in enumerate(to_scan, start=1):
                if cancel_event is not None and cancel_event.is_set():
                    stats["cancelled"] = True
                    return stats

                if progress_cb:
                    percent = 20 + int((i - 1) / max(1, total_to_scan) * 80)
                    progress_cb(percent, f"扫描 {i}/{total_to_scan}：{path.name}")

                # 删除该文件旧索引
                conn.execute("DELETE FROM sn_index WHERE file_path = ?", (path_str,))
                conn.execute("DELETE FROM files WHERE path = ?", (path_str,))

                sn_rows = read_sn_column(path, log_cb=log_cb)

                conn.execute(
                    """
                    INSERT OR REPLACE INTO files
                    (path, process_code, mtime, size, row_count, error)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (path_str, process_code, mtime, size, len(sn_rows), None),
                )

                if sn_rows:
                    conn.executemany(
                        """
                        INSERT INTO sn_index
                        (sn, file_path, row_index, process_code, file_mtime, file_size)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        [
                            (sn, path_str, row_index, process_code, mtime, size)
                            for row_index, sn in sn_rows
                        ],
                    )

                conn.commit()
                stats["scanned_files"] += 1
                stats["sn_count"] += len(sn_rows)

                if log_cb and (i % 50 == 0 or i == total_to_scan):
                    log_cb(f"索引进度：{i}/{total_to_scan}，当前 SN 记录数：{stats['sn_count']}")

            stats["skipped_files"] = max(0, len(csv_files) - len(to_scan))

            if progress_cb:
                progress_cb(100, "索引完成")

            return stats

        finally:
            conn.close()