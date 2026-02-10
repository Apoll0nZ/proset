import os
import sys

# 実行ファイルがある場所を取得し、packageフォルダを検索パスに追加
current_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.join(current_dir, "package"))

import json
import os
import re
import time
import math
from datetime import datetime, timezone
from typing import Any, Dict, Optional, List

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
# バリデーション（30行目付近に追加）
# -----------------------------------------------------------------------------
def is_valid_article_url(url: str) -> bool:
    """
    記事URLの妥当性を検証

    Returns:
        True: 有効な記事URL
        False: 無効なURL（プレースホルダー、フィードURLなど）
    """
    if not url:
        print("[VALIDATION] URL is empty")
        return False

    url_lower = url.lower()

    # プレースホルダーURLを除外
    if "example.com" in url_lower or "placeholder" in url_lower:
        print(f"[VALIDATION] Rejected placeholder URL: {url}")
        return False

    # フィードURLパターンを除外
    invalid_patterns = [".rss", ".xml", "/feed/", "/rss/", "/atom/"]
    if any(pattern in url_lower for pattern in invalid_patterns):
        print(f"[VALIDATION] Rejected feed-like URL: {url}")
        return False

    # 有効なHTTP(S) URLのみ許可
    if not url.startswith(("http://", "https://")):
        print(f"[VALIDATION] Rejected non-HTTP URL: {url}")
        return False

    print(f"[VALIDATION] URL is valid: {url}")
    return True

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
# プロンプト分割（Gemini負荷軽減）
# -----------------------------------------------------------------------------
def _split_prompt_with_roles(template: str, article: Dict[str, Any]) -> List[Dict[str, str]]:
    """
    プロンプトを3つの役割に分割し、Geminiの負荷を軽減
    
    各ステップは独立したJSONを生成し、最後にマージする
    
    Returns:
        [
            {"role": "metadata", "prompt": "..."},
            {"role": "script", "prompt": "..."},
            {"role": "thumbnail", "prompt": "..."}
        ]
    """
    # 記事情報を整形
    title = article.get("title", "タイトル不明")
    url = article.get("url", "")
    summary = article.get("summary", "要約なし")

    # 共通の記事情報
    base_info = f"""
【記事情報】
- タイトル: {title}
- URL: {url}
- 要約: {summary}

【重要な禁止事項】
- example.com のようなプレースホルダーURLは絶対に使用しないでください
- 架空の情報を含めないでください
- 記事の内容に基づいた事実のみを記述してください
"""

    return [
        # STEP 1: メタデータ生成
        {
            "role": "metadata",
            "prompt": f"""{base_info}

【STEP 1/3: メタデータ生成】
YouTube動画のタイトルと説明文を生成してください。

以下のJSON形式**のみ**で出力してください（説明文は不要）：

{{
  "title": "YouTube動画タイトル（50-60文字、記事の核心を端的に）",
  "description": "動画説明文（150-200文字、記事の要点を簡潔に）"
}}

重要: 
- タイトルは視聴者の興味を引く具体的な内容にする
- 説明文は記事の主要なポイントを3つ程度含める
- JSONのみを出力し、前後に説明を付けない
"""
        },

        # STEP 2: スクリプト生成（最も重要）
        {
            "role": "script",
            "prompt": f"""{base_info}

【STEP 2/3: 台本コンテンツ生成】
動画の本編となるナレーション台本を生成してください。

以下のJSON形式**のみ**で出力してください：

{{
  "content": {{
    "topic_summary": "トピックの要約（100文字程度、記事の核心を1文で）",
    "script_parts": [
      {{"part": "article_1", "text": "導入部分（100-150文字）", "speaker_id": 1}},
      {{"part": "article_2", "text": "本題解説1（200-300文字）", "speaker_id": 1}},
      {{"part": "article_3", "text": "本題解説2（200-300文字）", "speaker_id": 1}},
      {{"part": "article_4", "text": "詳細解説（200-300文字）", "speaker_id": 1}},
      {{"part": "reaction", "text": "ネットの反応・コメント（150-200文字）", "speaker_id": 2}},
      {{"part": "owner_comment", "text": "まとめコメント（100-150文字）", "speaker_id": 3}}
    ]
  }}
}}

重要なルール:
1. script_partsは**必ず6つ以上8つ以下**のパートを含めてください
2. 各パートは具体的な情報を含み、単なる繋ぎの文は避けてください
3. article_1から順に論理的な流れを作ってください
4. speaker_id: 1=メインナレーター, 2=サブ解説, 3=まとめ
5. JSONのみを出力し、前後に説明を付けない
"""
        },

        # STEP 3: サムネイル情報
        {
            "role": "thumbnail",
            "prompt": f"""{base_info}

【STEP 3/3: サムネイル情報生成】
YouTube動画のサムネイル用テキストを生成してください。

以下のJSON形式**のみ**で出力してください：

{{
  "thumbnail": {{
    "title": "サムネイルタイトル（15-20文字、インパクト重視）",
    "subtitle": "サブタイトル（20-30文字、補足情報）"
  }}
}}

重要:
- titleは視聴者の目を引く短いフレーズ
- subtitleは製品名や具体的な数値を含める
- JSONのみを出力し、前後に説明を付けない
"""
        }
    ]


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
    """GeminiのレスポンスからJSON部分を抽出（改良版）"""
    import re

    # Markdownコードブロックを除去
    if "```json" in response_text:
        match = re.search(r'```json\s*\n(.*?)\n```', response_text, re.DOTALL)
        if match:
            response_text = match.group(1)
    elif "```" in response_text:
        match = re.search(r'```\s*\n(.*?)\n```', response_text, re.DOTALL)
        if match:
            response_text = match.group(1)

    # 最初の { から最後の } までを抽出
    start = response_text.find("{")
    end = response_text.rfind("}")

    if start != -1 and end != -1 and end > start:
        json_text = response_text[start : end + 1].strip()

        # バリデーション: パース可能かテスト
        try:
            json.loads(json_text)
            return json_text
        except json.JSONDecodeError as e:
            print(f"[ERROR] Extracted text is not valid JSON: {e}")
            print(f"[DEBUG] First 500 chars: {json_text[:500]}")
            return None

    print("[ERROR] Could not find valid JSON structure in response")
    print(f"[DEBUG] Response (first 500 chars): {response_text[:500]}")
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


def split_prompt_into_three(prompt_text: str) -> List[str]:
    """
    gemini_script_prompt.txt を意味解釈せず、順序保持で機械的に3分割する。
    Gemini の負荷分散と暴走防止が目的。
    """
    length = len(prompt_text)
    if length == 0:
        return ["", "", ""]
    chunk = math.ceil(length / 3)
    return [
        prompt_text[0:chunk],
        prompt_text[chunk : 2 * chunk],
        prompt_text[2 * chunk :],
    ]


def build_article_info_block(article: Dict[str, Any]) -> str:
    """Gemini入力用の記事情報ブロック（URLを含めない）"""
    title = article.get("title", "")
    summary = article.get("summary", "")
    body = article.get("body", "")
    return "\n\n[記事情報]\nTITLE: {title}\nSUMMARY: {summary}\nBODY: {body}".format(
        title=title,
        summary=summary,
        body=body,
    )


def contains_example_dot_com(value: Any) -> bool:
    if isinstance(value, str):
        return "example.com" in value.lower()
    if isinstance(value, dict):
        return any(contains_example_dot_com(v) for v in value.values())
    if isinstance(value, list):
        return any(contains_example_dot_com(v) for v in value)
    return False


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
    article_title = pending_article.get("title", "Unknown")
    article_url = pending_article.get("url", "")

    print(f"Loaded article: {article_title}")
    print(f"Article URL: {article_url}")

    # URL妥当性チェック
    if not is_valid_article_url(article_url):
        print(f"[SKIP] Invalid article URL detected, deleting pending file: {article_url}")
        delete_object(bucket, key)
        print("Invalid pending file deleted")
        return {
            "status": "skipped",
            "reason": "invalid_url",
            "url": article_url,
            "pending_key": key,
        }

    print(f"[VALIDATION] Article URL is valid: {article_url}")

    # プロンプトテンプレートを読み込み
    print("Loading prompt template...")
    prompt_template = load_prompt_template()

    print("Splitting prompt into 3 parts (mechanical, no edits)...")
    prompt_parts = split_prompt_into_three(prompt_template)

    # 記事情報ブロックを作成（URLは含めない）
    article_info_block = build_article_info_block(pending_article)

    # Geminiを3回直列実行（独立・非共有）
    merged_script: Dict[str, Any] = {}
    step_key_whitelist = {
        1: ["title", "description"],
        2: ["content"],
        3: ["thumbnail"],
    }

    for idx, part in enumerate(prompt_parts, start=1):
        print(f"[Gemini] STEP{idx}/3 - calling with isolated prompt part")
        step_prompt = part + article_info_block
        response_text = call_gemini_generate_content(step_prompt)
        if response_text is None:
            raise RuntimeError(f"Gemini STEP{idx} で有効なレスポンスが得られませんでした")
        if len(response_text.strip()) < 200:
            raise RuntimeError(f"Gemini STEP{idx} の出力が短すぎます")
        if "example.com" in response_text.lower():
            raise RuntimeError(f"Gemini STEP{idx} の出力に example.com が含まれています")

        # JSONを抽出
        print(f"[Gemini] STEP{idx}/3 - extracting JSON")
        json_text = extract_json_text(response_text)
        if json_text is None:
            raise RuntimeError(f"STEP{idx} のレスポンスから JSON を抽出できませんでした")

        # JSONをパース
        try:
            part_data = json.loads(json_text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"STEP{idx} の JSON 解析に失敗しました: {exc}") from exc

        # ステップごとの役割に合わせて必要キーのみ採用
        allowed_keys = step_key_whitelist.get(idx, [])
        filtered = {k: part_data.get(k) for k in allowed_keys if k in part_data}
        if len(filtered) != len(allowed_keys):
            missing = [k for k in allowed_keys if k not in filtered]
            raise RuntimeError(f"STEP{idx} で必要なキーが不足しています: {missing}")

        merged_script.update(filtered)

    # マージ結果の検証
    required_keys = ["title", "description", "content", "thumbnail"]
    missing_keys = [key for key in required_keys if key not in merged_script]
    if missing_keys:
        raise RuntimeError(f"台本に必須項目が不足しています: {missing_keys}")

    if contains_example_dot_com(merged_script):
        raise RuntimeError("生成結果に example.com が含まれています")

    script_payload = merged_script

    # メタ情報を上書き（Gemini出力は使用しない）
    script_payload["meta"] = {
        "url": pending_article.get("url"),
        "source": pending_article.get("source", ""),
        "selected_at": pending_article.get("selected_at"),
        "written_at": _iso_now(),
    }

    if not script_payload["meta"]["url"]:
        raise RuntimeError("meta.url が空です（pending記事から取得できませんでした）")

    # topic_summary が欠落している場合はpending記事のsummaryで補完
    content_obj = script_payload.get("content", {}) or {}
    if not content_obj.get("topic_summary"):
        content_obj["topic_summary"] = pending_article.get("summary", "")
        script_payload["content"] = content_obj

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
