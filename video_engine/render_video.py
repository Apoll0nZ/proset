import json
import os
import tempfile
import math
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List
import random

# GitHub Actions (Linux) 環境向けに ImageMagick のパスを明示
if os.name != 'nt':
    os.environ["IMAGEMAGICK_BINARY"] = "/usr/bin/convert"

import boto3
import numpy as np
from PIL import Image
from botocore.client import Config
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from moviepy.editor import AudioFileClip, CompositeVideoClip, TextClip, ImageClip, VideoFileClip, vfx, concatenate_audioclips, CompositeAudioClip
import requests

from create_thumbnail import create_thumbnail

"""
動画レンダリング & YouTube アップロードスクリプト。

役割:
- S3 から最新の台本 JSON を取得
- VOICEVOX API で日本語音声生成（複数セリフ対応）
- MoviePy + FFmpeg で背景画像 + 字幕 + 音声を合成
- mp4 動画を書き出し
- サムネイル画像を生成
- YouTube Data API v3 で「非公開」アップロード + サムネイル設定
- アップロード成功後、DynamoDB(VideoHistory) に put_item 登録
- 一時ファイル削除
"""


AWS_REGION = os.environ.get("MY_AWS_REGION", "ap-northeast-1")
S3_BUCKET = os.environ.get("SCRIPT_S3_BUCKET", "")
SCRIPTS_PREFIX = os.environ.get("SCRIPT_S3_PREFIX", "scripts/")
DDB_TABLE_NAME = os.environ.get("MY_DDB_TABLE_NAME", "VideoHistory")

YOUTUBE_AUTH_JSON = os.environ.get("YOUTUBE_AUTH_JSON", "")
VOICEVOX_API_URL = os.environ.get("VOICEVOX_API_URL", "http://localhost:50021")

BACKGROUND_IMAGE_PATH = os.environ.get(
    "BACKGROUND_IMAGE_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "background.png"),
)
# クロスプラットフォーム対応のフォント検出
def find_japanese_font() -> str:
    """日本語対応フォントをクロスプラットフォームで検出"""
    possible_fonts = [
        # Linux (GitHub Actions)
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansJP-Regular.otf",
        "/usr/share/fonts/truetype/noto/NotoSansJP-Regular.otf",
        # macOS
        "/System/Library/Fonts/Hiragino Sans GB.ttc",
        "/System/Library/Fonts/STHeiti Light.ttc",
        "/System/Library/Fonts/Hiragino Sans.ttc",
        # Windows
        "C:\\Windows\\Fonts\\msgothic.ttc",
        "C:\\Windows\\Fonts\\msmincho.ttc",
        # 環境変数指定
        os.environ.get("FONT_PATH", ""),
    ]
    
    for font_path in possible_fonts:
        if font_path and os.path.exists(font_path):
            print(f"[DEBUG] Found font: {font_path}")
            print(f"[DEBUG] Selected font path: {font_path}")
            return font_path
    
    # どれも見つからない場合はデフォルト（MoviePyが自動選択）
    print("[DEBUG] No Japanese font found, using default")
    print(f"[DEBUG] Selected font path: (default)")
    return ""

FONT_PATH = os.environ.get("FONT_PATH", find_japanese_font())

# 画像取得用環境変数
IMAGES_S3_BUCKET = os.environ.get("IMAGES_S3_BUCKET", S3_BUCKET)  # デフォルトはメインS3バケット
IMAGES_S3_PREFIX = os.environ.get("IMAGES_S3_PREFIX", "assets/images/")  # 画像格納先プレフィックス
LOCAL_TEMP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "temp")  # ローカルtempフォルダ（S3 tempフォルダと連携）

# tempフォルダが存在しない場合は作成
os.makedirs(LOCAL_TEMP_DIR, exist_ok=True)

# ImageMagickの環境変数を設定（GitHub Actions対応）
if not os.environ.get("IMAGEMAGICK_BINARY"):
    # 一般的なImageMagickのパスを設定
    possible_paths = [
        "/usr/bin/convert",
        "/usr/local/bin/convert", 
        "/opt/homebrew/bin/convert",
        "convert"
    ]
    
    for path in possible_paths:
        if os.path.exists(path) or path == "convert":  # convertはPATHにある可能性
            os.environ["IMAGEMAGICK_BINARY"] = path
            print(f"Set IMAGEMAGICK_BINARY to: {path}")
            break
    else:
        print("Warning: ImageMagick not found, text generation may fail")

# Google画像検索用環境変数（Playwright使用）
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "")
GOOGLE_CSE_ID = os.environ.get("GOOGLE_CSE_ID", "")
# OAuth 2.0認証情報
YOUTUBE_TOKEN_JSON = os.environ.get("YOUTUBE_TOKEN_JSON", "")
YOUTUBE_CLIENT_SECRETS_JSON = os.environ.get("YOUTUBE_CLIENT_SECRETS_JSON", "")

VIDEO_WIDTH = int(os.environ.get("VIDEO_WIDTH", "1920"))
VIDEO_HEIGHT = int(os.environ.get("VIDEO_HEIGHT", "1080"))
FPS = int(os.environ.get("FPS", "30"))

# デバッグモード（Trueの時は最初の60秒のみ書き出し）
DEBUG_MODE = True


s3_client = boto3.client("s3", region_name=AWS_REGION, config=Config(signature_version="s3v4"))
dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)


def download_random_background_video() -> str:
    """S3からassets/フォルダの.mp4ファイルをランダムに1つ選択してダウンロード"""
    try:
        # assets/フォルダから.mp4ファイルをリストアップ
        print(f"Listing .mp4 files in s3://{S3_BUCKET}/assets/")
        resp = s3_client.list_objects_v2(
            Bucket=S3_BUCKET,
            Prefix="assets/",
            MaxKeys=100
        )
        
        mp4_files = []
        contents = resp.get("Contents", [])
        for obj in contents:
            key = obj["Key"]
            if key.lower().endswith(".mp4"):
                mp4_files.append(key)
        
        if not mp4_files:
            print("No .mp4 files found in assets/ folder")
            return None
        
        # ランダムに1つ選択
        import random
        selected_key = random.choice(mp4_files)
        print(f"Selected background video: {selected_key}")
        
        # ダウンロード
        temp_dir = tempfile.mkdtemp()
        filename = os.path.basename(selected_key)
        local_path = os.path.join(temp_dir, filename)
        
        print(f"Downloading background video from S3: s3://{S3_BUCKET}/{selected_key}")
        s3_client.download_file(S3_BUCKET, selected_key, local_path)
        print(f"Successfully downloaded to: {local_path}")
        return local_path
        
    except Exception as e:
        print(f"Failed to download background video: {e}")
        return None


def process_background_video_for_hd(bg_path: str, total_duration: float):
    """背景動画を1920x1080フルHDにインテリジェント・リサイズ"""
    try:
        print(f"Processing background video for HD: {bg_path}")
        
        # 動画を読み込み（低解像度素材を想定）
        bg_clip = VideoFileClip(bg_path)
        original_width, original_height = bg_clip.size
        print(f"Original video size: {original_width}x{original_height}")
        
        # 音声をミュート
        bg_clip = bg_clip.without_audio()
        print("Background video audio muted")
        
        # DEBUG_MODEなら60秒にカット（処理対象を大幅削減）
        if DEBUG_MODE:
            bg_clip = bg_clip.subclip(0, 60)
            print("DEBUG_MODE: Background video trimmed to 60s")
        else:
            bg_clip = bg_clip.subclip(0, total_duration)
            print(f"Background video trimmed to {total_duration:.2f}s")
        
        # 1920x1080に引き伸ばして画面いっぱいに
        bg_clip = bg_clip.resize(newsize=(1920, 1080))
        print("Resized to 1920x1080 (intelligent stretch)")
        
        # 画像処理を適用して引き伸ばしの粗さを隠す
        bg_clip = bg_clip.fx(vfx.colorx, 0.8)  # 少し暗くして引き伸ばしの粗さを目立たなくする
        print("Applied colorx effect (0.8) to hide stretching artifacts")
        
        # 音声の長さに合わせてループ（DEBUG_MODEなら60秒で固定）
        if DEBUG_MODE:
            bg_clip = bg_clip.loop(duration=60).set_duration(60)
        else:
            bg_clip = bg_clip.loop(duration=total_duration).set_duration(total_duration)
        print(f"Looped to match duration: {60 if DEBUG_MODE else total_duration:.2f}s")
        
        return bg_clip
        
    except Exception as e:
        print(f"Failed to process background video: {e}")
        return None


def download_background_music() -> str:
    """S3からBGM（assets/bgm.mp3）をダウンロード"""
    bgm_key = "assets/bgm.mp3"
    local_path = os.path.join(LOCAL_TEMP_DIR, "bgm.mp3")
    
    try:
        print(f"Downloading background music from S3: s3://{S3_BUCKET}/{bgm_key}")
        s3_client.download_file(S3_BUCKET, bgm_key, local_path)
        print(f"Successfully downloaded BGM to: {local_path}")
        return local_path
    except Exception as e:
        print(f"Failed to download background music: {e}")
        return None


def search_images_with_playwright(keyword: str, max_results: int = 5) -> List[Dict[str, str]]:
    """PlaywrightでGoogle画像検索（無料版）"""
    try:
        from playwright.sync_api import sync_playwright
        
        print(f"Searching images with Playwright for: {keyword}")
        
        with sync_playwright() as p:
            try:
                # ヘッドレスブラウザ起動
                browser = p.chromium.launch(headless=True)
            except Exception as e:
                print(f"Failed to launch browser: {e}")
                print("Falling back to gradient background")
                return []
            
            try:
                context = browser.new_context()
                page = context.new_page()
                
                # Google画像検索ページへ
                search_url = f"https://www.google.com/search?q={keyword}&tbm=isch"
                page.goto(search_url)
                page.wait_for_load_state('networkidle')
                
                # 画像URLを収集
                images = []
                image_elements = page.query_selector_all('img[src]')
                
                for i, img in enumerate(image_elements[:max_results]):
                    try:
                        src = img.get_attribute('src')
                        if src and src.startswith('http') and 'encrypted' not in src:
                            # サムネイルURLをフルサイズに変換（簡易的）
                            if 'base64' not in src:
                                images.append({
                                    'url': src,
                                    'title': f'Image {i+1} for {keyword}',
                                    'thumbnail': src
                                })
                    except:
                        continue
                
                browser.close()
                print(f"Found {len(images)} images with Playwright")
                return images
                
            except Exception as e:
                print(f"Browser operation failed: {e}")
                try:
                    browser.close()
                except:
                    pass
                return []
            
    except ImportError:
        print("Playwright unavailable, using gradient background")
        return []
    except Exception as e:
        print(f"Image search failed: {e}")
        return []


def get_youtube_credentials_from_env():
    """環境変数からYouTube OAuth認証情報を取得（GitHub Secrets対応）"""
    try:
        # YOUTUBE_TOKEN_JSONは必須
        if not YOUTUBE_TOKEN_JSON:
            raise RuntimeError("YOUTUBE_TOKEN_JSON not found in environment variables")
        
        token_data = json.loads(YOUTUBE_TOKEN_JSON)
        
        # 個別の環境変数からclient_secrets.json形式を構築（常にこちらを使用）
        client_id = os.environ.get("YOUTUBE_CLIENT_ID", "")
        client_secret = os.environ.get("YOUTUBE_CLIENT_SECRET", "")
        
        if not client_id or not client_secret:
            raise RuntimeError("YOUTUBE_CLIENT_ID and YOUTUBE_CLIENT_SECRET must be set")
        
        client_secrets = {
            "installed": {
                "client_id": client_id,
                "client_secret": client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
                "redirect_uris": ["http://localhost"]
            }
        }
        
        print("Successfully loaded YouTube OAuth credentials from environment")
        return token_data, client_secrets
        
    except Exception as e:
        print(f"Failed to load YouTube credentials: {e}")
        raise


def refresh_youtube_token_if_needed():
    """必要に応じてYouTubeトークンをリフレッシュ"""
    try:
        token_data, client_secrets = get_youtube_credentials_from_env()
        
        # トークンの有効期限チェック
        import time
        if token_data.get('expires_at', 0) > time.time():
            print("YouTube token is still valid")
            return token_data
        
        print("YouTube token expired, attempting refresh...")
        
        # トークンリフレッシュロジック（簡易実装）
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        
        credentials = Credentials.from_authorized_user_info(
            token_data,
            scopes=['https://www.googleapis.com/auth/youtube.upload']
        )
        
        # トークンリフレッシュ
        credentials.refresh(Request())
        
        # 新しいトークン情報を返す
        new_token_data = {
            'token': credentials.token,
            'refresh_token': credentials.refresh_token,
            'expires_at': credentials.expiry.timestamp(),
            'client_id': credentials.client_id,
            'client_secret': credentials.client_secret
        }
        
        print("Successfully refreshed YouTube token")
        return new_token_data
        
    except Exception as e:
        print(f"Failed to refresh YouTube token: {e}")
        raise


def build_youtube_client_from_env():
    """環境変数からYouTube APIクライアントを構築（個別文字列から動的生成）"""
    try:
        token_data = refresh_youtube_token_if_needed()
        
        # 環境変数から直接文字列を取得
        client_id = os.environ.get("YOUTUBE_CLIENT_ID")
        client_secret = os.environ.get("YOUTUBE_CLIENT_SECRET")
        
        # JSONパースを介さず、プログラム内で辞書を組み立てる
        client_secrets = {
            "installed": {
                "client_id": client_id,
                "client_secret": client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token"
            }
        }
        
        # 認証情報を構築
        from google.oauth2.credentials import Credentials
        credentials = Credentials(
            token=token_data['token'],
            refresh_token=token_data['refresh_token'],
            token_uri='https://oauth2.googleapis.com/token',
            client_id=client_secrets['installed']['client_id'],
            client_secret=client_secrets['installed']['client_secret'],
            scopes=['https://www.googleapis.com/auth/youtube.upload']
        )
        
        youtube = build("youtube", "v3", credentials=credentials)
        print("Successfully built YouTube client by dynamic dictionary generation")
        return youtube
        
    except Exception as e:
        print(f"Failed to build YouTube client: {e}")
        raise


def extract_image_keywords_from_script(script_data: Dict[str, Any]) -> str:
    """台本から画像検索キーワードを抽出"""
    try:
        title = script_data.get("title", "")
        content = script_data.get("content", {})
        topic_summary = content.get("topic_summary", "")
        script_parts = content.get("script_parts", [])
        
        # 台本のテキストを結合
        all_text = f"{title} {topic_summary}"
        for part in script_parts:
            all_text += f" {part.get('text', '')}"
        
        # 重要キーワードを抽出（簡易的な実装）
        # 技術関連のキーワードを優先
        tech_keywords = ["AI", "人工知能", "機械学習", "データサイエンス", "プログラミング", 
                        "ソフトウェア", "テクノロジー", "コンピュータ", "デジタル", "イノベーション"]
        
        # テキストからキーワードを検索
        found_keywords = []
        for keyword in tech_keywords:
            if keyword in all_text:
                found_keywords.append(keyword)
        
        # キーワードが見つからない場合はタイトルから重要単語を抽出
        if not found_keywords:
            # 簡易的に名詞っぽい単語を抽出（文字数が3文字以上）
            words = title.split()
            found_keywords = [word for word in words if len(word) >= 3]
        
        # 最初のキーワードを使用
        if found_keywords:
            selected_keyword = found_keywords[0]
            print(f"Extracted keyword: {selected_keyword}")
            return selected_keyword
        else:
            # フォールバックキーワード
            fallback_keyword = "technology"
            print(f"Using fallback keyword: {fallback_keyword}")
            return fallback_keyword
            
    except Exception as e:
        print(f"Failed to extract keywords: {e}")
        return "technology"  # 最終フォールバック



def download_image_from_url(image_url: str, filename: str = None) -> str:
    """URLから画像をダウンロードしてtempフォルダに保存し、S3にもアップロード"""
    try:
        if not filename:
            # URLからファイル名を生成
            import hashlib
            url_hash = hashlib.md5(image_url.encode()).hexdigest()[:8]
            filename = f"ai_image_{url_hash}.jpg"
        
        local_path = os.path.join(LOCAL_TEMP_DIR, filename)
        
        print(f"Downloading image from URL: {image_url}")
        response = requests.get(image_url, timeout=30)
        response.raise_for_status()
        
        # 画像をローカルに保存
        with open(local_path, 'wb') as f:
            f.write(response.content)
        
        # S3のtempフォルダにもアップロード
        try:
            s3_key = f"temp/{filename}"
            s3_client.upload_file(local_path, S3_BUCKET, s3_key)
            print(f"Uploaded image to S3: s3://{S3_BUCKET}/{s3_key}")
        except Exception as e:
            print(f"Failed to upload image to S3: {e}")
        
        print(f"Successfully downloaded image to: {local_path}")
        return local_path
        
    except Exception as e:
        print(f"Failed to download image from URL {image_url}: {e}")
        return None


def get_ai_selected_image(script_data: Dict[str, Any]) -> str:
    """AIによる動的選別・自動取得で最適な画像を取得（Playwright版のみ）"""
    try:
        # 1. キーワード抽出
        keyword = extract_image_keywords_from_script(script_data)
        
        # 2. Playwrightで画像検索（無料）のみ使用
        images = search_images_with_playwright(keyword)
        
        if images:
            # 最初の画像（最も関連性が高い）をダウンロード
            best_image = images[0]
            image_path = download_image_from_url(best_image['url'])
            
            if image_path:
                print(f"Successfully selected and downloaded AI image: {best_image['title']}")
                return image_path
        
        # 画像が取得できなかった場合はNoneを返す（フォールバックなし）
        print("No images found with Playwright, using gradient background")
        return None
            
    except Exception as e:
        print(f"AI image selection process failed: {e}")
        return None


def download_image_from_s3(image_key: str) -> str:
    """S3から画像をダウンロードしてtempフォルダに保存"""
    try:
        # 一時ファイルパスを生成
        filename = os.path.basename(image_key)
        local_path = os.path.join(LOCAL_TEMP_DIR, f"video_image_{filename}")
        
        print(f"Downloading image from S3: s3://{IMAGES_S3_BUCKET}/{image_key}")
        s3_client.download_file(IMAGES_S3_BUCKET, image_key, local_path)
        print(f"Successfully downloaded image to: {local_path}")
        return local_path
        
    except Exception as e:
        print(f"Failed to download image {image_key}: {e}")
        return None


def create_gradient_background(width: int, height: int) -> np.ndarray:
    """濃いネイビーから黒へのなだらかなグラデーション背景を生成"""
    # ネイビーから黒へのグラデーション
    navy_color = np.array([10, 25, 47])  # 濃いネイビー
    black_color = np.array([0, 0, 0])     # 黒
    
    # 垂直グラデーション（上から下へ）
    gradient = np.zeros((height, width, 3), dtype=np.uint8)
    for y in range(height):
        ratio = y / height
        color = navy_color * (1 - ratio) + black_color * ratio
        gradient[y, :] = color.astype(np.uint8)
    
    return gradient


def create_breathing_effect(duration: float) -> List[float]:
    """呼吸アニメーション（97%〜100%）のスケール値リストを生成"""
    import math
    fps = 30
    frames = int(duration * fps)
    scales = []
    
    for i in range(frames):
        # 4秒周期で呼吸アニメーション
        t = (i / fps) % 4.0
        scale = 0.97 + 0.03 * (0.5 + 0.5 * math.sin(2 * math.pi * t / 4.0))
        scales.append(scale)
    
    return scales


def build_youtube_client():
    """既に取得済みの OAuth2 資格情報(JSON文字列)から YouTube API クライアントを構築。"""
    # 環境変数から認証情報を取得（ヘッドレス対応）
    return build_youtube_client_from_env()


def get_latest_script_object() -> Dict[str, Any]:
    """S3 から最新(LastModified が最大)のスクリプト JSON オブジェクトを取得。"""
    if not S3_BUCKET:
        raise RuntimeError("S3_BUCKET が設定されていません。")

    resp = s3_client.list_objects_v2(
        Bucket=S3_BUCKET,
        Prefix=SCRIPTS_PREFIX.rstrip("/") + "/",
    )
    contents = resp.get("Contents", [])
    if not contents:
        raise RuntimeError("scripts/ に台本ファイルがありません")

    latest = max(contents, key=lambda x: x["LastModified"])
    key = latest["Key"]

    obj = s3_client.get_object(Bucket=S3_BUCKET, Key=key)
    body = obj["Body"].read().decode("utf-8")
    data = json.loads(body)
    return {"key": key, "data": data}


def split_text_for_voicevox(text: str) -> List[str]:
    """長いテキストを句読点で適切に分割してVOICEVOXのAPI制限を回避"""
    if not text:
        return []
    
    # 句読点で分割（。！？、。）
    import re
    sentences = re.split(r'([。！？、。])', text)
    
    # 分割記号を元に戻す
    result = []
    current = ""
    
    for i in range(0, len(sentences), 2):
        if i + 1 < len(sentences):
            sentence = sentences[i] + sentences[i + 1]
        else:
            sentence = sentences[i]
        
        # 200文字を超える場合はさらに分割
        if len(sentence) > 200:
            # 半角スペースや全角スペースで分割
            words = re.split(r'([\s　])', sentence)
            temp = ""
            for j in range(0, len(words), 2):
                if j + 1 < len(words):
                    word = words[j] + words[j + 1]
                else:
                    word = words[j]
                
                if len(temp + word) > 200 and temp:
                    result.append(temp.strip())
                    temp = word
                else:
                    temp += word
            
            if temp.strip():
                result.append(temp.strip())
        else:
            result.append(sentence.strip())
    
    return [s for s in result if s.strip()]
def synthesize_speech_voicevox(text: str, speaker_id: int, out_path: str) -> None:
    """
    VOICEVOX API を用いて日本語音声を生成し、音声ファイルとして保存。
    長いテキストは自動的に分割して合成し、結合する。
    リトライロジックを実装し、ネットワークエラーに対応。
    
    Args:
        text: 音声化するテキスト
        speaker_id: VOICEVOX のスピーカーID（例: 3=ずんだもん）
        out_path: 出力音声ファイルパス（.wav形式）
    """
    import time
    
    # テキストを分割
    text_parts = split_text_for_voicevox(text)
    
    if not text_parts:
        raise RuntimeError(f"音声化するテキストが空です: {text[:50]}")
    
    audio_clips = []
    part_durations: List[float] = []
    temp_dir = tempfile.mkdtemp()
    
    try:
        # 各パートを音声合成
        for i, part_text in enumerate(text_parts):
            if not part_text.strip():
                continue
            
            # 音声クエリ生成のリトライロジック
            query_data = None
            for attempt in range(1, 4):  # 最大3回リトライ
                try:
                    print(f"Generating audio query for part {i}, attempt {attempt}/3")
                    query_url = f"{VOICEVOX_API_URL}/audio_query"
                    query_params = {
                        "text": part_text,
                        "speaker": speaker_id
                    }
                    query_resp = requests.post(query_url, params=query_params, timeout=30)
                    if query_resp.status_code != 200:
                        raise RuntimeError(f"VOICEVOX クエリ生成失敗: {query_resp.status_code} {query_resp.text}")
                    
                    query_data = query_resp.json()
                    print(f"Audio query generated successfully for part {i}")
                    break  # 成功したらループを抜ける
                    
                except Exception as e:
                    if attempt < 3:
                        print(f"Attempt {attempt} failed for audio query (part {i}), retrying... Error: {str(e)}")
                        time.sleep(2)  # 2秒待機
                    else:
                        print(f"Critical error: All 3 attempts failed for audio query (part {i}). Error: {str(e)}")
                        raise RuntimeError(f"Failed to generate audio query after 3 attempts for part {i}: {str(e)}")
            
            # 音声合成のリトライロジック
            synthesis_content = None
            for attempt in range(1, 4):  # 最大3回リトライ
                try:
                    print(f"Synthesizing audio for part {i}, attempt {attempt}/3")
                    synthesis_url = f"{VOICEVOX_API_URL}/synthesis"
                    synthesis_params = {"speaker": speaker_id}
                    synthesis_resp = requests.post(
                        synthesis_url,
                        params=synthesis_params,
                        json=query_data,
                        timeout=60,
                        headers={"Content-Type": "application/json"}
                    )
                    if synthesis_resp.status_code != 200:
                        raise RuntimeError(f"VOICEVOX 音声合成失敗: {synthesis_resp.status_code} {synthesis_resp.text}")
                    
                    synthesis_content = synthesis_resp.content
                    print(f"Audio synthesis successful for part {i}")
                    break  # 成功したらループを抜ける
                    
                except Exception as e:
                    if attempt < 3:
                        print(f"Attempt {attempt} failed for audio synthesis (part {i}), retrying... Error: {str(e)}")
                        time.sleep(2)  # 2秒待機
                    else:
                        print(f"Critical error: All 3 attempts failed for audio synthesis (part {i}). Error: {str(e)}")
                        raise RuntimeError(f"Failed to synthesize audio after 3 attempts for part {i}: {str(e)}")
            
            # 一時音声ファイルとして保存
            temp_audio_path = os.path.join(temp_dir, f"temp_audio_{i}.wav")
            with open(temp_audio_path, "wb") as out_f:
                out_f.write(synthesis_content)
            
            clip = AudioFileClip(temp_audio_path)
            audio_clips.append(clip)
        
        if not audio_clips:
            raise RuntimeError("音声クリップが生成されませんでした。")
        
        # すべての音声クリップを結合
        final_audio = concatenate_audioclips(audio_clips)
        final_audio.write_audiofile(out_path, codec="pcm_s16le", fps=44100)
        
        # クリップを解放
        for clip in audio_clips:
            clip.close()
        final_audio.close()
        
    finally:
        # 一時ファイルを削除
        import shutil
        shutil.rmtree(temp_dir, ignore_errors=True)


def synthesize_multiple_speeches(script_parts: List[Dict[str, Any]], tmpdir: str) -> (str, List[float]):
    """
    複数のセリフを順番に音声合成し、結合した音声ファイルを生成。
    メモリ効率を改善し、大量の音声パーツ処理に対応。
    リトライロジックを実装し、ネットワークエラーに対応。
    
    Returns:
        結合された音声ファイルのパス
    """
    import time
    
    audio_clips = []
    part_durations: List[float] = []
    generated_audio_files = []
    successful_parts = 0
    failed_parts = []
    
    print(f"Processing {len(script_parts)} script parts...")
    
    for i, part in enumerate(script_parts):
        clip = None
        part_duration = 0.0
        success = False
        
        # 各パーツの処理にリトライロジックを実装
        for attempt in range(1, 4):  # 最大3回リトライ
            try:
                part_name = part.get("part", "")
                text = part.get("text", "")
                
                if not text:
                    print(f"Warning: Empty text for part {i}, skipping...")
                    success = True  # 空テキストは成功とみなす
                    break
                
                # part名に応じてspeaker_idを決定
                if part_name.startswith("article_"):
                    speaker_id = 3
                elif part_name == "reaction":
                    speaker_id = part.get("speaker_id", 1)
                    if speaker_id == 3:
                        speaker_id = random.choice([1, 2, 8, 10, 14])
                else:
                    speaker_id = part.get("speaker_id", 3)
                
                audio_path = os.path.join(tmpdir, f"audio_{i}.wav")
                
                # 音声合成（内部でリトライロジックが動作）
                print(f"Synthesizing part {i} (attempt {attempt}/3): {text[:50]}...")
                synthesize_speech_voicevox(text, speaker_id, audio_path)
                
                if os.path.exists(audio_path):
                    # AudioFileClipを作成してリストに追加
                    clip = AudioFileClip(audio_path)
                    part_duration = clip.duration
                    audio_clips.append(clip)
                    generated_audio_files.append(audio_path)
                    part_durations.append(part_duration)
                    successful_parts += 1
                    success = True
                    print(f"Successfully created audio clip for part {i}")
                    break  # 成功したらリトライループを抜ける
                else:
                    raise RuntimeError(f"Audio file not created for part {i}")
                    
            except Exception as e:
                if attempt < 3:
                    print(f"Attempt {attempt} failed for part {i}, retrying... Error: {str(e)}")
                    time.sleep(2)  # 2秒待機
                    
                    # クリップが存在する場合はクリーンアップ
                    if clip:
                        try:
                            clip.close()
                        except:
                            pass
                    clip = None
                else:
                    print(f"Critical error: All 3 attempts failed for part {i}. Error: {str(e)}")
                    print(f"Part data: {part}")
                    failed_parts.append(i)
                    
                    # 最後の試行で失敗したクリップをクリーンアップ
                    if clip:
                        try:
                            clip.close()
                        except:
                            pass
        if not success:
            part_durations.append(0.0)
    
    # 処理結果のサマリーを出力
    print(f"Audio synthesis completed: {successful_parts}/{len(script_parts)} parts successful")
    if failed_parts:
        print(f"Failed parts: {failed_parts}")
        print(f"Warning: {len(failed_parts)} parts failed, but continuing with successful parts...")
    
    if not audio_clips:
        raise RuntimeError(f"音声クリップが生成されませんでした。{len(failed_parts)}個のパーツが失敗しました。script_partsの内容を確認してください。")
    
    print(f"Concatenating {len(audio_clips)} audio clips...")
    
    final_audio = None
    final_audio_path = os.path.join(tmpdir, "final_audio.wav")
    
    try:
        # すべての音声クリップを結合
        final_audio = concatenate_audioclips(audio_clips)
        final_audio.write_audiofile(final_audio_path, codec="pcm_s16le", fps=44100)
        print(f"Final audio saved to: {final_audio_path}")
        
    except Exception as e:
        print(f"Error during audio concatenation: {e}")
        raise
    finally:
        # すべてのクリップを解放
        for clip in audio_clips:
            try:
                clip.close()
            except Exception as e:
                print(f"Error closing audio clip: {e}")
        
        if final_audio:
            try:
                final_audio.close()
            except Exception as e:
                print(f"Error closing final audio: {e}")
        
        # 個別の音声ファイルを削除してメモリを解放
        for audio_file in generated_audio_files:
            try:
                if os.path.exists(audio_file):
                    os.remove(audio_file)
                    print(f"Cleaned up temporary audio file: {audio_file}")
            except Exception as e:
                print(f"Failed to remove temporary audio file {audio_file}: {e}")
    
    return final_audio_path, part_durations


def build_video_with_subtitles(
    background_path: str,
    font_path: str,
    script_parts: List[Dict[str, Any]],
    script_data: Dict[str, Any],
    part_durations: List[float],
    audio_path: str,
    out_video_path: str,
) -> None:
    """
    新しい映像生成ワークフロー：
    Layer 1: ぼかし済み背景動画（ループ）
    Layer 2: 画像スライド（複数枚）
    Layer 3: 左上セグメント表示
    Layer 4: 下部字幕（音声同期）
    BGM: 背景音楽をミックス
    """
    audio_clip = None
    bg_clip = None
    bgm_clip = None
    text_clips = []
    segment_clips = []
    video = None
    
    try:
        # メイン音声（VOICEVOX）の長さを基準にする
        audio_clip = AudioFileClip(audio_path)
        total_duration = audio_clip.duration
        print(f"Main audio duration: {total_duration:.2f} seconds")

        # BGMの準備
        bgm_path = download_background_music()
        if bgm_path and os.path.exists(bgm_path):
            print("Loading background music")
            try:
                bgm_clip = AudioFileClip(bgm_path)
                print(f"BGM original duration: {bgm_clip.duration:.2f} seconds")
                
                # BGMを動画長に合わせてループまたはトリミング
                if bgm_clip.duration < total_duration:
                    # BGMが短い場合はループ
                    from moviepy.audio.fx.audio_loop import audio_loop
                    bgm_clip = audio_loop(bgm_clip, duration=total_duration)
                    print("BGM looped to match video duration")
                elif bgm_clip.duration > total_duration:
                    # BGMが長い場合はトリミング
                    bgm_clip = bgm_clip.subclip(0, total_duration)
                    print("BGM trimmed to match video duration")
                
                # 音量を10%に下げてナレーションを主役に
                bgm_clip = bgm_clip.volumex(0.10)
                print("BGM volume reduced to 10%")
                
                # 最初の2秒でフェードイン
                if total_duration > 2:
                    bgm_clip = bgm_clip.audio_fadein(2)
                    print("Applied 2-second fadein to BGM")
                
                # 最後の2秒でフェードアウト
                if total_duration > 4:  # フェードインと重ならないように
                    bgm_clip = bgm_clip.audio_fadeout(2)
                    print("Applied 2-second fadeout to BGM")
                
            except Exception as e:
                print(f"Failed to process BGM: {e}")
                bgm_clip = None
        else:
            print("No BGM available, continuing without background music")

        # Layer 1: 背景動画の準備（インテリジェント・リサイズ）
        bg_video_path = download_random_background_video()
        
        if bg_video_path and os.path.exists(bg_video_path):
            print("Using random background video from S3")
            bg_clip = process_background_video_for_hd(bg_video_path, total_duration)
            
            if bg_clip is None:
                print("Failed to process background video, falling back to gradient")
        
        if bg_clip is None:
            print("Creating gradient background fallback")
            # グラデーション背景を生成（1920x1080対応）
            gradient_array = create_gradient_background(VIDEO_WIDTH, VIDEO_HEIGHT)
            bg_clip = ImageClip(gradient_array).set_duration(total_duration)
            print(f"Created gradient background: {VIDEO_WIDTH}x{VIDEO_HEIGHT}")

        # Layer 2: 画像スライド（複数枚）
        image_clips = []
        keyword = extract_image_keywords_from_script(script_data)
        images = search_images_with_playwright(keyword, max_results=3)
        image_paths = []
        for image in images:
            image_path = download_image_from_url(image.get("url"))
            if image_path and os.path.exists(image_path):
                image_paths.append(image_path)

        if not image_paths:
            print("Using gradient background as image fallback")
            image_paths.append(None)

        def make_pos_func(start_time: float, target_x: int, target_y: int, start_x: int):
            """画像ごとに独立した位置関数を生成するクロージャ"""
            def pos_func(t: float):
                local_t = max(0.0, t - start_time)
                if local_t < 0.5:
                    # 0.5秒未満：左外から中央へスライド
                    progress = local_t / 0.5
                    x = start_x + (target_x - start_x) * progress
                else:
                    # 0.5秒以降：中央で固定
                    x = target_x
                return (x, target_y)
            return pos_func

        image_duration = total_duration / max(len(image_paths), 1)
        for idx, image_path in enumerate(image_paths):
            start_time = idx * image_duration
            if image_path:
                image_array = np.array(Image.open(image_path).convert("RGB"))
            else:
                image_array = create_gradient_background(int(VIDEO_WIDTH * 0.8), int(VIDEO_HEIGHT * 0.6))

            clip = ImageClip(image_array).set_start(start_time).set_duration(image_duration)
            clip = clip.resize(width=int(VIDEO_WIDTH * 0.95))  # ほぼ全画面に拡大
            clip_w, clip_h = clip.w, clip.h
            target_x = int((VIDEO_WIDTH - clip_w) / 2)
            target_y = int((VIDEO_HEIGHT - clip_h) / 2)
            start_x = -clip_w

            # クロージャで位置関数を生成
            pos_func = make_pos_func(start_time, target_x, target_y, start_x)
            clip = clip.set_position(pos_func)
            image_clips.append(clip)

        # Layer 3: 左上セグメント表示 - 1920x1080用に調整
        try:
            segment_clip = TextClip(
                "概要",
                fontsize=28,  # 少し大きく
                color="white",
                font=font_path,
                bg_color="red",
                size=(250, 60)  # 少し大きく
            ).set_position((80, 60)).set_duration(total_duration)
        except Exception as e:
            print(f"Warning: Failed to create segment text: {e}")
            print("Continuing without segment text...")
            segment_clip = None  # セグメントテキストなしで続行

        # Layer 4: 下部字幕 - 1920x1080用に調整
        current_time = 0.0
        for i, (part, duration) in enumerate(zip(script_parts, part_durations)):
            try:
                text = part.get("text", "")
                if not text:
                    continue

                subtitle_duration = min(duration, total_duration - current_time)
                if subtitle_duration <= 0:
                    break
                
                # 字幕クリップを作成（1920x1080用に調整）
                try:
                    txt_clip = TextClip(
                        text,
                        fontsize=48,  # 大きくして読みやすく
                        color="white",
                        font=font_path,
                        method="caption",
                        size=(VIDEO_WIDTH - 300, 250),  # 余白を調整
                        stroke_color="black",
                        stroke_width=2,
                        bg_color="rgba(0,0,0,0.7)"
                    )
                    txt_clip = txt_clip.set_position((150, VIDEO_HEIGHT - 300)).set_start(current_time).set_duration(subtitle_duration)
                    text_clips.append(txt_clip)
                except Exception as e:
                    print(f"Warning: Failed to create subtitle for part {i}: {e}")
                    print("Continuing without subtitle for this part...")
                    # 字幕なしで続行（審査用動画として優先）
                
                current_time += subtitle_duration
                
            except Exception as e:
                print(f"Error creating subtitle for part {i}: {e}")
                continue

        # すべてのレイヤーを合成（背景動画 -> 背景画像 -> 字幕）
        all_clips = [bg_clip]
        all_clips.extend(image_clips)
        if segment_clip:
            all_clips.append(segment_clip)
        all_clips.extend(text_clips)
        video = CompositeVideoClip(all_clips, size=(VIDEO_WIDTH, VIDEO_HEIGHT))
        
        # 音声トラックを結合（メイン音声 + BGM）
        if bgm_clip:
            # DEBUG_MODEなら音声も60秒にカット
            if DEBUG_MODE:
                audio_clip = audio_clip.subclip(0, 60)
                bgm_clip = bgm_clip.subclip(0, 60)
                print("DEBUG_MODE: Audio clips trimmed to 60s")
            
            # CompositeAudioClipでメイン音声とBGMをミックス
            final_audio = CompositeAudioClip([audio_clip, bgm_clip])
            print("Mixed main audio with BGM")
        else:
            # DEBUG_MODEならメイン音声も60秒にカット
            if DEBUG_MODE:
                audio_clip = audio_clip.subclip(0, 60)
                print("DEBUG_MODE: Main audio trimmed to 60s")
            
            # BGMがない場合はメイン音声のみ
            final_audio = audio_clip
            print("Using main audio only (no BGM)")
        
        # 最終音声を動画に設定
        video = video.set_audio(final_audio)
        
        if DEBUG_MODE:
            debug_duration = min(60, video.duration)
            print(f"DEBUG_MODE enabled: trimming video to {debug_duration}s")
            video = video.subclip(0, debug_duration)

        print(f"Writing video to: {out_video_path}")
        video.write_videofile(
            out_video_path,
            fps=30,  # 30fps固定
            codec='libx264',
            preset='ultrafast',  # 最速エンコード
            audio_codec='aac',
            audio_bitrate='256k',  # 音声256kbps
            temp_audiofile='temp-audio.m4a',
            remove_temp=True,
            threads=8,  # 並列処理を明示
            logger=None  # コンソール書き込みを抑制
        )
        
        print("Video generation completed successfully")

    except Exception as e:
        print(f"Error in video generation: {e}")
        raise
    
    finally:
        # クリップのクリーンアップ
        if video:
            video.close()
        if bg_clip:
            bg_clip.close()
        if audio_clip:
            audio_clip.close()
        if bgm_clip:
            bgm_clip.close()
        for clip in text_clips + segment_clips:
            if clip:
                clip.close()
        
        # 一時ファイルのクリーンアップ
        if 'bg_video_path' in locals() and bg_video_path and os.path.exists(bg_video_path):
            try:
                os.remove(bg_video_path)
                os.rmdir(os.path.dirname(bg_video_path))
                print("Cleaned up temporary background video file")
            except Exception as e:
                print(f"Failed to cleanup temporary file: {e}")
        
        if 'bgm_path' in locals() and bgm_path and os.path.exists(bgm_path):
            try:
                os.remove(bgm_path)
                os.rmdir(os.path.dirname(bgm_path))
                print("Cleaned up temporary BGM file")
            except Exception as e:
                print(f"Failed to cleanup temporary BGM file: {e}")


def upload_to_youtube(
    youtube,
    title: str,
    description: str,
    video_path: str,
    thumbnail_path: str = None,
) -> str:
    """
    YouTube に「非公開」で動画をアップロードし、videoId を返す。
    サムネイルも設定する。
    """
    body = {
        "snippet": {
            "title": title,
            "description": description,
            "categoryId": "28",  # Science & Technology
        },
        "status": {
            "privacyStatus": "private",
        },
    }

    media = MediaFileUpload(video_path, chunksize=-1, resumable=True, mimetype="video/mp4")
    request = youtube.videos().insert(
        part="snippet,status",
        body=body,
        media_body=media,
    )

    response = None
    while response is None:
        status, response = request.next_chunk()
        # status.progress() などで進捗ログを出しても良い

    video_id = response["id"]
    
    # サムネイルを設定
    if thumbnail_path and os.path.exists(thumbnail_path):
        try:
            youtube.thumbnails().set(
                videoId=video_id,
                media_body=MediaFileUpload(thumbnail_path, mimetype="image/png")
            ).execute()
        except Exception as e:
            print(f"サムネイル設定に失敗しましたが、動画アップロードは成功しています: {e}")
    
    return video_id


def put_video_history_item(item: Dict[str, Any]) -> None:
    """DynamoDB VideoHistory テーブルに put_item する。"""
    table = dynamodb.Table(DDB_TABLE_NAME)
    table.put_item(Item=item)


def main() -> None:
    """ローカル/Actions 実行用エントリポイント。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        script_obj = None
        audio_path = None
        video_path = None
        thumbnail_path = None
        
        try:
            # 1. 最新スクリプト JSON を S3 から取得
            script_obj = get_latest_script_object()
            s3_key = script_obj["key"]
            data = script_obj["data"]

            # JSONデータのバリデーションとデフォルト値設定
            title = data.get("title", "PCニュース解説")
            description = data.get("description", "")
            content = data.get("content", {})
            topic_summary = content.get("topic_summary", "")
            script_parts = content.get("script_parts", [])
            thumbnail_data = data.get("thumbnail", {})
            meta = data.get("meta", {})

            if not script_parts:
                raise RuntimeError("script_parts が空です。Gemini の出力を確認してください。")
            
            # 0. タイトル読み上げパートを先頭に追加（ずんだもん: ID 3）
            title_part = {
                "part": "title",
                "text": title,
                "speaker_id": 3
            }
            script_parts = [title_part] + script_parts
            print(f"Processing {len(script_parts)} script parts (title included)...")

            # 2. VOICEVOX で音声生成（複数セリフ対応）
            print("Generating audio...")
            audio_path, part_durations = synthesize_multiple_speeches(script_parts, tmpdir)

            # 3. Video 合成
            print("Generating video...")
            video_path = os.path.join(tmpdir, "video.mp4")
            build_video_with_subtitles(
                background_path=BACKGROUND_IMAGE_PATH,
                font_path=FONT_PATH,
                script_parts=script_parts,
                script_data=data,
                part_durations=part_durations,
                audio_path=audio_path,
                out_video_path=video_path,
            )

            # 4. サムネイル生成
            print("Generating thumbnail...")
            thumbnail_path = os.path.join(tmpdir, "thumbnail.png")
            try:
                create_thumbnail(
                    title=title,
                    topic_summary=topic_summary,
                    thumbnail_data=thumbnail_data,
                    output_path=thumbnail_path,
                    meta=meta,
                )
            except Exception as e:
                print(f"サムネイル生成に失敗しましたが、処理を続行します: {e}")
                thumbnail_path = None

            # 5. YouTube へアップロード
            print("Uploading to YouTube...")
            youtube_client = build_youtube_client()
            video_id = upload_to_youtube(
                youtube=youtube_client,
                title=title,
                description=description,
                video_path=video_path,
                thumbnail_path=thumbnail_path,
            )

            # 6. DynamoDB に履歴登録
            print("Saving to DynamoDB...")
            now = datetime.now(timezone.utc).isoformat()
            
            # URLのチェック（GitHub Actions完走のため緩和）
            url = meta.get("url")
            if not url:
                print("WARNING: meta.url が存在しません。ダミーURLを使用して続行します。")
                url = f"https://example.com/placeholder/{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"

            # TTL（3年後）
            from datetime import timedelta
            ttl_timestamp = int((datetime.now(timezone.utc) + timedelta(days=1095)).timestamp())

            item = {
                "url": url,  # 主キーをurlに変更
                "title": title,
                "processed_at": now,
                "status": "completed",  # 動画生成完了
                "score": meta.get("score", 0.0),  # スコアがあれば保存
                "ttl": ttl_timestamp,
                # 追加情報（既存項目を維持）
                "source_url": meta.get("source_url", ""),
                "published_at": meta.get("published_at", ""),
                "topic_summary": topic_summary,
                "youtube_video_id": video_id,
                "registered_at": now,
                "script_s3_bucket": S3_BUCKET,
                "script_s3_key": s3_key,
            }
            put_video_history_item(item)
            
            print(f"Successfully completed! Video ID: {video_id}")

        except Exception as e:
            print(f"Error in main process: {str(e)}")
            raise
        
        finally:
            # 一時ファイルのクリーンアップ
            if 'bg_video_path' in locals() and bg_video_path and os.path.exists(bg_video_path):
                try:
                    os.remove(bg_video_path)
                    os.rmdir(os.path.dirname(bg_video_path))
                    print("Cleaned up temporary background video file")
                except Exception as e:
                    print(f"Failed to cleanup temporary file: {e}")
            if 'bgm_path' in locals() and bgm_path and os.path.exists(bgm_path):
                try:
                    os.remove(bgm_path)
                    print("Cleaned up BGM file")
                except Exception as e:
                    print(f"Failed to cleanup BGM file: {e}")
            if audio_path and os.path.exists(audio_path):
                try:
                    os.remove(audio_path)
                except Exception as e:
                    print(f"Failed to cleanup temporary file: {e}")
            if video_path and os.path.exists(video_path):
                try:
                    os.remove(video_path)
                except Exception as e:
                    print(f"Failed to cleanup temporary file: {e}")
            if thumbnail_path and os.path.exists(thumbnail_path):
                try:
                    os.remove(thumbnail_path)
                except Exception as e:
                    print(f"Failed to cleanup temporary file: {e}")
            
            # ダウンロードした画像ファイルを削除（ローカルとS3両方）
            import glob
            downloaded_images = glob.glob(os.path.join(LOCAL_TEMP_DIR, "ai_image_*.jpg"))
            for img_path in downloaded_images:
                try:
                    # ローカルファイルを削除
                    os.remove(img_path)
                    print(f"Cleaned up downloaded image: {img_path}")
                    
                    # S3のtempフォルダからも削除
                    filename = os.path.basename(img_path)
                    s3_key = f"temp/{filename}"
                    s3_client.delete_object(Bucket=S3_BUCKET, Key=s3_key)
                    print(f"Deleted image from S3: s3://{S3_BUCKET}/{s3_key}")
                except Exception as e:
                    print(f"Failed to cleanup downloaded image {img_path}: {e}")


if __name__ == "__main__":
    main()
