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

from html.parser import HTMLParser

class PatchParser(HTMLParser):
    """Read the patch containers and stable date anchors on Blizzard's page."""
    def __init__(self):
        super().__init__()
        self.patches, self.previous = [], []
        self.current = None
        self.depth = 0
        self.in_title = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = attrs.get("class", "").split()
        href = attrs.get("href", "")
        if re.search(r"/patch-notes/live/\d{4}/\d{2}/?$", href):
            self.previous.append(href)
        if tag == "div" and "PatchNotes-patch" in classes:
            if self.current is not None:
                raise RuntimeError("Unexpected nested Blizzard patch container")
            self.current = {"anchor": "", "title": [], "text": []}
            self.depth = 1
        elif self.current is not None and tag == "div":
            self.depth += 1
        if self.current is not None:
            anchor = attrs.get("id", "")
            if re.fullmatch(r"patch-\d{4}-\d{2}-\d{2}(?:-\d+)?", anchor):
                self.current["anchor"] = anchor
            if tag == "h3" and "PatchNotes-patchTitle" in classes:
                self.in_title = True

    def handle_data(self, data):
        if self.current is not None:
            self.current["text"].append(data)
            if self.in_title:
                self.current["title"].append(data)

    def handle_endtag(self, tag):
        if tag == "h3":
            self.in_title = False
        if tag == "div" and self.current is not None:
            self.depth -= 1
            if self.depth == 0:
                if not self.current["anchor"] or not self.current["title"]:
                    raise RuntimeError("Incomplete Blizzard patch entry")
                self.patches.append(self.current)
                self.current = None

def fetch_patches(since):
    url = PATCH_URL
    visited, items = set(), {}
    for _ in range(12):
        if url in visited:
            raise RuntimeError("Blizzard archive pagination did not advance")
        visited.add(url)
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
        with urllib.request.urlopen(req, timeout=30) as response:
            markup = response.read().decode("utf-8")
        parser = PatchParser()
        parser.feed(markup)
        parser.close()
        if parser.current is not None or not parser.patches:
            raise RuntimeError("Blizzard patch page could not be parsed; history was preserved")
        for patch in parser.patches:
            day = datetime.strptime(patch["anchor"][6:16], "%Y-%m-%d").replace(tzinfo=timezone.utc)
            stamp = int(day.timestamp())
            if stamp > time.time():
                continue
            anchor = patch["anchor"]
            items[anchor] = {"gid": "blizzard-live:" + anchor, "date": stamp,
                "title": " ".join(patch["title"]).strip(), "contents": " ".join(patch["text"]),
                "feedname": PATCH_SOURCE,
                "url": "https://overwatch.blizzard.com/en-us/news/patch-notes/live/" + day.strftime("%Y/%m/") + "#" + anchor}
        if items and min(x["date"] for x in items.values()) <= since:
            return list(items.values())
        previous = []
        for href in parser.previous:
            target = urllib.parse.urljoin(url, href)
            parts = urllib.parse.urlsplit(target)
            if parts.scheme == "https" and parts.hostname == "overwatch.blizzard.com" and target not in visited:
                previous.append(target)
        if not previous:
            return list(items.values())
        url = sorted(previous, reverse=True)[0]
    raise RuntimeError("Blizzard patch backlog exceeds 12 pages; history was preserved")

UA="DiscordMonitor/2.0"
PATCH_SOURCE="blizzard_patch_notes"
PATCH_URL="https://overwatch.blizzard.com/en-us/news/patch-notes/"
BHVR="bhvr_patch_notes"
