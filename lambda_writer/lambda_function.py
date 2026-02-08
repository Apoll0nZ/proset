import os
import sys

# 実行ファイルがある場所を取得し、packageフォルダを検索パスに追加
current_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.join(current_dir, "package"))

import json
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import boto3
import requests

# -----------------------------------------------------------------------------
# 環境変数
# -----------------------------------------------------------------------------
S3_BUCKET = os.environ["S3_BUCKET"]
PENDING_PREFIX = os.environ.get("PENDING_PATH", "pending/")
SCRIPTS_PREFIX = os.environ.get("SCRIPTS_PATH", "scripts/")
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
GEMINI_MODEL_NAME = os.environ.get("GEMINI_MODEL_NAME", "gemini-2.5-flash-lite")
GEMINI_API_VERSION = os.environ.get("GEMINI_API_VERSION", "v1")
AWS_REGION = os.environ.get("MY_AWS_REGION", os.environ.get("AWS_REGION", "ap-northeast-1"))
PROMPT_PATH = os.path.join(os.path.dirname(__file__), "gemini_script_prompt.txt")

# GitHub連携用環境変数
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
GITHUB_REPO = os.environ.get("GITHUB_REPO")
GITHUB_EVENT_TYPE = os.environ.get("GITHUB_EVENT_TYPE", "generate_video")

# -----------------------------------------------------------------------------
# AWS クライアント
# -----------------------------------------------------------------------------
s3_client = boto3.client("s3", region_name=AWS_REGION)


# -----------------------------------------------------------------------------
# ユーティリティ
# -----------------------------------------------------------------------------
def _ensure_trailing_slash(value: str) -> str:
    return value if value.endswith("/") else value + "/"


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# -----------------------------------------------------------------------------
# Gemini API
# -----------------------------------------------------------------------------
def call_gemini_generate_content(prompt: str) -> Optional[str]:
    """Gemini APIを呼び出して台本を生成"""
    if not GEMINI_API_KEY:
        raise RuntimeError("環境変数 GEMINI_API_KEY が設定されていません")

    url = (
        f"https://generativelanguage.googleapis.com/{GEMINI_API_VERSION}/models/"
        f"{GEMINI_MODEL_NAME}:generateContent?key={GEMINI_API_KEY}"
    )

    headers = {"Content-Type": "application/json"}
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.7,
            "max_output_tokens": 8192,
        },
        "safetySettings": [
            {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_ONLY_HIGH"},
            {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_ONLY_HIGH"},
            {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_ONLY_HIGH"},
            {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_ONLY_HIGH"},
        ],
    }

    max_retries = 3
    for attempt in range(max_retries):
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=(5, 60))
        except requests.RequestException as exc:
            print(f"Gemini request error: {exc}")
            if attempt < max_retries - 1:
                wait_time = min(2 ** attempt, 30)
                print(f"Retrying Gemini call in {wait_time}s (attempt {attempt + 1}/{max_retries})")
                time.sleep(wait_time)
                continue
            return None

        if response.status_code == 503:
            print(f"Gemini overloaded (503). attempt={attempt + 1}/{max_retries}")
            if attempt < max_retries - 1:
                wait_time = min(2 ** attempt, 30)
                print(f"Retrying in {wait_time}s")
                time.sleep(wait_time)
                continue
            return None

        if response.status_code != 200:
            print(f"Gemini API error: {response.status_code} - {response.text}")
            return None

        try:
            data = response.json()
            return data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError, json.JSONDecodeError) as exc:
            print(f"Failed to parse Gemini response: {exc}")
            if attempt < max_retries - 1:
                time.sleep(2)
                continue
            return None

    return None


def extract_json_text(response_text: str) -> Optional[str]:
    """GeminiのレスポンスからJSON部分を抽出"""
    start = response_text.find("{")
    end = response_text.rfind("}")
    
    if start != -1 and end != -1 and end > start:
        return response_text[start : end + 1].strip()

    print("Failed to locate JSON block in Gemini response")
    return None


# -----------------------------------------------------------------------------
# S3 ヘルパー
# -----------------------------------------------------------------------------
def find_latest_pending_file(bucket: str) -> Optional[str]:
    """S3バケットのpending/ディレクトリから最新のファイルを検索"""
    try:
        response = s3_client.list_objects_v2(
            Bucket=bucket,
            Prefix=PENDING_PREFIX,
            MaxKeys=100
        )
        
        objects = response.get("Contents", [])
        if not objects:
            return None
            
        # 最終更新時刻でソートして最新のファイルを取得
        latest_object = max(objects, key=lambda obj: obj.get("LastModified", datetime.min))
        return latest_object["Key"]
        
    except Exception as e:
        print(f"Error finding latest pending file: {e}")
        return None


def load_pending_article(bucket: str, key: str) -> Dict[str, Any]:
    """S3からpending記事を読み込む"""
    response = s3_client.get_object(Bucket=bucket, Key=key)
    body = response["Body"].read().decode("utf-8")
    return json.loads(body)


def save_script(bucket: str, prefix: str, filename: str, payload: Dict[str, Any]) -> str:
    """生成した台本をS3に保存"""
    key = prefix + filename
    body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    s3_client.put_object(
        Bucket=bucket,
        Key=key,
        Body=body,
        ContentType="application/json; charset=utf-8",
    )
    return key


def delete_object(bucket: str, key: str) -> None:
    """S3オブジェクトを削除"""
    s3_client.delete_object(Bucket=bucket, Key=key)


# -----------------------------------------------------------------------------
# GitHub連携
# -----------------------------------------------------------------------------
def trigger_github_actions(script_key: str, s3_bucket: str, content_hash: str) -> bool:
    """GitHub Actionsを起動"""
    if not (GITHUB_TOKEN and GITHUB_REPO):
        print("GitHub credentials not configured, skipping GitHub Actions trigger")
        return False

    url = f"https://api.github.com/repos/{GITHUB_REPO}/dispatches"
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"token {GITHUB_TOKEN}",
    }
    payload = {
        "event_type": GITHUB_EVENT_TYPE,
        "client_payload": {
            "s3_bucket": s3_bucket,
            "s3_key": script_key,
            "content_hash": content_hash,
        },
    }

    try:
        print(f"Triggering GitHub Actions for {GITHUB_REPO}...")
        resp = requests.post(url, headers=headers, json=payload, timeout=10)
        
        if resp.status_code not in (200, 201, 204):
            print(f"GitHub dispatch failed: {resp.status_code} - {resp.text}")
            return False
        
        print("GitHub Actions triggered successfully")
        return True
        
    except Exception as e:
        print(f"Error triggering GitHub Actions: {e}")
        return False


# -----------------------------------------------------------------------------
# プロンプト読み込み
# -----------------------------------------------------------------------------
def load_prompt_template() -> str:
    """台本生成用のプロンプトテンプレートを読み込む"""
    try:
        with open(PROMPT_PATH, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        print(f"Failed to load prompt template: {e}")
        # フォールバック用の基本的なプロンプト
        return """
以下の記事情報を元に、YouTube動画用の詳細な台本を生成してください：

記事タイトル: {{TITLE}}
記事URL: {{URL}}
記事要約: {{SUMMARY}}

以下のJSON形式で出力してください：
{{
  "title": "動画タイトル",
  "description": "動画説明文",
  "content": {{
    "topic_summary": "トピック要約",
    "script_parts": [
      {{
        "part": "title",
        "text": "タイトルナレーション",
        "speaker_id": 3
      }},
      {{
        "part": "article_1",
        "text": "本文ナレーション1",
        "speaker_id": 1
      }}
    ]
  }},
  "thumbnail": {{
    "title": "サムネイルタイトル",
    "subtitle": "サブタイトル"
  }}
}}
"""


def _build_prompt(template: str, article: Dict[str, Any]) -> str:
    """プロンプトを構築 - replace方式でJSONの波括弧と衝突を回避"""
    result = template
    
    # 記事情報を置換
    result = result.replace("{{TITLE}}", article.get("title", ""))
    result = result.replace("{{URL}}", article.get("url", ""))
    result = result.replace("{{SUMMARY}}", article.get("summary", ""))
    
    return result


# -----------------------------------------------------------------------------
# メイン処理
# -----------------------------------------------------------------------------
def lambda_handler(event, context):
    """Writer Lambdaのメイン処理 - 純粋な受け身の台本作成"""
    print("Lambda writer started - Pure script generation mode")
    time.sleep(2)

    # S3イベントをチェック
    records = event.get("Records", [])
    
    if records:
        # S3イベントがある場合：通常の処理
        record = records[0]
        bucket = record.get("s3", {}).get("bucket", {}).get("name") or S3_BUCKET
        key = record.get("s3", {}).get("object", {}).get("key")
        if not key:
            raise RuntimeError("S3イベントから object.key を取得できません")
        print(f"Processing S3 event: s3://{bucket}/{key}")
    else:
        # S3イベントがない場合：最新のpendingファイルを検索
        print("No S3 event found, searching for latest pending file...")
        bucket = S3_BUCKET
        key = find_latest_pending_file(bucket)
        if not key:
            raise RuntimeError("pending/ ディレクトリにファイルが見つかりません")
        print(f"Found latest pending file: s3://{bucket}/{key}")

    # pending記事を読み込み
    print("Loading pending article...")
    pending_article = load_pending_article(bucket, key)
    print(f"Loaded article: {pending_article.get('title', 'Unknown')}")

    # プロンプトテンプレートを読み込み
    print("Loading prompt template...")
    prompt_template = load_prompt_template()
    
    # プロンプトを構築
    print("Building prompt...")
    prompt = _build_prompt(prompt_template, pending_article)
    
    # Geminiで台本を生成
    print("Generating script with Gemini...")
    response_text = call_gemini_generate_content(prompt)
    if response_text is None:
        raise RuntimeError("Gemini API から有効なレスポンスが得られませんでした")

    # JSONを抽出
    print("Extracting JSON from response...")
    json_text = extract_json_text(response_text)
    if json_text is None:
        raise RuntimeError("Gemini レスポンスから JSON ブロックを抽出できませんでした")

    # JSONをパース
    print("Parsing generated script...")
    try:
        script_payload = json.loads(json_text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"生成された JSON の解析に失敗しました: {exc}") from exc

    # メタ情報を追加
    script_payload.setdefault("meta", {})
    script_payload["meta"].update({
        "source_url": pending_article.get("url"),
        "selected_at": pending_article.get("selected_at"),
        "written_at": _iso_now(),
    })

    # 台本を保存
    filename = f"script_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{pending_article.get('content_hash', 'unknown')[:8]}.json"
    print(f"Saving script as: {filename}")
    script_key = save_script(S3_BUCKET, SCRIPTS_PREFIX, filename, script_payload)
    print(f"Script saved to: s3://{S3_BUCKET}/{script_key}")

    # pendingファイルを削除
    print("Deleting pending file...")
    delete_object(bucket, key)
    print("Pending file deleted")

    # GitHub Actionsを起動
    print("Triggering GitHub Actions...")
    content_hash = pending_article.get("content_hash", "unknown")
    github_success = trigger_github_actions(script_key, S3_BUCKET, content_hash)
    
    return {
        "status": "ok",
        "script_key": script_key,
        "pending_key": key,
        "github_triggered": github_success,
        "mode": "pure_script_generation"
    }
