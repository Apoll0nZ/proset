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
import email.utils
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
SCRIPTS_PATH = os.environ.get("SCRIPTS_PATH", "scripts/")

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


def _parse_rss_datetime(value: Any) -> Optional[datetime]:
    if not value:
        return None

    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    if isinstance(value, time.struct_time):
        dt = datetime(*value[:6], tzinfo=timezone.utc)
        return dt

    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None

        try:
            dt = datetime.fromisoformat(raw.replace('Z', '+00:00'))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            pass

        try:
            dt = email.utils.parsedate_to_datetime(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            return None


def _to_utc_isoformat(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


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


def fetch_multiple_entries(feed_url: str, max_entries: int = 8, seen_urls: set = None) -> List[feedparser.FeedParserDict]:
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

    # 新鮮さチェック：30日以内の記事のみ取得
    freshness_cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    fresh_entries = []
    processed_count = 0
    
    # 重複チェック用のセットが渡されていない場合は空セットを使用
    if seen_urls is None:
        seen_urls = set()
    
    # max_entriesの2.5倍まで試行して、重複スキップでも十分な数を確保
    max_attempts = min(int(max_entries * 2.5), len(feed.entries))
    
    for entry in feed.entries[:max_attempts]:
        if not entry.get("title"):
            continue
            
        # published_atを取得して新鮮さチェック
        published = None
        for date_field in ['published', 'updated', 'created']:
            if hasattr(entry, date_field) and getattr(entry, date_field):
                published = getattr(entry, date_field)
                break
        
        if published:
            published_dt = _parse_rss_datetime(published)
            if not published_dt:
                print(f"Date parsing error for RSS entry: could not parse date: {published}")
                continue

            if published_dt >= freshness_cutoff:
                # URLを正規化して重複チェック
                url = normalize_url(entry, feed_url)
                if url and url not in seen_urls:
                    fresh_entries.append(entry)
                    processed_count += 1
                    if processed_count >= max_entries:
                        break
                else:
                    print(f"[FETCH] Skipping duplicate during fetch: {entry.get('title', 'Untitled')}")
            else:
                print(f"Skipping old RSS entry: {entry.get('title', 'Untitled')} (published: {published})")
        else:
            # 日付がない記事は除外
            print(f"Skipping RSS entry without date: {entry.get('title', 'Untitled')}")
            continue
    
    print(f"Fetched {len(fresh_entries)} fresh entries from {feed_url} (checked {max_attempts} entries)")
    return fresh_entries


def build_topic_summary(entry: feedparser.FeedParserDict) -> str:
    title = entry.get("title", "")
    summary = entry.get("summary", "") or entry.get("description", "")
    combined = f"{title}\n{summary}".strip()
    return combined[:800]


def normalize_url(entry: feedparser.FeedParserDict, fallback: str) -> Optional[str]:
    """
    RSSエントリから記事URLを抽出する。フィードURLやRSSパスは除外し、有効なhttp(s)のみ返す。
    Returns:
        str: 正常な記事URL
        None: 有効なURLが見つからない場合
    """
    for key in ("link", "id", "guid"):
        value = entry.get(key)
        if not value:
            continue

        url = str(value).strip()

        # フィードURLそのものは無効
        if url == fallback:
            print(f"[NORMALIZE] Skipping feed URL itself: {url}")
            continue

        # RSS/Atom/フィード系のパスを除外
        invalid_patterns = ['.rss', '.xml', '/feed', '/rss', '/atom']
        if any(pattern in url.lower() for pattern in invalid_patterns):
            print(f"[NORMALIZE] Skipping feed-like URL: {url}")
            continue

        # http/https のみ許可
        if url.startswith(("http://", "https://")):
            return url

        print(f"[NORMALIZE] Invalid URL scheme: {url}")

    print(f"[NORMALIZE] No valid URL found for entry: {entry.get('title', 'Untitled')}")
    return None


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


def save_article_with_score(url: str, title: str, score: float, published_date: str, status: str = "evaluated") -> None:
    """記事をスコア付きで保存（公開日必須）"""
    if not published_date:
        raise RuntimeError(f"Cannot save article without published date: {title}")
    
    try:
        ddb_table.put_item(
            Item={
                "url": url,
                "title": title,
                "processed_at": published_date,  # 評価日時ではなく記事公開日を使用
                "ttl": _ttl(1095),  # 3年間保持
                "status": status,
                "score": Decimal(str(score))
            }
        )
        print(f"Saved article with score: {title} ({score}点)")
    except Exception as exc:
        print(f"DynamoDB put_item error: {exc}")
        raise

def mark_url_processed(url: str, score: float, title: str = "", status: str = "selected") -> None:
    """記事のステータスのみを更新（既存情報を保持）"""
    try:
        update_expression = "SET #s = :status, #ua = :updated_at"
        expression_names = {
            "#s": "status",
            "#ua": "updated_at"
        }
        expression_values = {
            ":status": status,
            ":updated_at": _iso_now()
        }
        
        # タイトルが空でない場合のみタイトルも更新
        if title:
            update_expression += ", #t = :title"
            expression_names["#t"] = "title"
            expression_values[":title"] = title
        
        ddb_table.update_item(
            Key={"url": url},
            UpdateExpression=update_expression,
            ExpressionAttributeNames=expression_names,
            ExpressionAttributeValues=expression_values
        )
        print(f"Updated status for {url} to '{status}'")
    except Exception as exc:
        print(f"DynamoDB update_item error: {exc}")
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
BASE_SCORE_THRESHOLD = 65.0  # 基準点
STOCK_DAYS = 7  # 過去何日間のストック記事を対象にするか
MAX_EVALUATION_ATTEMPTS = 3  # 最大評価試行回数

def evaluate_article_with_gemini(article: Dict[str, Any]) -> Optional[float]:
    """Geminiを使って記事を評価し、スコアを返す（0-100点）"""
    summary = article["topic_summary"].replace("\n", " ")[:500]
    
    prompt = (
    f"以下の記事を『PC周辺機器・ノートPC専門の考察動画』としての価値（0-100点）で評価してください。\n\n"

    f"【絶対条件：PCハードウェア判定】\n"
    f"・記事の主役が『ノートPC』『マウス』『キーボード』『モニター』『PCパーツ』『周辺機器』のいずれにも該当しない場合、"
    f"内容に関わらず即座に【0点】としてください。\n"
    f"・『AIゲーミングノートPC』『AI PC』『Copilot+ PC』はノートPCとして扱い、0点にしないこと。\n"
    f"・製品名や記事タイトルに『AI』が含まれていても、主役がノートPC・デスクトップPC・周辺機器であれば対象とする。\n"
    f"・イヤホン・スピーカーはPC向け（ゲーミング・DTM・モニター用途）であれば周辺機器として対象とする。スマートフォン専用・完全ワイヤレスのみの場合は0点。\n"
    f"・スマートフォン単体・スマートウォッチ単体の記事はPC周辺機器ではないため0点。\n\n"
    
    f"【最優先：ブランド・カテゴリ別基礎点】\n" 
    f"★ Tier 1（指名検索・熱狂的ファン・購買意欲が極めて高い）: +60点\n"
    f"  - キーボード: HHKB (Happy Hacking Keyboard), 東プレ (REALFORCE), NiZ (静電容量無接点方式全般)\n"
    f"  - PC本体: Apple (MacBook), Microsoft (Surface), Dell (XPS/Alienware), ASUS (ROG/Zenbook), Razer\n"
    f"  - 主要パーツ: NVIDIA (RTX), Intel (Core), AMD (Ryzen)\n"
    f"  - 周辺機器: Logicool (G/MXシリーズ), Anker, Sony (INZONE/WHシリーズ)\n\n"
    
    f"★ Tier 2（比較検討の常連・スタンダード）: +30点\n"
    f"  - Keychron, NuPhy, Wooting, SteelSeries, Corsair, HP (Spectre/OMEN), Lenovo (ThinkPad/Legion), BenQ, LG, Samsung\n\n"
    
    f"★ Tier 3（その他・一般メーカー）: +20点\n"
    f"  - 上記以外のPCメーカー、モニターメーカー、周辺機器ブランド\n\n"

    f"【評価の加点・減点ルール】\n"
    f"1. 新製品発表・スペック公開 (+40点): 新モデル発表、スペック詳細、発売日確定などは最優先加点。\n"
    f"2. 静電容量無接点・打鍵感の深掘り (+30点): HHKB/REALFORCE/NiZの新情報は加点。\n"
    f"3. 筐体設計・ビルドクオリティ (+15点): デザイン・軽量化・新素材への言及。\n"
    f"4. スペック・検証の質 (+15点): リフレッシュレート、色域、ポーリングレートなど数値的根拠。\n"
    f"5. セール・価格情報: 単なるセールは-20点。新製品発表に伴う旧型値下げは加点なし（そのまま）。\n\n"

    f"【スコア例】\n"
    f"- 「Surface Laptop 新型にOLED搭載」→ Tier 1(+60) + 新製品発表(+40) = 100点\n"
    f"- 「AcerのゲーミングモニターIPSパネル新モデル発表」→ Tier 2(+40) + 新製品発表(+40) = 80点\n"
    f"- 「HHKB専用の木製パームレスト発売」→ Tier 1(+60) + 新製品発表(+40) = 100点\n"
    f"- 「NiZの新作テンキーレスが予約開始」→ Tier 1(+60) + 新製品発表(+40) = 100点\n"
    f"- 「DellのビジネスノートPC新ラインナップ」→ Tier 1(+60) + 新製品発表(+40) = 100点\n"
    f"- 「MSI 240HzゲーミングモニターのスペックレビューQHD対応」→ Tier 2(+40) + スペック(+15) = 55点\n"
    f"- 「Surface旧モデルがAmazonでセール中」→ Tier 1(+60) - セール(-20) = 40点\n"
    f"- 「Windowsの新しい絵文字追加」→ ハードウェアではないため 0点\n\n"
    
    f"## JSON出力ルール\n"
    f"必ず {{\"score\": 85}} の形式のみで返してください。\n\n" 
    f"記事タイトル: {article['title']}\n要約: {summary}"
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
            
            # status='pending' は新着扱いにする（先行登録済みだが未評価）
            if status == "pending":
                new_articles.append(article)
                continue
            
            # 過去にボツ判定済みの場合は除外
            if score < BASE_SCORE_THRESHOLD:
                print(f"Skipping low score article: {article['title']} ({score}点)")
                continue
            
            # 7日以内の記事かチェック
            try:
                if processed_at:
                    processed_dt = _parse_rss_datetime(processed_at)
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
    """新着記事を評価（リトライ機能付き・取得数増強）"""
    if not new_articles:
        return []
    
    remaining_ms = context.get_remaining_time_in_millis()
    if remaining_ms < 120_000:
        print(f"Insufficient time for new article evaluation: {remaining_ms}ms")
        return []
    
    evaluated_articles = []
    
    # 評価対象を増やす（最大30件に拡張）
    max_evaluation_count = len(new_articles)  # 全記事を評価
    
    # 新着記事を評価（リトライ機能付き）
    for i, article in enumerate(new_articles[:max_evaluation_count]):
        print(f"Evaluating fresh article {i+1}/{max_evaluation_count}: {article['title']}")
        
        # リトライ機能で評価を実行
        score = None
        for attempt in range(MAX_EVALUATION_ATTEMPTS):
            try:
                score = evaluate_article_with_gemini(article)
                if score is not None:
                    break
                else:
                    print(f"Evaluation attempt {attempt + 1} failed for: {article['title']}")
                    if attempt < MAX_EVALUATION_ATTEMPTS - 1:
                        print(f"Retrying evaluation in 2 seconds...")
                        time.sleep(2)
            except Exception as e:
                print(f"Evaluation error on attempt {attempt + 1}: {e}")
                if attempt < MAX_EVALUATION_ATTEMPTS - 1:
                    time.sleep(2)
        
        if score is not None:
            article["score"] = score
            evaluated_articles.append(article)
            
            
            # スコアに関わらず全記事を保存（重複評価を防ぐ）
            published_date = article.get("published_date")
            if not published_date:
                print(f"[SKIP] Article without published date: {article['title']} ({score}点)")
                continue
            
            # スコアに関わらず全記事を保存
            save_article_with_score(article["url"], article["title"], score, published_date, "evaluated")
            print(f"Saved evaluated article: {article['title']} ({score}点)")
        else:
            # 評価失敗記事もDBに記録してスキップ済みフラグを立てる
            published_date = article.get("published_date")
            if published_date:
                save_article_with_score(
                    article["url"], 
                    article["title"], 
                    0.0,  # スコア0で保存
                    published_date, 
                    "eval_failed"  # 専用ステータス
                )
                print(f"Saved failed evaluation article: {article['title']} (0点, eval_failed)")
            else:
                print(f"[SKIP] Failed evaluation article without published date: {article['title']}")
            
            print(f"Failed to evaluate after {MAX_EVALUATION_ATTEMPTS} attempts: {article['title']}")
        
        # APIレート制限対策
        if i < max_evaluation_count - 1:
            time.sleep(1)
    
    print(f"Evaluated {len(evaluated_articles)} new articles (out of {max_evaluation_count})")
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




def preregister_new_articles(articles: List[Dict[str, Any]]) -> None:
    """
    新着記事を全件 status='pending' で先行DB登録する。
    これにより途中でLambdaがタイムアウトしても次回実行時に重複評価されない。
    published_date がない記事はスキップ（save_article_with_score が必須チェック）。
    """
    registered = 0
    skipped = 0
    for article in articles:
        published_date = article.get("published_date")
        if not published_date:
            print(f"[PREREG] Skipped (no published_date): {article['title']}")
            skipped += 1
            continue
        try:
            save_article_with_score(
                article["url"],
                article["title"],
                0.0,
                published_date,
                "pending"
            )
            registered += 1
        except Exception as e:
            print(f"[PREREG] DB error for {article['title']}: {e}")

    print(f"[PREREG] Registered {registered} articles as 'pending' ({skipped} skipped, no date)")


def fetch_recent_script_titles(days: int = 7) -> List[str]:
    """
    S3の台本フォルダから過去 days 日分のJSONを取得し、
    台本の title フィールド一覧を返す。
    取得失敗時は空リストを返して処理を止めない。
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    titles: List[str] = []

    try:
        paginator = s3_client.get_paginator("list_objects_v2")
        pages = paginator.paginate(Bucket=S3_BUCKET, Prefix=_ensure_trailing_slash(SCRIPTS_PATH))

        keys_to_fetch: List[str] = []
        for page in pages:
            for obj in page.get("Contents", []):
                last_modified = obj.get("LastModified")
                if last_modified and last_modified.replace(tzinfo=timezone.utc) >= cutoff:
                    keys_to_fetch.append(obj["Key"])

        print(f"[SCRIPT_TITLES] Found {len(keys_to_fetch)} script files in past {days} days")

        for key in keys_to_fetch:
            try:
                response = s3_client.get_object(Bucket=S3_BUCKET, Key=key)
                body = response["Body"].read().decode("utf-8")
                data = json.loads(body)

                title = data.get("title", "")

                # meta.written_at で日付を二重チェック（S3 LastModified のズレ対策）
                written_at = data.get("meta", {}).get("written_at") or data.get("meta", {}).get("selected_at", "")
                if written_at:
                    written_dt = _parse_rss_datetime(written_at)
                    if written_dt and written_dt < cutoff:
                        print(f"[SCRIPT_TITLES] Skipping old script (by written_at): {title}")
                        continue

                if title:
                    titles.append(title)
                    print(f"[SCRIPT_TITLES] Loaded title: {title}")
            except Exception as e:
                print(f"[SCRIPT_TITLES] Failed to parse {key}: {e}")
                continue

    except Exception as e:
        print(f"[SCRIPT_TITLES] S3 list/fetch error: {e}")

    print(f"[SCRIPT_TITLES] Total recent titles: {len(titles)}")
    return titles


def filter_articles_by_topic_similarity(
    articles: List[Dict[str, Any]],
    recent_titles: List[str]
) -> List[Dict[str, Any]]:
    """
    Gemini APIで recent_titles と内容が被る記事を除外する。
    除外した記事は status='topic_duplicate' でDB更新し、次回以降のURL重複チェックで弾かれる。
    recent_titles が空の場合はそのまま全記事を返す。
    """
    if not recent_titles or not articles:
        return articles

    recent_titles_text = "\n".join(f"- {t}" for t in recent_titles)
    article_list_text = "\n".join(f"{i}: {a['title']}" for i, a in enumerate(articles))

    prompt = (
        f"あなたはYouTubeチャンネルの編集者です。\n"
        f"「過去7日間に公開済みの動画タイトル」と「新着記事タイトル一覧」を比較し、"
        f"完全に同一のトピックを扱っている記事のみを重複と判定してください。\n\n"
        f"【重複と判定する条件（以下を両方満たす場合のみ）】\n"
        f"1. 同一の製品名・モデル名・固有名詞が一致している\n"
        f"2. 伝えるメッセージや結論が実質的に同じ\n\n"
        f"【重複と判定しない例（これらは必ず通過させる）】\n"
        f"- 過去台本がAI・スマホ系でも、新記事がキーボード・モニター・ノートPCなら重複なし\n"
        f"- メーカーが同じでも製品カテゴリが違えば重複なし（例：Apple MacBook ≠ Apple iPhone）\n"
        f"- 同じ製品でも「発表」「レビュー」「価格変動」など視点が異なれば重複なし\n"
        f"- 過去台本に類似する製品カテゴリが存在しない場合は重複なし\n\n"
        f"【最重要ルール】\n"
        f"判断に迷う場合は必ず『重複なし』として扱うこと。\n"
        f"過剰除外はチャンネルの機会損失になるため、確実に同一と判断できる場合のみ重複とする。\n\n"
        f"【過去7日間の公開済み動画タイトル】\n"
        f"{recent_titles_text}\n\n"
        f"【新着記事タイトル一覧（index: タイトル）】\n"
        f"{article_list_text}\n\n"
        f"重複と判定した記事のindexのみをJSON配列で返してください。重複なしは空配列。解説不要。\n"
        f"例: {{\"duplicate_indexes\": [0, 3, 5]}}"
    )

    response = call_gemini_generate_content(prompt)
    if response is None:
        print("[TOPIC_FILTER] Gemini call failed, skipping topic filter")
        return articles

    try:
        parsed = robust_json_loads(response)
        duplicate_indexes = set(parsed.get("duplicate_indexes", []))
        print(f"[TOPIC_FILTER] Duplicate indexes: {duplicate_indexes}")
    except Exception as e:
        print(f"[TOPIC_FILTER] Parse error: {e} / raw: {response}")
        return articles

    filtered = []
    for i, article in enumerate(articles):
        if i in duplicate_indexes:
            print(f"[TOPIC_FILTER] Excluded (topic duplicate): {article['title']}")
            # 先行登録済みのレコードを topic_duplicate に更新
            try:
                mark_url_processed(
                    article["url"],
                    0.0,
                    article["title"],
                    "topic_duplicate"
                )
            except Exception as e:
                print(f"[TOPIC_FILTER] DB update error for {article['title']}: {e}")
        else:
            filtered.append(article)

    print(f"[TOPIC_FILTER] {len(articles)} → {len(filtered)} articles after topic dedup")
    return filtered


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
    seen_urls = set()  # 重複チェック用のセット
    total_skipped = 0  # スキップした重複の総数
    
    for feed_url in group_a:
        entries = fetch_multiple_entries(feed_url, max_entries=8, seen_urls=seen_urls)
        feed_skipped = 0  # このフィードでのスキップ数
        
        for entry in entries:
            url = normalize_url(entry, feed_url)
            
            # 無効なURLはスキップ
            if not url:
                print(f"[SKIP] RSS entry without valid URL: {entry.get('title', 'Untitled')} (feed: {feed_url})")
                continue
            
            # 重複URLチェック（二重チェック - fetch_multiple_entriesでもチェック済み）
            if url in seen_urls:
                feed_skipped += 1
                total_skipped += 1
                print(f"[DUPLICATE] Skipping already seen URL: {entry.get('title', 'Untitled')}")
                continue
            
            seen_urls.add(url)  # 重複チェックセットに追加

            topic_summary = build_topic_summary(entry)
            published_date = _extract_published(entry)
            
            # 公開日がない記事は除外
            if not published_date:
                print(f"[SKIP] RSS entry without published date: {entry.get('title', 'Untitled')}")
                continue
                
            all_articles.append(
                {
                    "entry": entry,
                    "url": url,
                    "title": entry.get("title", "Untitled"),
                    "topic_summary": topic_summary,
                    "content_hash": hash_text(topic_summary),
                    "source_url": feed_url,
                    "published_at": published_date,
                    "published_date": published_date,  # processed_atとして使用する公開日
                }
            )
        
        if feed_skipped > 0:
            print(f"[INFO] Feed {feed_url}: skipped {feed_skipped} duplicate entries")
    
    if total_skipped > 0:
        print(f"[INFO] Total duplicates skipped during fetch: {total_skipped}")

    if not all_articles:
        print("No articles fetched from RSS")
        return {"status": "no_articles"}

    print(f"Fetched {len(all_articles)} unique articles (from {len(group_a)} feeds)")

    reaction = fetch_reaction_summary(group_b)
    print(f"Reaction summary: {reaction['site']}")

    # Step 2a: 新着かどうかに関わらず全記事をまず DB に先行登録（status='pending'）
    # → 途中でタイムアウトしても次回実行時に URL 重複チェックで弾かれる
    print("Step 2a: Pre-registering all articles to DB...")
    # filter_and_collect_candidates でDBの状態を見て分類するため、
    # 先行登録は「DBに存在しない記事のみ」に絞る
    unseen_articles = [a for a in all_articles if not get_article_info(a["url"])]
    preregister_new_articles(unseen_articles)

    # Step 2b: 過去7日間の台本タイトルを S3 から取得
    print("Step 2b: Fetching recent script titles from S3...")
    recent_script_titles = fetch_recent_script_titles(days=7)

    # Step 2c: トピック重複フィルタ（スコアリング前に除外してAPI呼び出しを節約）
    if recent_script_titles:
        print(f"Step 2c: Filtering {len(all_articles)} articles by topic similarity...")
        all_articles = filter_articles_by_topic_similarity(all_articles, recent_script_titles)
        print(f"After topic filter: {len(all_articles)} articles remain")
    else:
        print("Step 2c: No recent scripts found, skipping topic filter")

    # Step 2d: DB の状態に基づいて新着 / ストック候補に分類
    print("Step 2d: Filtering articles and collecting candidates...")
    new_articles, stock_candidates = filter_and_collect_candidates(all_articles)
    
    if not new_articles and not stock_candidates:
        print("No candidates available")
        return {"status": "no_candidates"}
    
    # 新着記事を評価
    evaluated_new_articles = []
    if new_articles:
        print(f"Step 3: Evaluating {len(new_articles)} new articles...")
        evaluated_new_articles = evaluate_new_articles(new_articles, context)
    
    # 基準点以上の新着記事を候補に追加
    qualified_new = [article for article in evaluated_new_articles if article.get("score", 0.0) >= BASE_SCORE_THRESHOLD]
    print(f"Qualified new articles: {len(qualified_new)}件")
    
    # 基準点以上の候補のみを使用
    all_candidates = qualified_new + stock_candidates
    
    if not all_candidates:
        print("No qualified candidates available")
        return {"status": "no_qualified_candidates"}
    
    print(f"Step 4: Selecting best article from {len(all_candidates)} candidates...")
    selected_article = select_best_article(all_candidates)
    
    if not selected_article:
        return {"status": "selection_failed"}
    
    print(f"Selected article: {selected_article['title']} ({selected_article.get('score', 0)}点)")
    
    # lambda_writer用にS3に保存（低スコアでも保存）
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
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        parsed = entry.get(key)
        dt = _parse_rss_datetime(parsed)
        if dt:
            return _to_utc_isoformat(dt)

    for key in ("published", "updated", "created"):
        raw = entry.get(key)
        dt = _parse_rss_datetime(raw)
        if dt:
            return _to_utc_isoformat(dt)

    return ""
