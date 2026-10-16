"""Daily local recovery snapshots; still require encrypted offsite backups.

Backups contain sensitive customer data; never publish or serve this directory.
"""
import os
import sqlite3
import time
import zipfile
from pathlib import Path

from .db import _lock


def snapshot(db, keep=14):
    root = Path(db.path).parent
    folder = root / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    name = folder / f"wanai-{stamp}-{time.time_ns() % 1000000:06d}.zip"
    temp = folder / ("db-snapshot-" + str(time.time_ns()) + ".sqlite3")
    try:
        with _lock:
            with sqlite3.connect(temp) as dst:
                db._conn.backup(dst)
        with zipfile.ZipFile(name, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.write(temp, arcname="wanai.db")
            artifacts = root / "artifacts"
            if artifacts.is_dir():
                for path in artifacts.rglob("*"):
                    if path.is_file() and path.is_relative_to(artifacts):
                        archive.write(path, arcname=str(Path("artifacts") / path.relative_to(artifacts)))
        try:
            os.chmod(name, 0o600)
        except OSError:
            pass
    finally:
        temp.unlink(missing_ok=True)
    saved = sorted(folder.glob("wanai-*.zip"))
    for old in saved[:-max(1, min(keep, 365))]:
        old.unlink(missing_ok=True)
    return name
