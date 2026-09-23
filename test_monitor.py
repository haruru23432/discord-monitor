import copy
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

import monitor as m

NOW = 1790160000
USER = '123456789012345678'


def item(topic='ow', title='New hero announced', url='https://overwatch.blizzard.com/en-us/news/12345/new-hero/', **kw):
    return dict(topic=topic, title=title, url=url, gid=kw.pop('gid', url),
                source=topic+'-official', official=True, body=kw.pop('body', 'A new hero is joining the game on September 24, 2026.'),
                date=kw.pop('date', NOW-100), **kw)


def state():
    return dict(version=2, sent=[], seen={}, queue={}, pending={}, receipts=[])


class PolicyTests(unittest.TestCase):
    def test_major_news_deep_in_body(self):
        for topic, text in [('ow','A new hero joins the game.'), ('dbd','A new killer joins the game.'), ('apex','A new legend joins the game.')]:
            with self.subTest(topic=topic):
                self.assertIsNotNone(m.classify(item(topic, 'Development notes', body='Details. '*600 + text)))

    def test_minor_patch_not_important(self):
        for topic in ('ow','dbd','apex'):
            self.assertIsNone(m.classify(item(topic,'Hotfix 10.2.1',body='Fixed a small visual bug.')))

    def test_regular_advertising_excluded(self):
        for title in ('Weekend sale', 'OWCS tournament recap', 'New hero merchandise', 'グッズのお知らせ'):
            self.assertIsNone(m.classify(item(title=title)))

    def test_important_developer_posts(self):
        for title in ("Director's Take", 'Developer Update', 'Roadmap', 'Designer Notes', 'ディレクターの視点'):
            self.assertIsNotNone(m.classify(item(title=title,body='Plans for the game.')))

    def test_japanese_character(self):
        self.assertIsNotNone(m.classify(item(title='新ヒーロー「テスト」登場',body='')))

    def test_seen_unsent_recovered(self):
        s=state(); a=item(); s['seen']['ow:'+m.sources.digest(a['gid'])]=NOW-100
        p=m.plan(s,[a],NOW)
        self.assertEqual(len(p['queue']),1)
        self.assertTrue(next(iter(p['queue'].values()))['recovered'])
        self.assertFalse(p['sent'])

    def test_sent_no_repeat(self):
        s=state(); a=item(); s['sent']=list(m.scoped_keys(a))
        self.assertFalse(m.plan(s,[a],NOW)['queue'])

    def test_ja_en_once_japanese_first(self):
        en=item(); ja=item(title='新ヒーロー登場',url=en['url'].replace('/en-us/','/ja-jp/'),language='ja')
        p=m.plan(state(),[en,ja],NOW)
        self.assertEqual(len(p['queue']),1)
        self.assertIn('/ja-jp/',next(iter(p['queue'].values()))['url'])

    def test_old_reference_does_not_suppress_new_article(self):
        old=item(); new=item(title='New map announced',url='https://overwatch.blizzard.com/ja-jp/news/54321/map/',body='A new map. Previous news '+old['url'])
        s=state(); s['sent']=list(m.scoped_keys(old))
        self.assertEqual(len(m.plan(s,[new],NOW)['queue']),1)

    def test_steam_headline_and_japanese_publisher_slug(self):
        en=item('apex','Apex Legends VS Street Fighter 6 Event',url='https://steamstore-a.akamaihd.net/news/externalpost/steam_community_announcements/123',gid='123')
        ja=item('apex','ストリートファイター6イベント開催',url='https://www.ea.com/ja/games/apex-legends/apex-legends/news/street-fighter-6-event',language='ja')
        s=state(); s['sent']=['apex:gid:123']
        self.assertFalse(m.plan(s,[en,ja],NOW)['queue'])
        self.assertEqual(len(m.plan(state(),[en,ja],NOW)['queue']),1)

    def test_event_followup_is_separate(self):
        old=item('apex','Apex Legends VS Street Fighter 6 Event')
        new=item('apex','Street Fighter 6 Event extended',url='https://www.ea.com/ja/games/apex-legends/apex-legends/news/street-fighter-6-event-extended',body='Major event dates changed.')
        s=state(); s['sent']=list(m.scoped_keys(old))
        self.assertEqual(len(m.plan(s,[new],NOW)['queue']),1)

    def test_seven_day_window_and_explicit_fourteen(self):
        a=item(date=NOW-10*m.DAY)
        self.assertFalse(m.plan(state(),[a],NOW)['queue'])
        self.assertEqual(len(m.plan(state(),[a],NOW,14)['queue']),1)

    def test_backlog_not_lost_when_old(self):
        s=m.plan(state(),[item()],NOW)
        self.assertEqual(len(m.plan(s,[],NOW+9*m.DAY)['queue']),1)

    def test_repeated_discovery_not_duplicate_queue(self):
        s=m.plan(state(),[item()],NOW)
        self.assertEqual(len(m.plan(s,[item()],NOW)['queue']),1)

    def test_game_routes_including_supplement(self):
        for t in ('ow','dbd','apex'):
            self.assertEqual(m.CHANNELS[t],'game-monitor_'+t)
            self.assertNotEqual(m.HOOKS[t],'DISCORD_WEBHOOK_IMPORTANT')

    def test_non_game_routes(self):
        for t in ('windows','discord','steam','medal'):
            self.assertEqual(m.CHANNELS[t],'info_monitor_main')

    def test_windows_feature_update_is_excluded(self):
        self.assertIsNone(m.classify(item('windows','Windows 11 feature update now available',body='New features.')))

    def test_windows_existing_coverage(self):
        for title in ('Windows 11 unable to boot', 'Windows 11 apps crash', 'Windows 11 update installation fails', 'Windows 11 update breaks audio'):
            self.assertIsNotNone(m.classify(item('windows',title)))

    def test_baseline_not_sent(self):
        games={t:dict(started=1,sent=['initial','actual'],seen=[],pending=None,_confirmed_sent=['actual']) for t in m.BASELINES}
        bases={t:dict(started=1,sent=['initial']) for t in m.BASELINES}
        result=m.migrate(dict(sent=['discord:actual'],pending=None),games,bases,dict(incidents=[]),NOW)
        self.assertIn('ow:actual',result['sent'])
        self.assertNotIn('ow:initial',result['sent'])

    def test_later_seen_to_sent_upgrade_is_not_a_receipt(self):
        history=[dict(sent=['baseline']),dict(sent=['baseline','fabricated']),
                 dict(sent=['baseline','fabricated'],pending=dict(keys=['actual'])),
                 dict(sent=['baseline','fabricated','actual'],pending=None,last_message_id='123')]
        self.assertEqual(m.confirmed_history(history),['actual'])

    def test_pending_migration_refuses(self):
        with self.assertRaises(ValueError):
            m.migrate(dict(pending={'at':1}),{}, {},dict(incidents=[]),NOW)

    def test_date_parsing(self):
        self.assertEqual(m.stamp('2026-09-23'),m.stamp('2026-09-23T00:00:00Z'))

    def test_source_failure_not_empty_success(self):
        with patch.object(m.sources,'fetch_source',side_effect=TimeoutError):
            rows,failures,unavailable=m.collect(['discord'],NOW)
        self.assertFalse(rows)
        self.assertIn('discord-official',failures)
        self.assertEqual(unavailable,['discord'])

    def test_transient_source_failure_retries_read_only(self):
        with patch.object(m.sources,'fetch_source',side_effect=[TimeoutError(), []]) as fetch, patch.object(m.time,'sleep'):
            _, failures, unavailable=m.collect(['discord'],NOW)
        self.assertEqual(fetch.call_count,2)
        self.assertFalse(failures)
        self.assertFalse(unavailable)

    def test_one_official_source_success_preserves_monitoring(self):
        def fetch(source, now):
            if source['official']:return [item('discord')]
            raise TimeoutError()
        definitions=[dict(id='discord-official',topic='discord',official=True,kind='rss'),
                     dict(id='discord-auxiliary',topic='discord',official=False,kind='rss')]
        with patch.object(m.sources,'SOURCES',definitions),patch.object(m.sources,'fetch_source',side_effect=fetch):
            rows,failures,unavailable=m.collect(['discord'],NOW)
        self.assertEqual(len(rows),1)
        self.assertIn('discord-auxiliary',failures)
        self.assertFalse(unavailable)

    def test_all_windows_primary_failed_is_failure(self):
        with patch.object(m.sources,'fetch_source',return_value=[]),patch.object(m.sources,'request',side_effect=TimeoutError):
            _,_,unavailable=m.collect(['windows'],NOW)
        self.assertIn('windows',unavailable)

    def test_translation_failure_keeps_english_official_url(self):
        en=item()
        with patch.object(m,'article_page',side_effect=ValueError):
            self.assertEqual(m.localize(en)['url'],en['url'])

    def test_late_japanese_patch_does_not_repeat_confirmed_steam_patch(self):
        en=item('apex','Marked Midseason Patch Notes',url='https://steamcommunity.com/games/1172470/announcements/detail/42',gid='42')
        ja=item('apex','マークド ミッドシーズン パッチノート',url='https://www.ea.com/ja/games/apex-legends/news/marked-patch',language='ja')
        s=state();s['sent']=['apex:gid:42']
        self.assertFalse(m.plan(s,[ja,en],NOW)['queue'])

    def test_windows_cross_source_incident_already_sent(self):
        news=item('windows','Windows 11 update breaks audio',url='https://example.com/news',body='KB1234567 audio issue')
        official=item('windows','USB audio devices fail to start',body='KB1234567 USB audio issue',active_issue=True,environment='Windows 11 24H2')
        s=state();s['sent']=list(m.scoped_keys(news))
        self.assertFalse(m.plan(s,[official],NOW)['queue'])

    def test_plan_does_not_mutate_original_history(self):
        s=state();before=copy.deepcopy(s)
        m.plan(s,[item()],NOW)
        self.assertEqual(s,before)

    def test_multiple_backlog_items_survive_send_limit(self):
        rows=[item(title='New hero '+str(n),gid=str(n),url='https://overwatch.blizzard.com/news/'+str(n)+'/') for n in range(9)]
        self.assertEqual(len(m.plan(state(),rows,NOW)['queue']),9)


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.s=m.plan(state(),[item()],NOW)
        self.identity,self.article=next(iter(self.s['queue'].items()))
        self.saved=[]
        self.translation=patch.object(m.sources,'japanese_summary',return_value='新ヒーローが発表されました。')
        self.translation.start()

    def tearDown(self):
        self.translation.stop()

    def save(self,s):
        self.saved.append(copy.deepcopy(s))

    def send(self,topic,payload):
        self.assertFalse(self.saved[-1]['sent'])
        self.assertTrue(self.saved[-1]['pending'])
        self.assertEqual(payload['allowed_mentions']['users'],[USER])
        return dict(id='receipt',mentions=[dict(id=USER)])

    def test_success_then_second_run(self):
        self.assertTrue(m.deliver(self.s,self.identity,self.article,self.save,self.send,USER,NOW))
        self.assertTrue(self.s['sent'])
        self.assertFalse(self.s['pending'])
        self.assertFalse(self.s['queue'])
        self.assertTrue(self.s['receipts'][0]['mention_confirmed'])
        self.assertFalse(m.plan(self.s,[item()],NOW)['queue'])

    def test_explicit_rejection_retry(self):
        for status in (400,403,429):
            def fail(*args):raise HTTPError('redacted',status,'',{},None)
            with self.assertRaises(HTTPError):m.deliver(self.s,self.identity,self.article,self.save,fail,USER,NOW)
            self.assertFalse(self.s['sent'])
            self.assertFalse(self.s['pending'])
            self.assertTrue(self.s['queue'])

    def test_unknown_delivery_keeps_pending(self):
        def fail(*args):raise TimeoutError()
        with self.assertRaises(TimeoutError):m.deliver(self.s,self.identity,self.article,self.save,fail,USER,NOW)
        self.assertFalse(self.s['sent'])
        self.assertTrue(self.s['pending'])
        self.assertFalse(m.deliver(self.s,self.identity,self.article,self.save,self.send,USER,NOW))

    def test_500_is_ambiguous(self):
        def fail(*args):raise HTTPError('redacted',500,'',{},None)
        with self.assertRaises(HTTPError):m.deliver(self.s,self.identity,self.article,self.save,fail,USER,NOW)
        self.assertTrue(self.s['pending'])

    def test_no_receipt_is_ambiguous(self):
        with self.assertRaises(ValueError):m.deliver(self.s,self.identity,self.article,self.save,lambda *a:{},USER,NOW)
        self.assertFalse(self.s['sent'])
        self.assertTrue(self.s['pending'])

    def test_translation_failure_no_reservation(self):
        with patch.object(m.sources,'japanese_summary',side_effect=ValueError):
            with self.assertRaises(ValueError):m.deliver(self.s,self.identity,self.article,self.save,self.send,USER,NOW)
        self.assertFalse(self.s['pending'])
        self.assertTrue(self.s['queue'])

    def test_persist_failure_prevents_post(self):
        calls=[]
        def fail(*args):raise RuntimeError()
        with self.assertRaises(RuntimeError):m.deliver(self.s,self.identity,self.article,fail,lambda *a:calls.append(a),USER,NOW)
        self.assertFalse(calls)

    def test_payload_useful_fields(self):
        p=m.payload(self.article,USER)
        for text in ('内容','影響','推奨対応','情報源','記事公開日'):
            self.assertIn(text,p['embeds'][0]['description'])
        self.assertIn('<@'+USER+'>',p['content'])


if __name__=='__main__':
    unittest.main()
