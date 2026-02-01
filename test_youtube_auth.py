#!/usr/bin/env python3
"""
YouTube認証情報テストスクリプト
"""

import os
import json

def test_youtube_credentials():
    """YouTube認証情報をテスト"""
    
    # 環境変数から認証情報を取得
    token_json_str = os.environ.get("YOUTUBE_TOKEN_JSON", "")
    client_secrets_json_str = os.environ.get("YOUTUBE_CLIENT_SECRETS_JSON", "")
    
    print("=== YouTube認証情報テスト ===")
    print(f"YOUTUBE_TOKEN_JSON: {'✅ 設定済み' if token_json_str else '❌ 未設定'}")
    print(f"YOUTUBE_CLIENT_SECRETS_JSON: {'✅ 設定済み' if client_secrets_json_str else '❌ 未設定'}")
    print()
    
    if not token_json_str or not client_secrets_json_str:
        print("❌ 環境変数が設定されていません")
        return False
    
    try:
        # JSONのパーステスト
        token_data = json.loads(token_json_str)
        client_secrets = json.loads(client_secrets_json_str)
        
        print("✅ JSONパース成功")
        
        # 必須フィールドのチェック
        required_token_fields = ["token", "refresh_token", "token_uri", "client_id", "client_secret"]
        missing_token_fields = [field for field in required_token_fields if field not in token_data]
        
        if missing_token_fields:
            print(f"❌ トークンに必須フィールドが不足: {missing_token_fields}")
            return False
        
        print("✅ トークン必須フィールドOK")
        
        # client_secretsの構造チェック
        if "installed" in client_secrets:
            client_data = client_secrets["installed"]
            required_client_fields = ["client_id", "client_secret", "auth_uri", "token_uri"]
            missing_client_fields = [field for field in required_client_fields if field not in client_data]
            
            if missing_client_fields:
                print(f"❌ クライアントシークレットに必須フィールドが不足: {missing_client_fields}")
                return False
            
            print("✅ クライアントシークレット必須フィールドOK")
        
        # トークン有効期限チェック
        import time
        expires_at = token_data.get("expires_at", 0)
        current_time = time.time()
        
        if expires_at > current_time:
            remaining_time = expires_at - current_time
            print(f"✅ トークン有効 (残り{remaining_time/3600:.1f}時間)")
        else:
            print("⚠️ トークン有効期限切れ（リフレッシュが必要）")
        
        # YouTube API接続テスト
        try:
            from google.oauth2.credentials import Credentials
            from googleapiclient.discovery import build
            
            credentials = Credentials(
                token=token_data["token"],
                refresh_token=token_data["refresh_token"],
                token_uri=token_data["token_uri"],
                client_id=client_data["client_id"],
                client_secret=client_data["client_secret"],
                scopes=['https://www.googleapis.com/auth/youtube.upload']
            )
            
            # YouTube APIクライアント構築テスト
            youtube = build("youtube", "v3", credentials=credentials)
            
            # チャンネル情報取得テスト
            request = youtube.channels().list(part="snippet", mine=True)
            response = request.execute()
            
            if "items" in response and response["items"]:
                channel_title = response["items"][0]["snippet"]["title"]
                print(f"✅ YouTube API接続成功 (チャンネル: {channel_title})")
                return True
            else:
                print("⚠️ YouTube API接続できたが、チャンネル情報取得失敗")
                return False
                
        except Exception as e:
            print(f"❌ YouTube API接続失敗: {e}")
            return False
            
    except json.JSONDecodeError as e:
        print(f"❌ JSONパースエラー: {e}")
        return False
    except Exception as e:
        print(f"❌ 予期せぬエラー: {e}")
        return False

if __name__ == "__main__":
    success = test_youtube_credentials()
    if success:
        print("\n🎉 YouTube認証情報テスト成功！")
    else:
        print("\n💥 YouTube認証情報テスト失敗")
