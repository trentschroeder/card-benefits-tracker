"""Nightly SQLite backup with rotation, run by the card-benefits-backup timer.

Uses SQLite's online backup API, which produces a consistent copy even while
the app is writing (a plain cp can capture a torn file). Writes a timestamped
file into BACKUP_DIR and keeps the most recent KEEP copies.

    CB_BACKUP_DIR   where to write backups (default /var/backups/credit-card-benefits)
    CB_BACKUP_KEEP  how many to retain (default 14)
"""
import glob
import os
import sqlite3
import sys
from datetime import datetime, timezone

BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
DATABASE   = os.path.join(BASE_DIR, 'benefits.db')
BACKUP_DIR = os.environ.get('CB_BACKUP_DIR', '/var/backups/credit-card-benefits')
KEEP       = int(os.environ.get('CB_BACKUP_KEEP', '14'))


def main():
    if not os.path.exists(DATABASE):
        print(f'No database at {DATABASE}; nothing to back up.')
        return 1
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    dest  = os.path.join(BACKUP_DIR, f'benefits-{stamp}.db')

    src = sqlite3.connect(DATABASE)
    try:
        dst = sqlite3.connect(dest)
        try:
            with dst:
                src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()

    # Rotate: keep only the most recent KEEP backups.
    backups = sorted(glob.glob(os.path.join(BACKUP_DIR, 'benefits-*.db')))
    removed = 0
    for old in backups[:-KEEP] if KEEP > 0 else []:
        try:
            os.remove(old)
            removed += 1
        except OSError:
            pass
    print(f'Backup OK -> {dest} ({os.path.getsize(dest)} bytes); '
          f'kept {min(len(backups), KEEP)}, pruned {removed}.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
