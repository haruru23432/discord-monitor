"""Failure-path regressions found by the independent operational audit."""
import unittest
import os
from unittest.mock import patch, Mock
import monitor as m
from test_monitor import state, item, NOW


class ResilienceTests(unittest.TestCase):
    def setUp(self):
        # Fault-injection summaries must never reach the live Actions summary.
        environment = patch.dict(os.environ, {'GITHUB_STEP_SUMMARY': '', 'TEST_NOTIFICATION': 'false'})
        environment.start()
        self.addCleanup(environment.stop)

    def test_partial_article_failure_preserves_other_articles_and_reports_error(self):
        index = '<a href="https://overwatch.blizzard.com/en-us/news/2/new">new</a><a href="https://overwatch.blizzard.com/en-us/news/3/broken">broken</a>'
        good = dict(item(), language='ja')
        def article(url, *args):
            if '/3/' in url:
                raise TimeoutError()
            return good, None
        with patch.object(m.sources, 'request', return_value=index), patch.object(m, 'article_page', side_effect=article):
            rows = m.fetch_news('ow', NOW)
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows.errors)

    def test_collect_retains_good_rows_when_source_partly_fails(self):
        rows = m.sources.FetchRows([item()])
        rows.errors['https://example.com/broken'] = 'TimeoutError'
        definition = dict(id='discord-official', topic='discord', kind='rss', official=True)
        with patch.object(m.sources, 'SOURCES', [definition]), patch.object(m.sources, 'fetch_source', return_value=rows):
            found, failures, unavailable = m.collect(['discord'], NOW)
        self.assertEqual(len(found), 1)
        self.assertIn('PartialFetch', failures['discord-official'])

    def test_partial_source_failure_cannot_report_successful_run(self):
        store = Mock()
        store.load.return_value = dict(state(), checked={}, source_failures={})
        with patch.object(m.sys, 'argv', ['monitor.py', 'apex']), patch.object(m, 'Store', return_value=store), patch.object(m, 'collect', return_value=([], {'apex-website': 'TimeoutError'}, [])):
            self.assertTrue(m.main())

    def test_related_article_is_not_used_as_translation(self):
        original = item(topic='ow', title='New Support Hero Doctrine', body='More: https://overwatch.blizzard.com/en-us/news/7/skins', url='https://steamcommunity.com/games/2357570/announcements/detail/456')
        unrelated = dict(original, title='Season 5 skins', language='en')
        with patch.object(m, 'article_page', return_value=(unrelated, None)) as fetch:
            self.assertEqual(m.localize(original), original)
        self.assertEqual(fetch.call_count, 1)

    def test_missing_mention_receipt_is_operational_failure_without_resend(self):
        s = dict(state(), checked={}, source_failures={})
        s['receipts'] = [dict(id='confirmed-message', mention_confirmed=False)]
        store = Mock(); store.load.return_value = s
        with patch.object(m.sys, 'argv', ['monitor.py', 'apex']), patch.object(m, 'Store', return_value=store), patch.object(m, 'collect', return_value=([], {}, [])), patch.object(m, 'deliver') as send:
            self.assertTrue(m.main())
            send.assert_not_called()

    def test_verified_original_headline_allows_translation(self):
        original = item(topic='ow', title='New Support Hero Doctrine', body='https://overwatch.blizzard.com/en-us/news/7/hero', url='https://steamcommunity.com/games/2357570/announcements/detail/456')
        ja = dict(original, title='新ヒーロー', language='ja')
        with patch.object(m, 'article_page', side_effect=[(original, None), (ja, None)]):
            self.assertEqual(m.localize(original)['language'], 'ja')


if __name__ == '__main__':
    unittest.main()
