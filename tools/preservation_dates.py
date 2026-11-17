"""Extend final-service content windows without changing the archived master DB."""
from functools import lru_cache
from pathlib import Path
import hashlib
import msgpack
from helpers.mastermemory import unpack,pack
from models.keys import KEYS
ROOT=Path(__file__).resolve().parents[1]
# Recovered master contains both clock representations at the service boundary.
BOUNDARIES={1790658000,1790690400}
END=4102444800  # 2100-01-01; game already uses this sentinel for permanent charts.

def extend_row(name,row,changes):
    if not isinstance(row,list):return
    for k,field,base,array,kind,*_ in KEYS.get(name,()):
        if k>=len(row):continue
        value=row[k]
        if base=='DateTime' and 'end' in field and isinstance(value,msgpack.Timestamp) and value.seconds in BOUNDARIES:
            row[k]=msgpack.Timestamp(END,value.nanoseconds);changes.append((name,row[0],field))
        elif kind=='model' and value is not None:
            for child in value if array else [value]:extend_row(base,child,changes)

# Official content the archive still ships complete even though its own window had already
# run out, so it is re-dated by name instead of by the service-boundary rule above:
#   EventMaster 21018 - the April-Fool ヤネウラ alternate-chart mode (event_type 23,
#   is_aside_live_button with aside_live_button_type 3, i.e. the side button on the live
#   screen). It closed 2026-04-08, while all 112 AnotherNotationMaster rows end in 2100 and
#   all 112 Notations/20xxx chart folders are here, so the entry row is the only thing
#   hiding them. The two earlier editions (21003, 21010) carry no button, and nothing else
#   in the master - banner, top menu, missions - refers to 21018, so this row is the entry.
REOPEN={'EventMaster':{21018}}

def reopen_row(name,row,changes):
    for k,field,base,array,kind,*_ in KEYS.get(name,()):
        if k>=len(row):continue
        if base=='DateTime' and 'end' in field and isinstance(row[k],msgpack.Timestamp):
            row[k]=msgpack.Timestamp(END,row[k].nanoseconds);changes.append((name,row[0],field))

@lru_cache(maxsize=1)
def preserved_master():
    tables=unpack((ROOT/'private/upstream/master-original.db').read_bytes());changes=[]
    for name,rows in tables.items():
        for row in rows:extend_row(name,row,changes)
    for name,ids in REOPEN.items():
        for row in tables.get(name,()):
            if row[0] in ids:reopen_row(name,row,changes)
    # The ladder windows that keep 演劇リーグ / トリプルキャスト running (see league_schedule).
    from league_schedule import extend_tables
    extend_tables(tables,changes)
    # The weekly course editions that keep ウィークリーコース running past 2026-09-21 (course_schedule).
    from course_schedule import extend_tables as extend_course_tables
    extend_course_tables(tables,changes)
    body=pack(tables)
    return body,hashlib.sha256(body).hexdigest()[:16],changes

def repair_cached_dates():
    from helpers.cache import cache
    from helpers.msgpack import iso_to_ts
    def walk(value):
        if isinstance(value,list):
            for x in value:walk(x)
        elif hasattr(type(value),'model_fields'):
            for field,x in list(value.__dict__.items()):
                if 'end' in field and isinstance(x,str):
                    try:
                        if iso_to_ts(x).seconds in BOUNDARIES:setattr(value,field,'2100-01-01T00:00:00Z')
                    except (ValueError,TypeError):pass
                elif isinstance(x,list) or hasattr(type(x),'model_fields'):walk(x)
    walk(cache)
