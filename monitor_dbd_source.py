import base64

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

from datetime import datetime, timezone

def fetch_bhvr(since):
    # Public, unauthenticated Vanilla API; follow its pagination metadata.
    url = "https://forums.bhvr.com/api/v2/articles?limit=100"
    result, visited = [], set()
    for _ in range(15):
        p = urllib.parse.urlsplit(url)
        if p.scheme != "https" or p.netloc != "forums.bhvr.com" or p.path != "/api/v2/articles" or url in visited:
            raise RuntimeError("Unexpected BHVR pagination")
        visited.add(url)
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=30) as response:
            page = json.load(response)
            next_url = response.headers.get("x-app-page-next-url")
        if not isinstance(page, list):
            raise RuntimeError("Unexpected BHVR response")
        for row in page:
            if row.get("knowledgeBaseID") != 1 or row.get("status") != "published" or row.get("locale") != "en":
                continue
            article_url = row["url"]
            if not article_url.startswith("https://forums.bhvr.com/dead-by-daylight/kb/articles/"):
                raise RuntimeError("Unexpected BHVR article URL")
            published = int(datetime.fromisoformat(row["dateInserted"]).timestamp())
            contents = ""
            if published >= since:
                endpoint = "https://forums.bhvr.com/api/v2/articles/" + str(int(row["articleID"]))
                with urllib.request.urlopen(urllib.request.Request(endpoint, headers={"User-Agent": UA}), timeout=30) as response:
                    details = json.load(response)
                contents = details.get("body", "")
                if not contents:
                    raise RuntimeError("BHVR article body missing")
            item = dict(gid="bhvr:" + str(row["articleID"]), title=row["name"],
                url=article_url, date=published, contents=contents, feedname=BHVR)
            if patch_channel(item) and published <= int(time.time()):
                result.append(item)
        if not next_url:
            if not result:
                raise RuntimeError("BHVR patch notes are empty")
            return result
        url = urllib.parse.urljoin(url, next_url)
    raise RuntimeError("BHVR pagination limit exceeded")

def plain(value):
    value = re.sub(r"<[^>]+>|\[[^\]]+\]", " ", value)
    return re.sub(r"\s+", " ", html.unescape(value)).strip()

def patch_channel(item):
    if item.get("feedname") not in ("steam_community_announcements", BHVR):
        return None
    # Keep [PTB] in titles; plain() removes bracketed formatting.
    title = html.unescape(re.sub(r"<[^>]+>", " ", item["title"])).lower()
    patch = re.search(r"\b(patch(?:\s+notes?)?|hot[ -]?fix|bug[ -]?fix)\b|パッチノート|ホットフィックス|バグ修正", title)
    version_title = re.match(r"\s*(?:\[?(?:ptb|live)\]?\s*[-|:]?\s*)?\d+\.\d+\.\d+\b", title)
    if not (patch or version_title):
        return None
    if re.search(r"\b(ptb|public test build)\b|公開テスト", title):
        return "PTB"
    return "通常サーバー"

UA="DiscordMonitor/2.0"
PATCH_SOURCE="blizzard_patch_notes"
PATCH_URL="https://overwatch.blizzard.com/en-us/news/patch-notes/"
BHVR="bhvr_patch_notes"
