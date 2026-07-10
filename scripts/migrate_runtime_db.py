#!/usr/bin/env python3
from __future__ import annotations

import argparse, hashlib, json, os, sqlite3, sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.runtime_guard import REQUIRED_SCHEMA_VERSION, STATE_ROOT
from app.storage import Storage

def metadata(path: Path) -> dict:
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
        objs=conn.execute("SELECT type,name,COALESCE(sql,'') FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name").fetchall()
        return {'bytes':path.stat().st_size,'integrity':conn.execute('PRAGMA integrity_check').fetchone()[0],
                'tables':sum(x[0]=='table' for x in objs),'schema_sha256':hashlib.sha256('\n'.join('|'.join(map(str,x)) for x in objs).encode()).hexdigest(),
                'versions':[x[0] for x in conn.execute('SELECT version FROM schema_migrations ORDER BY version')]}

def main() -> int:
    parser=argparse.ArgumentParser()
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--release-manifest', type=Path, required=True)
    parser.add_argument('--allow-production-migration', action='store_true')
    args=parser.parse_args()
    if os.getenv('TRADINGAGENT_ALLOW_PRODUCTION_DB_MIGRATION') != 'YES_I_AM_DEPLOYING' or not args.allow_production_migration:
        raise SystemExit('explicit production migration authorization is required')
    db=args.database.resolve(); manifest=json.loads(args.release_manifest.read_text())
    if not db.is_relative_to((STATE_ROOT/'database').resolve()): raise SystemExit('database must be under production state database/')
    if manifest.get('mode') != 'paper' or manifest.get('schema_version') != REQUIRED_SCHEMA_VERSION: raise SystemExit('release manifest is incompatible')
    if any(Path('/Users/elijahang/Projects/TradingAgent').resolve() in Path(x).resolve().parents for x in []): pass
    backup=STATE_ROOT/'backups'/f"explicit-pre-migration-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.sqlite3"
    with sqlite3.connect(f'file:{db}?mode=ro',uri=True) as src, sqlite3.connect(backup) as dst: src.backup(dst)
    os.chmod(backup,0o600)
    before=metadata(backup)
    if before['integrity'] != 'ok': raise SystemExit('backup integrity failed')
    if os.statvfs(db.parent).f_bavail * os.statvfs(db.parent).f_frsize < before['bytes'] * 2: raise SystemExit('insufficient free disk')
    Storage(db).apply_explicit_migrations(production_paper=True)
    after=metadata(db)
    if after['integrity'] != 'ok' or REQUIRED_SCHEMA_VERSION not in after['versions']: raise SystemExit('migration verification failed; keep services stopped and restore backup if needed')
    print(json.dumps({'backup':str(backup),'before':before,'after':after},sort_keys=True))
    return 0
if __name__ == '__main__': raise SystemExit(main())
