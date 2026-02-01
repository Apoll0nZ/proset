#!/usr/bin/env python3
"""
最も簡単なテスト：直接画像URLをダウンロードして確認
"""

import requests
import tempfile
import os

def test_direct_download():
    """直接画像URLをダウンロードするテスト"""
    
    # GoogleのサムネイルURL（テスト用）
    test_urls = [
        "https://encrypted-tbn0.gstatic.com/images?q=tbn:ANd9GcRM3YuUIdJxGTT8fAal5pI8S0lnrxyh7AoWAPuotvnTHhPM",
        "https://picsum.photos/200/300",  # フォールバック用
    ]
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    }
    
    for i, url in enumerate(test_urls):
        print(f"[TEST] URL {i+1}: {url}")
        try:
            response = requests.get(url, timeout=10, headers=headers)
            print(f"[TEST] Status: {response.status_code}")
            print(f"[TEST] Content-Type: {response.headers.get('Content-Type')}")
            
            if response.status_code == 200:
                # 一時ファイルに保存
                temp_path = os.path.join(tempfile.gettempdir(), f"test_image_{i}.jpg")
                with open(temp_path, 'wb') as f:
                    f.write(response.content)
                
                size = os.path.getsize(temp_path)
                print(f"[TEST] SUCCESS: 保存しました {temp_path} ({size} bytes)")
                return temp_path
            else:
                print(f"[TEST] FAILED: HTTP {response.status_code}")
                
        except Exception as e:
            print(f"[TEST] ERROR: {e}")
    
    return None

if __name__ == "__main__":
    result = test_direct_download()
    if result:
        print("[RESULT] OK - 画像ダウンロード成功")
    else:
        print("[RESULT] FAILED - すべてのURLで失敗")
