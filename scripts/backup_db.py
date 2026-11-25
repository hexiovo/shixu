from __future__ import annotations

import argparse
import sqlite3
import sys
from contextlib import closing
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.http.runtime import settings  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a consistent SQLite backup of the Shixu database")
    parser.add_argument(
        "destination", nargs="?", type=Path,
        help="New absolute path (default: a timestamped file in the data directory's backups folder)",
    )
    args = parser.parse_args()
    config = settings()
    source = config.database_path
    destination = args.destination
    if destination is None:
        timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S_%f")
        destination = config.backup_dir / f"shixu-{timestamp}.sqlite3"
    if not destination.is_absolute():
        parser.error("destination must be an absolute path")
    if not source.is_file():
        parser.error(f"Shixu database does not exist: {source}")
    if destination.exists():
        parser.error(f"backup destination already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_uri = source.resolve().as_uri() + "?mode=ro"
    with TemporaryDirectory(dir=destination.parent) as staging_dir:
        staged_backup = Path(staging_dir) / "backup.sqlite3"
        with closing(sqlite3.connect(source_uri, uri=True)) as source_db, closing(sqlite3.connect(staged_backup)) as backup_db:
            source_db.backup(backup_db)
        staged_backup.rename(destination)
    print(f"Backup created: {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
