from __future__ import annotations

import hashlib
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class GeneratedVideo:
    ref: str
    sha256: str
    mime_type: str
    file_path: Path
    size: int
    task_id: str


@dataclass(frozen=True)
class GeneratedFrame:
    ref: str
    sha256: str
    mime_type: str
    file_path: Path
    size: int
    task_id: str


class GeneratedVideoStore:
    """Private persistent scope-bound storage for videos and returned last frames."""

    def __init__(self, root: Path):
        self.root = Path(root) / "video_assets"
        self.blobs = self.root / "blobs"
        self.root.mkdir(parents=True, exist_ok=True)
        self.blobs.mkdir(parents=True, exist_ok=True)
        self.database_path = self.root / "generated_videos.sqlite3"
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.database_path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS assets (
                scope TEXT NOT NULL,
                ref TEXT NOT NULL,
                kind TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                mime_type TEXT NOT NULL,
                relative_path TEXT NOT NULL,
                byte_size INTEGER NOT NULL,
                task_id TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                touched_at INTEGER NOT NULL,
                PRIMARY KEY (scope, ref)
            )
            """
        )
        self._db.commit()

    @staticmethod
    def _suffix(mime_type: str) -> str:
        return {
            "video/mp4": ".mp4",
            "image/png": ".png",
            "image/jpeg": ".jpg",
            "image/webp": ".webp",
        }.get(str(mime_type).lower(), ".bin")

    def _put(self, *, scope: str, data: bytes, mime_type: str, task_id: str, kind: str):
        if not data:
            raise ValueError("生成物字节为空")
        digest = hashlib.sha256(data).hexdigest()
        prefix = "genvideo" if kind == "video" else "genframe"
        ref = f"{prefix}:{digest[:16]}"
        relative = Path("blobs") / f"{digest}{self._suffix(mime_type)}"
        path = self.root / relative
        if not path.exists():
            temporary = path.with_name(path.name + f".{threading.get_ident()}.tmp")
            temporary.write_bytes(data)
            try:
                temporary.replace(path)
            except FileExistsError:
                temporary.unlink(missing_ok=True)
        now = int(time.time())
        with self._lock:
            self._db.execute(
                """
                INSERT INTO assets
                    (scope, ref, kind, sha256, mime_type, relative_path, byte_size, task_id, created_at, touched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(scope, ref) DO UPDATE SET
                    kind=excluded.kind,
                    mime_type=excluded.mime_type,
                    relative_path=excluded.relative_path,
                    byte_size=excluded.byte_size,
                    task_id=excluded.task_id,
                    touched_at=excluded.touched_at
                """,
                (scope, ref, kind, digest, mime_type, relative.as_posix(), len(data), str(task_id), now, now),
            )
            self._db.commit()
        cls = GeneratedVideo if kind == "video" else GeneratedFrame
        return cls(ref, digest, mime_type, path, len(data), str(task_id))

    def put_video(self, *, scope: str, data: bytes, task_id: str) -> GeneratedVideo:
        return self._put(scope=scope, data=data, mime_type="video/mp4", task_id=task_id, kind="video")

    def put_frame(self, *, scope: str, data: bytes, mime_type: str, task_id: str) -> GeneratedFrame:
        return self._put(scope=scope, data=data, mime_type=mime_type, task_id=task_id, kind="frame")

    def _resolve_many(self, scope: str, refs: list[str], kind: str):
        result = []
        now = int(time.time())
        cls = GeneratedVideo if kind == "video" else GeneratedFrame
        with self._lock:
            for ref in refs:
                row = self._db.execute(
                    "SELECT * FROM assets WHERE scope=? AND ref=? AND kind=?",
                    (scope, str(ref), kind),
                ).fetchone()
                if row is None:
                    result.append(None)
                    continue
                path = self.root / row["relative_path"]
                if not path.is_file():
                    result.append(None)
                    continue
                self._db.execute(
                    "UPDATE assets SET touched_at=? WHERE scope=? AND ref=?",
                    (now, scope, str(ref)),
                )
                result.append(cls(str(row["ref"]), str(row["sha256"]), str(row["mime_type"]), path, int(row["byte_size"]), str(row["task_id"])))
            self._db.commit()
        return result

    def resolve_videos(self, scope: str, refs: list[str]) -> list[GeneratedVideo | None]:
        return self._resolve_many(scope, refs, "video")

    def resolve_frames(self, scope: str, refs: list[str]) -> list[GeneratedFrame | None]:
        return self._resolve_many(scope, refs, "frame")

    def prune(self, *, ttl_days: int, max_bytes: int) -> dict:
        cutoff = int(time.time()) - max(int(ttl_days), 1) * 86400
        removed = []
        with self._lock:
            rows = self._db.execute("SELECT * FROM assets ORDER BY touched_at ASC").fetchall()
            keep = []
            total = 0
            for row in rows:
                if int(row["touched_at"]) < cutoff:
                    removed.append((row["scope"], row["ref"]))
                else:
                    keep.append(row)
                    total += int(row["byte_size"])
            while keep and total > max(int(max_bytes), 1):
                row = keep.pop(0)
                total -= int(row["byte_size"])
                removed.append((row["scope"], row["ref"]))
            if removed:
                self._db.executemany("DELETE FROM assets WHERE scope=? AND ref=?", removed)
                self._db.commit()
            live_paths = {str(row[0]) for row in self._db.execute("SELECT relative_path FROM assets")}
        removed_blobs = 0
        for path in self.blobs.iterdir():
            relative = path.relative_to(self.root).as_posix()
            if path.is_file() and relative not in live_paths:
                path.unlink(missing_ok=True)
                removed_blobs += 1
        return {"removed_records": len(removed), "removed_blobs": removed_blobs}

    def close(self):
        with self._lock:
            self._db.close()
