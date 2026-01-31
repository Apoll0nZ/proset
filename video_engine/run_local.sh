#!/bin/bash

echo "=== ローカル実行スクリプト ==="

# 必要な環境変数の設定
export GEMINI_API_KEY="${GEMINI_API_KEY:-}"
export YOUTUBE_CLIENT_ID="${YOUTUBE_CLIENT_ID:-}"
export YOUTUBE_CLIENT_SECRETS_JSON="${YOUTUBE_CLIENT_SECRETS_JSON:-}"
export YOUTUBE_TOKEN_JSON="${YOUTUBE_TOKEN_JSON:-}"

# AWS設定（ローカルテスト用）
export S3_BUCKET="${S3_BUCKET:-test-bucket}"
export MY_AWS_REGION="${MY_AWS_REGION:-ap-northeast-1}"
export SCRIPT_S3_BUCKET="${SCRIPT_S3_BUCKET:-test-bucket}"
export SCRIPT_S3_PREFIX="${SCRIPT_S3_PREFIX:-scripts/}"
export MY_DDB_TABLE_NAME="${MY_DDB_TABLE_NAME:-VideoHistory}"

# VOICEVOX設定
export VOICEVOX_API_URL="${VOICEVOX_API_URL:-http://localhost:50021}"

# 動画設定
export VIDEO_WIDTH="${VIDEO_WIDTH:-1920}"
export VIDEO_HEIGHT="${VIDEO_HEIGHT:-1080}"
export FPS="${FPS:-30}"

# 必須環境変数のチェック
if [ -z "$GEMINI_API_KEY" ]; then
    echo "❌ GEMINI_API_KEYが設定されていません"
    echo "export GEMINI_API_KEY=your_api_key"
    exit 1
fi

echo "✅ 環境変数の設定完了"
echo "GEMINI_API_KEY: ${GEMINI_API_KEY:0:10}..."
echo "VOICEVOX_API_URL: $VOICEVOX_API_URL"
echo "S3_BUCKET: $S3_BUCKET"

# スクリプトの実行
echo "=== 動画生成スクリプトを実行します ==="
python3 render_video.py --debug

echo "=== 実行完了 ==="
