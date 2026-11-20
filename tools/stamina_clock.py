"""Present the stamina recovery deadline on the client's JST clock."""

from msgpack import Timestamp

from helpers import user_data
from helpers.msgpack import DATETIME_MIN
from models.keys import KEYS
from models.unions import IDATA_OBJECT_KEY


_USER = IDATA_OBJECT_KEY['User']
_FULL_AT = next(key for key, attr, *_ in KEYS['User'] if attr == 'max_stamina_restored_at')
_JST_SECONDS = 9 * 3600

database_present = user_data.build_present
_database_object = user_data.data_object


def _client_object(item):
    if item is None or item[0] != _USER:
        return item
    values = item[1]
    if len(values) <= _FULL_AT:
        return item
    full_at = values[_FULL_AT]
    if not isinstance(full_at, Timestamp) or full_at == DATETIME_MIN:
        return item
    # StaminaHelper subtracts TimeAdjuster.Now (JST wall time) directly from
    # MaxStaminaRestoredAt. Convert here, keeping the database and exports in UTC.
    values = list(values)
    values[_FULL_AT] = Timestamp(full_at.seconds + _JST_SECONDS, full_at.nanoseconds)
    return [_USER, values]


def for_client(items):
    return [_client_object(item) for item in items]


def data_object(type_name, row_model):
    return _client_object(_database_object(type_name, row_model))


async def build_present(app, user_id, *updated):
    return for_client(await database_present(app, user_id, *updated))


def install():
    """Install before importing routes that bind the vendor presentation helpers."""
    user_data.data_object = data_object
    user_data.build_present = build_present
