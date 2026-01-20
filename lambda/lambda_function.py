import json
import os
import hashlib
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import boto3
import feedparser
import requests

try:
    import google.generativeai as genai
except ImportError:
    # ランタイムでモジュールが無い場合は後続処理で明示的にエラーを投げる
    genai = None  # type: ignore


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
    "GEMINI_MODEL_NAME", "gemini-1.5-pro"
)  # 必要に応じて変更
AWS_REGION = os.environ.get("AWS_REGION", "ap-northeast-1")

RSS_SOURCES_PATH = os.path.join(os.path.dirname(__file__), "rss_sources.json")
GEMINI_SCRIPT_PROMPT_PATH = os.path.join(
    os.path.dirname(__file__), "gemini_script_prompt.txt"
)


dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
s3_client = boto3.client("s3", region_name=AWS_REGION)


def load_rss_sources() -> Dict[str, List[str]]:
    """rss_sources.json を読み込む。"""
    with open(RSS_SOURCES_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def fetch_latest_entry(feed_url: str) -> Optional[feedparser.FeedParserDict]:
    """指定 RSS フィードから最新のエントリを 1 件取得。"""
    feed = feedparser.parse(feed_url)
    if not feed.entries:
        return None
    return feed.entries[0]


def build_topic_summary(entry: feedparser.FeedParserDict) -> str:
    """RSS エントリから簡易な topic_summary を生成。"""
    title = entry.get("title", "")
    summary = entry.get("summary", "") or entry.get("description", "")
    text = f"{title}\n{summary}"
    # 文字数制限（Gemini への入力やハッシュ安定のため軽くトリム）
    return text.strip()[:2000]


def calc_content_hash(topic_summary: str) -> str:
    """topic_summary から SHA256 ハッシュを生成。"""
    return hashlib.sha256(topic_summary.encode("utf-8")).hexdigest()


def exists_in_dynamodb(content_hash: str) -> bool:
    """VideoHistory テーブルに content_hash が存在するか確認。"""
    table = dynamodb.Table(DDB_TABLE_NAME)
    resp = table.get_item(Key={"content_hash": content_hash})
    return "Item" in resp


def init_gemini() -> None:
    """Gemini クライアント初期化。"""
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY が環境変数に設定されていません。")
    if genai is None:
        raise RuntimeError("google-generativeai がインポートできません。requirements を確認してください。")
    genai.configure(api_key=GEMINI_API_KEY)


def score_news_relevance(topic_summary: str) -> float:
    """
    Gemini に PC/ハードウェアニュースとしての適合度を 0-10 で採点させる。
    返り値は float。パースに失敗した場合は 0 を返す。
    """
    init_gemini()
    model = genai.GenerativeModel(GEMINI_MODEL_NAME)
    prompt = (
        "以下のニュース要約が、日本語のPC/自作PC/ハードウェア系YouTubeチャンネルで扱う"
        "トピックとしてどれくらい適しているかを 0〜10 の実数で 1 行だけ出力してください。\n\n"
        "評価基準:\n"
        "- 10: GPU/CPU/PCパーツ/自作PC/ベンチマーク/ゲーミングPC に直結する話題\n"
        "- 7〜9: PCユーザーや自作勢に強く関係するOS/ドライバ/プラットフォーム/半導体ニュース\n"
        "- 4〜6: 広くテックニュースだがPC/ハードウェア要素は薄い\n"
        "- 0〜3: PCやハードウェアとはほぼ無関係\n\n"
        f"ニュース要約:\n{topic_summary}\n\n"
        "数値のみ（例: 7.5）の形式で出力してください。"
    )
    resp = model.generate_content(prompt)
    text = resp.text.strip()
    try:
        return float(text)
    except ValueError:
        return 0.0


def fetch_reaction_summary(group_b_sources: List[str]) -> Tuple[str, str]:
    """
    グループB（コミュニティ反応ソース）から簡易な反応概要を取得。
    ここではサンプルとして最初のフィードのタイトルを軽くまとめるだけ。
    戻り値: (サイト名 or URL, 要約テキスト)
    """
    if not group_b_sources:
        return "", ""

    url = group_b_sources[0]
    feed = feedparser.parse(url)
    if not feed.entries:
        return url, ""

    # 最新エントリ数件のタイトルを まとめる
    titles = [e.get("title", "") for e in feed.entries[:5] if e.get("title")]
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
    init_gemini()
    model = genai.GenerativeModel(GEMINI_MODEL_NAME)

    prompt = (
        base_prompt
        + "\n\n"
        + "### 入力データ具体値\n"
        + f"[事実ソース（サイト名: {site_name_a}）]\n"
        + f"タイトル: {title_a}\n"
        + f"内容要約: {summary_a}\n\n"
        + f"[反応ソース（コミュニティ名: {site_name_b or '（反応ソース無し）'}）]\n"
        + f"話題のトピック: {summary_b or '（反応ソースが取得できなかった場合は、一般的な反応を仮定して生成してください）'}\n\n"
        + "JSON 形式が壊れないように、必ず有効な JSON のみを出力してください。"
    )

    resp = model.generate_content(prompt)
    text = resp.text.strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # JSON で無かった場合の簡易リカバリ（将来的に再試行戦略を入れても良い）
        raise RuntimeError("Gemini から有効な JSON 応答を取得できませんでした。")

    return data


def save_script_to_s3(
    script_json: Dict[str, Any], content_hash: str, source_url: str, published_at: str
) -> str:
    """
    台本 JSON を S3 に保存。
    戻り値: S3 オブジェクトキー
    """
    if not S3_BUCKET_NAME:
        raise RuntimeError("SCRIPT_S3_BUCKET が環境変数に設定されていません。")

    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    key = f"{S3_PREFIX.rstrip('/')}/{now}_{content_hash}.json"

    # メタ情報を付加して保存
    enriched = dict(script_json)
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
    s3_client.put_object(Bucket=S3_BUCKET_NAME, Key=key, Body=body, ContentType="application/json; charset=utf-8")
    return key


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

    # シンプルに group_a[0] の最新記事を処理対象とする（将来的にループ処理に拡張可能）
    primary_url = group_a[0]
    primary_entry = fetch_latest_entry(primary_url)
    if primary_entry is None:
        raise RuntimeError(f"RSS から記事を取得できませんでした: {primary_url}")

    topic_summary = build_topic_summary(primary_entry)
    content_hash = calc_content_hash(topic_summary)

    # DynamoDB で重複チェック
    if exists_in_dynamodb(content_hash):
        return {
            "status": "skipped",
            "reason": "already_exists",
            "content_hash": content_hash,
        }

    # Gemini で PC チャンネル適合度スコアリング
    score = score_news_relevance(topic_summary)
    if score < 7.0:
        return {
            "status": "skipped",
            "reason": "low_score",
            "score": score,
            "content_hash": content_hash,
        }

    # グループBから反応ソース概要を取得
    reaction_site_name, reaction_summary = fetch_reaction_summary(group_b)

    # 台本プロンプトを読み込み
    script_prompt = load_script_prompt()

    site_name_a = primary_entry.get("source", {}).get("title") if primary_entry.get("source") else ""
    site_name_a = site_name_a or primary_entry.get("feedburner_origlink", primary_url)
    title_a = primary_entry.get("title", "")
    published_at = ""
    if "published_parsed" in primary_entry and primary_entry.published_parsed:
        dt = datetime(*primary_entry.published_parsed[:6], tzinfo=timezone.utc)
        published_at = dt.isoformat()
    source_url = primary_entry.get("link", primary_url)

    # Gemini で台本 JSON を生成
    script_json = generate_script_with_gemini(
        base_prompt=script_prompt,
        site_name_a=site_name_a,
        title_a=title_a,
        summary_a=topic_summary,
        site_name_b=reaction_site_name,
        summary_b=reaction_summary,
    )

    # S3 に保存
    s3_key = save_script_to_s3(
        script_json=script_json,
        content_hash=content_hash,
        source_url=source_url,
        published_at=published_at,
    )

    # GitHub Actions を起動
    trigger_github_dispatch(s3_key=s3_key, content_hash=content_hash)

    return {
        "status": "ok",
        "content_hash": content_hash,
        "score": score,
        "s3_bucket": S3_BUCKET_NAME,
        "s3_key": s3_key,
        "source_url": source_url,
        "published_at": published_at,
    }

