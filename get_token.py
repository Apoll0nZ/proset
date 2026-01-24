import os
import pickle
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request

# 許可する権限（YouTubeへのアップロード）
SCOPES = ['https://www.googleapis.com/auth/youtube.upload']

def main():
    creds = None
    # client_secrets.jsonを読み込む（youtube/ は不要）
    flow = InstalledAppFlow.from_client_secrets_file(
        'client_secrets.json', SCOPES)
    
    # ローカルサーバーを起動してブラウザで承認を得る
    creds = flow.run_local_server(port=0)

    # 【重要】ここを 'token.json' に修正（youtube/ を削除）
    with open('token.json', 'w') as token:
        token.write(creds.to_json())
    
    print("\n✅ 成功しました！ 'token.json' が生成されました。")
    print("このファイルの中身をコピーして、GitHub Secrets の 'YOUTUBE_TOKEN_JSON' に貼り付けてください。")

if __name__ == '__main__':
    main()