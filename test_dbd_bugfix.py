"""Official Bugfix releases must survive policy filtering and mirror deduplication."""
import unittest
import monitor as m
from test_monitor import item, state


class DbdBugfixTests(unittest.TestCase):
    def release(self, version, source='dbd-official', ptb=False):
        result = item('dbd', ('[PTB] ' if ptb else '') + version + ' | Bugfix Patch',
                    url='https://example.com/' + source + '/' + version + ('/ptb' if ptb else '/live'),
                    body='Killer Perk Updates. Fixed crashes.',
                    date=m.stamp('2026-10-14'))
        result['source'] = source
        return result

    def test_official_bugfix_releases(self):
        for version in ('10.2.1', '10.2.2', '10.2.3'):
            for source in ('dbd-official', 'dbd-patch'):
                self.assertEqual(m.classify(self.release(version, source)), '公式パッチ更新')

    def test_no_version_mentions_or_third_party(self):
        a=self.release('10.2.1')
        for title in ('Developer Update | 10.2.1', 'How to play in 10.2.1', '10.2.1 merchandise'):
            a['title']=title
            self.assertNotEqual(m.classify(a), '公式パッチ更新')
        a=self.release('10.2.1', 'dbd-pcgamer'); a['official']=False
        self.assertIsNone(m.classify(a))

    def test_recovery_mirrors_and_sent_receipts(self):
        rows=[self.release(v, s) for v in ('10.2.1', '10.2.2')
              for s in ('dbd-official', 'dbd-patch')]
        now=m.stamp('2026-10-22')
        self.assertFalse(m.plan(state(), rows, now)['queue'])
        recovered=m.plan(state(), rows, now, days=14)
        self.assertEqual(len(recovered['queue']), 2)
        recovered['sent']=sorted(set().union(*(m.scoped_keys(a) for a in recovered['queue'].values())))
        recovered['queue']={}
        self.assertFalse(m.plan(recovered, rows, now, days=14)['queue'])

    def test_ptb_separate_from_live(self):
        rows=[self.release('10.2.2', ptb=p) for p in (False, True)]
        self.assertEqual(m.classify(rows[1]), '公式パッチ更新（PTB）')
        self.assertEqual(len(m.plan(state(), rows, m.stamp('2026-10-15'))['queue']), 2)


if __name__ == '__main__':
    unittest.main()
