import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'tools'), str(ROOT / 'vendor/server-of-dreams')]

from league_history import for_client
from helpers.msgpack import to_wire
from models import LeagueHistory, TripleCastHistory
from models.unions import IDATA_OBJECT_KEY


class LeagueHistoryWire(unittest.TestCase):
    def row(self, name, ident, change=0, paid=False):
        cls = LeagueHistory if name == 'LeagueHistory' else TripleCastHistory
        key = 'league_master_id' if name == 'LeagueHistory' else 'triple_cast_master_id'
        row = cls(**{key: ident}, class_type=2, is_sended_reward=paid,
                  is_played=True, class_change_type=change)
        return [IDATA_OBJECT_KEY[name], to_wire(row)]

    def test_current_ungraded_history_never_reaches_the_popup(self):
        source = [None, self.row('LeagueHistory', 172), self.row('TripleCastHistory', 3041),
                  [IDATA_OBJECT_KEY['User'], [7]]]
        self.assertEqual(for_client(source), [None, [IDATA_OBJECT_KEY['User'], [7]]])
        self.assertEqual(len(source), 4, 'Filtering must not alter the stored/exported snapshot')

    def test_latest_completed_result_wins_even_when_database_rows_are_unsorted(self):
        old = self.row('LeagueHistory', 165, 3, True)
        new = self.row('LeagueHistory', 171, 2, False)
        triple = self.row('TripleCastHistory', 3036, 3, True)
        for rows in ([old, new], [new, old]):
            self.assertEqual(for_client(rows + [self.row('LeagueHistory', 172), triple]), [new, triple])

    def test_all_completed_class_changes_remain_visible_before_and_after_payment(self):
        for name in ('LeagueHistory', 'TripleCastHistory'):
            for change in (1, 2, 3):
                for paid in (False, True):
                    row = self.row(name, 171, change, paid)
                    self.assertEqual(for_client([row]), [row])
