"""The game consumes one completed result per ladder, not in-progress bookkeeping."""
from models.keys import KEYS
from models.unions import IDATA_OBJECT_KEY

_FIELDS = {
    IDATA_OBJECT_KEY[name]: {field: key for key, field, *_ in KEYS[name]}
    for name in ('LeagueHistory', 'TripleCastHistory')
}


def for_client(items):
    """Expose the latest graded result; retain every history in storage/exports.

    UserDataHelper.GetLeagueHistory/GetTripleCastHistory use FirstOrDefault, and
    the previous-result popup does not check the history's window dates. Sending
    a current, ungraded row makes that popup try to render ClassChangeType.None.
    """
    output, latest = [], {}
    for item in items:
        if item is None or item[0] not in _FIELDS:
            output.append(item)
            continue
        tag, values = item
        fields = _FIELDS[tag]
        if (len(values) <= max(fields['class_change_type'], fields['class_type'])
                or values[fields['class_change_type']] not in (1, 2, 3)
                or values[fields['class_type']] not in range(1, 8)):
            continue
        if tag not in latest or values[0] > latest[tag][1][0]:
            latest[tag] = item
    return output + list(latest.values())
