# YouTube 自動動画作成システム

## 1. プロジェクトの目的
このプロジェクトは、**「情報の鮮度」と「運用の完全自動化」**を両立させた、YouTube解説動画生成システムです。最新のテック・ハードウェアニュースを24時間監視し、AI（Gemini）が「バズる」可能性の高い記事を厳選。ナレーション生成（ずんだもん）、動画合成、YouTube投稿までを一切の人間を介さずに完結させることを目的としています。

## 2. システムアーキテクチャ（実行フロー）
本システムは、**「選定」「執筆」「動画生成」**の3つのフェーズで構成されています。

### ① 【選定】Lambda Selector（司令塔）
**RSS監視**: 厳選された14のテック系ニュースソース（主要メディア・Reddit等）を定期的にチェック。

**重複排除**: DynamoDBに3年間の履歴を保持（TTL管理）。過去に評価・採用した記事は二度と処理せず、APIコストを最小化。

**AI選定**: Gemini 2.5 Flash Lite が「YouTube視聴者が興味を持つか」という視点で記事を採点。

**トリガー**: 合格点（75点以上）の記事が見つかった場合、S3にデータを保存し、GitHub Actionsをキックします。

### ② 【執筆】Lambda Writer（放送作家）
**台本生成**: Gemini 2.0 Flash（高性能モデル）を使用。

**プロンプト設計**: 視聴維持率を意識した「衝撃的な導入」「中学生でもわかる解説」「エンゲージメントを誘う問いかけ」を含む台本を構成。

**構造化出力**: 動画編集ソフトや合成スクリプトが読み取りやすいJSON形式で出力。

### ③ 【生成・投稿】GitHub Actions（工場）
**音声合成**: VOICEVOX を使用して「ずんだもん」のナレーションを生成。

**動画合成**: MoviePy を用い、ナレーション・テロップ・背景画像を統合したMP4ファイルを生成。

**自動投稿**: YouTube Data API v3を経由し、タイトル・概要欄・タグを設定して自動アップロード。

## 3. 技術スタック
- **Language**: Python 3.12
- **AI**: Google Gemini API (2.0 Flash / 2.5 Flash Lite)
- **Cloud**: AWS (Lambda, S3, DynamoDB)
- **CI/CD**: GitHub Actions
- **Video/Audio**: VOICEVOX, MoviePy

## 4. 詳細システム仕様

### 4.1 Lambda Selector（記事選定エンジン）
**役割**: RSSフィードから最適な記事を1件選定

**処理フロー**:
1. **RSS収集**: 14の有効なRSSフィードから最新記事を取得
   - Group A: 主要ニュースサイト（8サイト）
   - Group B: コミュニティ/反応サイト（6サイト）
2. **重複排除**: DynamoDBで過去3年間の処理済みURLをチェック
3. **記事評価**: Gemini 2.5 Flash Liteで最大30件をバッチ評価
   - 10件ずつ×3バッチで評価
   - 新製品発表、OSアップデート、技術リークなどを優先
4. **最終選定**: 評価候補から最適な1記事を選択
5. **S3保存**: 選定記事をS3の`pending/`に保存

**重要設定**:
- モデル: `gemini-2.5-flash-lite`
- DynamoDB TTL: 3年間（1095日）
- 評価記事数: 最大30件（最新順）
- JSON応答強制 + 正規表現フォールバック

### 4.2 Lambda Writer（台本生成エンジン）
**役割**: 選定記事から動画台本を生成

**処理フロー**:
1. **S3取得**: `pending/`から選定記事を取得
2. **台本生成**: Gemini 2.0 Flashで動画台本を作成
   - 導入、本編、まとめの構成
   - 視聴者向けの分かりやすい解説調
   - 衝撃的な導入・中学生でもわかる解説・エンゲージメント誘う問いかけ
3. **S3保存**: 生成台本を`scripts/`に保存

**重要設定**:
- モデル: `gemini-2.0-flash`
- 台本テンプレート使用
- JSON構造化出力

### 4.3 Video Engine（動画生成エンジン）
**役割**: 台本から動画を生成してYouTubeにアップロード

**処理フロー**:
1. **台本取得**: S3の`scripts/`から最新台本を取得
2. **音声生成**: VOICEVOXで「ずんだもん」の日本語音声を合成
   - 複数セリフに対応
3. **動画レンダリング**: MoviePy + FFmpegで動画生成
   - 背景画像 + 字幕 + 音声を合成
   - 字幕はNotoSansJPフォント使用
4. **サムネイル生成**: PC猫スタイル（2chスレタイ風）
   - 上部70%: 記事関連画像2枚
   - 下部30%: 鮮やかな黄色背景 + スレタイ風字幕
5. **YouTubeアップロード**: 非公開でアップロード
   - YouTube Data API v3使用
   - サムネイル設定も自動
6. **履歴登録**: DynamoDB VideoHistoryに記録

**重要設定**:
- 動画サイズ: 1920x1080
- 音声: VOICEVOX（ずんだもん）
- サムネイル: 1280x720
- アップロード: 非公開ステータス

## 5. 動画作成ロジック詳細

### 5.1 記事選定ロジック
1. **RSSフィード監視**: 14サイトから定期的に記事取得
2. **鮮度優先**: published_atで最新順にソート
3. **重複排除**: 過去3年間の処理済みURLは除外
4. **AI評価**: Geminiが速報性・具体性を評価
5. **最適選定**: 視聴者価値が最も高い記事を1件選択

### 5.2 台本生成ロジック
1. **記事解析**: タイトル、内容、重要ポイントを抽出
2. **構成設計**: 導入→本編→まとめの3部構成
3. **調整**: 専門用語を平易に、興味を引く表現に
4. **時間調整**: 5-10分程度の動画長に対応

### 5.3 動画生成ロジック
1. **音声合成**: VOICEVOXで「ずんだもん」の自然な日本語音声
2. **字幕同期**: セリフと音声を完璧に同期
3. **視覚効果**: 背景画像 + 動的字幕
4. **ブランド統一**: PC猫スタイルのサムネイル

## 6. 環境変数設定

### 6.1 Lambda Selector
- `DYNAMODB_TABLE`: DynamoDBテーブル名
- `S3_BUCKET`: S3バケット名
- `PENDING_PATH`: pendingフォルダパス
- `GEMINI_API_KEY`: Gemini APIキー
- `GEMINI_MODEL_NAME`: gemini-2.5-flash-lite
- `AWS_REGION`: ap-northeast-1

### 6.2 Lambda Writer
- `S3_BUCKET`: S3バケット名
- `PENDING_PATH`: pendingフォルダパス
- `SCRIPTS_PATH`: scriptsフォルダパス
- `GEMINI_API_KEY`: Gemini APIキー
- `GEMINI_MODEL_NAME`: gemini-2.0-flash

### 6.3 Video Engine
- `SCRIPT_S3_BUCKET`: 台本S3バケット
- `SCRIPT_S3_PREFIX`: scripts/プレフィックス
- `MY_DDB_TABLE_NAME`: VideoHistoryテーブル
- `YOUTUBE_AUTH_JSON`: YouTube認証JSON
- `VOICEVOX_API_URL`: VOICEVOX API URL
- `BACKGROUND_IMAGE_PATH`: 背景画像パス
- `FONT_PATH`: フォントパス

## 7. デプロイ方法

### 7.1 Lambda関数デプロイ
```bash
# lambda_selector
cd lambda_selector
zip -r lambda_selector.zip lambda_function.py rss_sources.json package/

# lambda_writer  
cd lambda_writer
zip -r lambda_writer.zip lambda_function.py gemini_script_prompt.txt package/
```

### 7.2 IAMロール設定
- DynamoDBアクセス権限
- S3アクセス権限
- Lambda実行権限

### 7.3 環境変数設定
各Lambda関数に必要な環境変数を設定

## 8. 監視と運用

### 8.1 CloudWatchログ
- 各Lambda関数の実行ログ
- エラー監視
- パフォーマンス監視

### 8.2 DynamoDBテーブル
- 記事重複排除テーブル（3年TTL）
- 動画履歴テーブル

### 8.3 S3ストレージ
- pending/: 選定待ち記事
- scripts/: 生成台本
- 動画ファイル（一時）

## 9. 注意事項
- VOICEVOX（ずんだもん）は確定済み
- YouTubeアップロードは非公開で実行
- RSSフィードは定期的に接続性チェックが必要
- Gemini APIレート制限に準拠（15回/分）
