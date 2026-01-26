import json
import os
import tempfile
import math
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List
import random

import boto3
import numpy as np
from PIL import Image
# Pillow互換性パッチ：ANTIALIASをLANCZOSにリンク
if not hasattr(Image, 'ANTIALIAS'):
    Image.ANTIALIAS = Image.LANCZOS
from botocore.client import Config
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from moviepy.editor import AudioFileClip, CompositeVideoClip, TextClip, ImageClip, VideoFileClip, vfx
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
FONT_PATH = os.environ.get(
    "FONT_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "NotoSansJP-Regular.otf"),
)

# 画像取得用環境変数
IMAGES_S3_BUCKET = os.environ.get("IMAGES_S3_BUCKET", S3_BUCKET)  # デフォルトはメインS3バケット
IMAGES_S3_PREFIX = os.environ.get("IMAGES_S3_PREFIX", "assets/images/")  # 画像格納先プレフィックス
LOCAL_TEMP_DIR = os.environ.get("LOCAL_TEMP_DIR", tempfile.gettempdir())  # 一時フォルダ

# Google画像検索用環境変数
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "")
GOOGLE_CSE_ID = os.environ.get("GOOGLE_CSE_ID", "")

VIDEO_WIDTH = int(os.environ.get("VIDEO_WIDTH", "1920"))
VIDEO_HEIGHT = int(os.environ.get("VIDEO_HEIGHT", "1080"))
FPS = int(os.environ.get("FPS", "30"))


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
        
        # 1920x1080に引き伸ばして画面いっぱいに
        bg_clip = bg_clip.resize(newsize=(1920, 1080))
        print("Resized to 1920x1080 (intelligent stretch)")
        
        # ガウスぼかしを適用して引き伸ばしの粗さを隠す
        bg_clip = bg_clip.fx(vfx.gaussian_blur, sigma=5)
        print("Applied gaussian blur (sigma=5) to hide stretching artifacts")
        
        # 音声の長さに合わせてループ
        bg_clip = bg_clip.loop(duration=total_duration).set_duration(total_duration)
        print(f"Looped to match audio duration: {total_duration:.2f}s")
        
        return bg_clip
        
    except Exception as e:
        print(f"Failed to process background video: {e}")
        return None


def download_background_music() -> str:
    """S3からBGM（assets/bgm.mp3）をダウンロード"""
    bgm_key = "assets/bgm.mp3"
    temp_dir = tempfile.mkdtemp()
    local_path = os.path.join(temp_dir, "bgm.mp3")
    
    try:
        print(f"Downloading background music from S3: s3://{S3_BUCKET}/{bgm_key}")
        s3_client.download_file(S3_BUCKET, bgm_key, local_path)
        print(f"Successfully downloaded BGM to: {local_path}")
        return local_path
    except Exception as e:
        print(f"Failed to download background music: {e}")
        if os.path.exists(local_path):
            os.remove(local_path)
        return None


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


def search_google_images(keyword: str, max_results: int = 5) -> List[Dict[str, str]]:
    """Google画像検索APIで画像を検索"""
    if not GOOGLE_API_KEY or not GOOGLE_CSE_ID:
        print("Google API credentials not configured")
        return []
    
    try:
        import urllib.parse
        
        # 検索クエリをURLエンコード
        encoded_keyword = urllib.parse.quote(keyword)
        
        # Google Custom Search APIのURL
        url = f"https://www.googleapis.com/customsearch/v1"
        
        params = {
            'key': GOOGLE_API_KEY,
            'cx': GOOGLE_CSE_ID,
            'q': keyword,
            'searchType': 'image',
            'num': max_results,
            'imgSize': 'large',  # 大きな画像のみ
            'imgType': 'photo',  # 写真のみ
            'safe': 'active',    # セーフサーチ
        }
        
        print(f"Searching Google Images for: {keyword}")
        response = requests.get(url, params=params, timeout=30)
        response.raise_for_status()
        
        data = response.json()
        
        # 検索結果を処理
        images = []
        if 'items' in data:
            for item in data['items']:
                if 'link' in item:
                    images.append({
                        'url': item['link'],
                        'title': item.get('title', ''),
                        'thumbnail': item.get('image', {}).get('thumbnailLink', '')
                    })
        
        print(f"Found {len(images)} images for keyword: {keyword}")
        return images
        
    except Exception as e:
        print(f"Google Images search failed: {e}")
        return []


def download_image_from_url(image_url: str, filename: str = None) -> str:
    """URLから画像をダウンロードしてtempフォルダに保存"""
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
        
        # 画像を保存
        with open(local_path, 'wb') as f:
            f.write(response.content)
        
        print(f"Successfully downloaded image to: {local_path}")
        return local_path
        
    except Exception as e:
        print(f"Failed to download image from URL {image_url}: {e}")
        return None


def get_ai_selected_image(script_data: Dict[str, Any]) -> str:
    """AIによる動的選別・自動取得で最適な画像を取得"""
    try:
        # 1. キーワード抽出
        keyword = extract_image_keywords_from_script(script_data)
        
        # 2. Google画像検索
        images = search_google_images(keyword)
        
        if images:
            # 最初の画像（最も関連性が高い）をダウンロード
            best_image = images[0]
            image_path = download_image_from_url(best_image['url'])
            
            if image_path:
                print(f"Successfully selected and downloaded AI image: {best_image['title']}")
                return image_path
        
        # 3. フォールバック：S3のデフォルト画像
        print("AI image selection failed, using fallback image from S3")
        fallback_path = download_image_from_s3("assets/images/default.jpg")
        
        if fallback_path:
            return fallback_path
        else:
            print("All image acquisition methods failed, using gradient background")
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
    
    Args:
        text: 音声化するテキスト
        speaker_id: VOICEVOX のスピーカーID（例: 3=ずんだもん）
        out_path: 出力音声ファイルパス（.wav形式）
    """
    # テキストを分割
    text_parts = split_text_for_voicevox(text)
    
    if not text_parts:
        raise RuntimeError(f"音声化するテキストが空です: {text[:50]}")
    
    audio_clips = []
    temp_dir = tempfile.mkdtemp()
    
    try:
        # 各パートを音声合成
        for i, part_text in enumerate(text_parts):
            if not part_text.strip():
                continue
                
            # 音声クエリを生成
            query_url = f"{VOICEVOX_API_URL}/audio_query"
            query_params = {
                "text": part_text,
                "speaker": speaker_id
            }
            query_resp = requests.post(query_url, params=query_params, timeout=30)
            if query_resp.status_code != 200:
                raise RuntimeError(f"VOICEVOX クエリ生成失敗: {query_resp.status_code} {query_resp.text}")
            
            query_data = query_resp.json()
            
            # 音声合成
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
            
            # 一時音声ファイルとして保存
            temp_audio_path = os.path.join(temp_dir, f"temp_audio_{i}.wav")
            with open(temp_audio_path, "wb") as out_f:
                out_f.write(synthesis_resp.content)
            
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


def synthesize_multiple_speeches(script_parts: List[Dict[str, Any]], tmpdir: str) -> str:
    """
    複数のセリフを順番に音声合成し、結合した音声ファイルを生成。
    新しいJSONフォーマットに対応し、part名に応じてspeaker_idを決定。
    
    Returns:
        結合された音声ファイルのパス
    """
    audio_clips = []
    
    for i, part in enumerate(script_parts):
        try:
            part_name = part.get("part", "")
            text = part.get("text", "")
            
            if not text:
                print(f"Warning: Empty text for part {i}, skipping...")
                continue
            
            # part名に応じてspeaker_idを決定
            if part_name.startswith("article_"):
                # 解説パートはすべてspeaker_id: 3（ずんだもん）
                speaker_id = 3
            elif part_name == "reaction":
                # 反応パートはJSON内のspeaker_idを使用
                speaker_id = part.get("speaker_id", 3)
            else:
                # その他の場合はデフォルトでずんだもん
                speaker_id = part.get("speaker_id", 3)
            
            audio_path = os.path.join(tmpdir, f"audio_{i}.wav")
            synthesize_speech_voicevox(text, speaker_id, audio_path)
            
            clip = AudioFileClip(audio_path)
            audio_clips.append(clip)
            
        except Exception as e:
            print(f"Error processing part {i}: {str(e)}")
            print(f"Part data: {part}")
            # エラーがあっても処理を継続
            continue
    
    if not audio_clips:
        raise RuntimeError("音声クリップが生成されませんでした。script_partsの内容を確認してください。")
    
    # すべての音声クリップを結合
    final_audio = concatenate_audioclips(audio_clips)
    final_audio_path = os.path.join(tmpdir, "final_audio.wav")
    final_audio.write_audiofile(final_audio_path, codec="pcm_s16le", fps=44100)
    
    # クリップを解放
    for clip in audio_clips:
        clip.close()
    final_audio.close()
    
    return final_audio_path


def build_video_with_subtitles(
    background_path: str,
    font_path: str,
    script_parts: List[Dict[str, Any]],
    audio_path: str,
    out_video_path: str,
) -> None:
    """
    新しい映像生成ワークフロー：
    Layer 1: ぼかし済み背景動画（ループ）
    Layer 2: 中央画像（呼吸アニメーション）
    Layer 3: 左上セグメント表示
    Layer 4: 下部字幕
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
                    bgm_clip = bgm_clip.loop(duration=total_duration)
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

        # Layer 2: 中央画像（AI選択画像 + 呼吸アニメーション）- 1920x1080用に調整
        # AIによる動的選別・自動取得で最適な画像を取得
        center_image_path = get_ai_selected_image(data)
        
        if center_image_path and os.path.exists(center_image_path):
            print(f"Using AI-selected image: {center_image_path}")
            try:
                # 画像を読み込んで中央画像として使用
                center_image_array = np.array(Image.open(center_image_path).convert("RGB"))
                center_clip = ImageClip(center_image_array).set_duration(total_duration)
                center_clip = center_clip.resize(newsize=(1000, 600)).set_position('center')
                print("Successfully loaded AI-selected image")
            except Exception as e:
                print(f"Failed to load AI-selected image: {e}")
                # フォールバック：グラデーション画像
                center_image_array = create_gradient_background(1000, 600)
                center_clip = ImageClip(center_image_array).set_duration(total_duration)
                center_clip = center_clip.resize(newsize=(1000, 600)).set_position('center')
        else:
            print("Using gradient background as center image")
            # フォールバック：グラデーション画像
            center_image_array = create_gradient_background(1000, 600)
            center_clip = ImageClip(center_image_array).set_duration(total_duration)
            center_clip = center_clip.resize(newsize=(1000, 600)).set_position('center')
        
        # 呼吸アニメーションを適用（シンプルな実装）
        def breathing_effect(t):
            # 4秒周期で97%〜100%のスケール変化
            scale = 0.97 + 0.03 * (0.5 + 0.5 * math.sin(2 * math.pi * t / 4.0))
            return scale
        
        center_clip = center_clip.resize(lambda t: breathing_effect(t))

        # Layer 3: 左上セグメント表示 - 1920x1080用に調整
        segment_clip = TextClip(
            "概要",
            fontsize=28,  # 少し大きく
            color="white",
            font=font_path,
            bg_color="red",
            size=(250, 60)  # 少し大きく
        ).set_position((80, 60)).set_duration(total_duration)

        # Layer 4: 下部字幕 - 1920x1080用に調整
        current_time = 0
        for i, part in enumerate(script_parts):
            try:
                text = part.get("text", "")
                if not text:
                    continue
                
                # このセリフの音声長を推定
                estimated_duration = min(len(text) * 0.08, 8.0)
                
                if current_time + estimated_duration > total_duration:
                    estimated_duration = total_duration - current_time
                
                if estimated_duration <= 0:
                    break
                
                # 字幕クリップを作成（1920x1080用に調整）
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
                txt_clip = txt_clip.set_position((150, VIDEO_HEIGHT - 300)).set_start(current_time).set_duration(estimated_duration)
                text_clips.append(txt_clip)
                
                current_time += estimated_duration
                
            except Exception as e:
                print(f"Error creating subtitle for part {i}: {e}")
                continue

        # すべてのレイヤーを合成
        all_clips = [bg_clip, center_clip, segment_clip] + text_clips
        video = CompositeVideoClip(all_clips, size=(VIDEO_WIDTH, VIDEO_HEIGHT))
        
        # 音声トラックを結合（メイン音声 + BGM）
        if bgm_clip:
            # CompositeAudioClipでメイン音声とBGMをミックス
            final_audio = CompositeAudioClip([audio_clip, bgm_clip])
            print("Mixed main audio with BGM")
        else:
            # BGMがない場合はメイン音声のみ
            final_audio = audio_clip
            print("Using main audio only (no BGM)")
        
        # 最終音声を動画に設定
        video = video.set_audio(final_audio)
        
        print(f"Writing video to: {out_video_path}")
        video.write_videofile(
            out_video_path,
            fps=FPS,
            codec='libx264',
            audio_codec='aac',
            temp_audiofile='temp-audio.m4a',
            remove_temp=True,
            threads=4
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
            
            print(f"Processing {len(script_parts)} script parts...")

            # 2. VOICEVOX で音声生成（複数セリフ対応）
            print("Generating audio...")
            audio_path = synthesize_multiple_speeches(script_parts, tmpdir)

            # 3. Video 合成
            print("Generating video...")
            video_path = os.path.join(tmpdir, "video.mp4")
            build_video_with_subtitles(
                background_path=BACKGROUND_IMAGE_PATH,
                font_path=FONT_PATH,
                script_parts=script_parts,
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
            
            # urlを主キーとして使用、content_hashは必須ではない
            url = meta.get("url") or ""
            if not url:
                print("WARNING: meta.url が存在しません。content_hashをフォールバックとして使用します。")
                url = meta.get("content_hash") or ""
            
            if not url:
                raise RuntimeError("meta.url と meta.content_hash の両方が存在しません。Lambda 側の保存処理を確認してください。")

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
                    os.rmdir(os.path.dirname(bgm_path))
                    print("Cleaned up temporary BGM file")
                except Exception as e:
                    print(f"Failed to cleanup temporary BGM file: {e}")
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


if __name__ == "__main__":
    main()
