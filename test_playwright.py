#!/usr/bin/env python3
"""
Playwright Async API テストスクリプト
"""

import asyncio
import sys
import os

# render_video.pyのモジュールをインポート
sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'video_engine'))

from render_video import search_images_with_playwright

async def test_image_search():
    """画像検索のテスト"""
    print("=== Playwright Async API テスト開始 ===")
    
    # テスト用のキーワード
    test_keyword = "Grace CPU"
    
    try:
        print(f"検索キーワード: {test_keyword}")
        print("デバッグ情報を詳細に出力します...")
        
        # 標準出力をキャプチャしてデバッグ情報を取得
        import sys
        from io import StringIO
        
        # 一時的にstdoutをリダイレクト
        old_stdout = sys.stdout
        sys.stdout = captured_output = StringIO()
        
        try:
            images = await search_images_with_playwright(test_keyword, max_results=3)
        finally:
            # stdoutを元に戻す
            sys.stdout = old_stdout
            debug_output = captured_output.getvalue()
        
        # デバッグ情報を表示
        print("\n=== デバッグ情報 ===")
        for line in debug_output.split('\n'):
            if line.strip():
                print(f"[DEBUG] {line}")
        
        if images:
            print(f"\n✅ 成功: {len(images)}枚の画像URLを取得")
            for i, img in enumerate(images, 1):
                print(f"  画像{i}: {img['url'][:100]}...")
                print(f"    ソース: {img.get('source', 'unknown')}")
                print(f"    alt: {img.get('alt', '')[:50]}...")
        else:
            print("\n❌ 失敗: 画像が見つかりませんでした")
            print("\n=== 考えられる原因 ===")
            if "Bing may be blocking us" in debug_output:
                print("- Bingにブロックされています")
            if "image elements with metadata" in debug_output:
                print("- メタデータ要素は見つかりました")
            if "Bing extraction returned no results" in debug_output:
                print("- Bing extractionが機能していません")
            if "FALLBACK" in debug_output:
                print("- フォールバック検索が実行されました")
            if "SUCCESS" in debug_output:
                print("- 画像抽出に成功しましたが、フィルタリングされました")
            if "JSON metadata" in debug_output:
                print("- JSONメタデータ抽出を試みました")
            
    except Exception as e:
        print(f"\n❌ エラー: {e}")
        import traceback
        traceback.print_exc()
    
    print("\n=== テスト完了 ===")

if __name__ == "__main__":
    asyncio.run(test_image_search())
