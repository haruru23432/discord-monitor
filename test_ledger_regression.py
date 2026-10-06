import unittest

import monitor as m
from test_monitor import item, state

NOW = 1791302204


class LedgerRegressionTests(unittest.TestCase):
    def dbd(self, title, gid='new', **kw):
        return item('dbd', title, gid=gid, url='https://example.com/' + gid,
                    date=NOW - 100, **kw)

    def test_developer_update_is_not_a_patch_version_alias(self):
        for title in ('Dev Update | 10.2.0 Perks Update',
                      'Developer Update | 10.2.0 PTB to Live Changes'):
            self.assertFalse(any(k.startswith('patch-version:') for k in m.keys(self.dbd(title))))

    def test_ptb_live_and_developer_update_are_distinct(self):
        rows = [self.dbd('10.2.0 | PTB Patch Notes', 'ptb'),
                self.dbd('10.2.0 | Mid-Chapter', 'live'),
                self.dbd('Developer Update | 10.2.0 PTB to Live Changes', 'dev')]
        planned = m.plan(state(), rows, NOW)
        self.assertEqual(len(planned['queue']), 3)

    def test_same_patch_japanese_english_deduplicate_without_fake_receipt(self):
        en = self.dbd('10.2.0 | Mid-Chapter', 'en')
        ja = self.dbd('10.2.0 パッチノート', 'ja')
        s = state()
        s['sent'] = sorted(m.scoped_keys(en))
        s['repairs'] = {'dbd_version_alias_20261007': {}}
        planned = m.plan(s, [ja], NOW)
        self.assertFalse(planned['queue'])
        self.assertNotIn('dbd:gid:ja', planned['sent'])

    def test_proven_repair_preserves_receipts_and_runs_once(self):
        s = state()
        bad = 'dbd:gid:1845383656394283'
        real = 'dbd:gid:1845383656396647'
        s['sent'] = [bad, real, 'dbd:patch-version:PTB:10.2.0', 'dbd:gid:1843481262701761']
        s['receipts'] = [{'id': 'actual', 'keys': [real]}]
        m.repair_dbd_aliases(s)
        self.assertNotIn(bad, s['sent'])
        self.assertIn(real, s['sent'])
        self.assertIn('dbd:patch-version:PTB:10.2.0', s['sent'])
        self.assertIn('dbd:gid:1843481262701761', s['sent'])
        s['sent'].append(bad)
        m.repair_dbd_aliases(s)
        self.assertIn(bad, s['sent'])


if __name__ == '__main__':
    unittest.main()
