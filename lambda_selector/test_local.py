#!/usr/bin/env python3
"""
ローカル環境で lambda_selector をテストするためのスクリプト
"""

import os
import sys
import json
from unittest.mock import Mock, patch
from datetime import datetime, timezone

# 現在のディレクトリをパスに追加
current_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.append(current_dir)

# モックのイベントとコンテキスト
def create_mock_event():
    return {}

def create_mock_context():
    """モックのLambdaコンテキストを作成"""
    context = Mock()
    context.get_remaining_time_in_millis = Mock(return_value=300000)  # 5分
    context.function_name = "test_lambda_selector"
    context.function_version = "$LATEST"
    context.invoked_function_arn = "arn:aws:lambda:us-east-1:123456789012:function:test_lambda_selector"
    context.memory_limit_in_mb = 512
    context.aws_request_id = "test-request-id"
    context.log_group_name = "/aws/lambda/test_lambda_selector"
    context.log_stream_name = "2023/01/01/[$LATEST]test-stream"
    return context

def mock_dynamodb_operations():
    """DynamoDB操作をモック化してAWS通信を回避"""
    
    def mock_get_item(Key=None, ProjectionExpression=None, ExpressionAttributeNames=None):
        # 常に「未処理」を返すモック
        return {}
    
    def mock_put_item(Item=None):
        print(f"Mock DynamoDB put_item: {Item.get('url', 'unknown')}")
        pass
    
    return mock_get_item, mock_put_item

def mock_s3_operations():
    """S3操作をモック化してAWS通信を回避"""
    
    def mock_put_object(Bucket=None, Key=None, Body=None, ContentType=None):
        print(f"Mock S3 put_object: s3://{Bucket}/{Key}")
        pass
    
    return mock_put_object

def main():
    print("=== Lambda Selector ローカルテスト開始 ===")
    
    # 環境変数のチェック
    required_env_vars = ["GEMINI_API_KEY", "DYNAMODB_TABLE", "S3_BUCKET"]
    missing_vars = [var for var in required_env_vars if not os.environ.get(var)]
    
    if missing_vars:
        print(f"❌ 以下の環境変数が設定されていません: {', '.join(missing_vars)}")
        print("以下のコマンドで環境変数を設定してください：")
        print('export GEMINI_API_KEY="your-api-key"')
        print('export DYNAMODB_TABLE="youtube-processed-urls"')
        print('export S3_BUCKET="youtube-auto-3"')
        return
    
    print("✅ 環境変数が設定されています")
    
    try:
        # lambda_functionをインポート
        from lambda_function import lambda_handler
        
        # モックを適用
        mock_get_item, mock_put_item = mock_dynamodb_operations()
        mock_put_object = mock_s3_operations()
        
        with patch('lambda_function.ddb_table.get_item', side_effect=mock_get_item), \
             patch('lambda_function.ddb_table.put_item', side_effect=mock_put_item), \
             patch('lambda_function.s3_client.put_object', side_effect=mock_put_object):
            
            # テスト実行
            print("\n🚀 Lambdaハンドラーを実行します...")
            event = create_mock_event()
            context = create_mock_context()
            
            result = lambda_handler(event, context)
            
            print("\n=== 実行結果 ===")
            print(json.dumps(result, indent=2, ensure_ascii=False))
            
            if result.get("status") == "ok":
                print("\n✅ テスト成功！記事が正常に選定されました")
                print(f"Pendingキー: {result.get('pending_key')}")
                print(f"選定URL: {result.get('url')}")
            else:
                print(f"\n⚠️ テスト完了（ステータス: {result.get('status')}）")
    
    except Exception as e:
        print(f"\n❌ エラーが発生しました: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    main()
