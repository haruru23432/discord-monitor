"""Closure and official backend-release regressions observed November 2026."""
import unittest
from unittest.mock import patch
import monitor as m
import monitor_dbd_source as dbd
import test_dbd_bugfix as bugfix
from test_monitor import state


class ForumRetirementTests(unittest.TestCase):
    def test_closed_forum_not_requested_but_official_feed_still_collected(self):
        row=bugfix.DbdBugfixTests().release('10.2.2b')
        source=dict(id='dbd-official',topic='dbd',kind='steam',official=True)
        with patch.object(m.sources,'SOURCES',[source]), patch.object(m.sources,'fetch_source',return_value=[row]) as steam, patch.object(m,'fetch_news',return_value=[]), patch.object(dbd,'fetch_bhvr',side_effect=AssertionError('closed forum requested')):
            rows, failures, unavailable=m.collect(['dbd'],m.stamp('2026-11-22'))
        steam.assert_called_once()
        self.assertEqual(rows,[row])
        self.assertEqual(failures,{})
        self.assertEqual(unavailable,[])

    def test_remaining_source_errors_are_not_hidden(self):
        source=dict(id='dbd-official',topic='dbd',kind='steam',official=True)
        with patch.object(m.sources,'SOURCES',[source]), patch.object(m.sources,'fetch_source',side_effect=ValueError('malformed')), patch.object(m,'fetch_news',side_effect=ValueError('malformed')):
            rows, failures, unavailable=m.collect(['dbd'],m.stamp('2026-11-22'))
        self.assertEqual(set(failures),{'dbd-official','dbd-website'})
        self.assertEqual(unavailable,['dbd'])

    def test_suffixed_versions_do_not_collide_with_confirmed_base_patch(self):
        factory=bugfix.DbdBugfixTests()
        base=factory.release('10.2.2'); s=state();s['sent']=sorted(m.scoped_keys(base))
        rows=[factory.release(v) for v in ('10.2.2a','10.2.2b')]
        for row in rows:
            self.assertEqual(m.classify(row),'公式パッチ更新')
        planned=m.plan(s,rows,m.stamp('2026-10-15'))
        self.assertEqual(len(planned['queue']),2)
        self.assertFalse(m.scoped_keys(rows[0]) & m.scoped_keys(rows[1]))

    def test_ptb_to_live_preview_remains_separate_from_patch(self):
        row=bugfix.DbdBugfixTests().release('10.3.0')
        row['title']='PTB-to-Live Update | The Terrifier'
        self.assertEqual(m.classify(row),'開発チームの重要発表・今後の予定')
        self.assertFalse(any('patch-version:' in k for k in m.keys(row)))


if __name__=='__main__':
    unittest.main()
