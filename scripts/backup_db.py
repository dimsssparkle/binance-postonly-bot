"""Бэкап bot.db как сжатый SQL-дамп (sqlite3.iterdump) + ротация старых копий.

Осознанно ИСКЛЮЧАЕТ book_snapshots из бэкапа: это фоновые данные для
будущих depth-стратегий, растут ~1.2GB/год и не несут финансового риска
при потере (в отличие от intents/intent_orders/events_log, где живут
деньги и история сделок) — бэкапить их целиком каждый день раздувало бы
хранилище бэкапов кратно размеру основной базы без реальной пользы.

Запуск на сервере: python3 scripts/backup_db.py
Расписание — через systemd timer (см. deploy/binance-bot-backup.timer).
"""
import gzip
import os
import sqlite3
import sys
import time

DB_PATH = os.environ.get("DB_PATH", "bot.db")
BACKUP_DIR = os.environ.get("BACKUP_DIR", "backups")
KEEP_LAST = int(os.environ.get("BACKUP_KEEP_LAST", "14"))
EXCLUDE_TABLES = {"book_snapshots"}


def _is_excluded_line(line: str) -> bool:
    return any(f'INTO "{t}"' in line or f"INTO {t} " in line for t in EXCLUDE_TABLES)


def main() -> None:
    if not os.path.exists(DB_PATH):
        print(f"DB not found at {DB_PATH}, nothing to back up", file=sys.stderr)
        return

    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    dst_path = os.path.join(BACKUP_DIR, f"bot_{stamp}.sql.gz")

    conn = sqlite3.connect(DB_PATH)
    with gzip.open(dst_path, "wt") as f:
        for line in conn.iterdump():
            if _is_excluded_line(line):
                continue
            f.write(line + "\n")
    conn.close()
    size_kb = os.path.getsize(dst_path) / 1024
    print(f"backed up {DB_PATH} -> {dst_path} ({size_kb:.1f} KB, excluding {', '.join(EXCLUDE_TABLES)})")

    # ротация: учитываем и новый формат (.sql.gz), и старые полные .db-бэкапы
    # (до этого изменения) — так они естественно вытесняются со временем.
    backups = sorted(
        f for f in os.listdir(BACKUP_DIR)
        if f.startswith("bot_") and (f.endswith(".sql.gz") or f.endswith(".db"))
    )
    stale = backups[:-KEEP_LAST] if len(backups) > KEEP_LAST else []
    for f in stale:
        os.remove(os.path.join(BACKUP_DIR, f))
    if stale:
        print(f"removed {len(stale)} old backup(s), keeping last {KEEP_LAST}")


if __name__ == "__main__":
    main()
