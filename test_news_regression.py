"""Regression evidence from official articles observed on 2026-10-07."""
import contextlib
import io
import unittest
from unittest.mock import patch
import monitor as m


class OfficialNewsRegression(unittest.TestCase):
    def item(self, title, body='', topic='ow'):
        return m.sources.article({'topic': topic, 'id': topic+'-website', 'official': True}, title,
                                 'https://example.com/news/test', body, m.stamp('2026-10-05'))

    def test_pc_anticheat_migration_ja_and_en(self):
        for text in ('PC版アンチチートシステムがEA Javelin Anticheatに切り替わります。',
                     'Apex Legends will be switching its PC anticheat system to EA Javelin Anticheat.'):
            self.assertEqual(m.classify(self.item('Javelin Anticheat', text, 'apex')), 'PC環境・アンチチートの重要変更')

    def test_role_qualified_hero_map_and_role_change(self):
        for text in ('Doctrine, our long-awaited new Support Hero.', 'The first new Escort map in years: Grímsvötn.',
                     'Sombra moves from Damage to Support.'):
            self.assertIsNotNone(m.classify(self.item('Developer news', text)))

    def test_matchmaking_results_without_implemented_change(self):
        self.assertIsNone(m.classify(self.item('マッチメイキングのテスト結果を分析',
            '現時点では、この1ティア制限を恒久的なものにはいたしません。詳細については実装が近づき次第共有いたします。', 'apex')))

    def test_cosmetics_with_season_branding(self):
        self.assertIsNone(m.classify(self.item('New Mythic Weapon Skin', 'Available in Season 5.')))
        self.assertIsNone(m.classify(self.item('NieR Collection', 'New outfits in this collaboration.', 'dbd')))
        self.assertIsNone(m.classify(self.item('NieRとDead by Daylightのコラボが実現', 'スキン・装飾アイテム 新たなコレクションが登場。', 'dbd')))
        self.assertIsNotNone(m.classify(self.item('Collection Event', 'A limited time mode arrives.', 'apex')))

    def test_english_discovery_survives_missing_japanese_article(self):
        ja=m.NEWS['ow']; en=ja.replace('/ja-jp/', '/en-us/')
        old=ja+'1/old'; new=en+'2/new'
        pages={ja: '<a href="'+old+'">old</a>', en:'<a href="'+new+'">new</a>'}
        def article(url, topic, fallback=None):
            if '/ja-jp/news/2/' in url: raise ValueError('404')
            item=self.item('New Support Hero' if url==new else 'Old news')
            item.update(url=url, language='en' if url==new else 'ja', date=m.stamp('2026-10-05' if url==new else '2026-08-01'))
            return item, None
        with patch.object(m.sources,'request',side_effect=pages.__getitem__), patch.object(m,'article_page',side_effect=article):
            rows=m.fetch_news('ow',m.stamp('2026-10-07'))
        self.assertEqual([x['url'] for x in rows],[new])

    def test_missing_date_is_observable_error(self):
        page='<a href="https://overwatch.blizzard.com/en-us/news/2/new">new</a>'
        item=self.item('News'); item.update(date=None, language='en')
        out=io.StringIO()
        with patch.object(m.sources,'request',return_value=page), patch.object(m,'article_page',return_value=(item,None)), contextlib.redirect_stdout(out):
            with self.assertRaisesRegex(ValueError,'unavailable'): m.fetch_news('ow',m.stamp('2026-10-07'))
        self.assertIn('publication date missing',out.getvalue())

    def test_localized_delivery_keeps_label_and_original_identity(self):
        en=self.item('New hero',topic='ow')
        en.update(url='https://overwatch.blizzard.com/en-us/news/2/new', label='new', language='en')
        ja=dict(en,title='新ヒーロー登場',language='ja',url=en['url'].replace('/en-us/','/ja-jp/'))
        with patch.object(m,'article_page',return_value=(ja,None)):
            localized=m.localize(en)
        self.assertEqual(localized['label'],'new')

    def test_verified_steam_season_mirror_is_not_resent(self):
        from test_monitor import state
        official=self.item('シーズン5 不死者の教義')
        official.update(url='https://overwatch.blizzard.com/ja-jp/news/24303008/5',gid='official')
        mirror=self.item('Season 5 A Grim Doctrine Now Live', 'New Support Hero Doctrine')
        mirror['gid']='1845383656397269'
        s=state();s['sent']=sorted(m.scoped_keys(official))
        self.assertFalse(m.plan(s,[mirror],m.stamp('2026-10-07'))['queue'])
        followup=dict(mirror,gid='another',title='Season 5 balance followup',body='Major gameplay overhaul')
        self.assertEqual(len(m.plan(s,[followup],m.stamp('2026-10-07'))['queue']),1)

    def test_summary_prefers_migration_over_disclaimer_or_url(self):
        a=self.item('アンチチート変更', 'https://example.com/news/update\n\n本告知は今後変更される可能性があります。2026年9月29日よりPC版アンチチートがEA Javelin Anticheatに切り替わります。', 'apex')
        summary=m.sources.brief_summary(a,'PC環境・アンチチートの重要変更')
        self.assertIn('9月29日',summary)
        self.assertNotIn('https://example.com',summary)

    def test_backlog_cosmetics_removed_but_uncertain_preserved(self):
        from test_monitor import state
        a=self.item('NieRとDead by Daylightのコラボが実現','スキン・装飾アイテム 新たなコレクションが登場。','dbd')
        s=state();s['queue']['q']=dict(a,label='コラボ')
        self.assertFalse(m.plan(s,[],m.stamp('2026-10-07'))['queue'])
        s['pending']['game-monitor_dbd']={'queue_id':'q','keys':list(m.scoped_keys(a))}
        self.assertIn('q',m.plan(s,[],m.stamp('2026-10-07'))['queue'])


if __name__ == '__main__':
    unittest.main()
