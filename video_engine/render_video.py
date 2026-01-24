import json
import os
import tempfile
from datetime import datetime, timezone
from typing import Any, Dict, List
import random

import boto3
from botocore.client import Config
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from moviepy.editor import AudioFileClip, CompositeVideoClip, TextClip, ImageClip, concatenate_audioclips
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
SCRIPT_S3_BUCKET = os.environ.get("SCRIPT_S3_BUCKET", "")
SCRIPT_S3_PREFIX = os.environ.get("SCRIPT_S3_PREFIX", "scripts/")
DDB_TABLE_NAME = os.environ.get("MY_DDB_TABLE_NAME", "VideoHistory")

YOUTUBE_AUTH_JSON = os.environ.get("YOUTUBE_AUTH_JSON", "")
VOICEVOX_API_URL = os.environ.get("VOICEVOX_API_URL", "http://localhost:50021")

BACKGROUND_IMAGE_PATH = os.environ.get(
    "BACKGROUND_IMAGE_PATH",
    os.path.join(os.path.dirname(__file__), "assets", "background.png"),
)
FONT_PATH = os.environ.get(
    "FONT_PATH",
    os.path.join(os.path.dirname(__file__), "assets", "NotoSansJP-Regular.otf"),
)

VIDEO_WIDTH = int(os.environ.get("VIDEO_WIDTH", "1920"))
VIDEO_HEIGHT = int(os.environ.get("VIDEO_HEIGHT", "1080"))
FPS = int(os.environ.get("FPS", "30"))


s3_client = boto3.client("s3", region_name=AWS_REGION, config=Config(signature_version="s3v4"))
dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)


def get_latest_script_object() -> Dict[str, Any]:
    """S3 から最新(LastModified が最大)のスクリプト JSON オブジェクトを取得。"""
    if not SCRIPT_S3_BUCKET:
        raise RuntimeError("SCRIPT_S3_BUCKET が設定されていません。")

    resp = s3_client.list_objects_v2(
        Bucket=SCRIPT_S3_BUCKET,
        Prefix=SCRIPT_S3_PREFIX.rstrip("/") + "/",
    )
    contents = resp.get("Contents", [])
    if not contents:
        raise RuntimeError("スクリプト JSON が S3 に存在しません。")

    latest = max(contents, key=lambda x: x["LastModified"])
    key = latest["Key"]

    obj = s3_client.get_object(Bucket=SCRIPT_S3_BUCKET, Key=key)
    body = obj["Body"].read().decode("utf-8")
    data = json.loads(body)
    return {"key": key, "data": data}


def synthesize_speech_voicevox(text: str, speaker_id: int, out_path: str) -> None:
    """
    VOICEVOX API を用いて日本語音声を生成し、音声ファイルとして保存。
    
    Args:
        text: 音声化するテキスト
        speaker_id: VOICEVOX のスピーカーID（例: 3=ずんだもん）
        out_path: 出力音声ファイルパス（.wav形式）
    """
    # 音声クエリを生成
    query_url = f"{VOICEVOX_API_URL}/audio_query"
    query_params = {
        "text": text,
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
    
    # WAVファイルとして保存
    with open(out_path, "wb") as out_f:
        out_f.write(synthesis_resp.content)


def synthesize_multiple_speeches(script_parts: List[Dict[str, Any]], tmpdir: str) -> str:
    """
    複数のセリフを順番に音声合成し、結合した音声ファイルを生成。
    
    Returns:
        結合された音声ファイルのパス
    """
    audio_clips = []
    
    for i, part in enumerate(script_parts):
        speaker_id = part.get("speaker_id", 3)  # デフォルトはずんだもん
        text = part.get("text", "")
        if not text:
            continue
        
        audio_path = os.path.join(tmpdir, f"audio_{i}.wav")
        synthesize_speech_voicevox(text, speaker_id, audio_path)
        
        clip = AudioFileClip(audio_path)
        audio_clips.append(clip)
    
    if not audio_clips:
        raise RuntimeError("音声クリップが生成されませんでした。")
    
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
    MoviePy を用いて背景 + 字幕 + 音声を合成し mp4 を生成。
    各セリフごとに字幕を表示する。
    """
    audio_clip = AudioFileClip(audio_path)
    total_duration = audio_clip.duration

    bg_clip = ImageClip(background_path).set_duration(total_duration)
    bg_clip = bg_clip.resize(newsize=(VIDEO_WIDTH, VIDEO_HEIGHT))

    # 各セリフの開始時間を計算
    current_time = 0
    text_clips = []
    
    for part in script_parts:
        text = part.get("text", "")
        if not text:
            continue
        
        # このセリフの音声長を推定（簡易的に文字数から計算）
        estimated_duration = min(len(text) * 0.1, 5.0)  # 最大5秒
        
        if current_time + estimated_duration > total_duration:
            estimated_duration = total_duration - current_time
        
        if estimated_duration <= 0:
            break
        
        txt_clip = TextClip(
            text,
            fontsize=48,
            color="white",
            font=font_path,
            method="caption",
            size=(VIDEO_WIDTH - 200, VIDEO_HEIGHT - 200),
        ).set_position("center").set_start(current_time).set_duration(estimated_duration)
        
        text_clips.append(txt_clip)
        current_time += estimated_duration
    
    # 背景と字幕を合成
    video = CompositeVideoClip([bg_clip] + text_clips)
    video = video.set_audio(audio_clip)

    # ffmpeg が PATH にある前提
    video.write_videofile(
        out_video_path,
        fps=FPS,
        codec="libx264",
        audio_codec="aac",
        temp_audiofile=os.path.join(tempfile.gettempdir(), "temp-audio.m4a"),
        remove_temp=True,
    )
    
    # クリップを解放
    audio_clip.close()
    bg_clip.close()
    for clip in text_clips:
        clip.close()
    video.close()


def build_youtube_client():
    """既に取得済みの OAuth2 資格情報(JSON文字列)から YouTube API クライアントを構築。"""
    if not YOUTUBE_AUTH_JSON:
        raise RuntimeError("YOUTUBE_AUTH_JSON が設定されていません。")

    info = json.loads(YOUTUBE_AUTH_JSON)
    creds = Credentials.from_authorized_user_info(info)
    youtube = build("youtube", "v3", credentials=creds)
    return youtube


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
        # 1. 最新スクリプト JSON を S3 から取得
        script_obj = get_latest_script_object()
        s3_key = script_obj["key"]
        data = script_obj["data"]

        title = data.get("title", "PCニュース解説")
        description = data.get("description", "")
        content = data.get("content", {})
        topic_summary = content.get("topic_summary", "")
        script_parts = content.get("script_parts", [])
        thumbnail_data = data.get("thumbnail", {})
        meta = data.get("meta", {})

        if not script_parts:
            raise RuntimeError("script_parts が空です。Gemini の出力を確認してください。")

        # 2. VOICEVOX で音声生成（複数セリフ対応）
        audio_path = synthesize_multiple_speeches(script_parts, tmpdir)

        # 3. Video 合成
        video_path = os.path.join(tmpdir, "video.mp4")
        build_video_with_subtitles(
            background_path=BACKGROUND_IMAGE_PATH,
            font_path=FONT_PATH,
            script_parts=script_parts,
            audio_path=audio_path,
            out_video_path=video_path,
        )

        # 4. サムネイル生成
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
        youtube_client = build_youtube_client()
        video_id = upload_to_youtube(
            youtube=youtube_client,
            title=title,
            description=description,
            video_path=video_path,
            thumbnail_path=thumbnail_path,
        )

        # 6. DynamoDB に履歴登録
        now = datetime.now(timezone.utc).isoformat()
        content_hash = meta.get("content_hash") or ""
        if not content_hash:
            raise RuntimeError("meta.content_hash が存在しません。Lambda 側の保存処理を確認してください。")

        item = {
            "content_hash": content_hash,
            "title": title,
            "source_url": meta.get("source_url", ""),
            "published_at": meta.get("published_at", ""),
            "topic_summary": topic_summary,
            "youtube_video_id": video_id,
            "registered_at": now,
            "script_s3_bucket": SCRIPT_S3_BUCKET,
            "script_s3_key": s3_key,
        }
        put_video_history_item(item)

        # 7. 一時ファイルは TemporaryDirectory コンテキストを抜けると自動削除


if __name__ == "__main__":
    main()
