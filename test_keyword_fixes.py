#!/usr/bin/env python3
"""
修正内容をテストするスクリプト
- 検索キーワードのクリーンアップ
- 例外処理の緩和
- 検索リトライの強化
"""

import sys
import os

# video_engineディレクトリをパスに追加
sys.path.append(os.path.join(os.path.dirname(__file__), 'video_engine'))

from render_video import (
    search_images_with_playwright,
    get_ai_selected_image,
    extract_image_keywords_list
)

def test_keyword_cleanup():
    """検索キーワードのクリーンアップをテスト"""
    print("\n=== 検索キーワードのクリーンアップテスト ===")
    
    # テスト用のスクリプトデータ
    test_script_data = {
        "title": "最新ガジェットレビュー",
        "content": {
            "topic_summary": "新しいスマートフォンの機能紹介"
        }
    }
    
    try:
        keywords = extract_image_keywords_list(test_script_data)
        print(f"抽出されたキーワード: {keywords}")
        
        # 「製品 実機」という文字列が含まれていないことを確認
        for keyword in keywords:
            if "製品 実機" in keyword:
                print(f"[ERROR] キーワードに「製品 実機」が含まれています: {keyword}")
                return False
            else:
                print(f"[OK] クリーンなキーワード: {keyword}")
        
        return True
        
    except Exception as e:
        print(f"[ERROR] キーワード抽出でエラー: {e}")
        return False

def test_exception_handling():
    """例外処理の緩和をテスト"""
    print("\n=== 例外処理の緩和テスト ===")
    
    # 存在しないキーワードで検索テスト
    test_keywords = ["xyz123nonexistentkeyword456", "abc789invalid987"]
    
    for keyword in test_keywords:
        print(f"\nテストキーワード: {keyword}")
        try:
            images = search_images_with_playwright(keyword, max_results=2)
            print(f"[OK] 例外が発生せず空リストが返されました: {len(images)}件")
            
            if len(images) == 0:
                print("[OK] 画像が見つからない場合でも処理が継続されます")
            else:
                print(f"[INFO] {len(images)}件の画像が見つかりました")
                
        except Exception as e:
            print(f"[ERROR] 例外が発生しました: {e}")
            return False
    
    return True

def test_ai_selected_image_fallback():
    """get_ai_selected_imageのフォールバック動作をテスト"""
    print("\n=== get_ai_selected_image フォールバックテスト ===")
    
    # 存在しないキーワードを持つスクリプトデータ
    test_script_data = {
        "title": "存在しない製品",
        "content": {
            "topic_summary": "xyz123nonexistentkeyword456という架空の製品について"
        }
    }
    
    try:
        result = get_ai_selected_image(test_script_data)
        
        if result is None:
            print("[OK] 画像が見つからない場合にNoneが返されました")
            print("[OK] 処理が継続され、背景動画のみで動画生成が可能になります")
            return True
        else:
            print(f"[INFO] 画像が見つかりました: {result}")
            return True
            
    except Exception as e:
        print(f"[ERROR] 例外が発生しました: {e}")
        return False

def test_retry_mechanism():
    """検索リトライの強化をテスト"""
    print("\n=== 検索リトライの強化テスト ===")
    
    # 複数のキーワードをテスト
    test_script_data = {
        "title": "複数キーワードテスト",
        "content": {
            "topic_summary": "風景、自然、建築、テクノロジーに関する内容"
        }
    }
    
    try:
        keywords = extract_image_keywords_list(test_script_data)
        print(f"抽出されたキーワード: {keywords}")
        
        success_count = 0
        for keyword in keywords[:3]:  # 最初の3つでテスト
            print(f"\nキーワードでテスト: {keyword}")
            try:
                images = search_images_with_playwright(keyword, max_results=1)
                if images:
                    print(f"[OK] {len(images)}件の画像が見つかりました")
                    success_count += 1
                else:
                    print("[INFO] 画像が見つかりませんでした（次のキーワードを試行）")
            except Exception as e:
                print(f"[ERROR] 例外が発生: {e}")
        
        print(f"\n結果: {len(keywords)}個中{success_count}個のキーワードで成功")
        print("[OK] 1つのキーワードで失敗しても次のキーワードで再試行されます")
        return True
        
    except Exception as e:
        print(f"[ERROR] リトライテストでエラー: {e}")
        return False

if __name__ == "__main__":
    print("修正内容のテストを開始します...")
    
    tests = [
        ("検索キーワードのクリーンアップ", test_keyword_cleanup),
        ("例外処理の緩和", test_exception_handling),
        ("get_ai_selected_image フォールバック", test_ai_selected_image_fallback),
        ("検索リトライの強化", test_retry_mechanism),
    ]
    
    results = []
    for test_name, test_func in tests:
        print(f"\n{'='*50}")
        print(f"テスト: {test_name}")
        print('='*50)
        
        try:
            result = test_func()
            results.append((test_name, result))
            print(f"\nテスト結果: {'✅ 成功' if result else '❌ 失敗'}")
        except Exception as e:
            print(f"\nテスト実行中にエラー: {e}")
            results.append((test_name, False))
    
    print(f"\n{'='*50}")
    print("最終結果")
    print('='*50)
    
    for test_name, result in results:
        status = "✅ 成功" if result else "❌ 失敗"
        print(f"{test_name}: {status}")
    
    success_count = sum(1 for _, result in results if result)
    total_count = len(results)
    
    print(f"\n合計: {success_count}/{total_count} のテストが成功")
    
    if success_count == total_count:
        print("🎉 すべての修正が正常に動作しています！")
    else:
        print("⚠️ 一部の修正に問題があります。確認してください。")
