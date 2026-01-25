import os
import sys

# 実行ファイルがある場所を取得し、packageフォルダを検索パスに追加
current_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.join(current_dir, "package"))

import hashlib
import json
import os
import random
import re
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

import boto3
import feedparser
import requests

# -----------------------------------------------------------------------------
# 環境変数
# -----------------------------------------------------------------------------
DYNAMODB_TABLE_NAME = os.environ["DYNAMODB_TABLE"]
S3_BUCKET = os.environ["S3_BUCKET"]
PENDING_PREFIX = os.environ.get("PENDING_PATH", "pending/")
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
GEMINI_MODEL_NAME = os.environ.get("GEMINI_MODEL_NAME", "gemini-2.5-flash-lite")
GEMINI_API_VERSION = os.environ.get("GEMINI_API_VERSION", "v1beta")
AWS_REGION = os.environ.get("MY_AWS_REGION", os.environ.get("AWS_REGION", "ap-northeast-1"))
RSS_SOURCES_PATH = os.path.join(os.path.dirname(__file__), "rss_sources.json")

# -----------------------------------------------------------------------------
# AWS クライアント
# -----------------------------------------------------------------------------
dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
ddb_table = dynamodb.Table(DYNAMODB_TABLE_NAME)
s3_client = boto3.client("s3", region_name=AWS_REGION)

# -----------------------------------------------------------------------------
# ユーティリティ
# -----------------------------------------------------------------------------
def _ensure_trailing_slash(value: str) -> str:
    return value if value.endswith("/") else value + "/"


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ttl(days: int = 1095) -> int:  # 3年間 = 1095日
    return int((datetime.now(timezone.utc) + timedelta(days=days)).timestamp())


PENDING_PREFIX = _ensure_trailing_slash(PENDING_PREFIX)

# -----------------------------------------------------------------------------
# Gemini 呼び出し
# -----------------------------------------------------------------------------
def call_gemini_generate_content(prompt: str) -> Optional[str]:
    if not GEMINI_API_KEY:
        raise RuntimeError("環境変数 GEMINI_API_KEY が設定されていません")

    url = (
        f"https://generativelanguage.googleapis.com/{GEMINI_API_VERSION}/models/"
        f"{GEMINI_MODEL_NAME}:generateContent?key={GEMINI_API_KEY}"
    )

    headers = {"Content-Type": "application/json"}
    payload: Dict[str, Any] = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.0,
            "max_output_tokens": 4096,
            "response_mime_type": "application/json"  # JSON出力を強制
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

        if response.status_code == 429:
            print(f"Gemini quota exceeded (429). attempt={attempt + 1}/{max_retries}")
            if attempt < max_retries - 1:
                wait_time = [15, 30, 60][attempt] if attempt < 3 else 60
                print(f"Backing off for {wait_time}s")
                time.sleep(wait_time)
                continue
            return None

        if response.status_code != 200:
            print(f"Gemini unexpected status: {response.status_code} {response.text}")
            response.raise_for_status()

        break

    if not response.text:
        print("Gemini response empty")
        return None

    data = response.json()
    candidates = data.get("candidates", [])
    if not candidates:
        print(f"Gemini response missing candidates: {data}")
        return None

    parts = candidates[0].get("content", {}).get("parts", [])
    if not parts:
        print(f"Gemini response missing parts: {data}")
        return None
    
    text_parts = [part.get("text", "") for part in parts if part.get("text")]
    if not text_parts:
        print(f"Gemini response parts exist but no text found: {data}")
        return None
        
    text = "\n".join(text_parts).strip()
    if not text:
        print(f"Gemini response text empty after join: {data}")
        return None

    return text


# -----------------------------------------------------------------------------
# RSS / 記事処理
# -----------------------------------------------------------------------------
def load_rss_sources() -> Dict[str, List[str]]:
    with open(RSS_SOURCES_PATH, "r", encoding="utf-8") as fp:
        return json.load(fp)


def fetch_multiple_entries(feed_url: str, max_entries: int = 15) -> List[feedparser.FeedParserDict]:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/91.0.4472.124 Safari/537.36"
        ),
        "Accept": "application/rss+xml, application/xml, text/xml"
    }
    try:
        response = requests.get(feed_url, headers=headers, timeout=10)
        response.raise_for_status()
        feed = feedparser.parse(response.content)
    except Exception as exc:
        print(f"RSS fetch error {feed_url}: {exc}")
        return []

    return [entry for entry in feed.entries[:max_entries] if entry.get("title")]


def build_topic_summary(entry: feedparser.FeedParserDict) -> str:
    title = entry.get("title", "")
    summary = entry.get("summary", "") or entry.get("description", "")
    combined = f"{title}\n{summary}".strip()
    return combined[:800]


def normalize_url(entry: feedparser.FeedParserDict, fallback: str) -> str:
    for key in ("link", "id", "guid"):
        value = entry.get(key)
        if value:
            return str(value)
    return fallback


def hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def is_processed_url(url: str) -> bool:
    try:
        response = ddb_table.get_item(
            Key={"url": url},
            ProjectionExpression="#u",
            ExpressionAttributeNames={"#u": "url"}
        )
    except Exception as exc:
        print(f"DynamoDB get_item error: {exc}")
        return False
    return "Item" in response


def mark_urls_processed_batch(articles: List[Dict[str, Any]]) -> None:
    """評価対象になったすべての記事URLをbatch_writeで保存（3年間保持）"""
    if not articles:
        return
    
    try:
        with ddb_table.batch_writer(overwrite_by_pkeys=["url"]) as batch:
            for article in articles:
                batch.put_item(
                    Item={
                        "url": article["url"],
                        "title": article["title"],
                        "processed_at": _iso_now(),
                        "ttl": _ttl(1095),  # 3年間保持
                        "status": "evaluated",
                        "content_hash": article.get("content_hash", "")
                    }
                )
        print(f"Batch wrote {len(articles)} URLs to DynamoDB (3-year retention)")
    except Exception as exc:
        print(f"DynamoDB batch_write error: {exc}")
        # batch_writeに失敗した場合は個別に保存
        for article in articles:
            try:
                mark_url_processed(article["url"], article["title"], "evaluated")
            except Exception as e:
                print(f"Fallback write failed for {article['url']}: {e}")


def mark_url_processed(url: str, title: str, status: str = "selected") -> None:
    try:
        ddb_table.put_item(
            Item={
                "url": url,
                "title": title,
                "processed_at": _iso_now(),
                "ttl": _ttl(1095),  # 3年間保持
                "status": status
            }
        )
    except Exception as exc:
        print(f"DynamoDB put_item error: {exc}")
        raise


def fetch_reaction_summary(group_b_sources: List[str]) -> Dict[str, str]:
    if not group_b_sources:
        return {"site": "", "summary": ""}

    url = group_b_sources[0]
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/91.0.4472.124 Safari/537.36"
        )
    }
    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        feed = feedparser.parse(response.content)
    except Exception as exc:
        print(f"Reaction RSS fetch error {url}: {exc}")
        return {"site": url, "summary": ""}

    if not feed.entries:
        return {"site": feed.feed.get("title", url), "summary": ""}

    titles = [entry.get("title", "") for entry in feed.entries[:3] if entry.get("title")]
    summary = " / ".join(titles)
    return {"site": feed.feed.get("title", url), "summary": summary}


# -----------------------------------------------------------------------------
# Gemini 評価ロジック（10件ずつ × 最大3回）
# -----------------------------------------------------------------------------
def evaluate_articles_batch(
    articles: List[Dict[str, Any]],
    context: Any,
) -> Optional[Dict[str, Any]]:
    remaining_ms = context.get_remaining_time_in_millis()
    if remaining_ms < 120_000:
        print(f"Insufficient time for evaluation: {remaining_ms}ms")
        return None

    # 最新の最大30件を対象にする
    articles_to_eval = articles[:30]
    if len(articles_to_eval) < 1:
        print("No articles available for evaluation")
        return None

    print(f"Evaluating {len(articles_to_eval)} articles in batches of 10...")
    
    batch_results = []
    evaluated_articles = []
    
    # 10件ずつ×最大3回の評価
    for i in range(0, len(articles_to_eval), 10):
        batch_num = i // 10 + 1
        batch = articles_to_eval[i:i+10]
        
        print(f"Step {batch_num}: Evaluating batch {batch_num} (articles {i+1}-{min(i+10, len(articles_to_eval))})...")
        
        if batch_num > 1:
            print("Waiting 1 second between batches...")
            time.sleep(1)
        
        result = _evaluate_single_batch(batch, f"batch_{batch_num}")
        if result:
            batch_results.append(result)
            evaluated_articles.extend(batch)  # 評価対象記事を記録
        else:
            print(f"Batch {batch_num} evaluation failed")
    
    # 評価対象になったすべての記事を保存
    if evaluated_articles:
        mark_urls_processed_batch(evaluated_articles)
    
    if not batch_results:
        print("All batches failed")
        return None
    
    if len(batch_results) == 1:
        print("Only one successful batch. Returning winner directly.")
        return batch_results[0]
    
    # 複数バッチの勝者から最終選択
    print("Final selection from batch winners...")
    time.sleep(1)
    final_result = _select_final_winner(batch_results)
    return final_result or batch_results[0]


def _evaluate_single_batch(articles: List[Dict[str, Any]], batch_name: str) -> Optional[Dict[str, Any]]:
    if not articles:
        return None

    items_lines = []
    for idx, article in enumerate(articles, start=1):
        summary = article["topic_summary"].replace("\n", " ")[:200]
        items_lines.append(f"{idx}. ID:{article['content_hash']} | {article['title']}\n要約: {summary}")

    prompt = (
        f"以下の{len(articles)}件の記事からYouTube動画に最適な記事を1件だけ選んでください。\n"
        "新製品発表、OSアップデート、技術リーク、企業買収など具体的で速報性のある話題を優先してください。\n"
        "必ず {{\"selected_index\": 5}} のようなJSON形式で返してください。番号のみの返答も受け付けます。\n"
        f"\n記事一覧:\n{os.linesep.join(items_lines)}\n"
    )

    print(f"Evaluating {batch_name} with {len(articles)} articles...")
    response = call_gemini_generate_content(prompt)
    if response is None:
        print(f"{batch_name}: Gemini response None")
        return None

    response = response.strip()
    print(f"{batch_name} raw response: {response}")

    # JSON形式で解析を試みる
    selected_index = None
    try:
        parsed = json.loads(response)
        if isinstance(parsed, dict) and "selected_index" in parsed:
            selected_index = int(parsed["selected_index"])
            print(f"{batch_name}: JSON parsed successfully, selected_index={selected_index}")
    except (json.JSONDecodeError, ValueError, KeyError):
        print(f"{batch_name}: JSON parsing failed, trying regex fallback")
        # フォールバック: 正規表現で数字を抽出
        pattern = re.compile(r"([1-" + str(len(articles)) + "])")
        match = pattern.search(response)
        if match:
            selected_index = int(match.group(1))
            print(f"{batch_name}: Regex fallback found index {selected_index}")

    if selected_index is None:
        print(f"{batch_name}: no valid choice in response")
        return None

    # インデックスを0ベースに変換
    selected_index -= 1
    if selected_index < 0 or selected_index >= len(articles):
        print(f"{batch_name}: selected index out of range -> {selected_index}")
        return None

    return {
        "article": articles[selected_index],
        "reason": f"{batch_name} winner",
    }


def _select_final_winner(candidates: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    valid_candidates = [candidate for candidate in candidates if candidate]
    if len(valid_candidates) <= 1:
        print(f"Expected multiple candidates, got {len(valid_candidates)}. Returning first candidate.")
        return valid_candidates[0] if valid_candidates else None

    lines = []
    for idx, candidate in enumerate(valid_candidates, start=1):
        article = candidate["article"]
        summary = article["topic_summary"].replace("\n", " ")[:200]
        lines.append(f"{idx}. ID:{article['content_hash']} | {article['title']}\n要約: {summary}")

    prompt = (
        f"{len(valid_candidates)}つの候補から、視聴者にとって最も価値の高いテックニュースを1つ選んでください。\n"
        "速報性と具体的な発表内容を重視し、最も魅力的な記事番号だけを半角数字で回答してください。\n"
        "必ず {{\"selected_index\": 2}} のようなJSON形式で返してください。番号のみの返答も受け付けます。\n"
        f"\n候補一覧:\n{os.linesep.join(lines)}\n"
    )

    response = call_gemini_generate_content(prompt)
    if response is None:
        print("Final selection: Gemini response None")
        return None

    response = response.strip()
    print(f"Final selection raw response: {response}")

    # JSON形式で解析を試みる
    selected_index = None
    try:
        parsed = json.loads(response)
        if isinstance(parsed, dict) and "selected_index" in parsed:
            selected_index = int(parsed["selected_index"])
            print(f"Final selection: JSON parsed successfully, selected_index={selected_index}")
    except (json.JSONDecodeError, ValueError, KeyError):
        print("Final selection: JSON parsing failed, trying regex fallback")
        # フォールバック: 正規表現で数字を抽出
        match = re.search(r"([1-" + str(len(valid_candidates)) + "])", response)
        if match:
            selected_index = int(match.group(1))
            print(f"Final selection: Regex fallback found index {selected_index}")

    if selected_index is None:
        print("Final selection: no valid number")
        return None

    # インデックスを0ベースに変換
    selected_index -= 1
    if selected_index < 0 or selected_index >= len(valid_candidates):
        print(f"Final selection: index out of range -> {selected_index}")
        return None

    winner = valid_candidates[selected_index]
    winner["reason"] = "final winner"
    return winner


# -----------------------------------------------------------------------------
# S3 保存
# -----------------------------------------------------------------------------
def save_pending_article(article: Dict[str, Any], reaction: Dict[str, str]) -> str:
    entry = article["entry"]
    title = article["title"]
    url = article["url"]
    payload = {
        "title": title,
        "url": url,
        "summary": article["topic_summary"],
        "content_hash": article["content_hash"],
        "published_at": article.get("published_at", ""),
        "source": entry.get("source", {}).get("title") if entry.get("source") else "",
        "reaction": reaction,
        "selected_at": _iso_now(),
    }

    key = f"{PENDING_PREFIX}item_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{article['content_hash'][:8]}.json"
    body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")

    try:
        s3_client.put_object(
            Bucket=S3_BUCKET,
            Key=key,
            Body=body,
            ContentType="application/json; charset=utf-8",
        )
    except Exception as exc:
        print(f"S3 put_object error: {exc}")
        raise

    return key


# -----------------------------------------------------------------------------
# メインハンドラー
# -----------------------------------------------------------------------------
def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    print("Lambda selector started (Pro version - 3-year retention)")

    sources = load_rss_sources()
    group_a = sources.get("group_a", [])
    group_b = sources.get("group_b", [])

    if not group_a:
        raise RuntimeError("rss_sources.json に group_a が定義されていません")

    print("Step 1: Fetching RSS articles...")
    all_articles: List[Dict[str, Any]] = []
    for feed_url in group_a:
        entries = fetch_multiple_entries(feed_url, max_entries=15)
        for entry in entries:
            url = normalize_url(entry, feed_url)
            topic_summary = build_topic_summary(entry)
            all_articles.append(
                {
                    "entry": entry,
                    "url": url,
                    "title": entry.get("title", "Untitled"),
                    "topic_summary": topic_summary,
                    "content_hash": hash_text(topic_summary),
                    "source_url": feed_url,
                    "published_at": _extract_published(entry),
                }
            )

    if not all_articles:
        print("No articles fetched from RSS")
        return {"status": "no_articles"}

    print(f"Fetched {len(all_articles)} articles")

    print("Step 2: Removing already processed URLs via DynamoDB...")
    new_articles = [article for article in all_articles if not is_processed_url(article["url"])]
    print(f"Remaining articles after dedup: {len(new_articles)}")
    if not new_articles:
        return {"status": "no_new_articles"}

    # 最新の記事順にソート（published_atが新しい順）
    new_articles.sort(key=lambda x: x.get("published_at", ""), reverse=True)

    reaction = fetch_reaction_summary(group_b)
    print("Starting Gemini evaluation...")

    evaluation_result = evaluate_articles_batch(new_articles, context)
    if not evaluation_result:
        return {"status": "evaluation_failed"}

    selected_article = evaluation_result["article"]
    print(f"Selected article: {selected_article['title']}")

    # 採用記事を「selected」ステータスで保存
    mark_url_processed(selected_article["url"], selected_article["title"], "selected")

    # lambda_writer用にS3に保存
    pending_key = save_pending_article(selected_article, reaction)

    print(f"Pending article saved to s3://{S3_BUCKET}/{pending_key}")
    return {
        "status": "ok",
        "pending_key": pending_key,
        "url": selected_article["url"],
        "title": selected_article["title"],
        "evaluated_count": len(new_articles[:30]),  # 評価対象記事数
    }


def _extract_published(entry: feedparser.FeedParserDict) -> str:
    if "published_parsed" in entry and entry.published_parsed:
        dt = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
        return dt.isoformat()
    if entry.get("published"):
        return str(entry["published"])
    return ""
