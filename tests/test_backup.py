from zipfile import ZipFile
from app.db import Database
from app.backup import snapshot

def test_backup_contains_database_and_retains_archive(tmp_path):
    db = Database(tmp_path / "test.db")
    db.execute("INSERT INTO settings(key,value) VALUES(?,?)", ("hello", "world"))
    backup = snapshot(db)
    with ZipFile(backup) as archive:
        assert "wanai.db" in archive.namelist()
        assert archive.read("wanai.db")[:16] == b"SQLite format 3\x00"
    assert backup.exists()
    db.close()
