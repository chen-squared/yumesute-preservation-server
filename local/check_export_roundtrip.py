"""Dry-run the importer's parsing path on an export ZIP, without touching any database."""
import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'vendor/server-of-dreams'))
sys.path.insert(0, str(ROOT / 'tools'))
sys.path.insert(0, str(ROOT))

from server import bootstrap

bootstrap()

from exporter import verify_export
from helpers.user_data import _table
from models.unions import IDATA_OBJECT
from protocol import envelope
from import_account import row_dict
from db import user as queries

for archive in sorted(Path(sys.argv[1]).glob('*.zip')):
    manifest = verify_export(archive)
    with zipfile.ZipFile(archive) as z:
        raw = z.read('user-data.response.bin')
    original = envelope(raw)[1]
    names = []
    for item in original:
        if item is None:
            continue
        tag, values = item
        name = IDATA_OBJECT.get(tag)
        if name is None:
            raise SystemExit('UNSUPPORTED entity tag %s in %s' % (tag, archive.name))
        row = row_dict(name, values)
        if getattr(queries, 'upsert_' + _table(name), None) is None:
            raise SystemExit('NO UPSERT FUNCTION for %s in %s' % (name, archive.name))
        names.append(name)
    print('OK %s: %d records, %d distinct entity types, all rows parsed and all upsert targets exist'
          % (archive.name, len(names), len(set(names))))
    print('   user_id=%s entries=%s bridge=%s' % (manifest['user_id'], manifest['entries'], manifest['login_bridge_available']))
