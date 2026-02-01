#!/usr/bin/env python3
"""
render_video.pyの画像取得機能をテストするスクリプト
"""

import os
import sys
import tempfile

# video_engineモジュールをインポートするためのパス設定
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

def test_render_video_image_functions():
    """render_video.pyの画像取得関数をテスト"""
    try:
        from render_video import search_images_with_playwright, download_image_from_url
        
        print("=" * 60)
        print("render_video.py 画像取得機能テスト")
        print("=" * 60)
        
        # テスト用キーワード
        test_keyword = "AI技術"
        print(f"テストキーワード: {test_keyword}")
        
        # 画像検索テスト
        print("\n--- 画像検索テスト ---")
        images = search_images_with_playwright(test_keyword, max_results=2)
        
        if images:
            print(f"[SUCCESS] {len(images)}個の画像が見つかりました")
            
            # 画像ダウンロードテスト
            for i, image_info in enumerate(images):
                print(f"\n--- 画像 {i+1} ダウンロードテスト ---")
                print(f"URL: {image_info['url']}")
                print(f"タイトル: {image_info['title']}")
                if 'is_google_thumbnail' in image_info:
                    print(f"Googleサムネイル: {image_info['is_google_thumbnail']}")
                
                downloaded_path = download_image_from_url(image_info['url'])
                
                if downloaded_path:
                    print(f"[SUCCESS] ダウンロード成功: {downloaded_path}")
                    # ファイルサイズを確認
                    if os.path.exists(downloaded_path):
                        size = os.path.getsize(downloaded_path)
                        print(f"ファイルサイズ: {size} bytes")
                else:
                    print(f"[FAIL] ダウンロード失敗")
        else:
            print("[FAIL] 画像が見つかりませんでした")
            
    except ImportError as e:
        print(f"[ERROR] モジュールインポート失敗: {e}")
    except Exception as e:
        print(f"[ERROR] テスト実行中のエラー: {e}")
        print(f"[ERROR] エラータイプ: {type(e).__name__}")

if __name__ == "__main__":
    test_render_video_image_functions()
