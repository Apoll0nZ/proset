#!/usr/bin/env python3
"""
テストスクリプト：修正された画像検索・ダウンロード機能の検証
"""

import sys
import os
sys.path.append('video_engine')

from render_video import search_images_with_playwright, download_image_from_url

def test_image_search():
    """画像検索機能のテスト"""
    print("=== 画像検索テスト ===")
    
    # テストキーワード
    keyword = "風景 自然"
    
    try:
        images = search_images_with_playwright(keyword, max_results=3)
        
        print(f"検索結果: {len(images)}件")
        
        for i, img in enumerate(images):
            url = img.get('url', '')
            print(f"\n画像 {i+1}:")
            print(f"  URL: {url[:100]}...")
            print(f"  gstatic.comを含む: {'gstatic.com' in url}")
            print(f"  encrypted-tbnを含む: {'encrypted-tbn' in url}")
            
            # ダウンロードテスト
            print("  ダウンロードテスト中...")
            downloaded_path = download_image_from_url(url)
            
            if downloaded_path:
                file_size = os.path.getsize(downloaded_path) if os.path.exists(downloaded_path) else 0
                print(f"  ダウンロード成功: {downloaded_path}")
                print(f"  ファイルサイズ: {file_size} bytes ({file_size/1024:.1f} KB)")
                
                # クリーンアップ
                if os.path.exists(downloaded_path):
                    os.remove(downloaded_path)
                    print("  テストファイルを削除しました")
            else:
                print("  ダウンロード失敗または拒否")
                
    except Exception as e:
        print(f"エラー: {e}")

def test_download_validation():
    """ダウンロードバリデーションのテスト"""
    print("\n=== ダウンロードバリデーションテスト ===")
    
    # テスト用のURL（実際に存在する画像URL）
    test_urls = [
        "https://encrypted-tbn0.gstatic.com/images?q=tbn:example",  # 拒否されるべき
        "https://picsum.photos/800/600",  # 許可されるべき（ランダム画像）
        "https://via.placeholder.com/800x600.png/0000FF/FFFFFF?text=Test",  # 許可されるべき（プレースホルダー画像）
    ]
    
    for url in test_urls:
        print(f"\nテストURL: {url}")
        result = download_image_from_url(url)
        print(f"結果: {'拒否' if result is None else '許可'}")
        
        if result:
            # ダウンロード成功の場合、ファイルサイズを確認
            try:
                file_size = os.path.getsize(result)
                print(f"  ファイルサイズ: {file_size} bytes ({file_size/1024:.1f} KB)")
                # クリーンアップ
                if os.path.exists(result):
                    os.remove(result)
                    print("  テストファイルを削除しました")
            except Exception as e:
                print(f"  ファイル確認エラー: {e}")

if __name__ == "__main__":
    test_image_search()
    test_download_validation()
