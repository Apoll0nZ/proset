import json
import os
import tempfile
from datetime import datetime, timezone
from typing import Any, Dict

import boto3
from botocore.client import Config
from google.cloud import texttospeech
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from moviepy.editor import AudioFileClip, CompositeVideoClip, TextClip, ImageClip

"""
動画レンダリング & YouTube アップロードスクリプト。

役割:
- S3 から最新の台本 JSON を取得
- Google Text-to-Speech で日本語音声生成
- MoviePy + FFmpeg で背景画像 + 字幕 + 音声を合成
- mp4 動画を書き出し
- YouTube Data API v3 で「非公開」アップロード
- アップロード成功後、DynamoDB(VideoHistory) に put_item 登録
- 一時ファイル削除
"""


AWS_REGION = os.environ.get("AWS_REGION", "ap-northeast-1")
SCRIPT_S3_BUCKET = os.environ.get("SCRIPT_S3_BUCKET", "")
SCRIPT_S3_PREFIX = os.environ.get("SCRIPT_S3_PREFIX", "scripts/")
DDB_TABLE_NAME = os.environ.get("DDB_TABLE_NAME", "VideoHistory")

YOUTUBE_AUTH_JSON = os.environ.get("YOUTUBE_AUTH_JSON", "")
GOOGLE_TTS_PROJECT_ID = os.environ.get("GOOGLE_TTS_PROJECT_ID", "")  # ここでは未使用だが将来用

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


def synthesize_speech(text: str, out_path: str) -> None:
    """Google Cloud Text-to-Speech を用いて日本語音声を生成し、音声ファイルとして保存。"""
    client = texttospeech.TextToSpeechClient()

    synthesis_input = texttospeech.SynthesisInput(text=text)
    voice = texttospeech.VoiceSelectionParams(
        language_code="ja-JP",
        ssml_gender=texttospeech.SsmlVoiceGender.NEUTRAL,
    )
    audio_config = texttospeech.AudioConfig(
        audio_encoding=texttospeech.AudioEncoding.MP3
    )

    response = client.synthesize_speech(
        input=synthesis_input, voice=voice, audio_config=audio_config
    )

    with open(out_path, "wb") as out_f:
        out_f.write(response.audio_content)


def build_video_with_subtitles(
    background_path: str,
    font_path: str,
    script_text: str,
    audio_path: str,
    out_video_path: str,
) -> None:
    """
    MoviePy を用いて背景 + 字幕 + 音声を合成し mp4 を生成。
    ここでは簡易的に、全文を1枚字幕として中央に表示する。
    """
    audio_clip = AudioFileClip(audio_path)

    bg_clip = ImageClip(background_path).set_duration(audio_clip.duration)
    bg_clip = bg_clip.resize(newsize=(VIDEO_WIDTH, VIDEO_HEIGHT))

    txt_clip = TextClip(
        script_text,
        fontsize=48,
        color="white",
        font=font_path,
        method="caption",
        size=(VIDEO_WIDTH - 200, VIDEO_HEIGHT - 200),
    ).set_position("center").set_duration(audio_clip.duration)

    video = CompositeVideoClip([bg_clip, txt_clip])
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
) -> str:
    """YouTube に「非公開」で動画をアップロードし、videoId を返す。"""
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
        script_text = content.get("script_text", "")
        meta = data.get("meta", {})

        if not script_text:
            raise RuntimeError("script_text が空です。Gemini の出力を確認してください。")

        # 2. TTS 音声生成
        audio_path = os.path.join(tmpdir, "audio.mp3")
        synthesize_speech(script_text, audio_path)

        # 3. Video 合成
        video_path = os.path.join(tmpdir, "video.mp4")
        build_video_with_subtitles(
            background_path=BACKGROUND_IMAGE_PATH,
            font_path=FONT_PATH,
            script_text=script_text,
            audio_path=audio_path,
            out_video_path=video_path,
        )

        # 4. YouTube へアップロード
        youtube_client = build_youtube_client()
        video_id = upload_to_youtube(
            youtube=youtube_client,
            title=title,
            description=description,
            video_path=video_path,
        )

        # 5. DynamoDB に履歴登録
        now = datetime.now(timezone.utc).isoformat()
        content_hash = meta.get("content_hash") or ""
        if not content_hash:
            # 念の為 script_text から再計算してもよいが、ここでは例外にする
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

        # 6. 一時ファイルは TemporaryDirectory コンテキストを抜けると自動削除


if __name__ == "__main__":
    main()

