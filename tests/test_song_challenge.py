import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, call

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'tools'), str(ROOT / 'vendor/server-of-dreams')]
from song_challenge import draw


class SongChallengeRandom(unittest.TestCase):
    def test_every_draw_uses_the_complete_pool_and_allows_immediate_repeats(self):
        rng = Mock()
        rng.choice.side_effect = [3, 3, 1]
        self.assertEqual([draw([1, 2, 3], rng) for _ in range(3)], [3, 3, 1])
        self.assertEqual(rng.choice.call_args_list, [call([1, 2, 3])] * 3)

    def test_duplicate_charts_do_not_give_a_song_extra_weight(self):
        rng = Mock()
        rng.choice.return_value = 2
        self.assertEqual(draw([1, 1, 2], rng), 2)
        rng.choice.assert_called_once_with([1, 2])

    def test_single_song_pool_remains_playable(self):
        self.assertEqual(draw([1]), 1)

    def test_empty_pool_fails_without_selecting_an_unplayable_song(self):
        with self.assertRaises(ValueError):
            draw([])
