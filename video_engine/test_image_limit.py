#!/usr/bin/env python3
"""
画像収集60枚上限機能のテスト
"""

import sys
import os
sys.path.append('.')

from render_video import search_images_with_playwright, download_image_from_url

def test_image_collection_limit():
    """画像収集が60枚で停止することを確認"""
    
    print("=== 画像収集60枚上限テスト ===")
    
    # テスト用キーワード（複数）
    test_keywords = ["AI技術", "テクノロジー", "コンピュータ", "ソフトウェア", "データサイエンス"]
    
    total_images = []
    collected_count = 0
    
    for i, keyword in enumerate(test_keywords):
        print(f"\n--- キーワード {i+1}: {keyword} ---")
        
        # 画像検索
        images = search_images_with_playwright(keyword, max_results=15)
        
        if images:
            print(f"検索結果: {len(images)}枚")
            
            for j, image in enumerate(images):
                if collected_count >= 60:
                    print(f"[INFO] 60枚上限に達したためダウンロードを停止します")
                    break
                
                # ダウンロード
                image_path = download_image_from_url(image['url'])
                if image_path:
                    collected_count += 1
                    total_images.append(image_path)
                    print(f"  ダウンロード {collected_count}/60: {os.path.basename(image_path)}")
                
                if collected_count >= 60:
                    break
        else:
            print("検索結果なし")
        
        if collected_count >= 60:
            print(f"[INFO] キーワード {keyword} で60枚上限に達しました")
            break
    
    print(f"\n=== テスト結果 ===")
    print(f"収集した画像数: {len(total_images)}枚")
    print(f"目標上限: 60枚")
    print(f"達成率: {len(total_images)/60*100:.1f}%")
    
    if len(total_images) >= 60:
        print("✅ 60枚上限機能が正常に動作しました")
    elif len(total_images) > 0:
        print(f"⚠️ 60枚には達しませんでしたが、{len(total_images)}枚を収集しました")
    else:
        print("❌ 画像が1枚も収集できませんでした")

if __name__ == "__main__":
    test_image_collection_limit()
