#!/usr/bin/env python3
"""
Test RSS sources in rss_sources.json for reachability and parsable entries.
"""
import json
import os
import sys
import time
from typing import List

import requests
import feedparser

ROOT = os.path.dirname(os.path.abspath(__file__))
RSS_PATH = os.path.join(ROOT, "rss_sources.json")


def load_sources() -> dict:
    with open(RSS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def test_feed(url: str, timeout: int = 10) -> dict:
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    }
    try:
        resp = requests.get(url, headers=headers, timeout=timeout)
        status = resp.status_code
        ok = 200 <= status < 300
        if not ok:
            return {"url": url, "ok": False, "status": status, "error": f"HTTP {status}"}
        feed = feedparser.parse(resp.content)
        entries = feed.entries or []
        sample_link = entries[0].get("link") if entries else None
        return {
            "url": url,
            "ok": True,
            "status": status,
            "entries": len(entries),
            "sample_link": sample_link,
        }
    except Exception as e:
        return {"url": url, "ok": False, "status": None, "error": str(e)}


def run_group(name: str, urls: List[str]) -> bool:
    print(f"\n=== {name} ({len(urls)}) ===")
    all_ok = True
    for url in urls:
        result = test_feed(url)
        if result.get("ok"):
            print(f"[OK] {url} | status={result['status']} entries={result['entries']} sample={result['sample_link']}")
        else:
            all_ok = False
            print(f"[FAIL] {url} | error={result.get('error')}")
        time.sleep(0.5)
    return all_ok


def main() -> int:
    if not os.path.exists(RSS_PATH):
        print(f"rss_sources.json not found: {RSS_PATH}")
        return 1
    data = load_sources()
    group_a = data.get("group_a", [])
    group_b = data.get("group_b", [])
    ok_a = run_group("group_a", group_a)
    ok_b = run_group("group_b", group_b)
    if ok_a and ok_b:
        print("\nAll feeds OK")
        return 0
    print("\nSome feeds failed")
    return 2


if __name__ == "__main__":
    sys.exit(main())
