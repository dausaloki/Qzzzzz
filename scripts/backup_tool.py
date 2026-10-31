#!/usr/bin/env python3
"""quiz_backup.json tool: export, verify and restore — all admin operations.

    python scripts/backup_tool.py status
    python scripts/backup_tool.py export  [--db /data/quiz.db] [--out /data/quiz_backup.json]
    python scripts/backup_tool.py verify  [--file /data/quiz_backup.json] [--db /data/quiz.db]
    python scripts/backup_tool.py restore [--file /data/quiz_backup.json] [--db /data/quiz.db] [--apply] [--migrate]

``restore`` is a **dry run** unless ``--apply`` is given.  With ``--apply`` the
current database is first copied to ``<db>.before-restore-<timestamp>`` and every
table is upserted on its natural key, so restoring the same file twice never
duplicates a quiz, a question, an attempt or a leaderboard row.

If the target database was created by an older bot version and the backup holds
data it has no place for (batches, authorized chats, batch/private quiz access,
quiz modes ...), ``--apply`` refuses instead of silently dropping that data.  Add
``--migrate`` to run the bot's own schema migration (``init_db``) on the target
first — after the safety copy has been taken.

The bot itself never writes to the database from a backup — it only exports.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import backup                                                    # noqa: E402
import config                                                    # noqa: E402
import database as db                                             # noqa: E402


def _use(db_file: str | None, out_file: str | None) -> None:
    if db_file:
        db.set_db_path(Path(db_file))
    if out_file:
        config.BACKUP_PATH = Path(out_file)


def cmd_status(args) -> int:
    _use(args.db, args.out)
    print(backup.status_text().replace("<b>", "").replace("</b>", "").replace("<code>", "")
          .replace("</code>", ""))
    ok, problems, info = backup.verify_file(compare_db=True)
    for p in problems:
        print("  problem:", p)
    for s in info.get("stale", []):
        print("  drift:", s)
    return 0 if ok else 1


def cmd_export(args) -> int:
    _use(args.db, args.out)
    res = backup.export_now(reason="cli", force=True)
    if not res.get("ok"):
        print("❌ export failed:", "; ".join(res.get("errors") or ["unknown error"]))
        return 1
    print(f"✅ wrote {res['path']} ({res['bytes']:,} bytes, attempts={res['attempts']})")
    counts = res.get("counts") or {}
    print("   records: " + ", ".join(f"{k}={counts.get(k, 0)}" for k in
                                     ("quizzes", "questions", "attempts", "answers",
                                      "group_sessions", "users")))
    return 0


def cmd_verify(args) -> int:
    _use(args.db, args.out)
    ok, problems, info = backup.verify_file(Path(args.file) if args.file else None, compare_db=not args.no_db)
    print(f"file: {info.get('path')}")
    print(f"bytes: {info.get('bytes', 0):,}  generated: {info.get('generated_at')}")
    print(f"counts: {info.get('counts')}")
    if ok:
        print("✅ JSON syntax, structure and references are valid")
    else:
        print("❌ problems:")
        for p in problems:
            print("   -", p)
    for s in info.get("stale", []):
        print("⚠️  drift vs database:", s)
    return 0 if ok else 1


def cmd_restore(args) -> int:
    _use(args.db, args.out)
    path = Path(args.file) if args.file else backup.backup_path()
    target = Path(args.db) if args.db else db.get_db_path()
    if not path.exists():
        print(f"❌ {path} does not exist")
        return 1
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        print(f"❌ {path} is not valid JSON: {exc}")
        return 1
    plan = backup.restore_payload(data, db_file=target, apply=False)
    if not plan.get("ok"):
        print("❌ backup is not valid, nothing was restored:")
        for p in plan.get("problems", [])[:20]:
            print("   -", p)
        return 1
    print(f"backup: {path}  ({data.get('generated_at')})")
    print(f"target: {target}")
    if plan.get("note"):
        print("note:", plan["note"])
    if plan.get("ignored_removed_feature"):
        print("note: ignored data of the removed payments/premium feature (not written, nothing "
              "deleted):", ", ".join(f"{k}={n}" for k, n in plan["ignored_removed_feature"].items()))
    for table in backup._RESTORE_ORDER:
        ins = plan["inserted"].get(table, 0)
        upd = plan["updated"].get(table, 0)
        if ins or upd:
            print(f"  {table:16s} new={ins:<6d} already-present={upd}")
    outdated = plan.get("schema_outdated") or []
    if not args.apply:
        hint = " --migrate" if outdated else ""
        print(f"\nDRY RUN — nothing was written.  Re-run with --apply{hint} to restore.")
        return 0
    if outdated and not args.migrate:
        print("❌ the target database schema is older than this backup; nothing was written.")
        print("   Re-run with --apply --migrate (a safety copy is taken before the migration).")
        return 1
    if not args.yes and sys.stdin.isatty():
        if input("Restore now? [y/N] ").strip().lower() not in ("y", "yes"):
            print("cancelled")
            return 1
    stamp = time.strftime("%Y%m%d-%H%M%S")
    if target.exists() and not args.no_backup:
        safety = Path(f"{target}.before-restore-{stamp}")
        try:
            con_src = target
            shutil.copy2(con_src, safety)                       # plain file copy
            for suffix in ("-wal", "-shm"):                     # include the WAL
                extra = Path(f"{con_src}{suffix}")
                if extra.exists():
                    shutil.copy2(extra, Path(f"{safety}{suffix}"))
            print(f"safety copy: {safety}")
        except OSError as exc:
            print(f"❌ could not create the safety copy ({exc}); restore aborted")
            return 1
    if args.migrate and target.exists():
        db.set_db_path(target)
        db.init_db()                                  # same migration the bot runs on start
        print("schema: upgraded with the bot's init_db migration")
    applied = backup.restore_payload(data, db_file=target, apply=True)
    if not applied.get("ok"):
        print("❌ restore failed:", "; ".join(applied.get("problems", [])[:3]))
        return 1
    new_total = sum(applied["inserted"].values())
    print("✅ restored: " + (", ".join(f"{t}+{n}" for t, n in applied["inserted"].items() if n)
                             or "nothing new"))
    print(f"   already present (updated in place): {sum(applied['updated'].values())} rows")
    if applied.get("skipped"):
        print("   skipped:", applied["skipped"])
    for c in applied.get("conflicts") or []:
        print("   ⚠️", c)
    if not new_total:
        print("   (the database already contained every record in this backup — no duplicates created)")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="quiz_backup.json export / verify / restore")
    ap.add_argument("--db", help="SQLite database (default: DATA_DIR/quiz.db)")
    ap.add_argument("--out", help="backup file to write (export) or read (default DATA_DIR/quiz_backup.json)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("status", help="show the backup state")
    p.add_argument("--db"); p.add_argument("--out"); p.set_defaults(func=cmd_status)

    p = sub.add_parser("export", help="write a fresh backup from the database")
    p.add_argument("--db"); p.add_argument("--out"); p.set_defaults(func=cmd_export)

    p = sub.add_parser("verify", help="validate the backup file (and compare with the database)")
    p.add_argument("--db"); p.add_argument("--out"); p.add_argument("--file")
    p.add_argument("--no-db", action="store_true", help="skip the database comparison")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("restore", help="restore a backup without duplicating records")
    p.add_argument("--db"); p.add_argument("--out"); p.add_argument("--file")
    p.add_argument("--apply", action="store_true", help="actually write (default: dry run)")
    p.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    p.add_argument("--no-backup", action="store_true", help="skip the safety copy of the database")
    p.add_argument("--migrate", action="store_true",
                   help="upgrade an older target schema (bot's init_db) before restoring")
    p.set_defaults(func=cmd_restore)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
