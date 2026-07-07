"""Консистентный бэкап bot.db через SQLite Online Backup API (безопасно даже
с активным WAL) + ротация старых копий.

Запуск на сервере: python3 scripts/backup_db.py
Расписание — через systemd timer (см. deploy/binance-bot-backup.timer).
"""
import os
import sqlite3
import sys
import time

DB_PATH = os.environ.get("DB_PATH", "bot.db")
BACKUP_DIR = os.environ.get("BACKUP_DIR", "backups")
KEEP_LAST = int(os.environ.get("BACKUP_KEEP_LAST", "14"))


def main() -> None:
    if not os.path.exists(DB_PATH):
        print(f"DB not found at {DB_PATH}, nothing to back up", file=sys.stderr)
        return

    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    dst_path = os.path.join(BACKUP_DIR, f"bot_{stamp}.db")

    src = sqlite3.connect(DB_PATH)
    dst = sqlite3.connect(dst_path)
    with dst:
        src.backup(dst)
    dst.close()
    src.close()
    print(f"backed up {DB_PATH} -> {dst_path}")

    backups = sorted(
        (f for f in os.listdir(BACKUP_DIR) if f.startswith("bot_") and f.endswith(".db")),
    )
    stale = backups[:-KEEP_LAST] if len(backups) > KEEP_LAST else []
    for f in stale:
        os.remove(os.path.join(BACKUP_DIR, f))
    if stale:
        print(f"removed {len(stale)} old backup(s), keeping last {KEEP_LAST}")


if __name__ == "__main__":
    main()
