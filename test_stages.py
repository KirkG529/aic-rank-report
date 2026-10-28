import json
import tempfile
import unittest
from pathlib import Path
from report import build_model, load_feed
from notify_wecom import build_message


class StageTransitionTests(unittest.TestCase):
    def test_history_uses_only_selected_stage(self):
        row = dict(csbh='1', team='Team', rank=1, score=0.9, submit='', scored_at='')
        snaps = [
            dict(hour='2026-10-01 12:00', stages={'复赛': [dict(row, score=0.8)]}),
            dict(hour='2026-10-28 12:00', stages={'半决赛': [row]}),
        ]
        hours, teams, stats = build_model(snaps, '半决赛')
        self.assertEqual(hours, ['2026-10-28 12:00'])
        self.assertIsNone(teams[0]['score_delta'])
        self.assertEqual(teams[0]['series'], [[0, 1, 0.9]])
        self.assertEqual(stats['total'], 1)
        snaps.append(dict(hour='2026-10-28 13:00', stages={'半决赛': [dict(row, score=0.95)]}))
        hours, teams, stats = build_model(snaps, '半决赛')
        self.assertEqual(teams[0]['score_delta'], 0.05)
        self.assertIn('AIC 半决赛排名变化', build_message(hours, teams, stats, '', stage='半决赛'))

    def test_feed_filters_before_limit(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp) / 'data' / 'changes'
            folder.mkdir(parents=True)
            records = [dict(ts='2026-10-28 12:00', stage='半决赛'),
                       dict(ts='2026-10-28 13:00', stage='复赛')]
            (folder / '2026-10-28.jsonl').write_text('\n'.join(json.dumps(r) for r in records), encoding='utf-8')
            self.assertEqual(load_feed(temp, limit=1, stage='半决赛'), records[:1])

    def test_no_selected_stage_has_no_history(self):
        self.assertEqual(build_model([dict(hour='old', stages={'复赛': []})], '半决赛')[0], [])


if __name__ == '__main__':
    unittest.main()
