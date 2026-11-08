"""nexus.db backup and restore (NIGUN-134 module L section 133; also round 4's deploy
ask). Uses sqlite3's own backup API (Connection.backup), not a raw file copy -- a plain
copy of a live SQLite file can capture a half-written page; the backup API is safe to
run against an open connection.

CLI:
  python -m nexus_master.backup make  --db nexus.db --out backups/
  python -m nexus_master.backup restore --backup backups/nexus-<ts>.db --db nexus.db
"""
import argparse
import shutil
import sqlite3
import time
from pathlib import Path


def make_backup(db_path, out_dir):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / f"nexus-{int(time.time())}.db"
    src_conn = sqlite3.connect(db_path)
    dest_conn = sqlite3.connect(str(dest))
    try:
        src_conn.backup(dest_conn)
    finally:
        dest_conn.close()
        src_conn.close()
    return dest


def restore_backup(backup_path, db_path):
    """Restores db_path from backup_path. The existing db_path (if any) is moved aside
    with a .before-restore-<ts> suffix first -- restore must never destroy what it is
    replacing, even if the backup turns out to be bad."""
    backup_path, db_path = Path(backup_path), Path(db_path)
    if not backup_path.exists():
        raise FileNotFoundError(f"קובץ גיבוי לא קיים: {backup_path}")
    if db_path.exists():
        aside = db_path.with_name(f"{db_path.name}.before-restore-{int(time.time())}")
        shutil.move(str(db_path), str(aside))
    src_conn = sqlite3.connect(str(backup_path))
    dest_conn = sqlite3.connect(str(db_path))
    try:
        src_conn.backup(dest_conn)
    finally:
        dest_conn.close()
        src_conn.close()
    return db_path


def main(argv=None):
    p = argparse.ArgumentParser(prog="nexus_master.backup")
    p.add_argument("action", choices=["make", "restore"])
    p.add_argument("--db", default="nexus.db")
    p.add_argument("--out", default="backups")
    p.add_argument("--backup")
    a = p.parse_args(argv)
    if a.action == "make":
        dest = make_backup(a.db, a.out)
        print(f"גיבוי נוצר: {dest}")
    else:
        if not a.backup:
            p.error("--backup <path> is required for restore")
        restore_backup(a.backup, a.db)
        print(f"שוחזר ל-{a.db} מ-{a.backup}")


if __name__ == "__main__":
    main()
