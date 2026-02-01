#!/usr/bin/env python3
"""
YouTube OAuth 2.0 認証用スクリプト
一度だけ実行してtoken.jsonを取得する
"""

import os
import json
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

# スコープ設定（YouTubeアップロード権限）
SCOPES = ['https://www.googleapis.com/auth/youtube.upload']

def main():
    print("YouTube OAuth 2.0 認証を開始します...")
    
    # client_secrets.jsonのパス
    client_secrets_path = "client_secrets.json"
    
    if not os.path.exists(client_secrets_path):
        print(f"エラー: {client_secrets_path} が見つかりません")
        print("Google Cloud ConsoleからOAuth 2.0クライアントIDをダウンロードしてください")
        return
    
    # OAuthフロー実行
    flow = InstalledAppFlow.from_client_secrets_file(
        client_secrets_path, 
        SCOPES
    )
    
    # ローカルサーバー起動して認証
    print("ブラウザで認証ページが開きます...")
    credentials = flow.run_local_server(
        port=8080,
        prompt='consent',
        access_type='offline'  # refresh_tokenを取得するために重要
    )
    
    # 認証情報を保存
    token_data = {
        "token": credentials.token,
        "refresh_token": credentials.refresh_token,
        "token_uri": credentials.token_uri,
        "client_id": credentials.client_id,
        "client_secret": credentials.client_secret,
        "expires_at": credentials.expiry.timestamp() if credentials.expiry else None
    }
    
    # token.jsonとして保存
    with open("token.json", "w") as f:
        json.dump(token_data, f, indent=2)
    
    print(f"\n✅ 認証成功！")
    print(f"token.json を保存しました")
    print(f"有効期限: {credentials.expiry}")
    print(f"\n📋 GitHub Secretsに設定する内容:")
    print(f"YOUTUBE_TOKEN_JSON={json.dumps(token_data)}")
    print(f"\n📋 client_secrets.jsonの内容:")
    with open(client_secrets_path, "r") as f:
        client_secrets = f.read()
        print(f"YOUTUBE_CLIENT_SECRETS_JSON={client_secrets}")

if __name__ == "__main__":
    main()
