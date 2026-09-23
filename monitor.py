"""Shared policy and durable delivery for the existing free news collectors."""
import base64
import copy
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

import monitor_sources as sources

DAY = 86400
BRANCH, STATE_PATH = 'important-monitor-state', 'delivery-state-v2.json'
BASELINES = {'ow': '1d3e7d69941767bd6da66e4e884fadee33656a12', 'dbd': '3112d2cfc4ef5e13bc02ee39a5f63af59a6b6ed5', 'apex': '181f9bb164415a0ffd338b83e6a7734f0088536e'}
HOOKS = {t: 'DISCORD_WEBHOOK_' + t.upper() for t in sources.GAMES}
HOOKS.update({t: 'DISCORD_WEBHOOK_IMPORTANT' for t in ('windows', 'discord', 'steam', 'medal')})
CHANNELS = {'windows': 'info_monitor_main', 'discord': 'info_monitor_main',
            'steam': 'info_monitor_main', 'medal': 'info_monitor_main',
            'ow': 'game-monitor_ow', 'dbd': 'game-monitor_dbd', 'apex': 'game-monitor_apex'}
NEWS = {'ow': 'https://overwatch.blizzard.com/ja-jp/news/',
        'dbd': 'https://deadbydaylight.com/ja/news/',
        'apex': 'https://www.ea.com/ja/games/apex-legends/apex-legends/news'}


def stamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return int((parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).timestamp())
    except (ValueError, TypeError):
        return None


class Page(HTMLParser):
    """Small article extractor; exclude scripts/navigation from classification."""
    def __init__(self, raw):
        super().__init__()
        self.links, self.alternates, self.meta, self.parts = [], [], {}, []
        self.skip = 0
        self.feed(raw)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ('script', 'style', 'nav', 'footer'):
            self.skip += 1
        if tag == 'a' and a.get('href'):
            self.links.append(a['href'])
        if tag == 'meta':
            self.meta[a.get('property', a.get('name', a.get('itemprop', '')))] = a.get('content', '')
        if tag == 'link' and a.get('hreflang', '').startswith('ja'):
            self.alternates.append(a.get('href', ''))

    def handle_endtag(self, tag):
        if tag in ('script', 'style', 'nav', 'footer'):
            self.skip = max(0, self.skip - 1)
        if tag in ('p', 'li', 'h1', 'h2', 'h3', 'div'):
            self.parts.append('\n')

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def article_page(url, topic, fallback=None):
    raw = sources.request(url)
    page = Page(raw)
    # A localized navigation shell alone is not proof of a Japanese article.
    body_match = re.search(r'<article\b[^>]*>(.*?)</article>', raw, re.S | re.I)
    body = '\n'.join(Page(body_match[1]).parts) if body_match else '\n'.join(page.parts)
    heading = re.search(r'<h1\b[^>]*>(.*?)</h1>', raw, re.S | re.I)
    title = page.meta.get('og:title') or (sources.clean(heading[1]) if heading else '')
    date = stamp(page.meta.get('article:published_time') or page.meta.get('datePublished', ''))
    if date is None:
        match = re.search(r'"datePublished"\s*:\s*"([^"]+)"|<time[^>]*datetime=["\']([^"\']+)', raw)
        if match:
            date = stamp(match[1] or match[2])
    if not title or len(sources.clean(body)) < 80:
        raise ValueError('Article body missing')
    item = sources.article({'topic': topic, 'id': topic + '-website', 'official': True}, title, url, body, date)
    if fallback:
        item['date'] = item['date'] or fallback['date']
        item['original_keys'] = sorted(keys(fallback))
        item['gid'] = fallback['gid']
    item['language'] = 'ja' if re.search(r'[ぁ-んァ-ヶ]', title) else 'en'
    return item, page


def news_url(url, topic):
    p = urllib.parse.urlsplit(url)
    host = (p.hostname or '').removeprefix('www.')
    return p.scheme == 'https' and ((topic == 'ow' and host == 'overwatch.blizzard.com' and re.search(r'/news/\d+', p.path)) or
        (topic == 'dbd' and host == 'deadbydaylight.com' and re.search(r'/news/[^/]+', p.path)) or
        (topic == 'apex' and host == 'ea.com' and '/apex-legends/' in p.path and re.search(r'/news/[^/]+', p.path)))


def japanese_url(url):
    p = urllib.parse.urlsplit(url)
    host = (p.hostname or '').removeprefix('www.')
    if host in ('overwatch.blizzard.com', 'news.blizzard.com', 'learn.microsoft.com'):
        if '/ja-jp/' in url:
            return url
        return urllib.parse.urlunsplit(p._replace(path='/ja-jp' + re.sub(r'^/en-us', '', p.path)))
    if host == 'deadbydaylight.com':
        if '/ja/' in p.path:
            return url
        return re.sub(r'/(?:en/)?news/', '/ja/news/', url)
    if host == 'ea.com':
        if '/ja/' in p.path or '/ja-jp/' in p.path:
            return url
        return re.sub(r'/(?:en(?:-us)?/)?games/', '/ja/games/', url)
    return None


def localize(item):
    if item.get('active_issue'):
        candidate = japanese_url(item['url'])
        try:
            raw = sources.request(candidate)
            anchor = urllib.parse.urlsplit(candidate).fragment
            if anchor in raw and re.search(r'[ぁ-んァ-ヶ]', sources.clean(raw)):
                return dict(item, url=candidate, original_keys=sorted(keys(item)))
        except Exception:
            pass
        return item
    if item.get('language') == 'ja' or item['topic'] not in sources.GAMES:
        return item
    urls = [item['url']] + re.findall(r'https?://[^\s<>"\'\]\)]+', html.unescape(item['body']))
    # Only verified official article links; never replace with an unverified URL.
    for url in urls[:40]:
        if not news_url(url, item['topic']):
            continue
        candidate = japanese_url(url)
        if not candidate:
            continue
        try:
            localized, _ = article_page(candidate, item['topic'], item)
            same_date = not localized['date'] or not item['date'] or abs(localized['date'] - item['date']) <= 2 * DAY
            if localized['language'] == 'ja' and same_date:
                localized['recovered'] = item.get('recovered', False)
                return localized
        except Exception:
            pass
        break
    return item


def fetch_news(topic, now):
    if topic == 'dbd':
        data = json.loads(sources.request('https://deadbydaylight.com/page-data/ja/news/page-data.json'))
        nodes = data['result']['pageContext']['postsData']['articles']['edges']
        if not nodes:
            raise ValueError('DbD official index empty')
        rows = []
        for entry in nodes:
            row = entry['node']
            date = stamp(row['published_at'])
            if row['locale'] != 'ja' or date is None or date < now - 14 * DAY:
                continue
            url = 'https://deadbydaylight.com/ja/news/' + row['slug'] + '/'
            item, _ = article_page(url, topic)
            item['date'] = date
            rows.append(item)
        return rows
    page = Page(sources.request(NEWS[topic]))
    urls = list(dict.fromkeys(urllib.parse.urljoin(NEWS[topic], u) for u in page.links
                             if news_url(urllib.parse.urljoin(NEWS[topic], u), topic)))[:16]
    urls = [japanese_url(u) or u for u in urls if '/news/game/' not in u]
    if not urls:
        raise ValueError('Official news index layout changed')
    rows, failed = [], 0
    for url in urls:
        try:
            item, _ = article_page(url, topic)
            if item['date'] is not None and item['date'] >= now - 14 * DAY:
                rows.append(item)
        except Exception:
            failed += 1
    if failed == len(urls):
        raise ValueError('Official articles unavailable')
    if failed:
        print('::warning::' + topic + ' official article fetch failures: ' + str(failed))
    return rows


def severity(text):
    rules = [
        ('起動不能・再起動障害', r'boot loop|unbootable|fail\w* to boot|unable to boot|cannot boot|won.t boot|restart loop|unexpected\w* restart'),
        ('ブルースクリーン・システム停止', r'blue screen|black screen|BSOD|stop error|system\w* crash|devices?.{0,35}(?:freeze|unresponsive)'),
        ('データ損失・破損', r'data loss|data corruption|corrupt\w*.{0,25}(?:files?|data)|(?:files?|data).{0,25}(?:deleted|lost|corrupt)'),
        ('更新のインストール障害', r'(?:updates?|install\w*).{0,45}(?:fail|blocked|error)|(?:fail|unable).{0,35}(?:install|updat)'),
        ('ネットワーク接続障害', r'(?:wi-fi|wifi|internet|network|VPN).{0,40}(?:fail|broken|disconnect|unable|connectivity issues)|(?:breaks?|disables?).{0,30}(?:wi-fi|wifi|internet|network|VPN)'),
        ('アプリ・ゲームの動作停止', r'(?:apps?|applications?|games?|Outlook|Teams).{0,55}(?:crash|unresponsive|fail to (?:launch|open)|stop working)|(?:breaks?|crashes?).{0,30}(?:apps?|games?)'),
        ('悪用確認済みの脆弱性', r'actively exploited|exploited in the wild|under active (?:attack|exploitation)'),
        ('音声・通常機能の重大な障害', r'(?:audio|sound|microphone).{0,60}(?:broken|fail|not work|stop)|(?:breaks?|disables?).{0,60}(?:audio|sound|microphone)')]
    return next((label for label, pattern in rules if re.search(pattern, text, re.I)), None)


def windows_items(raw, source):
    tables = re.findall(r'<table\b[^>]*>.*?</table>', raw, re.S | re.I)
    table = next((t for t in tables if all(x in sources.clean(t) for x in ('Summary', 'Originating update', 'Last updated'))), None)
    if table is None:
        if re.search(r'There are no (?:active )?known issues', sources.clean(raw), re.I):
            return []
        raise ValueError('Windows official table changed')
    rows = []
    for row in re.findall(r'<tr\b[^>]*>(.*?)</tr>', table, re.S | re.I):
        cells = re.findall(r'<td\b[^>]*>(.*?)</td>', row, re.S | re.I)
        if not cells:
            continue
        if len(cells) != 4:
            raise ValueError('Windows table columns changed')
        link = re.search(r'<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', cells[0], re.S | re.I)
        if not link:
            raise ValueError('Windows issue link missing')
        url = urllib.parse.urljoin(source['url'], html.unescape(link[1]))
        if urllib.parse.urlsplit(url).hostname != 'learn.microsoft.com':
            raise ValueError('Unexpected Windows issue domain')
        if sources.clean(cells[2]).lower().startswith('resolved'):
            continue
        title = sources.clean(link[2])
        anchor = urllib.parse.urlsplit(url).fragment
        detail = re.search(r'<(?:div|a|h[2-4])[^>]*id=["\']' + re.escape(anchor) + r'["\'][^>]*>.*?(?=<div[^>]*id=["\']\d+msgdesc|<h2\b|$)', raw, re.S | re.I) if anchor else None
        body = sources.clean(detail[0] if detail else cells[0] + ' ' + cells[1])
        date_text = sources.clean(cells[3])
        date_match = re.search(r'\d{4}-\d{2}-\d{2}', date_text)
        date = stamp(date_match[0]) if date_match else None
        item = sources.article(source, title, url, body, date)
        item['environment'] = 'Windows 11 ' + source['id'].removeprefix('microsoft-').upper()
        item['active_issue'] = True
        rows.append(item)
    return rows


def classify(item):
    title, body = item['title'].lower(), sources.clean(item['body']).lower()
    if item['topic'] == 'windows':
        if not item.get('active_issue') and not re.search(r'windows\s*11', title + body):
            return None
        return severity(title) or (severity(body) if item.get('active_issue') else None)
    if item['topic'] not in sources.GAMES:
        return sources.classify(item)
    if re.search(r'\b(rumou?r|leak|speculation|sale|discount|giveaway|contest|esports|owcs|algs|tournament|merchandise)\b|セール|グッズ|大会結果|配信告知', title):
        return None
    text = title + '\n' + body
    # Concrete major changes in the full article also qualify under a generic title.
    patterns = [
        ('新キャラクター・新マップ・新チャプター', r'\b(?:new|all-new) (?:hero|character|killer|survivor|legend|map|chapter)\b|新(?:ヒーロー|キャラクター|キラー|サバイバー|レジェンド|マップ|チャプター)|chapter reveal'),
        ('コラボ', r'\bcollaboration\b|\bcrossover\b|\bcollab\b|コラボ'),
        ('新シーズン・大型アップデート', r'\bnew season\b|\bseason\s+\d+\b|\bmid[ -]?season\b|\bmid[ -]?chapter\b|major update|新シーズン|シーズン\s*\d+|大型アップデート|ミッドシーズン'),
        ('大規模なゲームプレイ・システム変更', r'(?:major|massive|complete).{0,40}(?:rework|overhaul|balance|gameplay)|(?:hero|legend|class|perk|ranked|matchmaking|system).{0,35}(?:rework|overhaul)|大規模|大幅.{0,15}(?:変更|調整)|リワーク'),
        ('主要期間限定イベント・モード', r'collection event|limited.time (?:event|mode)|new game mode|anniversary event|期間限定.{0,20}(?:イベント|モード)|コレクションイベント|新ゲームモード'),
    ]
    for label, pattern in patterns:
        for match in re.finditer(pattern, text, re.I):
            sentence = text[max(0, text.rfind('.', 0, match.start()) + 1):match.end() + 80]
            if not re.search(r'no new|not (?:a |adding )?new|last year|previous season|昨年|前シーズン', sentence):
                return label
    if item['official'] and re.search(r'roadmap|developer update|director.s take|designer.?s? notes|ロードマップ|開発者アップデート|ディレクターの視点|デザイナーノート', title):
        return '開発チームの重要発表・今後の予定'
    if item['official'] and re.search(r'\bevent\b|イベント', title) and not re.search(r'campaign|キャンペーン|配信|セール', title):
        return '主要期間限定イベント・モード'
    if item['topic'] == 'dbd' and re.search(r'\b\d+\.\d+\.0\b', title):
        return '大型アップデート' + ('（PTB）' if 'ptb' in title else '')
    return None


def keys(item):
    # Article identities use their own URL; referenced historical articles cannot suppress new news.
    result = {'gid:' + item['gid'], 'title:' + sources.digest(re.sub(r'\W+', '', sources.clean(item['title']).casefold()))}
    url = sources.canonical(item['url'])
    p = urllib.parse.urlsplit(url)
    path = re.sub(r'^/(?:en-us|en|ja-jp|ja)(?=/)', '', p.path)
    host = (p.hostname or '').removeprefix('www.')
    identity = urllib.parse.urlunsplit(('https', host, path, '', p.fragment))
    result.add('url:' + (item['gid'] if '/medal-changelog-' in url else identity))
    if host in ('overwatch.blizzard.com', 'news.blizzard.com'):
        match = re.search(r'/(?:news|article|overwatch)/(\d+)', path)
        if match:
            result.add('official:' + match[1])
    if host in ('deadbydaylight.com', 'ea.com'):
        match = re.search(r'/news/([^/]+)', path)
        if match:
            result.add('official:' + match[1].lower())
    if host == 'steamcommunity.com':
        match = re.search(r'/(?:announcements/detail|detail)/(\d+)', path)
        if match:
            result.add('steam:' + match[1])
    if p.fragment.startswith('patch-'):
        result.add('patch-date:通常サーバー:' + p.fragment[6:])
    if item['topic'] in sources.GAMES and item['date'] and re.search(r'patch notes|パッチノート', item['title'], re.I):
        day = datetime.fromtimestamp(item['date'], timezone.utc).strftime('%Y-%m-%d')
        result.add('patch-date:' + ('PTB' if 'ptb' in item['title'].lower() else '通常サーバー') + ':' + day)
    version = re.search(r'\b\d+\.\d+\.\d+\b', item['title'])
    if item['topic'] == 'dbd' and version:
        result.add('patch-version:' + ('PTB' if 'ptb' in item['title'].lower() else '通常サーバー') + ':' + version[0])
    result.update(item.get('original_keys', []))
    return result


def incident_keys(item):
    if item['topic'] != 'windows':
        return set()
    label = severity(item['title'])
    if not label:
        return set()
    return {'windows:incident:' + kb + ':' + label for kb in re.findall(r'KB\d{6,8}', item['body'].upper())}


def scoped_keys(item):
    return {item['topic'] + ':' + k for k in keys(item)} | incident_keys(item)


def migrate(old, games, baselines, windows, now):
    if old.get('pending') or any(g.get('pending') for g in games.values()) or any(i['status'] in ('reserved', 'unknown') for i in windows['incidents']):
        raise ValueError('Uncertain legacy delivery; preserve and inspect history')
    sent = set(old['sent'])
    for topic, game in games.items():
        baseline = baselines[topic]
        if baseline.get('last_message_id') or baseline.get('pending') or baseline['started'] != game['started']:
            raise ValueError('Unverified baseline; migration stopped')
        if '_confirmed_sent' not in game:
            raise ValueError('Delivery history audit required')
        sent.update(topic + ':' + k for k in game['_confirmed_sent'])
    state = {'version': 2, 'sent': sorted(sent), 'seen': {}, 'queue': {}, 'pending': {},
             'receipts': [], 'windows_legacy': [i for i in windows['incidents'] if i['status'] == 'sent'],
             'migrated_at': now, 'checked': {}, 'source_failures': {}, 'baseline_excluded': True}
    return state


def confirmed_history(history):
    """Only a pending -> message-ID-confirmed transition proves a historical send.

    Older DbD code also copied seen aliases into sent on later upgrades; subtracting
    the initial baseline alone therefore is not sufficient.
    """
    sent, last_id = set(), None
    previous = None
    for current in history:
        message_id = current.get('last_message_id')
        if message_id and message_id != last_id:
            pending = previous.get('pending') if previous else None
            if not pending or current.get('pending') or not set(pending['keys']) <= set(current['sent']):
                raise ValueError('Historical receipt lacks matching reservation')
            sent.update(pending['keys'])
            last_id = message_id
        previous = current
    return sorted(sent)


def windows_duplicate(item, state):
    if item['topic'] != 'windows':
        return False
    normalized = re.sub(r'[^a-z0-9]+', ' ', item['title'].lower()).strip()
    aliases = {'url:' + sources.digest(item['url']), 'title:' + sources.digest(normalized)}
    return any(aliases & set(i['keys']) for i in state.get('windows_legacy', []))


def plan(state, items, now, days=7):
    state = copy.deepcopy(state)
    selected_keys = set(state['sent'])
    # Recover cross-source incident aliases from already-confirmed articles still in feeds.
    for item in items:
        if {item['topic'] + ':' + k for k in keys(item)} & selected_keys:
            selected_keys.update(scoped_keys(item))
    for pending in state['pending'].values():
        selected_keys.update(pending['keys'])
    for queued in state['queue'].values():
        selected_keys.update(scoped_keys(queued))
    grouped = {}
    for original in items:
        item = dict(original)
        if item.get('active_issue'):
            incident = urllib.parse.urlsplit(item['url']).fragment
            if incident in grouped:
                grouped[incident]['environment'] += ' / ' + item['environment'].removeprefix('Windows 11 ')
                grouped[incident]['original_keys'] = sorted(set(grouped[incident].get('original_keys', [])) | keys(item))
                continue
            grouped[incident] = item
        else:
            grouped[item['topic'] + ':' + item['gid']] = item
    for item in sorted(grouped.values(), key=lambda i: (not i['official'], i.get('language') != 'ja', i['date'] or 0)):
        identity = item['topic'] + ':' + sources.digest(item['gid'])
        prior = identity in state['seen']
        state['seen'][identity] = now
        published = item['date']
        if not item.get('active_issue') and (published is None or not now - days * DAY <= published <= now):
            continue
        label = classify(item)
        ks = scoped_keys(item)
        if not label or ks & selected_keys or windows_duplicate(item, state):
            continue
        # A durable queue survives both rate limits and the seven-day discovery window.
        item = dict(item, recovered=prior, label=label)
        state['queue'][identity] = item
        selected_keys.update(ks)
    return state


def webhook(topic):
    hook = os.environ.get(HOOKS[topic], '').strip()
    p = urllib.parse.urlsplit(hook)
    if p.scheme != 'https' or p.netloc != 'discord.com' or not re.fullmatch(r'/api/webhooks/\d+/[^/]+', p.path) or p.query or p.fragment:
        raise ValueError('Missing or invalid webhook for ' + topic)
    return hook


def payload(item, user):
    if not re.fullmatch(r'\d{17,20}', user):
        raise ValueError('DISCORD_MENTION_USER_ID is missing or invalid')
    summary = item.get('summary_ja') or sources.japanese_summary(item, item['label'])
    impacts = {'新キャラクター・新マップ・新チャプター': '利用できるキャラクターや遊べるコンテンツが変わります。',
               'コラボ': '期間限定のコラボ内容と参加予定を確認する必要があります。',
               '新シーズン・大型アップデート': 'プレイ環境やシーズンの進行に関わる更新です。',
               '大規模なゲームプレイ・システム変更': 'これまでの戦術や設定、選択の見直しに関わる変更です。',
               '主要期間限定イベント・モード': '参加できる期間やゲームモードが変わります。',
               '開発チームの重要発表・今後の予定': '今後のプレイ内容や予定の判断に関わる発表です。'}
    impact = impacts.get(item['label'], item['label'])
    action = '発表内容と実施日を確認してください。設定変更などの必須対応は、この取得内容からは確認できていません。'
    if item['topic'] == 'windows':
        action = '対象環境・更新番号が一致するか確認してください。更新の削除や設定変更は、公式の回避策が適用される場合に限って検討してください。'
    body = sources.clean(item['body'])
    # Include source-grounded dates and instructions; translate with the same free adapter.
    dates = re.findall(r'\b20\d{2}-\d{2}-\d{2}\b|\b(?:January|February|March|April|May|June|July|August|September|October|November|December) \d{1,2}(?:, 20\d{2})?|\d{1,2}月\d{1,2}日', body)
    description = ('※取得済みの未送信情報を再評価しました。\n' if item.get('recovered') else '')
    description += '**内容**\n' + summary + '\n\n**影響**\n' + impact
    if item.get('environment'):
        description += '\n\n**対象環境**\n' + item['environment']
    if dates:
        description += '\n\n**本文で案内されている日付**\n' + ' / '.join(dict.fromkeys(dates))[:220] + '（用途・時刻は上記の内容または原文を確認）'
    description += '\n\n**推奨対応**\n' + action
    description += '\n\n**情報源**\n' + item['source'] + ('（公式）' if item['official'] else '（補助情報）') + '\n' + item['url']
    return {'content': '<@' + user + '> ' + sources.NAMES[item['topic']] + '｜重要情報',
            'allowed_mentions': {'parse': [], 'users': [user]},
            'embeds': [{'title': item['title'][:256], 'url': item['url'], 'description': description[:4000], 'color': 3447003}]}


def deliver(state, identity, item, save, send, user, now):
    route = CHANNELS[item['topic']]
    if route in state['pending'] or scoped_keys(item) & set(state['sent']):
        return False
    data = payload(item, user)  # Translation failures happen before a delivery reservation.
    record = {'at': now, 'keys': sorted(scoped_keys(item)), 'queue_id': identity, 'topic': item['topic'], 'url': item['url']}
    state['pending'][route] = record
    save(state)
    try:
        response = send(item['topic'], data)
    except urllib.error.HTTPError as error:
        # Explicit rejections are safe to retry; a 5xx or broken connection is ambiguous.
        if error.code in (400, 401, 403, 404, 405, 413, 429):
            del state['pending'][route]
            save(state)
        raise
    if not isinstance(response, dict) or not response.get('id'):
        raise ValueError('Discord receipt missing; pending retained')
    state['sent'] = sorted(set(state['sent']) | set(record['keys']))
    state['receipts'].append({'id': response['id'], 'channel': route, 'at': now,
                              'keys': record['keys'], 'mention_confirmed': any(u.get('id') == user for u in response.get('mentions', []))})
    del state['pending'][route]
    state['queue'].pop(identity, None)
    save(state)
    return True


def collect(topics, now):
    definitions = [s for s in sources.SOURCES if s['topic'] in topics]
    if 'windows' in topics:
        definitions.append(dict(id='bleepingcomputer', topic='windows', kind='rss', official=False,
                                url='https://www.bleepingcomputer.com/feed/'))
    definitions += [dict(id='microsoft-' + version, topic='windows', kind='windows', official=True,
        url='https://learn.microsoft.com/en-us/windows/release-health/status-windows-11-' + version)
        for version in ('23h2', '24h2', '25h2', '26h1') if 'windows' in topics]
    definitions += [dict(id=t + '-website', topic=t, kind='website', official=True) for t in sources.GAMES if t in topics]
    definitions += [dict(id=t + '-patch', topic=t, kind='patch', official=True) for t in ('ow', 'dbd') if t in topics]
    def fetch(source):
        topic = source['topic']
        try:
            if source['kind'] == 'windows':
                rows = windows_items(sources.request(source['url']), source)
            elif source['kind'] == 'website':
                rows = fetch_news(topic, now)
            elif source['kind'] == 'patch':
                if topic == 'ow':
                    import monitor_ow_source
                    data = monitor_ow_source.fetch_patches(now - 14 * DAY)
                else:
                    import monitor_dbd_source
                    data = monitor_dbd_source.fetch_bhvr(now - 14 * DAY)
                rows = [sources.article(source, r['title'], r['url'], r['contents'], r['date'], r['gid']) for r in data]
            else:
                rows = sources.fetch_source(source, now)
            return source, rows, None
        except Exception as error:
            return source, [], type(error).__name__ + (':' + str(error.code) if isinstance(error, urllib.error.HTTPError) else '')
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(fetch, definitions))
    for source, rows, error in results:
        print(source['id'] + ': ' + ('FAILED ' + error if error else str(len(rows)) + ' items'))
    failed = {s['id']: e for s, _, e in results if e}
    unavailable = [t for t in topics if not any(s['topic'] == t and s['official'] and not e for s, _, e in results)]
    if 'windows' in topics and not any(s['id'].startswith('microsoft-') and not e for s, _, e in results):
        if 'windows' not in unavailable:
            unavailable.append('windows')
    return [r for _, rows, _ in results for r in rows], failed, unavailable


class Store:
    def __init__(self):
        self.api = 'https://api.github.com/repos/' + os.environ['GITHUB_REPOSITORY']
        self.sha = None

    def gh(self, path, data=None, method=None):
        return json.loads(sources.request(self.api + path, data,
            {'Authorization': 'Bearer ' + os.environ['GH_TOKEN'], 'Accept': 'application/vnd.github+json'}, method))

    def read(self, path, branch):
        obj = self.gh('/contents/' + path + '?ref=' + urllib.parse.quote(branch, safe=''))
        return json.loads(base64.b64decode(obj['content'])), obj['sha']

    def load(self, now):
        try:
            state, self.sha = self.read(STATE_PATH, BRANCH)
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
            if self.gh('/commits?sha=' + BRANCH + '&path=' + STATE_PATH + '&per_page=1'):
                raise ValueError('History deleted; refusing reset')
            old, _ = self.read('important-state.json', BRANCH)
            games = {t: self.read(t + '-state.json', t + '-monitor-state')[0] for t in sources.GAMES}
            baselines = {t: self.read(t + '-state.json', sha)[0] for t, sha in BASELINES.items()}
            for topic, game in games.items():
                path = topic + '-state.json'
                commits = self.gh('/commits?sha=' + topic + '-monitor-state&path=' + path + '&per_page=100')
                if len(commits) >= 100:
                    raise ValueError('Legacy audit requires pagination before migration')
                history = [self.read(path, c['sha'])[0] for c in reversed(commits)]
                game['_confirmed_sent'] = confirmed_history(history)
                if history[-1].get('last_message_id') != game.get('last_message_id'):
                    raise ValueError('Legacy monitor changed during migration')
            windows, _ = self.read('.monitor/windows-state.json', os.environ['DEFAULT_BRANCH'])
            state = migrate(old, games, baselines, windows, now)
        if state.get('version') != 2 or not all(isinstance(state.get(k), dict) for k in ('seen', 'pending', 'queue')) or not isinstance(state.get('sent'), list):
            raise ValueError('Invalid shared state; refusing reset')
        return state

    def save(self, state):
        data = {'branch': BRANCH, 'message': 'Update confirmed monitor delivery history [skip ci]',
                'content': base64.b64encode(json.dumps(state, ensure_ascii=False).encode()).decode()}
        if self.sha:
            data['sha'] = self.sha
        self.sha = self.gh('/contents/' + STATE_PATH, data, 'PUT')['content']['sha']


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else 'important'
    topics = list(sources.NAMES) if mode == 'important' else [mode]
    if any(t not in sources.NAMES for t in topics):
        raise ValueError('Unknown topic')
    now = int(time.time())
    if os.environ.get('TEST_NOTIFICATION', '').lower() == 'true':
        store = Store()
        state = store.load(now)
        tests = ('windows', 'ow', 'dbd', 'apex') if mode == 'important' else topics
        for topic in tests:
            hook = webhook(topic)
            test_item = sources.article(dict(topic=topic, id='GitHub Actions 接続検証', official=True),
                '【接続テスト】' + CHANNELS[topic],
                'https://github.com/' + os.environ['GITHUB_REPOSITORY'], '', now, 'verification-v2-' + topic)
            test_item.update(label='通知先・メンションの検証', summary_ja='これは監視改善後の接続テストです。実際のニュースではありません。')
            deliver(state, 'test:' + topic, test_item, store.save,
                    lambda t, data: json.loads(sources.request(hook + '?wait=true', data)),
                    os.environ.get('DISCORD_MENTION_USER_ID', ''), now)
        print('Connection checks completed; confirmed checks are not posted again.')
        return bool(state['pending'])
    items, failures, unavailable = collect(topics, now)
    if '--check-sources' in sys.argv:
        print(json.dumps({'failures': failures, 'unavailable': unavailable, 'important': [{'topic': i['topic'], 'title': i['title'], 'url': i['url']} for i in items if i['date'] and i['date'] >= now - 14 * DAY and classify(i)]}, ensure_ascii=False))
        return bool(unavailable)
    store = Store()
    state = store.load(now)
    days = 14 if os.environ.get('RECOVERY_DAYS') == '14' else 7
    state = plan(state, items, now, days)
    state['checked'][mode] = now
    state['source_failures'][mode] = failures
    store.save(state)
    delivered, errors = 0, []
    for identity, item in list(state['queue'].items()):
        if item['topic'] not in topics or delivered >= 5:
            continue
        try:
            webhook(item['topic'])  # Configuration failures must not create ambiguous pending delivery.
            item = localize(item)
            if scoped_keys(item) & set(state['sent']):
                state['queue'].pop(identity)
                store.save(state)
                continue
            if deliver(state, identity, item, store.save,
                       lambda topic, data: json.loads(sources.request(webhook(topic) + '?wait=true', data)),
                       os.environ.get('DISCORD_MENTION_USER_ID', ''), now):
                delivered += 1
        except Exception as error:
            errors.append(item['topic'] + ':' + type(error).__name__)
            print('::warning::Delivery retained for inspection/retry: ' + errors[-1])
    summary = f'Monitor {mode}: delivered={delivered}, queued={len(state["queue"])}, pending={len(state["pending"])}.\n'
    summary += 'Source failures: ' + json.dumps(failures) + '\nUnavailable topics: ' + ', '.join(unavailable) + '\n'
    print(summary)
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as handle:
            handle.write(summary)
    return bool(unavailable or errors or state['pending'])


if __name__ == '__main__':
    try:
        sys.exit(1 if main() else 0)
    except Exception as error:
        print('::error::Monitor stopped safely: ' + type(error).__name__)
        sys.exit(1)
