import json
import os
import re
import hashlib
import time
import random
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
    "GEMINI_MODEL_NAME", "gemini-2.5-flash-lite"
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
            "max_output_tokens": 4096  # 出力トークンを削減して高速化
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

    # 短縮された指数バックオフ（最大30秒）
    max_retries = 3
    for attempt in range(max_retries):
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=(5, 60))  # タイムアウト短縮
        except requests.RequestException as exc:
            print(f"Error calling Gemini API: {str(exc)}")
            if attempt < max_retries - 1:
                wait_time = min(2 ** attempt, 30)  # 1, 2, 4, 最大30秒
                print(f"Retrying in {wait_time} seconds... (attempt {attempt + 1}/{max_retries})")
                time.sleep(wait_time)
                continue
            return None

        # 503エラーの場合はリトライ
        if response.status_code == 503:
            print(f"Gemini API overloaded (503). Attempt {attempt + 1}/{max_retries}")
            if attempt < max_retries - 1:
                wait_time = min(2 ** attempt, 30)  # 1, 2, 4, 最大30秒
                print(f"Retrying in {wait_time} seconds...")
                time.sleep(wait_time)
                continue
            else:
                print("Max retries reached for 503 error. Giving up.")
                return None

        if response.status_code == 429:
            print("API quota limit reached (429). Using short backoff...")
            if attempt < max_retries - 1:
                wait_time = min(5 * (attempt + 1), 30)  # 5, 10, 15, 最大30秒
                print(f"Backing off for {wait_time} seconds... (attempt {attempt + 1}/{max_retries})")
                time.sleep(wait_time)
                continue
            else:
                print("Max retries reached for 429 error. Skipping.")
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


def fetch_multiple_entries(feed_url: str, max_entries: int = 15) -> List[feedparser.FeedParserDict]:
    """指定 RSS フィードから複数のエントリを取得（User-Agent付き）。"""
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
    }
    
    try:
        # requestsでRSSフィードを取得
        response = requests.get(feed_url, headers=headers, timeout=10)
        response.raise_for_status()
        
        # 取得した内容をfeedparserで解析
        feed = feedparser.parse(response.content)
    except Exception as e:
        print(f"Error fetching feed {feed_url}: {str(e)}")
        return []
        
    if not feed.entries:
        return []
    
    # 有効なエントリのみを返す（最大max_entries件）
    valid_entries = []
    for entry in feed.entries[:max_entries]:
        if entry.get("title"):
            valid_entries.append(entry)
    
    return valid_entries


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


def update_rejected_record(content_hash: str, source_url: str, topic_summary: str, score: float) -> bool:
    """status='rejected'でレコードを更新（75点未満の記事用）"""
    if not ddb_table:
        return True
    
    try:
        ddb_table.put_item(
            Item={
                'content_hash': content_hash,
                'status': 'rejected',
                'source_url': source_url,
                'topic_summary': topic_summary,
                'score': score,
                'created_at': datetime.now(timezone.utc).isoformat(),
                'ttl': int((datetime.now(timezone.utc) + timedelta(days=90)).timestamp())
            }
        )
        print(f"Saved rejected record for {content_hash} with score {score}")
        return True
    except Exception as e:
        print(f"Error saving rejected record: {str(e)}")
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
    print(f"[DEBUG] GitHub dispatch attempt:")
    print(f"  GITHUB_REPO: {GITHUB_REPO}")
    print(f"  GITHUB_TOKEN: {'SET' if GITHUB_TOKEN else 'NOT_SET'}")
    print(f"  GITHUB_EVENT_TYPE: {GITHUB_EVENT_TYPE}")
    
    if not (GITHUB_REPO and GITHUB_TOKEN):
        print(f"[ERROR] GitHub credentials missing:")
        print(f"  GITHUB_REPO empty: {not GITHUB_REPO}")
        print(f"  GITHUB_TOKEN empty: {not GITHUB_TOKEN}")
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
    
    print(f"[DEBUG] Request details:")
    print(f"  URL: {url}")
    print(f"  Event type: {GITHUB_EVENT_TYPE}")
    print(f"  Payload: {payload}")

    try:
        print(f"[INFO] Triggering GitHub Actions for {GITHUB_REPO}...")
        resp = requests.post(url, headers=headers, json=payload, timeout=10)
        
        print(f"[DEBUG] Response details:")
        print(f"  Status code: {resp.status_code}")
        print(f"  Response headers: {dict(resp.headers)}")
        print(f"  Response text: {resp.text}")
        
        if resp.status_code not in (200, 201, 204):
            raise RuntimeError(f"GitHub dispatch 失敗: {resp.status_code} {resp.text}")
        
        print("[SUCCESS] GitHub Actions triggered successfully")
        
    except requests.exceptions.RequestException as e:
        print(f"[ERROR] Network error during GitHub dispatch: {e}")
        raise RuntimeError(f"GitHub dispatch ネットワークエラー: {e}")


def evaluate_articles_batch(articles: List[Dict[str, Any]], reaction_site_name: str, reaction_summary: str, script_prompt: str, context: Any) -> Optional[Dict[str, Any]]:
    """
    4件ずつ × 2回のバッチ処理で最も適した記事を1つ選択する。
    """
    # タイムアウトチェック：残り2分未満なら処理を終了
    remaining_time_ms = context.get_remaining_time_in_millis()
    if remaining_time_ms < 120000:  # 2分 = 120000ms
        print(f"Timeout approaching: {remaining_time_ms}ms remaining. Stopping evaluation.")
        return None
    
    # 評価用の記事データを構築（最大8件に制限）
    articles_to_eval = articles[:8]
    
    if len(articles_to_eval) < 4:
        print(f"Insufficient articles for batch evaluation: {len(articles_to_eval)}")
        return None
    
    # 1回目のバッチ：最初の4件を評価
    print("Step 4a: First batch evaluation (articles 1-4)...")
    first_batch = articles_to_eval[:4]
    first_result = _evaluate_single_batch(first_batch, "first")
    
    if first_result is None:
        print("First batch evaluation failed.")
        return None
    
    # 待機処理
    print("Waiting 10 seconds before second batch...")
    time.sleep(10)
    
    # 2回目のバッチ：次の4件を評価
    print("Step 4b: Second batch evaluation (articles 5-8)...")
    second_batch = articles_to_eval[4:8]
    second_result = _evaluate_single_batch(second_batch, "second")
    
    if second_result is None:
        print("Second batch evaluation failed.")
        return None
    
    # 待機処理
    print("Waiting 10 seconds before final selection...")
    time.sleep(10)
    
    # 最終選定：2つのバッチの上位候補からベスト記事を選択
    print("Step 4c: Final selection from batch winners...")
    final_candidates = [first_result, second_result]
    final_result = _select_final_winner(final_candidates)
    
    return final_result


def _evaluate_single_batch(articles: List[Dict[str, Any]], batch_name: str) -> Optional[Dict[str, Any]]:
    """
    4件の記事から最適なものを1つ選択する単一バッチ処理。
    """
    articles_text = ""
    
    for i, article_data in enumerate(articles):
        entry = article_data['entry']
        title = entry.get('title', 'Untitled')
        articles_text += f"\n{i+1}. ID:{article_data['content_hash']} | {title}\n"
    
    batch_prompt = (
        f"以下の4つの記事からYouTube動画として最も適した記事を1つだけ選んでください。\n"
        "新製品発表、OSアップデート、技術リーク、企業買収などの具体的な出来事を優先。\n"
        f"\n記事リスト:\n{articles_text}\n\n"
        "最適な記事のID番号（1-4）のみを数字で回答してください。"
    )
    
    print(f"Evaluating {batch_name} batch with {len(articles)} articles...")
    response = call_gemini_generate_content(batch_prompt)
    
    if response is None:
        print(f"{batch_name} batch evaluation failed due to API error.")
        return None
    
    # シンプルな数字回答を処理
    response = response.strip()
    print(f"{batch_name} batch raw response: {response}")
    
    # 数字のみを抽出（1-4の範囲）
    import re
    match = re.search(r'[1-4]', response)
    if not match:
        print(f"No valid article number found in {batch_name} batch response.")
        return None
    
    selected_num = int(match.group())
    if selected_num < 1 or selected_num > len(articles):
        print(f"Invalid article number in {batch_name} batch: {selected_num}")
        return None
    
    # 選択された記事を取得
    selected_article = articles[selected_num - 1]
    
    return {
        "article": selected_article,
        "score": 80,  # バッチ評価では固定スコア
        "reason": f"{batch_name} batch winner"
    }


def _select_final_winner(candidates: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """
    2つのバッチ勝者から最終的なベスト記事を選択する。
    """
    if len(candidates) != 2:
        print(f"Expected 2 candidates, got {len(candidates)}")
        return None
    
    articles_text = ""
    for i, candidate in enumerate(candidates):
        article = candidate['article']
        entry = article['entry']
        title = entry.get('title', 'Untitled')
        articles_text += f"\n{i+1}. ID:{article['content_hash']} | {title}\n"
    
    final_prompt = (
        f"以下の2つの記事から、最もYouTube動画に適した記事を1つだけ選んでください。\n"
        "技術的新規性、市場への影響、視聴者への有用性を総合的に評価してください。\n"
        f"\n最終候補:\n{articles_text}\n\n"
        "最適な記事のID番号（1-2）のみを数字で回答してください。"
    )
    
    print("Selecting final winner from 2 candidates...")
    response = call_gemini_generate_content(final_prompt)
    
    if response is None:
        print("Final selection failed due to API error.")
        return None
    
    response = response.strip()
    print(f"Final selection raw response: {response}")
    
    # 数字のみを抽出（1-2の範囲）
    import re
    match = re.search(r'[1-2]', response)
    if not match:
        print("No valid article number found in final selection response.")
        return None
    
    selected_num = int(match.group())
    if selected_num < 1 or selected_num > len(candidates):
        print(f"Invalid article number in final selection: {selected_num}")
        return None
    
    # 最終勝者を取得
    final_winner = candidates[selected_num - 1]
    final_winner["reason"] = "Final batch winner"
    
    print(f"Selected final winner: {final_winner['article']['entry'].get('title', 'Untitled')}")
    
    return final_winner


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Lambda メインハンドラー。
    - RSSソースから全記事を一括収集し、ランダムにシャッフル
    - バッチ評価で最適な記事を1つ選択（APIリクエスト削減）
    - 75点以上の記事は台本生成〜S3保存〜GitHub Dispatch を実行
    - タイムアウト管理と短縮された指数バックオフを実装
    """
    # タイムアウトチェック：開始時点で残り時間が少ない場合は早期終了
    remaining_time_ms = context.get_remaining_time_in_millis()
    if remaining_time_ms < 300000:  # 5分 = 300000ms
        print(f"Insufficient time remaining: {remaining_time_ms}ms. Exiting early.")
        return {
            "status": "timeout",
            "reason": "insufficient_time",
            "remaining_ms": remaining_time_ms
        }
    sources = load_rss_sources()
    group_a = sources.get("group_a", [])
    group_b = sources.get("group_b", [])

    if not group_a:
        raise RuntimeError("rss_sources.json に group_a が定義されていません。")

    result: Dict[str, Any] = {
        "status": "skipped",
        "reason": "no_items",
    }

    # ステップ1: RSSソースから全記事を一括収集
    print("Step 1: Collecting all articles from RSS sources...")
    all_articles = []
    
    for feed_url in group_a:
        # タイムアウトチェック
        if context.get_remaining_time_in_millis() < 240000:  # 4分
            print("Timeout approaching during RSS collection. Stopping.")
            break
            
        print(f"Fetching from {feed_url}...")
        entries = fetch_multiple_entries(feed_url, max_entries=10)  # 取得数を削減
        for entry in entries:
            topic_summary = build_topic_summary(entry)
            content_hash = calc_content_hash(topic_summary)
            
            article_data = {
                'entry': entry,
                'topic_summary': topic_summary,
                'content_hash': content_hash,
                'source_url': entry.get("link", feed_url)
            }
            all_articles.append(article_data)
    
    print(f"Total articles collected: {len(all_articles)}")
    
    if not all_articles:
        print("No articles collected from RSS sources.")
        return result
    
    # ステップ2: 記事リストをランダムにシャッフル
    print("Step 2: Shuffling articles randomly...")
    random.shuffle(all_articles)
    
    # ステップ3: DBで既存記事をフィルタリング
    print("Step 3: Filtering out existing articles from DB...")
    new_articles = []
    for article_data in all_articles:
        # タイムアウトチェック
        if context.get_remaining_time_in_millis() < 180000:  # 3分
            print("Timeout approaching during DB filtering. Stopping.")
            break
            
        if not exists_in_dynamodb(article_data['content_hash']):
            new_articles.append(article_data)
    
    print(f"New articles after DB filtering: {len(new_articles)}")
    
    if not new_articles:
        print("No new articles found.")
        return result
    
    # ステップ4: バッチ評価で最適な記事を1つ選択
    print("Step 4: Batch evaluating articles to find the best one...")
    reaction_site_name, reaction_summary = fetch_reaction_summary(group_b)
    script_prompt = load_script_prompt()
    
    batch_result = evaluate_articles_batch(new_articles, reaction_site_name, reaction_summary, script_prompt, context)
    
    if batch_result is None:
        print("Batch evaluation failed or timed out.")
        return {
            "status": "skipped",
            "reason": "batch_evaluation_failed",
            "attempted_articles": len(new_articles)
        }
    
    selected_article = batch_result["article"]
    score_value = batch_result["score"]
    selection_reason = batch_result["reason"]
    
    entry = selected_article['entry']
    topic_summary = selected_article['topic_summary']
    content_hash = selected_article['content_hash']
    source_url = selected_article['source_url']
    
    print(f"Selected article with score {score_value}: {entry.get('title', 'Untitled')}")
    print(f"Selection reason: {selection_reason}")
    
    # スコアチェック
    if score_value < 75.0:
        print(f"Selected article score too low: {score_value}. Saving as rejected...")
        update_rejected_record(content_hash, source_url, topic_summary, score_value)
        return {
            "status": "skipped",
            "reason": "score_too_low",
            "score": score_value,
            "content_hash": content_hash
        }
    
    # タイムアウトチェック：台本生成前
    if context.get_remaining_time_in_millis() < 120000:  # 2分
        print("Timeout approaching before script generation. Stopping.")
        return {
            "status": "timeout",
            "reason": "timeout_before_script",
            "content_hash": content_hash
        }
    
    # 二重実行防止：status='processing'でレコード作成
    if not create_processing_record(content_hash, source_url, topic_summary):
        print(f"Failed to create processing record (likely duplicate). Exiting.")
        return {
            "status": "skipped",
            "reason": "duplicate_processing",
            "content_hash": content_hash
        }
    
    # ステップ5: 台本生成（簡略化）
    print("Step 5: Generating script...")
    
    # 待機処理（バッチ評価後のAPIレート制限対策）
    print("Waiting 10 seconds before script generation...")
    time.sleep(10)
    
    site_name_a = entry.get("source", {}).get("title") if entry.get("source") else ""
    site_name_a = site_name_a or entry.get("feedburner_origlink", source_url)
    title_a = entry.get("title", "")
    published_at = ""
    if "published_parsed" in entry and entry.published_parsed:
        dt = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
        published_at = dt.isoformat()

    # 簡略化された台本生成プロンプト
    simplified_prompt = (
        "以下の情報からYouTube動画台本をJSON形式で生成してください。\n"
        "タイトル：魅力的な動画タイトル\n"
        "セクション：導入・本編・まとめの3部構成\n"
        f"ニュースタイトル: {title_a}\n"
        f"ニュース要約: {topic_summary[:500]}\n"
        f"反応ソース: {reaction_site_name}\n"
        f"反応概要: {reaction_summary or '該当なし'}\n\n"
        "出力形式：\n"
        "```json\n{\n  \"title\": \"動画タイトル\",\n  \"sections\": [\n    {\"heading\": \"導入\", \"body\": \"イントロ\"},\n    {\"heading\": \"本編\", \"body\": \"本文\"},\n    {\"heading\": \"まとめ\", \"body\": \"締め\"}\n  ],\n  \"tags\": [\"タグ1\", \"タグ2\"]\n}\n```\n"
    )

    gemini_response = call_gemini_generate_content(simplified_prompt)
    if gemini_response is None:
        print("Script generation failed due to API error.")
        return {
            "status": "skipped",
            "reason": "script_generation_failed",
            "content_hash": content_hash
        }

    json_text = extract_json_text(gemini_response)
    if not json_text:
        print("Failed to extract JSON from script generation response.")
        return {
            "status": "skipped",
            "reason": "script_json_extraction_failed",
            "content_hash": content_hash
        }

    try:
        script_json = json.loads(json_text)
    except json.JSONDecodeError as exc:
        # フォールバック：末尾の}が欠けている場合に補完してリトライ
        if not json_text.endswith('}'):
            try:
                script_json = json.loads(json_text + '}')
                print("SUCCESS: Script JSON parsed after adding closing brace")
            except json.JSONDecodeError:
                print(f"Failed to parse script JSON: {str(exc)}")
                return {
                    "status": "skipped",
                    "reason": "script_json_parse_failed",
                    "content_hash": content_hash
                }
        else:
            print(f"Failed to parse script JSON: {str(exc)}")
            return {
                "status": "skipped",
                "reason": "script_json_parse_failed",
                "content_hash": content_hash
            }
    
    # メタデータを付加
    script_json["reasoning"] = selection_reason
    script_json["score"] = score_value
    script_json["source_score"] = score_value  # 元のスコアを保持

    # タイムアウトチェック：S3保存前
    if context.get_remaining_time_in_millis() < 60000:  # 1分
        print("Timeout approaching before S3 save. Stopping.")
        return {
            "status": "timeout",
            "reason": "timeout_before_s3",
            "content_hash": content_hash
        }

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
    video_title = script_json.get('title', title_a or 'Untitled')
    print(f"DEBUG: Using video_title for DB: {video_title}")
    update_completed_record(content_hash, video_title, topic_summary)

    # タイムアウトチェック：GitHub呼び出し前
    if context.get_remaining_time_in_millis() < 30000:  # 30秒
        print("Timeout approaching before GitHub dispatch. Skipping.")
        # S3保存だけでも成功とみなす
        result = {
            "status": "partial_success",
            "reason": "timeout_before_github",
            "content_hash": content_hash,
            "score": score_value,
            "s3_bucket": S3_BUCKET_NAME or "youtube-auto-3",
            "s3_key": s3_key,
            "source_url": source_url,
            "published_at": published_at,
        }
    else:
        trigger_github_dispatch(s3_key=s3_key, content_hash=content_hash)
        result = {
            "status": "ok",
            "content_hash": content_hash,
            "score": score_value,
            "s3_bucket": S3_BUCKET_NAME or "youtube-auto-3",
            "s3_key": s3_key,
            "source_url": source_url,
            "published_at": published_at,
        }

    return result

