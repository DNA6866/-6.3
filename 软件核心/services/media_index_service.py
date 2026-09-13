import json
import os
import sqlite3
import threading
import time
from contextlib import closing
from typing import Dict, Iterable, List, Sequence, Tuple

from paths import config_dir


MEDIA_INDEX_VERSION = 1
DIRECTORY_FRESH_SECONDS = 90
_INDEX_LOCK = threading.RLock()
_DEFAULT_INDEX = None


def _normalized_path(path: str) -> str:
    return os.path.normcase(os.path.abspath(str(path or "")))


def _file_stamp(path: str) -> Tuple[int, int]:
    try:
        stat = os.stat(path)
        return int(stat.st_size), int(
            getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000))
        )
    except OSError:
        return 0, 0


def _directory_signature(root: str) -> str:
    """读取根目录及一级子目录时间戳，快速判断常见素材增删。"""
    parts = []
    try:
        root_stat = os.stat(root)
        parts.append(
            f".:{getattr(root_stat, 'st_mtime_ns', int(root_stat.st_mtime * 1_000_000_000))}"
        )
        with os.scandir(root) as entries:
            for entry in entries:
                if not entry.is_dir(follow_symlinks=False):
                    continue
                try:
                    stat = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                stamp = getattr(
                    stat,
                    "st_mtime_ns",
                    int(stat.st_mtime * 1_000_000_000),
                )
                parts.append(f"{entry.name}:{stamp}")
                if len(parts) >= 257:
                    break
    except OSError:
        return ""
    return "|".join(sorted(parts, key=str.casefold))


class MediaIndex:
    """跨启动复用目录列表和 FFprobe 元数据，数据库损坏时自动降级为空索引。"""

    def __init__(self, database_path: str = ""):
        self.database_path = database_path or os.path.join(
            config_dir(),
            "media_index.sqlite3",
        )
        self._ready = False

    def _connect(self):
        os.makedirs(os.path.dirname(self.database_path), exist_ok=True)
        connection = sqlite3.connect(self.database_path, timeout=3.0)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA busy_timeout=3000")
        if not self._ready:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS media_metadata (
                    path TEXT PRIMARY KEY,
                    size INTEGER NOT NULL,
                    mtime_ns INTEGER NOT NULL,
                    duration REAL,
                    width INTEGER,
                    height INTEGER,
                    codec_name TEXT,
                    pix_fmt TEXT,
                    color_space TEXT,
                    color_transfer TEXT,
                    color_primaries TEXT,
                    color_range TEXT,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS directory_cache (
                    cache_key TEXT PRIMARY KEY,
                    root TEXT NOT NULL,
                    extensions TEXT NOT NULL,
                    signature TEXT NOT NULL,
                    scanned_at REAL NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_media_updated
                ON media_metadata(updated_at);
                """
            )
            connection.execute(f"PRAGMA user_version={MEDIA_INDEX_VERSION}")
            connection.commit()
            self._ready = True
        return connection

    @staticmethod
    def _extension_key(allowed_exts: Sequence[str]) -> str:
        return "|".join(sorted({str(ext).lower() for ext in allowed_exts or []}))

    def _directory_key(self, root: str, allowed_exts: Sequence[str]) -> str:
        return f"{_normalized_path(root)}::{self._extension_key(allowed_exts)}"

    def cached_directory(
        self,
        root: str,
        allowed_exts: Sequence[str],
    ) -> Tuple[List[Tuple[str, str]], bool]:
        key = self._directory_key(root, allowed_exts)
        try:
            with _INDEX_LOCK, closing(self._connect()) as connection:
                row = connection.execute(
                    "SELECT signature, scanned_at, payload FROM directory_cache "
                    "WHERE cache_key=?",
                    (key,),
                ).fetchone()
            if not row:
                return [], False
            payload = json.loads(row[2] or "[]")
            files = [
                (str(item[0]), str(item[1]))
                for item in payload
                if isinstance(item, list) and len(item) == 2
            ]
            age = max(0.0, time.time() - float(row[1] or 0.0))
            current_signature = _directory_signature(root)
            fresh = bool(
                current_signature
                and current_signature == str(row[0] or "")
                and age <= DIRECTORY_FRESH_SECONDS
            )
            return files, fresh
        except (OSError, sqlite3.Error, TypeError, ValueError, json.JSONDecodeError):
            return [], False

    def store_directory(
        self,
        root: str,
        allowed_exts: Sequence[str],
        files: Iterable[Tuple[str, str]],
    ) -> None:
        key = self._directory_key(root, allowed_exts)
        normalized_files = [
            [str(display_name), os.path.abspath(str(full_path))]
            for display_name, full_path in files
        ]
        payload = json.dumps(normalized_files, ensure_ascii=False, separators=(",", ":"))
        try:
            with _INDEX_LOCK, closing(self._connect()) as connection:
                connection.execute(
                    "INSERT OR REPLACE INTO directory_cache "
                    "(cache_key, root, extensions, signature, scanned_at, payload) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        key,
                        _normalized_path(root),
                        self._extension_key(allowed_exts),
                        _directory_signature(root),
                        time.time(),
                        payload,
                    ),
                )
                connection.commit()
        except (OSError, sqlite3.Error, TypeError, ValueError):
            pass

    def metadata(self, path: str) -> Dict[str, object]:
        absolute = _normalized_path(path)
        size, mtime_ns = _file_stamp(absolute)
        if not absolute or size <= 0:
            return {}
        try:
            with _INDEX_LOCK, closing(self._connect()) as connection:
                row = connection.execute(
                    "SELECT size, mtime_ns, duration, width, height, codec_name, "
                    "pix_fmt, color_space, color_transfer, color_primaries, color_range "
                    "FROM media_metadata WHERE path=?",
                    (absolute,),
                ).fetchone()
            if not row or int(row[0]) != size or int(row[1]) != mtime_ns:
                return {}
            names = (
                "size",
                "mtime_ns",
                "duration",
                "width",
                "height",
                "codec_name",
                "pix_fmt",
                "color_space",
                "color_transfer",
                "color_primaries",
                "color_range",
            )
            return dict(zip(names, row))
        except (OSError, sqlite3.Error, TypeError, ValueError):
            return {}

    def update_metadata(self, path: str, **values) -> None:
        absolute = _normalized_path(path)
        size, mtime_ns = _file_stamp(absolute)
        if not absolute or size <= 0:
            return
        existing = self.metadata(absolute)
        fields = {
            "duration": values.get("duration", existing.get("duration")),
            "width": values.get("width", existing.get("width")),
            "height": values.get("height", existing.get("height")),
            "codec_name": values.get("codec_name", existing.get("codec_name")),
            "pix_fmt": values.get("pix_fmt", existing.get("pix_fmt")),
            "color_space": values.get("color_space", existing.get("color_space")),
            "color_transfer": values.get(
                "color_transfer",
                existing.get("color_transfer"),
            ),
            "color_primaries": values.get(
                "color_primaries",
                existing.get("color_primaries"),
            ),
            "color_range": values.get("color_range", existing.get("color_range")),
        }
        try:
            with _INDEX_LOCK, closing(self._connect()) as connection:
                connection.execute(
                    "INSERT OR REPLACE INTO media_metadata "
                    "(path, size, mtime_ns, duration, width, height, codec_name, "
                    "pix_fmt, color_space, color_transfer, color_primaries, color_range, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        absolute,
                        size,
                        mtime_ns,
                        fields["duration"],
                        fields["width"],
                        fields["height"],
                        fields["codec_name"],
                        fields["pix_fmt"],
                        fields["color_space"],
                        fields["color_transfer"],
                        fields["color_primaries"],
                        fields["color_range"],
                        time.time(),
                    ),
                )
                connection.commit()
        except (OSError, sqlite3.Error, TypeError, ValueError):
            pass


def get_media_index() -> MediaIndex:
    global _DEFAULT_INDEX
    with _INDEX_LOCK:
        if _DEFAULT_INDEX is None:
            _DEFAULT_INDEX = MediaIndex()
        return _DEFAULT_INDEX
