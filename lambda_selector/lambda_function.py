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
from decimal import Decimal
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

import boto3
import feedparser
import requests

def robust_json_loads(raw_text):
    """
    LLMが生成した不完全なJSON文字列をクレンジングしてパースする
    """
    # 1. 前後の不要な空白やMarkdown装飾を削除
    text = raw_text.strip()
    if text.startswith("```"):
        text = re.sub(r'^```(?:json)?\n?|```$', '', text, flags=re.MULTILINE).strip()
    
    # 2. JSON部分（{ } の中身）だけを抽出
    match = re.search(r'\{.*\}', text, re.DOTALL)
    if match:
        text = match.group(0)
    
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # 3. 最後の手段：末尾のカンマや制御文字を力技で消去
        text = re.sub(r',\s*([\]}])', r'\1', text) # 末尾カンマ削除
        text = re.sub(r'[\x00-\x1F\x7F]', '', text) # 制御文字削除
        return json.loads(text)

# -----------------------------------------------------------------------------
# 環境変数
# -----------------------------------------------------------------------------
DYNAMODB_TABLE_NAME = os.environ["DYNAMODB_TABLE"]
S3_BUCKET = os.environ["S3_BUCKET"]
PENDING_PREFIX = os.environ.get("PENDING_PATH", "pending/")
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
GEMINI_MODEL_NAME = os.environ.get("GEMINI_MODEL_NAME", "gemini-2.5-flash-lite")
GEMINI_API_VERSION = os.environ.get("GEMINI_API_VERSION", "v1")
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


def get_article_info(url: str) -> Optional[Dict[str, Any]]:
    """URLに対応する記事情報を取得（scoreとstatusを含む）"""
    try:
        response = ddb_table.get_item(
            Key={"url": url},
            ProjectionExpression="#u, #s, #sc, #pa, #ttl",
            ExpressionAttributeNames={
                "#u": "url",
                "#s": "status", 
                "#sc": "score",
                "#pa": "processed_at",
                "#ttl": "ttl"
            }
        )
        item = response.get("Item")
        if item and "score" in item:
            # Decimalをfloatに変換して比較処理で扱いやすくする
            item["score"] = float(item["score"])
        return item
    except Exception as exc:
        print(f"DynamoDB get_item error: {exc}")
        return None


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
                        "content_hash": article.get("content_hash", ""),
                        "score": Decimal(str(article.get("score", 0.0)))
                    }
                )
        print(f"Batch wrote {len(articles)} URLs to DynamoDB (3-year retention)")
    except Exception as exc:
        print(f"DynamoDB batch_write error: {exc}")
        # batch_writeに失敗した場合は個別に保存
        for article in articles:
            try:
                score = article.get("score", 0.0)
                save_article_with_score(article["url"], article["title"], score, "evaluated")
            except Exception as e:
                print(f"Fallback write failed for {article['url']}: {e}")


def save_article_with_score(url: str, title: str, score: float, status: str = "evaluated") -> None:
    """記事をスコア付きで保存"""
    try:
        ddb_table.put_item(
            Item={
                "url": url,
                "title": title,
                "processed_at": _iso_now(),
                "ttl": _ttl(1095),  # 3年間保持
                "status": status,
                "score": Decimal(str(score))
            }
        )
    except Exception as exc:
        print(f"DynamoDB put_item error: {exc}")
        raise

def mark_url_processed(url: str, score: float, title: str = "", status: str = "selected") -> None:
    try:
        ddb_table.put_item(
            Item={
                "url": url,
                "title": title,
                "processed_at": _iso_now(),
                "ttl": _ttl(1095),  # 3年間保持
                "status": status,
                "score": Decimal(str(score))
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
# Gemini 評価ロジック（スコアリング方式）
# -----------------------------------------------------------------------------
BASE_SCORE_THRESHOLD = 70.0  # 基準点
STOCK_DAYS = 7  # 過去何日間のストック記事を対象にするか

def evaluate_article_with_gemini(article: Dict[str, Any]) -> Optional[float]:
    """Geminiを使って記事を評価し、スコアを返す（0-100点）"""
    summary = article["topic_summary"].replace("\n", " ")[:500]
    
    prompt = (
        f"以下の記事をYouTube動画としての価値を0-100点で評価してください。"
        f"新製品発表、OSアップデート、技術リーク、企業買収など具体的で速報性のある話題を高く評価してください。"
        f"評価基準：速報性(30点)、具体性(25点)、視聴者への影響度(25点)、新規性(20点)。"
        f"\n\n## JSON出力に関する厳格なルール\n"
        f"1. 必ず有効なJSONフォーマットのみを出力してください。\n"
        f"2. 前後の説明文や挨拶、Markdown装飾（```jsonなど）は一切含めないでください。\n"
        f"3. 文字列内でダブルクォーテーションを使用する場合は必ずエスケープしてください。\n"
        f"4. 文字列内での実際の改行は禁止し、\\nを使用してください。\n"
        f"5. 制御文字を含めないでください。\n"
        f"6. リストの最後の要素の後にカンマを置かないでください。\n"
        f"必ず {{\"score\": 85}} のようなJSON形式のみで返してください。"
        f"\n\n記事タイトル: {article['title']}\n要約: {summary}"
    )

    response = call_gemini_generate_content(prompt)
    if response is None:
        print(f"Gemini evaluation failed for: {article['title']}")
        return None

    response = response.strip()
    print(f"Gemini evaluation response: {response}")

    try:
        parsed = robust_json_loads(response)
        if isinstance(parsed, dict) and "score" in parsed:
            score = float(parsed["score"])
            # 0-100の範囲にクリップ
            score = max(0.0, min(100.0, score))
            print(f"Article scored: {article['title']} -> {score}点")
            return score
    except (json.JSONDecodeError, ValueError, KeyError):
        print(f"JSON parsing failed for score, trying regex fallback")
        
        # フォールバック: 正規表現で数値を抽出
        pattern = re.compile(r"(\d+(?:\.\d+)?)")
        matches = pattern.findall(response)
        if matches:
            try:
                score = float(matches[0])
                score = max(0.0, min(100.0, score))
                print(f"Regex fallback score: {article['title']} -> {score}点")
                return score
            except ValueError:
                pass

    print(f"Could not extract valid score from response: {response}")
    return None
def filter_and_collect_candidates(all_articles: List[Dict[str, Any]]) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """重複フィルタリングと候補収集"""
    new_articles = []  # 初めて取得したURL
    stock_candidates = []  # 過去のストック記事（基準点以上）
    
    cutoff_date = datetime.now(timezone.utc) - timedelta(days=STOCK_DAYS)
    
    for article in all_articles:
        url = article["url"]
        existing_info = get_article_info(url)
        
        if not existing_info:
            # 初めて取得したURL -> 新着記事リストに追加
            new_articles.append(article)
        else:
            status = existing_info.get("status", "")
            score = existing_info.get("score", 0.0)
            processed_at = existing_info.get("processed_at", "")
            
            # 既に動画化済みの場合は除外
            if status == "selected":
                print(f"Skipping already selected article: {article['title']}")
                continue
            
            # 過去にボツ判定済みの場合は除外
            if score < BASE_SCORE_THRESHOLD:
                print(f"Skipping low score article: {article['title']} ({score}点)")
                continue
            
            # 7日以内の記事かチェック
            try:
                if processed_at:
                    processed_dt = datetime.fromisoformat(processed_at.replace('Z', '+00:00'))
                    if processed_dt >= cutoff_date:
                        # 過去7日間の基準点以上の記事 -> ストック候補に追加
                        article["score"] = score  # DBのスコアを設定
                        article["processed_at"] = processed_at
                        stock_candidates.append(article)
                        print(f"Added stock candidate: {article['title']} ({score}点)")
                    else:
                        print(f"Skipping old article: {article['title']} ({processed_at})")
                else:
                    print(f"Skipping article without processed_at: {article['title']}")
            except Exception as e:
                print(f"Date parsing error for {article['title']}: {e}")
                continue
    
    print(f"新着記事: {len(new_articles)}件, ストック候補: {len(stock_candidates)}件")
    return new_articles, stock_candidates

def evaluate_new_articles(new_articles: List[Dict[str, Any]], context: Any) -> List[Dict[str, Any]]:
    """新着記事をGeminiで評価"""
    if not new_articles:
        return []
    
    remaining_ms = context.get_remaining_time_in_millis()
    if remaining_ms < 120_000:
        print(f"Insufficient time for new article evaluation: {remaining_ms}ms")
        return []
    
    evaluated_articles = []
    
    # 新着記事を1件ずつ評価（APIコスト削減のため）
    for i, article in enumerate(new_articles[:20]):  # 最大20件に制限
        print(f"Evaluating new article {i+1}/{len(new_articles)}: {article['title']}")
        
        score = evaluate_article_with_gemini(article)
        if score is not None:
            article["score"] = score
            evaluated_articles.append(article)
            
            # 基準点以上の場合のみ保存
            if score >= BASE_SCORE_THRESHOLD:
                save_article_with_score(article["url"], article["title"], score, "evaluated")
        else:
            print(f"Failed to evaluate: {article['title']}")
        
        # APIレート制限対策
        if i < len(new_articles) - 1:
            time.sleep(1)
    
    print(f"Evaluated {len(evaluated_articles)} new articles")
    return evaluated_articles

def select_best_article(candidates: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """候補の中から最高スコアの記事を選出"""
    if not candidates:
        return None
    
    # スコアでソートして最高点の記事を選択
    best_article = max(candidates, key=lambda x: x.get("score", 0.0))
    best_score = best_article.get("score", 0.0)
    
    print(f"Selected best article: {best_article['title']} ({best_score}点)")
    
    # 選出された記事をstatus="selected"に更新
    mark_url_processed(best_article["url"], best_score, best_article["title"], "selected")
    
    return best_article




# -----------------------------------------------------------------------------
# S3 保存
# -----------------------------------------------------------------------------
def save_pending_article(article: Dict[str, Any], reaction: Dict[str, str]) -> str:
    entry = article["entry"]
    title = article["title"]
    url = article["url"]
    
    # Geminiにコンパクトな要約を生成させるためのプロンプト
    compact_summary_prompt = (
        f"以下の記事要約を20文字以内で最も重要なキーワードのみに凝縮してください：\n"
        f"{article['topic_summary'][:200]}\n"
        f"出力形式：キーワードのみ、説明なし、記号なし"
    )
    
    try:
        compact_summary = call_gemini_generate_content(compact_summary_prompt)
        if compact_summary and len(compact_summary.strip()) > 0:
            summary = compact_summary.strip()[:50]  # 最大50文字に制限
        else:
            summary = article["topic_summary"][:50]  # フォールバック
    except:
        summary = article["topic_summary"][:50]  # エラー時は元の要約を短縮
    
    # ソース名を短縮
    source_name = entry.get("source", {}).get("title", "")[:20] if entry.get("source") else ""
    
    # リアクションをコンパクト化
    compact_reaction = {
        "site": reaction.get("site", "")[:15],  # 最大15文字
        "summary": reaction.get("summary", "")[:30]  # 最大30文字
    }
    
    payload = {
        "title": title,
        "url": url,
        "summary": summary,
        "content_hash": article["content_hash"],
        "published_at": article.get("published_at", ""),
        "source": source_name,
        "reaction": compact_reaction,
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

    reaction = fetch_reaction_summary(group_b)
    print(f"Reaction summary: {reaction['site']}")

    print(f"Step 2: Filtering articles and collecting candidates...")
    new_articles, stock_candidates = filter_and_collect_candidates(all_articles)
    
    if not new_articles and not stock_candidates:
        print("No candidates available")
        return {"status": "no_candidates"}
    
    # 新着記事を評価
    evaluated_new_articles = []
    if new_articles:
        print(f"Step 3: Evaluating {len(new_articles)} new articles...")
        evaluated_new_articles = evaluate_new_articles(new_articles, context)
    
    # 基準点以上の新着記事のみを候補に追加
    qualified_new = [article for article in evaluated_new_articles if article.get("score", 0.0) >= BASE_SCORE_THRESHOLD]
    print(f"Qualified new articles: {len(qualified_new)}件")
    
    # すべての候補を結合
    all_candidates = qualified_new + stock_candidates
    
    if not all_candidates:
        print("No qualified candidates available")
        return {"status": "no_qualified_candidates"}
    
    print(f"Step 4: Selecting best article from {len(all_candidates)} candidates...")
    selected_article = select_best_article(all_candidates)
    
    if not selected_article:
        return {"status": "selection_failed"}
    
    print(f"Selected article: {selected_article['title']} ({selected_article.get('score', 0)}点)")
    
    # lambda_writer用にS3に保存
    pending_key = save_pending_article(selected_article, reaction)
    
    print(f"Pending article saved to s3://{S3_BUCKET}/{pending_key}")
    return {
        "status": "ok",
        "pending_key": pending_key,
        "url": selected_article["url"],
        "title": selected_article["title"],
        "score": selected_article.get("score", 0.0),
        "new_articles_count": len(new_articles),
        "stock_candidates_count": len(stock_candidates),
        "total_candidates": len(all_candidates)
    }


def _extract_published(entry: feedparser.FeedParserDict) -> str:
    if "published_parsed" in entry and entry.published_parsed:
        dt = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
        return dt.isoformat()
    if entry.get("published"):
        return str(entry["published"])
    return ""
