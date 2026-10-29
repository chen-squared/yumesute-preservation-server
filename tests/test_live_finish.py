import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
sys.path.insert(0, str(ROOT / 'vendor/server-of-dreams'))

from live_finish import (
    _non_perfect_star_count,
    _sp_rate_base,
    _sp_rate_bonus,
    _total_of_live_rates,
)
from models.enums import ClearLamps, TimingTypes


class SpRateMath(unittest.TestCase):
    def test_base_table(self):
        self.assertEqual(_sp_rate_base(1, 100.95), 60)
        self.assertEqual(_sp_rate_base(10, 100.95), 150)
        self.assertEqual(_sp_rate_base(10, 80.0), 14)
        self.assertEqual(_sp_rate_base(1, 79.99), 0)

    def test_bonus(self):
        self.assertEqual(_sp_rate_bonus(int(ClearLamps.AllPerfect), 0), 8)
        self.assertEqual(_sp_rate_bonus(int(ClearLamps.FullCombo), 30), 6)
        self.assertEqual(_sp_rate_bonus(int(ClearLamps.Clear), 100), 1)

    def test_loss_count(self):
        blocks = [
            SimpleNamespace(timing_type=TimingTypes.PERFECT_STAR),
            SimpleNamespace(timing_type=TimingTypes.PERFECT),
            SimpleNamespace(timing_type=TimingTypes.MISS),
        ]
        self.assertEqual(_non_perfect_star_count(blocks), 2)

    def test_total_of_live_rates_ignores_empty(self):
        self.assertEqual(_total_of_live_rates([1.5, None, 2.25, 0, 0.0]), 3.75)


if __name__ == '__main__':
    unittest.main()
