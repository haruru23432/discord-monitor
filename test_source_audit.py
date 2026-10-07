"""Independent collection-failure regressions; no network or delivery side effects."""
import io
import json
import unittest
import urllib.error
from datetime import datetime, timezone
from unittest.mock import patch

import monitor_sources as s
import monitor_dbd_source as dbd
import monitor_ow_source as ow


class Response(io.BytesIO):
    def __init__(self, body, headers=None):
        super().__init__(body.encode())
        self.headers = headers or {}


class CollectionAudit(unittest.TestCase):
    now = int(datetime(2026, 10, 7, tzinfo=timezone.utc).timestamp())

    def source(self, name):
        return next(x for x in s.SOURCES if x['id'] == name)

    def steam(self, gid='1', date=None):
        return dict(gid=gid, title='New Legend', date=date or self.now-3600,
                    url='https://steamcommunity.com/announcements/detail/'+gid,
                    contents='A new legend arrives.', feedname='steam_community_announcements')

    def test_steam_empty_official_is_failure_but_optional_empty_is_valid(self):
        raw = json.dumps({'appnews': {'appid': 1172470, 'newsitems': []}})
        with patch.object(s, 'request', return_value=raw):
            with self.assertRaisesRegex(ValueError, 'Empty official'):
                s.fetch_source(self.source('apex-official'), self.now)
            optional = s.fetch_source(self.source('apex-pcgamer'), self.now)
        self.assertEqual(optional, [])
        self.assertEqual(optional.errors, {})

    def test_steam_bad_schema_is_not_empty_success(self):
        for rows in ({}, 'empty', None):
            with patch.object(s, 'request', return_value=json.dumps({'appnews': {'appid':1172470,'newsitems':rows}})):
                with self.assertRaisesRegex(ValueError, 'schema'):
                    s.fetch_source(self.source('apex-official'), self.now)

    def test_steam_bad_row_preserves_good_rows_and_error_is_sanitized(self):
        rows = [self.steam(), dict(self.steam('2'), date='secret value'), self.steam('3')]
        with patch.object(s, 'request', return_value=json.dumps({'appnews': {'appid':1172470,'newsitems':rows}})):
            got = s.fetch_source(self.source('apex-official'), self.now)
        self.assertEqual([x['gid'] for x in got], ['1','3'])
        self.assertEqual(got.errors, {'page-0-row-1':'ValueError'})

    def test_steam_next_page_failure_preserves_first_page(self):
        rows = [self.steam(str(n), self.now-3600-n) for n in range(100)]
        with patch.object(s, 'request', side_effect=[json.dumps({'appnews': {'appid':1172470,'newsitems':rows}}), TimeoutError('private detail')]):
            got = s.fetch_source(self.source('apex-official'), self.now)
        self.assertEqual(len(got), 100)
        self.assertEqual(got.errors, {'page-1':'TimeoutError'})

    def test_steam_other_feed_missing_cursor_date_preserves_selected_articles(self):
        for invalid_date in ('missing', 'invalid'):
            rows = [self.steam(str(n)) for n in range(99)]
            other = dict(self.steam('other'), feedname='pcgamer')
            if invalid_date == 'missing':
                del other['date']
            else:
                other['date'] = 'not a date'
            rows.append(other)
            with self.subTest(invalid_date=invalid_date), patch.object(s, 'request', return_value=json.dumps({'appnews': {'appid':1172470,'newsitems':rows}})) as request:
                got = s.fetch_source(self.source('apex-official'), self.now)
                self.assertEqual(len(got), 99)
                self.assertEqual(request.call_count, 1)
                self.assertEqual(got.errors, {'page-0-row-99': 'KeyError' if invalid_date == 'missing' else 'ValueError'})

    def test_medal_publication_date_and_body_from_observed_webflow_structure(self):
        markup = '<div class="post-info">Medal</div><div class="post-info">Jun 17, 2026</div><div class="post_rich-text w-richtext"><h1>Premium changes</h1><p>Longer uploads are now supported.</p></div>'
        date, body = s.medal_article(markup)
        self.assertEqual(date, int(datetime(2026,6,17,tzinfo=timezone.utc).timestamp()))
        self.assertIn('Longer uploads', body)
        with self.assertRaises(ValueError):
            s.medal_article('<div>Jun 17, 2026</div><div class="post_rich-text">Body</div>')

    def test_medal_regular_articles_not_silently_dateless_and_partial_survives(self):
        index = ''.join('<a href="/blog/posts/'+name+'"><h2>New recording limits</h2></a>' for name in ('one','one','two'))
        page = '<div class="post-info">Oct 7, 2026</div><div class="post_rich-text">New recording limits are available.</div>'
        with patch.object(s, 'request', side_effect=[index,page,TimeoutError('private detail')]) as request:
            got = s.fetch_source(self.source('medal-official'), self.now)
        self.assertEqual(request.call_count, 3)
        self.assertEqual(len(got),1)
        self.assertEqual(got[0]['date'],self.now)
        self.assertIn('recording limits',got[0]['body'])
        self.assertEqual(got.errors,{'https://medal.tv/blog/posts/two':'TimeoutError'})

    def test_bhvr_one_article_failure_does_not_discard_other_patch(self):
        rows = [dict(knowledgeBaseID=1,status='published',locale='en',articleID=n,name='10.3.0 | Mid-Chapter',dateInserted='2026-10-07T00:00:00+00:00',url='https://forums.bhvr.com/dead-by-daylight/kb/articles/'+str(n)) for n in (1,2)]
        with patch.object(dbd.urllib.request,'urlopen',side_effect=[Response(json.dumps(rows)),TimeoutError('private detail'),Response(json.dumps({'body':'A new chapter arrives.'}))]), patch.object(dbd.time,'time',return_value=self.now+60):
            got = dbd.fetch_bhvr(self.now-86400)
        self.assertEqual([r['gid'] for r in got],['bhvr:2'])
        self.assertEqual(got.errors,{rows[0]['url']:'TimeoutError'})

    def test_ow_archive_failure_retains_new_patch(self):
        markup = '<div class="PatchNotes-patch"><h3 class="PatchNotes-patchTitle" id="patch-2026-10-06">Patch notes</h3><p>New hero released.</p></div><a href="/en-us/news/patch-notes/live/2026/09/">Previous</a>'
        with patch.object(ow.urllib.request,'urlopen',side_effect=[Response(markup),TimeoutError('private detail')]), patch.object(ow.time,'time',return_value=self.now):
            got = ow.fetch_patches(self.now-14*86400)
        self.assertEqual(len(got),1)
        self.assertEqual(got.errors,{'archive':'TimeoutError'})


if __name__ == '__main__':
    unittest.main()
