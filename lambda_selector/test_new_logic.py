#!/usr/bin/env python3
"""
新しいlambda_selectorロジックのテストスクリプト
"""

import os
import sys
import json
from unittest.mock import Mock, patch
from datetime import datetime, timezone, timedelta

# 現在のディレクトリをパスに追加
current_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.append(current_dir)

def test_filter_logic():
    """フィルタリングロジックのテスト"""
    from lambda_function import filter_and_collect_candidates, BASE_SCORE_THRESHOLD, STOCK_DAYS
    
    # モック記事データ
    test_articles = [
        {"url": "https://example.com/new1", "title": "新着記事1"},
        {"url": "https://example.com/selected", "title": "既選択記事"},
        {"url": "https://example.com/low_score", "title": "低スコア記事"},
        {"url": "https://example.com/high_score_old", "title": "高スコア古い記事"},
        {"url": "https://example.com/high_score_recent", "title": "高スコア新しい記事"},
        {"url": "https://example.com/new2", "title": "新着記事2"},
    ]
    
    # モックDynamoDBレスポンス
    mock_db_responses = {
        "https://example.com/new1": None,  # 新着
        "https://example.com/selected": {
            "status": "selected", "score": 80.0, "processed_at": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        },
        "https://example.com/low_score": {
            "status": "evaluated", "score": 50.0, "processed_at": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        },
        "https://example.com/high_score_old": {
            "status": "evaluated", "score": 85.0, "processed_at": (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        },
        "https://example.com/high_score_recent": {
            "status": "evaluated", "score": 90.0, "processed_at": (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
        },
        "https://example.com/new2": None,  # 新着
    }
    
    def mock_get_article_info(url):
        return mock_db_responses.get(url)
    
    # モックを適用
    with patch('lambda_function.get_article_info', side_effect=mock_get_article_info):
        new_articles, stock_candidates = filter_and_collect_candidates(test_articles)
    
    print("=== フィルタリングロジックテスト結果 ===")
    print(f"新着記事: {len(new_articles)}件")
    for article in new_articles:
        print(f"  - {article['title']}")
    
    print(f"ストック候補: {len(stock_candidates)}件")
    for article in stock_candidates:
        print(f"  - {article['title']} ({article.get('score', 0)}点)")
    
    # 検証
    assert len(new_articles) == 2, f"新着記事が2件であるべき: {len(new_articles)}"
    assert len(stock_candidates) == 1, f"ストック候補が1件であるべき: {len(stock_candidates)}"
    
    print("✅ テスト成功")

def test_score_evaluation():
    """スコア評価ロジックのテスト"""
    from lambda_function import evaluate_article_with_gemini
    
    # モック記事
    test_article = {
        "title": "iPhone 16発表！新機能がすごい",
        "topic_summary": "Appleが新しいiPhone 16を発表しました。カメラ性能が大幅に向上し、AI機能が強化されました。"
    }
    
    # モックGeminiレスポンス
    mock_response = '{"score": 85}'
    
    with patch('lambda_function.call_gemini_generate_content', return_value=mock_response):
        score = evaluate_article_with_gemini(test_article)
    
    print("=== スコア評価テスト結果 ===")
    print(f"評価スコア: {score}")
    
    assert score == 85.0, f"スコアが85.0であるべき: {score}"
    print("✅ テスト成功")

def test_selection_logic():
    """選出ロジックのテスト"""
    from lambda_function import select_best_article
    
    # モック候補記事
    candidates = [
        {"title": "記事A", "url": "https://example.com/a", "score": 75.0},
        {"title": "記事B", "url": "https://example.com/b", "score": 92.0},
        {"title": "記事C", "url": "https://example.com/c", "score": 68.0},
    ]
    
    # モックDynamoDB保存
    mock_saved = []
    
    def mock_mark_selected(url, title, status):
        mock_saved.append({"url": url, "title": title, "status": status})
    
    with patch('lambda_function.mark_url_processed', side_effect=mock_mark_selected):
        selected = select_best_article(candidates)
    
    print("=== 選出ロジックテスト結果 ===")
    print(f"選択された記事: {selected['title']} ({selected['score']}点)")
    
    assert selected['title'] == "記事B", f"最高スコアの記事Bが選択されるべき: {selected['title']}"
    assert len(mock_saved) == 1, "1件の記事が保存されるべき"
    assert mock_saved[0]['status'] == "selected", "statusがselectedであるべき"
    
    print("✅ テスト成功")

def main():
    print("=== Lambda Selector 新ロジックテスト開始 ===\n")
    
    # 環境変数をモック
    os.environ.setdefault("DYNAMODB_TABLE", "test-table")
    os.environ.setdefault("S3_BUCKET", "test-bucket")
    os.environ.setdefault("GEMINI_API_KEY", "test-key")
    
    try:
        test_filter_logic()
        print()
        test_score_evaluation()
        print()
        test_selection_logic()
        print("\n🎉 すべてのテストが成功しました！")
        
    except Exception as e:
        print(f"\n❌ テスト失敗: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    main()
