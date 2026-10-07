"""Free, standard-library-only supplemental monitor. Never writes legacy state."""
import base64
import copy
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

BRANCH, STATE_PATH = 'important-monitor-state', 'important-state.json'
DAY = 86400
# User-requested recovery only; sent aliases still prevent repeat delivery.
RECOVER_URLS = {'https://discord.com/blog/discord-patch-notes-september-8-2026'}
GAMES = {'dbd': 381210, 'ow': 2357570, 'apex': 1172470}
NAMES = {'windows': 'Windows 11', 'dbd': 'Dead by Daylight', 'ow': 'Overwatch',
         'apex': 'Apex Legends', 'discord': 'Discord', 'steam': 'Steam', 'medal': 'Medal.tv'}
SOURCES = [
    {'id': 'windows-official', 'topic': 'windows', 'url': 'https://blogs.windows.com/windowsexperience/feed/', 'kind': 'rss', 'official': True},
    {'id': 'windows-latest', 'topic': 'windows', 'url': 'https://www.windowslatest.com/feed/', 'kind': 'rss', 'official': False},
    {'id': 'discord-official', 'topic': 'discord', 'url': 'https://discord.com/blog/rss.xml', 'kind': 'rss', 'official': True},
    {'id': 'steam-official', 'topic': 'steam', 'url': 'https://store.steampowered.com/feeds/news/group/4145017', 'kind': 'rss', 'official': True},
    {'id': 'medal-official', 'topic': 'medal', 'url': 'https://medal.tv/blog/categories/features', 'kind': 'medal', 'official': True},
] + [{'id': topic + ('-official' if feed == 'steam_community_announcements' else '-pcgamer'),
      'topic': topic, 'kind': 'steam', 'official': feed == 'steam_community_announcements', 'feed': feed,
      'url': 'https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/?' + urllib.parse.urlencode(
          {'appid': appid, 'count': 100, 'maxlength': 0, 'feeds': feed})}
     for topic, appid in GAMES.items() for feed in ('steam_community_announcements', 'pcgamer')]


class FetchRows(list):
    """Usable public articles plus observable partial collection failures."""
    def __init__(self, rows=(), errors=None):
        super().__init__(rows)
        self.errors = dict(errors or {})


def fetch_error(error):
    return type(error).__name__ + (':' + str(error.code) if isinstance(error, urllib.error.HTTPError) else '')


def clean(text):
    return re.sub(r'\s+', ' ', html.unescape(re.sub(r'<[^>]+>|\[[^\]]+\]', ' ', text))).strip()


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def canonical(url):
    p = urllib.parse.urlsplit(html.unescape(url))
    if p.scheme != 'https' or not p.hostname or p.username or p.password:
        raise ValueError('Unsafe article URL')
    query = urllib.parse.urlencode([(k, v) for k, v in urllib.parse.parse_qsl(p.query)
                                   if not k.startswith('utm_') and k not in ('ref', 'source')])
    return urllib.parse.urlunsplit(('https', p.netloc.lower(), p.path.rstrip('/'), query, p.fragment))


def request(url, data=None, headers=None, method=None):
    req = urllib.request.Request(url, data=None if data is None else json.dumps(data).encode(),
        headers={'User-Agent': 'ImportantNewsMonitor/1.0', 'Content-Type': 'application/json', **(headers or {})}, method=method)
    # Do not forward credentials through redirects, including webhook redirects.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, hdrs, newurl):
            return None
    opener = urllib.request.build_opener(NoRedirect()) if data is not None or headers else urllib.request.build_opener()
    with opener.open(req, timeout=25) as response:
        raw = response.read(5_000_001)
        if len(raw) > 5_000_000:
            raise ValueError('Response too large')
        return raw.decode('utf-8')


def article(source, title, url, body='', date=None, gid=None):
    url = canonical(url)
    return dict(topic=source['topic'], source=source['id'], official=source['official'],
                title=clean(title), url=url, body=body, date=date, gid=str(gid or url))


def parse_rss(raw, source):
    root = ET.fromstring(raw)
    rows = root.findall('./channel/item')
    if not rows:
        raise ValueError('RSS layout changed or empty')
    result = []
    for row in rows:
        title, url, published = (row.findtext(k) for k in ('title', 'link', 'pubDate'))
        if not title or not url or not published:
            raise ValueError('RSS field missing')
        date = int(parsedate_to_datetime(published).timestamp())
        # Trust only articles on the source domain (Steam RSS uses the same domain).
        if urllib.parse.urlsplit(url).hostname != urllib.parse.urlsplit(source['url']).hostname:
            raise ValueError('Unexpected feed article domain')
        body = row.findtext('{http://purl.org/rss/1.0/modules/content/}encoded') or row.findtext('description') or ''
        result.append(article(source, title, url, body, date))
    return result


def parse_medal(raw, source):
    result = []
    class Cards(HTMLParser):
        def __init__(self):
            super().__init__()
            self.depth = 0
            self.heading = False
            self.title = ''
            self.url = ''

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag == 'div':
                if self.depth:
                    self.depth += 1
                elif 'blog-card' in attrs.get('class', '').split():
                    self.depth, self.title, self.url = 1, '', ''
            if self.depth:
                if tag in ('h2', 'h3', 'h4'):
                    self.heading = True
                if tag == 'a' and '/blog/posts/' in attrs.get('href', ''):
                    self.url = urllib.parse.urljoin(source['url'], attrs['href'])

        def handle_data(self, value):
            if self.depth and self.heading:
                self.title += value

        def handle_endtag(self, tag):
            if tag in ('h2', 'h3', 'h4'):
                self.heading = False
            if tag == 'div' and self.depth:
                self.depth -= 1
                if not self.depth and self.title and self.url:
                    if urllib.parse.urlsplit(self.url).hostname == 'medal.tv':
                        result.append(article(source, self.title, self.url))

    Cards().feed(raw)
    for match in re.finditer(r'<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', raw, re.S):
        url = urllib.parse.urljoin(source['url'], html.unescape(match[1]))
        if urllib.parse.urlsplit(url).hostname != 'medal.tv' or '/blog/posts/' not in url:
            continue
        heading = re.search(r'<h[1-6][^>]*>(.*?)</h[1-6]>', match[2], re.S)
        title = clean(heading[1] if heading else match[2])
        if title and title.lower() != 'read more':
            result.append(article(source, title, url))
    if not result:
        raise ValueError('Medal blog layout changed or empty')
    return result


def fetch_source(source, now):
    raw = request(source['url'])
    if source['kind'] == 'rss':
        rows = FetchRows(parse_rss(raw, source))
        if source['id'] == 'discord-official':
            for row in list(rows):
                if row['date'] >= now - 14 * DAY or row['url'] in RECOVER_URLS:
                    try:
                        row['body'] = discord_body(request(row['url']))
                    except Exception as error:
                        rows.errors[row['url']] = fetch_error(error)
                        rows.remove(row)
        return rows
    if source['kind'] == 'medal':
        cards = {row['url']: row for row in parse_medal(raw, source)}
        logs = sorted(url for url in cards if 'changelog' in url)[-1:]
        result = FetchRows()
        for url, row in cards.items():
            if 'changelog' in url and url not in logs:
                continue
            try:
                page = request(url)
                if url in logs:
                    result.extend(parse_changelog(page, source, url))
                else:
                    date, body = medal_article(page)
                    result.append(dict(row, date=date, body=body))
            except Exception as error:
                result.errors[url] = fetch_error(error)
        return result
    result, end = FetchRows(), now + 1
    for page in range(10):
        try:
            if page:
                raw = request(source['url'] + '&enddate=' + str(end))
            obj = json.loads(raw)['appnews']
            if int(obj['appid']) != GAMES[source['topic']]:
                raise ValueError('Wrong Steam app')
            rows = obj['newsitems']
            if not isinstance(rows, list):
                raise ValueError('Invalid Steam newsitems schema')
        except Exception as error:
            if not result:
                raise
            result.errors['page-' + str(page)] = fetch_error(error)
            return result
        cursor_dates = []
        for index, row in enumerate(rows):
            try:
                if not isinstance(row, dict) or not row.get('feedname'):
                    raise ValueError('Invalid Steam article schema')
                # Pagination considers every returned feed, not just the selected
                # feed. Validate its cursor date before accepting this page.
                cursor_dates.append(int(row['date']))
                if row['feedname'] == source['feed']:
                    if not all(row.get(key) for key in ('title', 'url', 'date', 'gid')):
                        raise ValueError('Steam article field missing')
                    result.append(article(source, row['title'], row['url'], row.get('contents', ''), int(row['date']), row['gid']))
            except Exception as error:
                result.errors['page-' + str(page) + '-row-' + str(index)] = fetch_error(error)
        if result.errors:
            # An invalid date cannot safely advance the archive cursor. Preserve
            # valid rows and make the incomplete page visible for the next run.
            return result
        if len(rows) < 100 or min(cursor_dates) < now - 14 * DAY:
            if not result and source['official']:
                raise ValueError('Empty official Steam feed')
            return result
        next_end = min(cursor_dates) - 1
        if next_end >= end:
            raise ValueError('Steam pagination did not advance')
        end = next_end
    raise ValueError('Steam backlog exceeds safe limit')


def medal_article(raw):
    """Read the article's own published date, never a date from related cards."""
    class Article(HTMLParser):
        def __init__(self):
            super().__init__()
            self.dates, self.display_dates, self.body = [], [], []
            self.info_depth = self.body_depth = 0
        def handle_starttag(self, tag, attrs):
            a = dict(attrs)
            if tag == 'meta' and a.get('property', a.get('name')) in ('article:published_time', 'datePublished'):
                self.dates.append(a.get('content', ''))
            if tag == 'div':
                classes = a.get('class', '').split()
                if self.info_depth: self.info_depth += 1
                elif 'post-info' in classes: self.info_depth = 1
                if self.body_depth: self.body_depth += 1
                elif 'post_rich-text' in classes: self.body_depth = 1
        def handle_endtag(self, tag):
            if tag == 'div':
                self.info_depth = max(0, self.info_depth - 1)
                self.body_depth = max(0, self.body_depth - 1)
            if self.body_depth and tag in ('p', 'li', 'h1', 'h2', 'h3'):
                self.body.append('\n')
        def handle_data(self, text):
            if self.info_depth: self.display_dates.append(text.strip())
            if self.body_depth: self.body.append(text)
    parsed = Article()
    parsed.feed(raw)
    dates = list(parsed.dates)
    # JSON-LD publication metadata is an alternative to Webflow's post-info date.
    dates.extend(re.findall(r'"datePublished"\s*:\s*"([^"]+)"', raw))
    for value in dates + parsed.display_dates:
        for fmt in (None, '%b %d, %Y', '%B %d, %Y'):
            try:
                day = datetime.fromisoformat(value.replace('Z', '+00:00')) if fmt is None else datetime.strptime(value, fmt)
                day = day if day.tzinfo else day.replace(tzinfo=timezone.utc)
                body = '\n'.join(parsed.body).strip()
                if not body:
                    raise ValueError('Medal article body missing')
                return int(day.timestamp()), body
            except (ValueError, TypeError):
                continue
    raise ValueError('Medal article publication date or body missing')


def parse_changelog(raw, source, url):
    # Annual pages are updated in place: identify individual dated sections.
    parts = re.split(r'<h1\b[^>]*>(.*?)</h1>', raw, flags=re.S)
    result = []
    for index in range(1, len(parts), 2):
        heading = re.sub(r'(\d)(?:st|nd|rd|th)\b', r'\1', clean(parts[index]))
        match = re.search(r'([A-Z][a-z]+ \d{1,2}, \d{4}) Changelog', heading)
        if not match:
            continue
        try:
            day = datetime.strptime(match[1], '%B %d, %Y')
        except ValueError:
            day = datetime.strptime(match[1], '%b %d, %Y')
        day = day.replace(tzinfo=timezone.utc)
        section = parts[index + 1]
        # Feature headings, not a generic annual changelog title, drive classification.
        headings = ' '.join(clean(h) for h in re.findall(r'<h[2-6]\b[^>]*>(.*?)</h[2-6]>', section, re.S))
        identity = url + '#' + day.strftime('%Y-%m-%d')
        result.append(article(source, 'Medal ' + headings, url, clean(section)[:1600], int(day.timestamp()), identity))
    if not result:
        raise ValueError('Medal changelog layout changed')
    return result


def classify_v1(item):
    title, body = item['title'].lower(), clean(item['body'])[:1600].lower()
    topic = item['topic']
    if re.search(r'\b(rumou?r|leak|speculation|weekly|roundup|recap|sale|discount|giveaway|survey|contest|esports|tournament)\b|how to|best .{0,30}(?:software|settings)|使い方|セール', title):
        return None
    if topic == 'windows' and not re.search(r'windows\s*11', title + ' ' + body):
        return None
    serious = re.search(r'boot loop|unbootable|blue screen|black screen|bsod|data loss|data corruption|actively exploited|major outage|widespread outage|重大.{0,10}(?:不具合|障害)', title)
    broken = re.search(r'(?:breaks?|kills?|disables?|stops? working|unusable|cannot|unable|crash|fail|broken|使用不能|接続不能)', title)
    function = re.search(r'audio|microphone|sound|desktop|taskbar|start menu|wsl|remote desktop|games?|recording|clips?|login|sign.in|voice|streaming|音声|マイク|録画', title)
    if serious or (broken and function):
        if topic != 'windows' or re.search(r'update|kb\d+|更新', title + ' ' + body):
            return '重大な不具合・利用への影響'
    if re.search(r'\b(hotfix|bugfix|minor|maintenance|beta|insider|preview)\b|小規模|メンテナンス', title):
        return None
    if topic in GAMES:
        for label, pattern in [
            ('新キャラクター・主要コンテンツ', r'new (?:killer|survivor|hero|legend|map|weapon)|new chapter|\bdlc\b|新キャラ|新キラー|新サバイバー'),
            ('コラボ', r'crossover|collaboration|\bcollab\b|\s[x×]\s|コラボ'),
            ('期間限定イベント', r'limited.time|collection event|anniversary|\bevent\b|期間限定|イベント'),
            ('大型更新・重要なゲーム変更', r'new season|season \d+|major update|overhaul|rework|ranked changes|ranked update|新シーズン|大型更新|大幅変更')]:
            if re.search(pattern, title):
                return label
        if re.search(r'introduc|announc|reveal|launch', title) and re.search(r'new (?:hero|legend|killer|survivor|map)|crossover', body[:600]):
            return '新キャラクター・主要コンテンツ'
        return None
    if re.search(r'end.{0,12}support|no longer support|system requirements|mandatory|age verification|privacy.{0,30}(?:change|new)|サポート終了|必須|仕様変更', title):
        return '重要な仕様・対応条件の変更'
    if topic == 'medal' and re.search(r'longer uploads|upload limits|recording limits|privacy and security|new.{0,12}overlay', title):
        return '録画・共有機能やプライバシーの重要変更'
    if topic == 'windows' and re.search(r'windows 11.{0,30}(?:\d{2}h[12]|feature update)', title) and re.search(r'available|roll|release|配信', title):
        return 'Windows大型機能更新'
    if re.search(r'introducing|launch|roll.{0,4}out|now available|new feature|major update|新機能|大型アップデート', title):
        if re.search(r'record|shar|famil|voice|video|stream|chat|overlay|security|privacy|accessib|backup|録画|共有|音声|セキュリティ', title + ' ' + body[:600]):
            return '主要機能の追加・変更'
    return None


def discord_body(raw):
    # Only article elements, excluding navigation and related-article cards.
    sections = re.findall(r'<article\b[^>]*>(.*?)</article>', raw, re.S | re.I)
    if not sections:
        raise ValueError('Discord article body missing')
    return '\n'.join(sections)


def discord_impacts(item):
    if item['topic'] != 'discord':
        return []
    raw = item['body']
    blocks = re.findall(r'<li\b[^>]*>(.*?)</li>', raw, re.S | re.I)
    if not blocks:
        blocks = re.split(r'(?<=[.!?])\s+|\n', clean(raw))
    impacts = []
    for block in blocks:
        text = clean(block).lower()
        if re.search(r'\b(?:not|never|rumour|rumor|might|could)\b.{0,35}(?:ship|launch|improv|increas|upgrad)', text):
            continue
        if re.search(r'windows|desktop', text) and re.search(r'cpu usage', text) and re.search(r'improv|reduc', text):
            values = [float(n) for n in re.findall(r'(\d+(?:\.\d+)?)\s*%', text)]
            if values and max(values) >= 10:
                impacts.append('デスクトップ版のCPU使用率に大きな改善（公式測定値。環境によって異なります）')
        if re.search(r'windows', text) and re.search(r'system[ -]wide echo cancellation', text) and re.search(r'shipped|introduced|launched|rolled out', text):
            impacts.append('Windowsでシステム全体を対象にしたエコーキャンセルを導入')
        if re.search(r'(?:free |file )?upload limit', text) and re.search(r'upgraded|increased|raised|doubled|reduced|lowered', text):
            match = re.search(r'from\s+(\d+\s*[mg]b)\s+to\s+(\d+\s*[mg]b)', text)
            if match:
                impacts.append('ファイルアップロード上限を ' + match[1].upper() + ' → ' + match[2].upper() + ' に変更')
    return list(dict.fromkeys(impacts))


def classify(item):
    """Require a concrete change and its impact in the same sentence/heading."""
    if discord_impacts(item):
        return 'PCでの通話・性能・ファイル共有の重要変更'
    old = classify_v1(item)
    if old:
        return old
    title = item['title'].lower()
    if re.search(r'\b(rumou?r|leak|speculation|roundup|recap|giveaway|survey|contest|esports|tournament)\b|how to|使い方', title):
        return None
    text = clean(item['body'])[:16000].lower()
    if item['topic'] == 'windows' and not re.search(r'windows\s*11', title + ' ' + text):
        return None
    segments = [title] + re.split(r'(?<=[.!?。])\s+|\n', text)
    for sentence in segments:
        sentence = sentence[:900]
        if re.search(r'\b(no |not |never |rumou?r|might|could|previously|last year)|既に終了', sentence):
            continue
        change = re.search(r'\b(introduc\w*|launch\w*|add\w*|releas\w*|roll\w*|now|new|will|remov\w*|replac\w*)\b|追加|実装|開催|変更', sentence)
        if re.search(r'boot loop|unbootable|data loss|data corruption|actively exploited|widespread outage|blue screen|bsod', sentence):
            if item['topic'] != 'windows' or re.search(r'update|kb\d+|更新', title + ' ' + sentence):
                return '重大な不具合・利用への影響'
        if item['topic'] in GAMES and change:
            if re.search(r'new (?:killer|survivor|hero|legend|map|chapter)|新(?:キャラ|キラー|マップ)', sentence):
                return '新キャラクター・主要コンテンツ'
            if re.search(r'crossover|collaboration|コラボ', sentence):
                return 'コラボ'
            if re.search(r'limited.time (?:event|mode)|collection event|期間限定', sentence):
                return '期間限定イベント'
            if re.search(r'matchmaking|ranked (?:system|scoring)|perk (?:system|rework)|overhaul|rework|大幅', sentence):
                return '大型更新・重要なゲーム変更'
        if item['topic'] not in GAMES:
            if re.search(r'end.{0,15}support|no longer supported|age verification.{0,40}required|mandatory.{0,40}(?:account|verification)|サポート終了', sentence):
                return '重要な仕様・対応条件の変更'
            if change and re.search(r'video editor|multi.track editing|family sharing|steam families|recording (?:format|limit)|upload limit|privacy (?:control|setting)|screen shar|voice chat|keyboard.{0,20}overlay', sentence):
                return '主要機能の追加・変更'
    return None








def brief_summary(item, label):
    points = discord_impacts(item)
    if points:
        return '\n'.join('・' + point for point in points[:3])
    # Extract source wording instead of inventing details or translating numbers.
    body = re.sub(r'<(?:script|style)\b[^>]*>.*?</(?:script|style)>', '', item['body'], flags=re.S | re.I)
    blocks = re.split(r'</(?:p|li|h[1-6])>|(?<=[.!?])\s+|(?<=。)|\n\s*\n', body)
    cues = r'new|introduc|launch|releas|chang|updat|increas|reduc|support|crash|outage|rework|event|追加|変更|不具合|開催|切り替|移行|アンチチート'
    eligible = [clean(block) for block in blocks
                if 25 <= len(clean(block)) <= 600 and re.search(cues + '|ヒーロー|レジェンド|シーズン|新マップ|開始|開幕|影響', clean(block), re.I)
                and not re.search(r'subscribe|cookie|read more|privacy policy|最近の記事', clean(block), re.I)
                and not re.fullmatch(r'https?://\S+', clean(block))]
    priority = r'new (?:hero|killer|survivor|legend|map)|新(?:ヒーロー|キラー|レジェンド|サバイバー|マップ)' if '新キャラクター' in label else cues
    if 'アンチチート' in label:
        priority = r'(?:anti[ -]?cheat|アンチチート).{0,100}(?:switch|migrat|replac|切り替|移行)|(?:switch|migrat|replac).{0,100}anti[ -]?cheat'
    ranked = sorted(enumerate(eligible), key=lambda pair: (not bool(re.search(priority, pair[1], re.I)), pair[0]))
    excerpt = '\n'.join(text for _, text in ranked[:2])
    if not excerpt:
        excerpt = clean(item['title'])
    excerpt = excerpt[:450] + ('…' if len(excerpt) > 450 else '')
    # Escape formatting supplied by third-party sources; mentions remain disallowed.
    excerpt = re.sub(r'([\\`*_~|])', r'\\\1', excerpt)
    return label + '\n原文の要点：' + excerpt




def translate_japanese(text):
    endpoint = 'https://api.mymemory.translated.net/get?' + urllib.parse.urlencode({'q': text, 'langpair': 'en|ja'})
    result = json.loads(request(endpoint))
    translated = html.unescape(result.get('responseData', {}).get('translatedText', '')).strip()
    if str(result.get('responseStatus')) != '200' or result.get('quotaFinished') or not re.search(r'[ぁ-んァ-ヶ一-龯]', translated):
        raise ValueError('Japanese translation unavailable; news remains unsent for next run')
    return translated


def japanese_title(item):
    if re.search(r'[ぁ-んァ-ヶ一-龯]', item['title']):
        return item['title']
    return translate_japanese(item['title'][:250])


def japanese_summary(item, label):
    summary = brief_summary(item, label)
    marker = '\n原文の要点：'
    if marker not in summary:
        return summary
    excerpt = summary.split(marker, 1)[1]
    if re.search(r'[ぁ-んァ-ヶ]', excerpt):
        return label + '\n' + excerpt
    # Public news text only; no account IDs, webhook URLs, or credentials.
    while len(excerpt.encode('utf-8')) > 480:
        excerpt = excerpt[:-1]
    translated = translate_japanese(excerpt)
    translated = re.sub(r'([\\`*_~|])', r'\\\1', translated)
    return label + '\n' + translated[:600] + '\n（自動翻訳）'





