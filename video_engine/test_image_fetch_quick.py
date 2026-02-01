#!/usr/bin/env python3
"""
短時間でPlaywrightの画像取得が動作するか確認する簡易テスト。
- 1キーワードのみ
- DOM読み込みのみ待機
- 1枚取得できたら即終了
"""

import os
import tempfile
import hashlib
import requests
from urllib.parse import urlparse


def quick_fetch_image(keyword: str = "AI技術") -> str:
    from playwright.sync_api import sync_playwright

    print(f"[QUICK] keyword={keyword}")
    search_url = f"https://www.google.com/search?q={keyword}&tbm=isch"
    print(f"[QUICK] url={search_url}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(search_url, wait_until="domcontentloaded", timeout=5000)
        page.wait_for_timeout(300)

        image_elements = page.query_selector_all('img[src]')
        print(f"[QUICK] img_count={len(image_elements)}")

        target_url = None
        for img in image_elements:
            src = img.get_attribute('src')
            if not src or not src.startswith('http'):
                continue
            if 'base64' in src:
                continue
            if 'encrypted-tbn0.gstatic.com' in src:
                target_url = src
                break

        browser.close()

    if not target_url:
        raise RuntimeError("No image url found (encrypted-tbn0) in quick test")

    print(f"[QUICK] download_url={target_url}")

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
    }
    response = requests.get(target_url, timeout=10, headers=headers)
    response.raise_for_status()

    ext = os.path.splitext(urlparse(target_url).path)[1].lower() or ".jpg"
    filename = f"quick_image_{hashlib.md5(target_url.encode()).hexdigest()[:8]}{ext}"
    local_path = os.path.join(tempfile.gettempdir(), filename)

    with open(local_path, "wb") as f:
        f.write(response.content)

    print(f"[QUICK] saved={local_path} size={os.path.getsize(local_path)}")
    return local_path


if __name__ == "__main__":
    try:
        quick_fetch_image()
        print("[QUICK] OK")
    except Exception as e:
        print(f"[QUICK] FAILED: {e}")
