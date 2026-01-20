## AI による PC ニュース自動 YouTube チャンネル生成システム（初期構成）

このリポジトリは、PC/ハードウェア系ニュースを自動収集し、Gemini で台本生成 → GitHub Actions で動画生成 → YouTube 自動投稿 → DynamoDB に履歴保存、までを行うシステムの初期実装です。

### 構成概要

- **AWS Lambda（`lambda/`）**
  - `rss_sources.json` で定義した RSS からニュース収集
  - Gemini API で PC チャンネル適合度を 10 点満点で採点し、7 点以上のみ採用
  - 重複判定用 `content_hash` を SHA256 で生成し、DynamoDB `VideoHistory` テーブルに存在する場合はスキップ
  - `gemini_script_prompt.txt` を読み込み、Gemini で台本 JSON を生成
  - 台本 JSON を S3 に保存
  - GitHub Repository Dispatch を送信し、動画生成ワークフローを起動

- **動画エンジン（`video_engine/`）**
  - S3 から最新の台本 JSON を取得
  - Google Cloud Text-to-Speech で日本語ナレーション音声を生成
  - MoviePy + FFmpeg で背景画像 + 字幕 + 音声から mp4 動画を生成
  - YouTube Data API v3 で「非公開」として動画アップロード
  - 成功後、DynamoDB `VideoHistory` に `put_item` で履歴登録

- **GitHub Actions（`.github/workflows/main.yml`）**
  - `repository_dispatch`（event_type: `generate_video`）で起動
  - Python 3.11 / ffmpeg をセットアップし、`video_engine/requirements.txt` をインストール
  - `render_video.py` を実行

- **設定サンプル（`config/settings_example.json`）**
  - Lambda / GitHub Actions / 各種シークレットの例示

### DynamoDB `VideoHistory` テーブル仕様

- **パーティションキー**
  - `content_hash` (String)
- **その他属性**
  - `title` (String)
  - `source_url` (String)
  - `published_at` (String, ISO8601)
  - `topic_summary` (String)
  - `youtube_video_id` (String)

Lambda 側は `content_hash` の存在チェックのみを行い、書き込みは行いません。  
GitHub Actions 側（`render_video.py`）が YouTube 投稿成功後に `put_item` で登録します。

### 主な環境変数

- **Lambda**
  - `AWS_REGION`（例: `ap-northeast-1`）
  - `DDB_TABLE_NAME`（例: `VideoHistory`）
  - `SCRIPT_S3_BUCKET`（台本 JSON を保存するバケット名）
  - `SCRIPT_S3_PREFIX`（台本 JSON のプレフィックス、例: `scripts/`）
  - `GEMINI_API_KEY`（Google Gemini API キー）
  - `GEMINI_MODEL_NAME`（例: `gemini-1.5-pro`）
  - `GITHUB_REPO`（`owner/repo` 形式）
  - `GITHUB_TOKEN`（Repository Dispatch 用 PAT）
  - `GITHUB_EVENT_TYPE`（デフォルト: `generate_video`）

- **GitHub Actions / `video_engine`**
  - `AWS_REGION`
  - `SCRIPT_S3_BUCKET`
  - `SCRIPT_S3_PREFIX`
  - `DDB_TABLE_NAME`
  - `YOUTUBE_AUTH_JSON`（YouTube Data API 用 OAuth2 資格情報 JSON 文字列）
  - `GOOGLE_APPLICATION_CREDENTIALS`（Google Cloud TTS 用サービスアカウント JSON へのパス）

### 初期セットアップ手順（概要）

1. **AWS リソース作成**
   - S3 バケット（台本 JSON 保存用）
   - DynamoDB テーブル `VideoHistory`
   - Lambda 関数（ランタイム: Python 3.11）
     - デプロイ時に `lambda/` 以下のコードと `requirements.txt` を含める
   - Lambda に S3 読み書き / DynamoDB 読み取り / Secrets Manager 等へのアクセス権限を付与

2. **Google API 設定**
   - Gemini API キーを発行し、Lambda の環境変数 `GEMINI_API_KEY` に設定
   - Google Cloud Text-to-Speech 用サービスアカウントを作成し、JSON キーを GitHub Actions で参照できるようにする
   - YouTube Data API v3 用 OAuth2 資格情報を取得し、`YOUTUBE_AUTH_JSON` として GitHub Secrets に登録

3. **GitHub リポジトリ設定**
   - `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` / `AWS_REGION` / `SCRIPT_S3_BUCKET` / `SCRIPT_S3_PREFIX` / `YOUTUBE_AUTH_JSON` / `GOOGLE_APPLICATION_CREDENTIALS` などを Secrets に登録
   - `GITHUB_TOKEN`（Repository Dispatch 用 PAT）を発行し、Lambda の環境変数に設定

4. **テストフロー**
   - Lambda を手動実行し、S3 に台本 JSON が生成され、Repository Dispatch が送信されることを確認
   - GitHub Actions が起動し、動画生成〜YouTube 非公開アップロード〜DynamoDB 登録まで完走することを確認

### 注意事項

- すべての API キー・認証情報は環境変数または Secret から参照し、コードに直書きしないでください。
- `video_engine/assets/background.png` と `NotoSansJP-Regular.otf` はプレースホルダーです。実運用では適切な背景画像とフォントファイルを配置してください。
- Gemini 出力が必ずしも完全な JSON になるとは限らないため、本番運用ではリトライやバリデーションロジックの強化を推奨します。


### GitHub に push する手順（`youtube/` フォルダをリポジトリルートとして利用）

1. **リポジトリを初期化**
   - まだ Git 管理されていない場合、この `youtube/` フォルダをルートとして以下を実行します。

   ```bash
   cd /path/to/your/project/root/youtube  # 例: /Users/あなたのユーザー名/Documents/app/youtube
   git init
   ```

2. **リモートリポジトリを作成**
   - GitHub 上で Private / Public いずれかのリポジトリを作成します（空の状態で OK）。
   - 作成したリポジトリの URL（例: `git@github.com:your-name/your-repo.git`）を控えておきます。

3. **.gitignore の確認**
   - この `youtube/` 直下の `.gitignore` に以下のようなファイル/ディレクトリが含まれていることを確認してください（既に設定済みですが、必要に応じて追記します）。
     - `.env`
     - `client_secrets.json`
     - `token.json`
     - `*.log`
     - `__pycache__/`
     - `*.pyc`
     - `generated_scripts/`
     - `generated_audio/`
     - `generated_video/`
     - `dist/`
     - `build/`
     - `.DS_Store`

4. **初回コミット**

   ```bash
   git add .
   git commit -m "Initial commit: AI PC news YouTube automation project"
   ```

5. **リモート設定と push**

   ```bash
   git remote add origin <YOUR_REPO_URL>
   git branch -M main
   git push -u origin main
   ```


### GitHub Secrets に登録すべき環境変数一覧

GitHub Actions（`.github/workflows/main.yml`）で使用するため、以下の値を **必ず GitHub Secrets として登録**してください。

- **AWS 関連**
  - `AWS_ACCESS_KEY_ID`
  - `AWS_SECRET_ACCESS_KEY`
  - `AWS_REGION`（例: `ap-northeast-1`）

- **S3 / DynamoDB 関連**
  - `SCRIPT_S3_BUCKET`（台本 JSON を保存するバケット名）
  - `SCRIPT_S3_PREFIX`（台本 JSON 用プレフィックス、例: `scripts/`）

- **YouTube / Google Cloud 関連**
  - `YOUTUBE_AUTH_JSON`
    - YouTube Data API v3 用の OAuth2 資格情報を JSON 文字列として保存したもの
    - この値は絶対にリポジトリに含めないでください
  - `GOOGLE_APPLICATION_CREDENTIALS`
    - Google Cloud Text-to-Speech 用サービスアカウント JSON のパス  
      （Actions 実行時にファイルとして配置する場合、そのパスを指定）

- **その他（必要に応じて）**
  - `SCRIPT_S3_BUCKET` / `SCRIPT_S3_PREFIX` と同じ値を Lambda 側にも設定（環境変数）
  - Lambda に対しては GitHub Secrets ではなく、AWS コンソールや IaC から `GEMINI_API_KEY` / `GITHUB_TOKEN` などを設定します。

