import unittest
from unittest.mock import patch
import monitor as m
from test_monitor import item, state, NOW


class MetricsTests(unittest.TestCase):
    def test_distinguishes_policy_sent_duplicates_and_dates(self):
        a = item(gid='a')
        sent = item(gid='sent', title='New map', url='https://overwatch.blizzard.com/news/789/map/')
        s = state()
        s['sent'] = list(m.scoped_keys(sent))
        minor = item(gid='minor', title='Small fix', body='Fixed a minor visual bug.')
        undated = item(gid='undated', date=None)
        old = item(gid='old', date=NOW-20*m.DAY)
        duplicate = dict(a, gid='another-language')
        metrics = {}
        planned = m.plan(s, [a, sent, minor, undated, old, duplicate], NOW, metrics=metrics)
        c = metrics['ow-official']
        self.assertEqual((c['important'], c['already_sent'], c['duplicate']), (3,1,1))
        self.assertEqual((c['missing_date'], c['outside_window'], c['unimportant']), (1,1,1))
        self.assertEqual(c['queued'], 1)
        self.assertEqual(len(planned['queue']), 1)

    def test_failed_source_is_not_zero_success(self):
        definitions = [dict(id='discord-official', topic='discord', official=True, kind='rss')]
        metrics = {}
        with patch.object(m.sources, 'SOURCES', definitions), patch.object(m.sources, 'fetch_source', side_effect=ValueError):
            rows, failures, unavailable = m.collect(['discord'], NOW, metrics)
        self.assertEqual(metrics['discord-official']['error'], 'ValueError')
        self.assertIn('discord-official', failures)
        self.assertEqual(unavailable, ['discord'])
