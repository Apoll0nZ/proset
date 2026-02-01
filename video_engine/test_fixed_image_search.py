#!/usr/bin/env python3
"""
修正した画像検索機能のテスト
"""

import sys
import os
sys.path.append('.')

from render_video import search_images_with_playwright, download_image_from_url

def test_fixed_search():
    keyword = "AI技術"
    print(f"=== 修正後画像検索テスト: {keyword} ===")
    
    # 画像検索
    images = search_images_with_playwright(keyword, max_results=2)
    
    if images:
        print(f"✅ 成功: {len(images)}個の画像が見つかりました")
        
        for i, img in enumerate(images):
            print(f"\n画像 {i+1}:")
            print(f"  URL: {img['url']}")
            print(f"  タイトル: {img['title']}")
            print(f"  種類: {'Fallback' if img.get('is_fallback') else 'Google'}")
            
            # ダウンロードテスト
            try:
                result = download_image_from_url(img['url'])
                if result:
                    size = os.path.getsize(result)
                    print(f"  ✅ ダウンロード成功: {size} bytes")
                else:
                    print(f"  ❌ ダウンロード失敗")
            except Exception as e:
                print(f"  ❌ ダウンロードエラー: {e}")
    else:
        print("❌ 失敗: 画像が見つかりませんでした")

if __name__ == "__main__":
    test_fixed_search()
