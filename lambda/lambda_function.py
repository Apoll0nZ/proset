import json
import os
import re
import hashlib
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

import boto3
import feedparser
import requests

# DynamoDBリソースの初期化（再利用効率のためlambda_handler外で実行）
dynamodb = boto3.resource('dynamodb')
ddb_table_name = os.environ.get('MY_DDB_TABLE_NAME')
ddb_table = dynamodb.Table(ddb_table_name) if ddb_table_name else None


"""
AWS Lambda エントリーポイント。

役割:
- RSS 収集
- Gemini によるニュース選別スコアリング
- Gemini による台本 JSON 生成
- DynamoDB による重複チェック (VideoHistory テーブル: 参照のみ)
- S3 への台本 JSON 保存
- GitHub Repository Dispatch による動画生成ワークフロー起動

※ この Lambda は DynamoDB への書き込みは行わない。
"""


DDB_TABLE_NAME = os.environ.get("DDB_TABLE_NAME", "VideoHistory")
S3_BUCKET_NAME = os.environ.get("SCRIPT_S3_BUCKET", "")
S3_PREFIX = os.environ.get("SCRIPT_S3_PREFIX", "scripts/")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "")  # 形式: owner/repo
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_EVENT_TYPE = os.environ.get("GITHUB_EVENT_TYPE", "generate_video")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL_NAME = os.environ.get(
    "GEMINI_MODEL_NAME", "gemini-1.5-flash"
)  # 環境変数を最優先、デフォルトは1.5-flash
GEMINI_API_BASE_URL = os.environ.get(
    "GEMINI_API_BASE_URL", "https://generativelanguage.googleapis.com"
)
GEMINI_API_VERSION = os.environ.get("GEMINI_API_VERSION", "v1") 
AWS_REGION = os.environ.get("AWS_REGION", "ap-northeast-1")

RSS_SOURCES_PATH = os.path.join(os.path.dirname(__file__), "rss_sources.json")
GEMINI_SCRIPT_PROMPT_PATH = os.path.join(
    os.path.dirname(__file__), "gemini_script_prompt.txt"
)


dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
s3_client = boto3.client("s3", region_name=AWS_REGION)


def call_gemini_generate_content(
    prompt: str,
) -> Optional[str]:
    """
    Gemini 3 Flash Preview を呼び出し、レスポンス文字列を返す。
    503エラーにはリトライロジックで対応。
    429 (Quota) の場合はログを残して None を返し、呼び出し側で Graceful Exit できるようにする。
    """

    api_key = os.environ.get("GEMINI_API_KEY")
    model_name = os.environ.get("GEMINI_MODEL_NAME", "gemini-3-flash-preview")
    api_version = os.environ.get("GEMINI_API_VERSION", "v1beta")

    if not api_key:
        raise ValueError("環境変数 GEMINI_API_KEY が設定されていません。")

    url = (
        f"https://generativelanguage.googleapis.com/{api_version}/models/"
        f"{model_name}:generateContent?key={api_key}"
    )

    headers = {"Content-Type": "application/json"}
    payload: Dict[str, Any] = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.1,
            "max_output_tokens": 8192
        },
        "safetySettings": [
            {
                "category": "HARM_CATEGORY_HARASSMENT",
                "threshold": "BLOCK_ONLY_HIGH",
            },
            {
                "category": "HARM_CATEGORY_HATE_SPEECH",
                "threshold": "BLOCK_ONLY_HIGH",
            },
            {
                "category": "HARM_CATEGORY_SEXUALLY_EXPLICIT",
                "threshold": "BLOCK_ONLY_HIGH",
            },
            {
                "category": "HARM_CATEGORY_DANGEROUS_CONTENT",
                "threshold": "BLOCK_ONLY_HIGH",
            },
        ],
    }

    # 503エラー用のリトライロジック（タイムアウト延長に伴い回数削減）
    max_retries = 2
    for attempt in range(max_retries):
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=(5, 90))
        except requests.RequestException as exc:
            print(f"Error calling Gemini API: {str(exc)}")
            if attempt < max_retries - 1:
                wait_time = (attempt + 1) * 5  # 5秒, 10秒, 15秒, 20秒
                print(f"Retrying in {wait_time} seconds... (attempt {attempt + 1}/{max_retries})")
                time.sleep(wait_time)
                continue
            return None

        # 503エラーの場合はリトライ
        if response.status_code == 503:
            print(f"Gemini API overloaded (503). Attempt {attempt + 1}/{max_retries}")
            if attempt < max_retries - 1:
                wait_time = (attempt + 1) * 5  # 5秒, 10秒, 15秒, 20秒
                print(f"Retrying in {wait_time} seconds...")
                time.sleep(wait_time)
                continue
            else:
                print("Max retries reached for 503 error. Giving up.")
                return None

        if response.status_code == 429:
            print("API quota limit reached. Skipping this cycle.")
            print(f"ERROR DETAILS: {response.text}")
            print(f"STATUS CODE: {response.status_code}")
            print(f"HEADERS: {dict(response.headers)}")
            # APIキーの断片を確認（セキュリティのため最初と最後の3文字のみ）
            if api_key and len(api_key) > 6:
                print(f"API KEY: {api_key[:3]}...{api_key[-3:]}")
            else:
                print("API KEY: Not available or too short")
            return None

        if response.status_code != 200:
            print(f"Gemini API Error: Status {response.status_code}")
            print(f"Response Body: {response.text}")
            response.raise_for_status()

        # 成功した場合、レスポンス処理へ
        break

    print(f"DEBUG: Raw Gemini Response: {response.text}")

    if not response.text or not response.text.strip():
        print("ERROR: Empty response from Gemini API")
        return None

    data = response.json()

    candidates = data.get("candidates", [])
    if not candidates:
        print(f"DEBUG: Response data without candidates: {data}")
        return None

    content = candidates[0].get("content", {})
    parts = content.get("parts", [])
    if not parts:
        print(f"DEBUG: Response data without parts: {data}")
        return None

    # Gemini 3では複数のテキストパートが返るため、全て結合
    text_parts = [part.get("text", "") for part in parts if part.get("text")]
    if not text_parts:
        print(f"DEBUG: No text parts found in response: {data}")
        return None

    text = "\n".join(text_parts).strip()
    if not text:
        print(f"DEBUG: Response data with empty text: {data}")
        return None

    return text


def extract_json_text(response_text: str) -> Optional[str]:
    """Gemini 応答から最外部の JSON ブロックを頑健に抽出する。"""
    if not response_text:
        return None

    # ログ出力は文字数制限をかけてLambdaが止まらないようにする
    print(f"DEBUG: Extracting JSON from response (length: {len(response_text)})")
    if len(response_text) > 500:
        print(f"DEBUG: Response preview (first 200 chars): {response_text[:200]}")
    
    # 正規表現で最初の{から最後の}までを強制的に抽出
    match = re.search(r'\{.*\}', response_text, re.DOTALL)
    if match:
        json_text = match.group(0).strip()
        print(f"DEBUG: Extracted JSON length: {len(json_text)}")
        return json_text
    
    # フォールバック：{と}の位置で抽出
    start = response_text.find('{')
    end = response_text.rfind('}')
    if start != -1 and end != -1 and end > start:
        json_text = response_text[start : end + 1].strip()
        print(f"DEBUG: Fallback extracted JSON length: {len(json_text)}")
        return json_text
    
    print("ERROR: No JSON structure found in response")
    return None


def load_rss_sources() -> Dict[str, List[str]]:
    """rss_sources.json を読み込む。"""
    with open(RSS_SOURCES_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def fetch_latest_entry(feed_url: str) -> Optional[feedparser.FeedParserDict]:
    """指定 RSS フィードから最新のエントリを取得（軽量化版・最大3件）。"""
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
    }
    
    try:
        # requestsでRSSフィードを取得
        response = requests.get(feed_url, headers=headers, timeout=10)
        response.raise_for_status()
        
        # 取得した内容をfeedparserで解析
        feed = feedparser.parse(response.content)
    except Exception:
        return None
        
    if not feed.entries:
        return None
    # 最新3件まで取得し、最初の有効なエントリを返す
    for i, entry in enumerate(feed.entries[:3]):
        if entry.get("title"):
            return entry
    return feed.entries[0] if feed.entries else None


def build_topic_summary(entry: feedparser.FeedParserDict) -> str:
    """RSS エントリから簡易な topic_summary を生成（軽量化版）。"""
    title = entry.get("title", "")
    summary = entry.get("summary", "") or entry.get("description", "")
    text = f"{title}\n{summary}"
    # 文字数制限を大幅に削減（Gemini 3 の思考時間短縮のため）
    return text.strip()[:800]


def calc_content_hash(topic_summary: str) -> str:
    """topic_summary から SHA256 ハッシュを生成。"""
    return hashlib.sha256(topic_summary.encode("utf-8")).hexdigest()


def exists_in_dynamodb(content_hash: str) -> bool:
    """DynamoDBにcontent_hashが存在するかチェック"""
    if not ddb_table:
        return False
    
    try:
        response = ddb_table.get_item(
            Key={'content_hash': content_hash},
            ProjectionExpression='content_hash'
        )
        return 'Item' in response
    except Exception as e:
        print(f"Error checking DynamoDB: {str(e)}")
        return False


def create_processing_record(content_hash: str, source_url: str, topic_summary: str) -> bool:
    """status='processing'でレコードを作成（重複防止付き）"""
    if not ddb_table:
        return True
    
    try:
        ddb_table.put_item(
            Item={
                'content_hash': content_hash,
                'status': 'processing',
                'source_url': source_url,
                'topic_summary': topic_summary,
                'created_at': datetime.now(timezone.utc).isoformat(),
                'ttl': int((datetime.now(timezone.utc) + timedelta(days=180)).timestamp())
            },
            ConditionExpression='attribute_not_exists(content_hash)'
        )
        print(f"Created processing record for {content_hash}")
        return True
    except Exception as e:
        if 'ConditionalCheckFailedException' in str(e):
            print(f"Record already exists for {content_hash}, skipping...")
            return False
        print(f"Error creating processing record: {str(e)}")
        return False


def update_completed_record(content_hash: str, video_title: str, topic_summary: str) -> bool:
    """status='completed'でレコードを更新"""
    if not ddb_table:
        return True
    
    try:
        ddb_table.update_item(
            Key={'content_hash': content_hash},
            UpdateExpression='SET #status = :status, video_title = :title, topic_summary = :summary',
            ExpressionAttributeNames={'#status': 'status'},
            ExpressionAttributeValues={
                ':status': 'completed',
                ':title': video_title,
                ':summary': topic_summary
            }
        )
        print(f"Updated record to completed for {content_hash}")
        return True
    except Exception as e:
        print(f"Error updating completed record: {str(e)}")
        return False


def fetch_reaction_summary(group_b_sources: List[str]) -> Tuple[str, str]:
    """
    グループB（コミュニティ反応ソース）から簡易な反応概要を取得（User-Agent付き）。
    ここではサンプルとして最初のフィードのタイトルを軽くまとめるだけ。
    戻り値: (サイト名 or URL, 要約テキスト)
    """
    if not group_b_sources:
        return "", ""

    url = group_b_sources[0]
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
    }
    
    try:
        # requestsでRSSフィードを取得
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        
        # 取得した内容をfeedparserで解析
        feed = feedparser.parse(response.content)
    except Exception:
        return url, ""
        
    if not feed.entries:
        return url, ""

    # 最新エントリ3件のタイトルをまとめる（軽量化）
    titles = [e.get("title", "") for e in feed.entries[:3] if e.get("title")]
    summary = " / ".join(titles)
    return feed.feed.get("title", url), summary


def load_script_prompt() -> str:
    """gemini_script_prompt.txt の内容を読み込む。"""
    with open(GEMINI_SCRIPT_PROMPT_PATH, "r", encoding="utf-8") as f:
        return f.read()


def generate_script_with_gemini(
    base_prompt: str,
    site_name_a: str,
    title_a: str,
    summary_a: str,
    site_name_b: str,
    summary_b: str,
) -> Dict[str, Any]:
    """
    Gemini に JSON 台本生成を依頼。
    base_prompt (gemini_script_prompt.txt) を元に、追記プロンプトを付与する。
    """
    prompt = (
        "まずステップバイステップで論理的に考え、その後に結果をJSONで出力してください。\n"
        + base_prompt
        + "\n\n"
        + "### 入力データ具体値\n"
        + f"[事実ソース（サイト名: {site_name_a}）]\n"
        + f"タイトル: {title_a}\n"
        + f"内容要約: {summary_a}\n\n"
        + f"[反応ソース（コミュニティ名: {site_name_b or '（反応ソース無し）'}）]\n"
        + f"話題のトピック: {summary_b or '（反応ソースが取得できなかった場合は、一般的な反応を仮定して生成してください）'}\n\n"
        + "必ず有効なJSON形式だけで回答してください。余計な解説やMarkdownのコードブロック（json ... ）は含めないでください。"
    )

    try:
        text = call_gemini_generate_content(prompt)
    except Exception as e:
        print(f"Failed to get script from Gemini: {str(e)}")
        # 空の台本データを返して処理を継続
        return {
            "title": "台本生成失敗",
            "description": "台本生成に失敗しました",
            "thumbnail": {
                "main_text": "生成失敗",
                "sub_texts": ["エラー"]
            },
            "content": {
                "topic_summary": "生成失敗",
                "script_parts": []
            },
            "meta": {"error": "Failed to generate script", "reason": str(e)}
        }
    
    json_text = extract_json_text(text)
    if not json_text:
        print("ERROR: Failed to extract JSON block from Gemini response.")
        print(f"DEBUG: Raw response: {text}")
        return {
            "title": "台本解析失敗",
            "description": "JSONパースに失敗しました",
            "thumbnail": {
                "main_text": "パース失敗",
                "sub_texts": ["エラー"]
            },
            "content": {
                "topic_summary": "パース失敗",
                "script_parts": []
            },
            "meta": {"error": "Invalid response format", "raw_response": text[:500]}
        }

    try:
        data = json.loads(json_text)
    except json.JSONDecodeError as e:
        # フォールバック：末尾の}が欠けている場合に補完してリトライ
        if not json_text.endswith('}'):
            print(f"DEBUG: JSON seems incomplete, trying to add closing brace")
            try:
                data = json.loads(json_text + '}')
                print("SUCCESS: JSON parsed after adding closing brace")
                return data
            except json.JSONDecodeError as e2:
                print(f"Failed to parse even after adding brace: {str(e2)}")
        
        # デバッグログ：パースしようとした文字列の先頭100文字を出力
        print(f"Failed to parse JSON response: {str(e)}")
        print(f"Attempted to parse (first 100 chars): {json_text[:100]}")
        print(f"Full cleaned text length: {len(json_text)}")
        # 生レスポンスが非常に長い場合は制限して出力
        if len(text) > 500:
            print(f"DEBUG: Raw response (first 200 chars): {text[:200]}")
        else:
            print(f"DEBUG: Raw response: {text}")
        
        # JSON解析に失敗した場合は空の台本データを返す
        return {
            "title": "台本解析失敗",
            "description": "JSONパースに失敗しました",
            "thumbnail": {
                "main_text": "パース失敗",
                "sub_texts": ["エラー"]
            },
            "content": {
                "topic_summary": "パース失敗",
                "script_parts": []
            },
            "meta": {"error": "Failed to parse JSON", "reason": str(e), "raw_response": text[:500]}
        }

    return data


def save_script_to_s3(
    script_json: Dict[str, Any], content_hash: str, source_url: str, published_at: str
) -> str:
    """
    台本 JSON を S3 に保存。
    戻り値: S3 オブジェクトキー
    """
    # バケット名の確認とデバッグ出力
    bucket_name = S3_BUCKET_NAME or "youtube-auto-3"  # 環境変数がなければ直接指定
    print(f"DEBUG: Using S3 bucket: {bucket_name}")
    print(f"DEBUG: S3_BUCKET_NAME env var: {S3_BUCKET_NAME}")
    
    if not bucket_name:
        raise RuntimeError("S3 バケット名が設定されていません。")

    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    key = f"{S3_PREFIX.rstrip('/')}/{now}_{content_hash}.json"
    
    print(f"DEBUG: S3 key to save: {key}")

    # メタ情報を付加して保存
    enriched = dict(script_json)  # script_json全体をコピー
    enriched.setdefault("meta", {})
    enriched["meta"].update(
        {
            "content_hash": content_hash,
            "source_url": source_url,
            "published_at": published_at,
            "created_at": now,
        }
    )

    body = json.dumps(enriched, ensure_ascii=False, indent=2).encode("utf-8")
    
    # デバッグログ：保存するJSONの文字数を確認
    json_str = json.dumps(enriched, ensure_ascii=False, indent=2)
    print(f"DEBUG: Saving JSON with {len(json_str)} characters")
    print(f"DEBUG: JSON preview (first 200 chars): {json_str[:200]}")
    if len(json_str) < 1000:
        print(f"WARNING: JSON seems too small ({len(json_str)} chars). Expected 3000+ chars.")
    
    try:
        print(f"DEBUG: Attempting to save to S3...")
        print(f"DEBUG: Body size: {len(body)} bytes")
        
        s3_client.put_object(
            Bucket=bucket_name, 
            Key=key, 
            Body=body, 
            ContentType="application/json; charset=utf-8"
        )
        
        print(f"SUCCESS: S3 save completed. Bucket: {bucket_name}, Key: {key}")
        return key
        
    except Exception as e:
        print(f"S3 ERROR: {str(e)}")
        print(f"S3 ERROR TYPE: {type(e).__name__}")
        print(f"S3 ERROR DETAILS: {e}")
        raise e


def trigger_github_dispatch(s3_key: str, content_hash: str) -> None:
    """
    GitHub Repository Dispatch を発火し、動画生成ワークフローを起動。
    """
    if not (GITHUB_REPO and GITHUB_TOKEN):
        raise RuntimeError("GITHUB_REPO と GITHUB_TOKEN が設定されていません。")

    url = f"https://api.github.com/repos/{GITHUB_REPO}/dispatches"
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"token {GITHUB_TOKEN}",
    }
    payload = {
        "event_type": GITHUB_EVENT_TYPE,
        "client_payload": {
            "s3_bucket": S3_BUCKET_NAME,
            "s3_key": s3_key,
            "content_hash": content_hash,
        },
    }

    resp = requests.post(url, headers=headers, json=payload, timeout=10)
    if resp.status_code not in (200, 201, 204):
        raise RuntimeError(f"GitHub dispatch 失敗: {resp.status_code} {resp.text}")


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Lambda メインハンドラー。
    - グループAから最新ニュースを 1 件取得し、適合度スコアが 7 以上であれば台本生成〜S3保存〜GitHub Dispatch を実行。
    - DynamoDB に content_hash が既に存在する場合はスキップ。
    """
    sources = load_rss_sources()
    group_a = sources.get("group_a", [])
    group_b = sources.get("group_b", [])

    if not group_a:
        raise RuntimeError("rss_sources.json に group_a が定義されていません。")

    result: Dict[str, Any] = {
        "status": "skipped",
        "reason": "no_items",
    }

    for idx, primary_url in enumerate(group_a):
        if idx >= 5:
            print(f"Checked {idx} articles without finding score >= 75. Breaking loop.")
            break

        primary_entry = fetch_latest_entry(primary_url)
        if primary_entry is None:
            print(f"Failed to fetch article from {primary_url}. Trying next...")
            continue

        topic_summary = build_topic_summary(primary_entry)
        content_hash = calc_content_hash(topic_summary)

        # 事前チェック：DynamoDBで重複確認
        if exists_in_dynamodb(content_hash):
            print(f"Article already exists in DB: {content_hash}. Trying next...")
            continue

        reaction_site_name, reaction_summary = fetch_reaction_summary(group_b)
        script_prompt = load_script_prompt()

        site_name_a = primary_entry.get("source", {}).get("title") if primary_entry.get("source") else ""
        site_name_a = site_name_a or primary_entry.get("feedburner_origlink", primary_url)
        title_a = primary_entry.get("title", "")
        published_at = ""
        if "published_parsed" in primary_entry and primary_entry.published_parsed:
            dt = datetime(*primary_entry.published_parsed[:6], tzinfo=timezone.utc)
            published_at = dt.isoformat()
        source_url = primary_entry.get("link", primary_url)

        combined_prompt = (
            "まずステップバイステップで論理的に考え、その後に結果をJSONで出力してください。"
            "あなたはテクノロジーニュースの編集長です。以下のRSSニュースを分析し、YouTube動画としての適性を0-100で評価してください。"
            "【重要】新製品発表、OSアップデート、市場統計、技術リーク、企業の買収・提携など具体的な出来事（Fact）には高得点（80-100点）を付与してください。"
            "【重要】思い出話、精神論、抽象的な評論、個人の感想など具体的なニュース価値の低い内容には低得点（0-40点）を付与してください。"
            "評価基準：技術的新規性、市場への影響、視聴者への有用性、情報の新鮮さを重視してください。"
            "出力は必ず次のJSON形式でお願いします。"
            "```json\n{\n"
            "  \"score\": <0-100の整数>,\n"
            "  \"reasoning\": \"スコア判断の理由。100字以内。\",\n"
            "  \"script\": {\n"
            "    \"title\": \"動画タイトル\",\n"
            "    \"sections\": [\n"
            "      {\"heading\": \"導入\", \"body\": \"イントロダクションの本文\"},\n"
            "      {\"heading\": \"本編\", \"body\": \"本編の本文\"},\n"
            "      {\"heading\": \"まとめ\", \"body\": \"締めの本文\"}\n"
            "    ],\n"
            "    \"tags\": [\"タグ1\", \"タグ2\"]\n"
            "  }\n"
            "}\n```\n"
            f"# ニュースタイトル: {title_a}\n"
            f"# ニュース要約:\n{topic_summary}\n"
            f"# 反応ソース: {reaction_site_name}\n"
            f"# 反応概要: {reaction_summary or '該当なし'}\n"
            f"# 既定台本プロンプト:\n{script_prompt}\n"
        )

        gemini_response = call_gemini_generate_content(
            combined_prompt,
        )
        if gemini_response is None:
            print(f"Gemini API failed for article {idx + 1}. Trying next...")
            result = {
                "status": "skipped",
                "reason": "gemini_quota_or_error",
                "content_hash": content_hash,
                "attempted_articles": idx + 1,
            }
            continue

        json_text = extract_json_text(gemini_response)
        if not json_text:
            print(f"Failed to locate JSON block in Gemini response for article {idx + 1}. Trying next...")
            result = {
                "status": "skipped",
                "reason": "gemini_invalid_json",
                "content_hash": content_hash,
                "attempted_articles": idx + 1,
            }
            continue

        try:
            parsed = json.loads(json_text)
        except json.JSONDecodeError as exc:
            # フォールバック：末尾の}が欠けている場合に補完してリトライ
            if not json_text.endswith('}'):
                print(f"DEBUG: Combined JSON seems incomplete, trying to add closing brace for article {idx + 1}")
                try:
                    parsed = json.loads(json_text + '}')
                    print("SUCCESS: Combined JSON parsed after adding closing brace")
                except json.JSONDecodeError as e2:
                    print(f"Failed to parse combined JSON even after adding brace: {str(e2)}")
                    print(f"JSON parse error: {str(exc)}")
                    # 生レスポンスが非常に長い場合は制限して出力
                    if len(gemini_response) > 500:
                        print(f"DEBUG: Raw combined response (first 200 chars): {gemini_response[:200]}")
                    else:
                        print(f"DEBUG: Raw combined response: {gemini_response}")
                    result = {
                        "status": "skipped",
                        "reason": "gemini_invalid_json",
                        "content_hash": content_hash,
                        "attempted_articles": idx + 1,
                    }
                    continue
            else:
                print(f"JSON parse error: {str(exc)}")
                # 生レスポンスが非常に長い場合は制限して出力
                if len(gemini_response) > 500:
                    print(f"DEBUG: Raw combined response (first 200 chars): {gemini_response[:200]}")
                else:
                    print(f"DEBUG: Raw combined response: {gemini_response}")
                result = {
                    "status": "skipped",
                    "reason": "gemini_invalid_json",
                    "content_hash": content_hash,
                    "attempted_articles": idx + 1,
                }
                continue

        score = parsed.get("score")
        try:
            score_value = float(score)
        except (TypeError, ValueError):
            print(f"Invalid score value from Gemini: {score}")
            score_value = 0.0

        if score_value < 75.0:
            print(f"Article {idx + 1} score too low: {score_value}. Trying next...")
            continue  # 次の記事をチェック

        # 75点以上の記事が見つかった場合
        print(f"Found suitable article with score {score_value}: {title_a}")
        
        # 二重実行防止：status='processing'でレコード作成
        if not create_processing_record(content_hash, source_url, topic_summary):
            print(f"Failed to create processing record (likely duplicate). Trying next...")
            continue
        
        script_section = parsed.get("script", {})
        if not script_section:
            # 新しい形式では content.script_parts を使用
            content_section = parsed.get("content", {})
            if not content_section or not content_section.get("script_parts"):
                print(f"Missing script_parts in Gemini response: {parsed}")
                result = {
                    "status": "skipped",
                    "reason": "missing_script_parts",
                    "score": score_value,
                    "content_hash": content_hash,
                    "attempted_articles": idx + 1,
                }
                break
            
            # 新しい形式の台本データを構築（Gemini JSON全体をベースに）
            script_json = parsed.copy()  # GeminiのJSON全体をコピー
            # 不足しているメタデータを付加
            script_json["reasoning"] = parsed.get("reasoning", "")
            script_json["score"] = score_value
            print(f"DEBUG: Script JSON (new format): {script_json}")
        else:
            # 旧形式の互換性（念のため）
            script_json = parsed.copy()  # GeminiのJSON全体をコピー
            # 不足しているメタデータを付加
            script_json["reasoning"] = parsed.get("reasoning", "")
            script_json["score"] = score_value
            print(f"DEBUG: Script JSON (old format): {script_json}")

        s3_key = save_script_to_s3(
            script_json=script_json,
            content_hash=content_hash,
            source_url=source_url,
            published_at=published_at,
        )
        print(
            f"SUCCESS: Uploaded script for '{script_json.get('title', 'Untitled')}' with score {score_value:.1f}"
        )

        # 完了ステータスの更新
        video_title = script_json.get('title', '')
        if not video_title:
            # 新しい形式では content.script_parts を使用
            content_section = script_json.get('content', {})
            if content_section:
                video_title = content_section.get('title', '')
        
        if not video_title:
            video_title = title_a or 'Untitled'
            
        print(f"DEBUG: Using video_title for DB: {video_title}")
        update_completed_record(content_hash, video_title, topic_summary)

        trigger_github_dispatch(s3_key=s3_key, content_hash=content_hash)

        result = {
            "status": "ok",
            "content_hash": content_hash,
            "score": score_value,
            "s3_bucket": S3_BUCKET_NAME or "youtube-auto-3",
            "s3_key": s3_key,
            "source_url": source_url,
            "published_at": published_at,
            "attempted_articles": idx + 1,
        }
        break

    return result

