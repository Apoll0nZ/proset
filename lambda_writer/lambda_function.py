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
    プロンプトを3つの役割に分割し、Geminiの負荷を軽減。
    gemini_script_prompt.txt の指示・定型文を活かしたまま役割別に出力させる。
    """
    title = article.get("title", "")
    summary = article.get("summary", "")
    url = article.get("url", "")
    reaction_summary = ""
    reaction = article.get("reaction")
    if isinstance(reaction, dict):
        reaction_summary = reaction.get("summary", "")
    elif isinstance(reaction, str):
        reaction_summary = reaction

    filled = template
    replacements = {
        "{title_A}": title,
        "{summary_A}": summary,
        "{url_A}": url,
        "{summary_B}": reaction_summary,
    }
    for key, value in replacements.items():
        filled = filled.replace(key, value or "")

    format_marker = "### 🚨 出力フォーマット"
    input_marker = "### 入力データ"
    format_idx = filled.find(format_marker)
    input_idx = filled.find(input_marker)

    if format_idx != -1 and input_idx != -1 and input_idx > format_idx:
        preamble = filled[:format_idx].strip()
        format_block = filled[format_idx:input_idx].strip()
        input_block = filled[input_idx:].strip()
    else:
        preamble = filled.strip()
        format_block = ""
        input_block = ""

    json_start_marker = "JSON output start:"
    if json_start_marker in input_block:
        input_block = input_block.split(json_start_marker, 1)[0].strip()

    description_template = ""
    if format_block:
        desc_match = re.search(
            r'"description"\s*:\s*"(?P<desc>[\s\S]*?)"\s*,\s*\n\s*"thumbnail"',
            format_block,
            re.DOTALL,
        )
        if desc_match:
            description_template = desc_match.group("desc").strip()

    if not description_template:
        description_template = "（技術的意義を凝縮した概要文）"

    description_template = description_template.replace("\\", "\\\\").replace('"', '\\"')

    metadata_output = (
        "{\n"
        '  "title": "（固有名詞を含む、知的好奇心を刺激するタイトル）",\n'
        f'  "description": "{description_template}"\n'
        "}"
    )
    script_output = (
        "{\n"
        '  "content": {\n'
        '    "topic_summary": "（事実・分析・本音の要約）",\n'
        '    "script_parts": [\n'
        '      { "part": "article_fact", "speaker_id": 3, "text": "（事実報道）" },\n'
        '      { "part": "article_analysis_1", "speaker_id": 3, "text": "（分析：400文字以上）" },\n'
        '      { "part": "article_analysis_2", "speaker_id": 3, "text": "（分析：400文字以上）" },\n'
        '      { "part": "reaction", "speaker_id": 2, "text": "（反応）" },\n'
        '      { "part": "owner_comment", "speaker_id": 3, "text": "（今回の件について、ガジェ丸はこう考えている。…で始まる総括）" }\n'
        "    ]\n"
        "  }\n"
        "}"
    )
    thumbnail_output = (
        "{\n"
        '  "thumbnail": {\n'
        '    "main_text": "（10字以上の強いフレーズ）",\n'
        '    "sub_texts": ["（煽り文言1つ）"]\n'
        "  }\n"
        "}"
    )

    def _build_prompt(step_label: str, step_note: str, output_format: str) -> str:
        parts = [
            preamble,
            input_block,
            f"### {step_label}",
            step_note,
            "### 出力フォーマット（JSON形式厳守）",
            "以下のJSON構造をテンプレートとして使用し、構造は変更せず中身のみ指示に従って埋めて出力せよ。",
            output_format,
        ]
        return "\n\n".join(p for p in parts if p).strip()

    return [
        {
            "role": "metadata",
            "prompt": _build_prompt(
                "STEP 1/3: メタデータ生成",
                "template内のtitle/descriptionの指示とフォーマットを厳守し、"
                "descriptionは以下の定型文を維持したまま冒頭の概要文のみ今回の記事に合わせて書き換え、"
                "title/descriptionのみを出力せよ。",
                metadata_output,
            ),
        },
        {
            "role": "script",
            "prompt": _build_prompt(
                "STEP 2/3: 台本コンテンツ生成",
                "template内のキャラクター設定・構成・文字数配分を厳守し、contentのみを出力せよ。",
                script_output,
            ),
        },
        {
            "role": "thumbnail",
            "prompt": _build_prompt(
                "STEP 3/3: サムネイル情報生成",
                "template内のthumbnail指示を厳守し、thumbnailのみを出力せよ。",
                thumbnail_output,
            ),
        },
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
    "main_text": "サムネイル主文",
    "sub_texts": ["サブ文"]
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

    # ★★★ 修正: 新しい役割ベース分割を使用 ★★★
    print("Splitting prompt into 3 role-based parts...")
    prompt_parts = _split_prompt_with_roles(prompt_template, pending_article)

    # 各パートを処理してマージ
    merged_script: Dict[str, Any] = {}

    for idx, part_info in enumerate(prompt_parts, start=1):
        role = part_info["role"]
        part_prompt = part_info["prompt"]

        print(f"[Gemini] STEP{idx}/3 ({role}) - calling API...")
        response_text = call_gemini_generate_content(part_prompt)

        if response_text is None:
            raise RuntimeError(f"Gemini API (STEP{idx}: {role}) から有効なレスポンスが得られませんでした")

        print(f"[Gemini] STEP{idx}/3 ({role}) - received {len(response_text)} characters")

        # JSONを抽出
        print(f"[Gemini] STEP{idx}/3 ({role}) - extracting JSON")
        json_text = extract_json_text(response_text)

        if json_text is None:
            print(f"[ERROR] Failed to extract JSON from STEP{idx} ({role})")
            print(f"[DEBUG] Response (first 1000 chars): {response_text[:1000]}")
            raise RuntimeError(f"STEP{idx} ({role}) のレスポンスから JSON を抽出できませんでした")

        # パース
        try:
            part_data = json.loads(json_text)
            print(f"[Gemini] STEP{idx}/3 ({role}) - JSON parsed successfully")
        except json.JSONDecodeError as exc:
            print(f"[ERROR] JSON parse failed for STEP{idx} ({role}): {exc}")
            print(f"[DEBUG] JSON text (first 1000 chars): {json_text[:1000]}")
            raise RuntimeError(f"STEP{idx} ({role}) の JSON 解析に失敗: {exc}")

        # 期待されるキーの検証（roleベース）
        expected_keys_map = {
            "metadata": ["title", "description"],
            "script": ["content"],
            "thumbnail": ["thumbnail"],
        }
        expected_keys = expected_keys_map.get(role, [])

        if expected_keys:
            missing = [key for key in expected_keys if key not in part_data]
            if missing:
                print(f"[ERROR] STEP{idx} ({role}) missing keys: {missing}")
                print(f"[DEBUG] Received keys: {list(part_data.keys())}")
                print(f"[DEBUG] Part data: {json.dumps(part_data, ensure_ascii=False, indent=2)[:500]}")
                raise RuntimeError(f"STEP{idx} ({role}) で必要なキーが不足しています: {missing}")

        if role == "thumbnail":
            thumbnail_obj = part_data.get("thumbnail")
            if not isinstance(thumbnail_obj, dict):
                raise RuntimeError(f"STEP{idx} ({role}) の thumbnail が不正です")
            missing_thumb_keys = [k for k in ["main_text", "sub_texts"] if k not in thumbnail_obj]
            if missing_thumb_keys:
                raise RuntimeError(f"STEP{idx} ({role}) の thumbnail に必要なキーが不足しています: {missing_thumb_keys}")
            if not isinstance(thumbnail_obj.get("sub_texts"), list):
                raise RuntimeError(f"STEP{idx} ({role}) の thumbnail.sub_texts は配列である必要があります")

        # マージ
        merged_script.update(part_data)
        print(f"[Gemini] STEP{idx}/3 ({role}) - merged into final script")

    # マージ結果の検証
    print("Validating merged script structure...")
    required_keys = ["title", "description", "content", "thumbnail"]
    missing_keys = [key for key in required_keys if key not in merged_script]

    if missing_keys:
        print(f"[ERROR] Merged script is missing required keys: {missing_keys}")
        print(f"[DEBUG] Current keys: {list(merged_script.keys())}")
        raise RuntimeError(f"台本に必須項目が不足しています: {missing_keys}")

    print("Script generation completed successfully")
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
