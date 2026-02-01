#!/usr/bin/env python3
"""
画像取得エラーハンドリングのテスト
"""

import sys
import os
sys.path.append('.')

from render_video import get_ai_selected_image, search_images_with_playwright

def test_error_handling():
    """画像取得エラーが適切に発生するかテスト"""
    
    print("=== 画像取得エラーハンドリングテスト ===")
    
    # テスト用のダミースクリプトデータ
    test_script_data = {
        "title": "テスト動画",
        "content": {
            "topic_summary": "AI技術についての解説",
            "script_parts": [
                {"text": "これはテストです", "speaker_id": 1}
            ]
        }
    }
    
    # 1. get_ai_selected_imageのテスト
    print("\n--- get_ai_selected_image テスト ---")
    try:
        result = get_ai_selected_image(test_script_data)
        if result:
            print(f"✅ 画像取得成功: {result}")
        else:
            print("❌ 画像取得失敗: Noneが返された")
    except RuntimeError as e:
        print(f"✅ 適切なエラー発生: {e}")
    except Exception as e:
        print(f"❌ 予期しないエラー: {e}")
    
    # 2. search_images_with_playwrightのテスト（無効なキーワード）
    print("\n--- 無効なキーワードでの検索テスト ---")
    try:
        images = search_images_with_playwright("無効なキーワードxyz123", max_results=1)
        if images:
            print(f"✅ 画像検索成功: {len(images)}枚")
        else:
            print("❌ 画像検索失敗: 空リストが返された")
    except Exception as e:
        print(f"❌ 画像検索で予期しないエラー: {e}")

if __name__ == "__main__":
    test_error_handling()
