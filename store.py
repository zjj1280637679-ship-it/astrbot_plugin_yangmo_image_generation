from __future__ import annotations

import hashlib
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class GeneratedImage:
    ref: str
    sha256: str
    mime_type: str
    file_path: Path
    size: int


class GeneratedImageStore:
    """Private, persistent, scope-bound generated-image store."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.blobs = self.root / "blobs"
        self.root.mkdir(parents=True, exist_ok=True)
        self.blobs.mkdir(parents=True, exist_ok=True)
        self.database_path = self.root / "generated_images.sqlite3"
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.database_path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS generated_images (
                scope TEXT NOT NULL,
                ref TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                mime_type TEXT NOT NULL,
                relative_path TEXT NOT NULL,
                byte_size INTEGER NOT NULL,
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
            "image/png": ".png",
            "image/jpeg": ".jpg",
            "image/webp": ".webp",
            "image/gif": ".gif",
        }.get(str(mime_type).lower(), ".bin")

    def put(self, *, scope: str, data: bytes, mime_type: str) -> GeneratedImage:
        if not data:
            raise ValueError("图片字节为空")
        digest = hashlib.sha256(data).hexdigest()
        ref = f"genimg:{digest[:16]}"
        relative = Path("blobs") / f"{digest}{self._suffix(mime_type)}"
        path = self.root / relative
        if not path.exists():
            temporary = path.with_suffix(path.suffix + ".tmp")
            temporary.write_bytes(data)
            temporary.replace(path)
        now = int(time.time())
        with self._lock:
            self._db.execute(
                """
                INSERT INTO generated_images
                    (scope, ref, sha256, mime_type, relative_path, byte_size, created_at, touched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(scope, ref) DO UPDATE SET
                    mime_type=excluded.mime_type,
                    relative_path=excluded.relative_path,
                    byte_size=excluded.byte_size,
                    touched_at=excluded.touched_at
                """,
                (scope, ref, digest, mime_type, relative.as_posix(), len(data), now, now),
            )
            self._db.commit()
        return GeneratedImage(ref, digest, mime_type, path, len(data))

    def resolve_many(self, scope: str, refs: list[str]) -> list[GeneratedImage | None]:
        result = []
        now = int(time.time())
        with self._lock:
            for ref in refs:
                row = self._db.execute(
                    "SELECT * FROM generated_images WHERE scope=? AND ref=?",
                    (scope, str(ref)),
                ).fetchone()
                if row is None:
                    result.append(None)
                    continue
                path = self.root / row["relative_path"]
                if not path.is_file():
                    result.append(None)
                    continue
                self._db.execute(
                    "UPDATE generated_images SET touched_at=? WHERE scope=? AND ref=?",
                    (now, scope, str(ref)),
                )
                result.append(
                    GeneratedImage(
                        str(row["ref"]),
                        str(row["sha256"]),
                        str(row["mime_type"]),
                        path,
                        int(row["byte_size"]),
                    )
                )
            self._db.commit()
        return result

    def prune(self, *, ttl_days: int, max_bytes: int) -> dict:
        cutoff = int(time.time()) - max(int(ttl_days), 1) * 86400
        removed_refs = []
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM generated_images ORDER BY touched_at ASC"
            ).fetchall()
            keep = []
            total = 0
            for row in rows:
                if int(row["touched_at"]) < cutoff:
                    removed_refs.append((row["scope"], row["ref"]))
                else:
                    keep.append(row)
                    total += int(row["byte_size"])
            while keep and total > max(int(max_bytes), 1):
                row = keep.pop(0)
                total -= int(row["byte_size"])
                removed_refs.append((row["scope"], row["ref"]))
            if removed_refs:
                self._db.executemany(
                    "DELETE FROM generated_images WHERE scope=? AND ref=?", removed_refs
                )
                self._db.commit()
            live_paths = {
                str(row[0])
                for row in self._db.execute("SELECT relative_path FROM generated_images")
            }
        removed_blobs = 0
        for path in self.blobs.iterdir():
            relative = path.relative_to(self.root).as_posix()
            if path.is_file() and relative not in live_paths:
                path.unlink(missing_ok=True)
                removed_blobs += 1
        return {"removed_records": len(removed_refs), "removed_blobs": removed_blobs}

    def close(self):
        with self._lock:
            self._db.close()

