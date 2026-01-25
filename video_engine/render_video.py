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
S3_BUCKET = os.environ.get("S3_BUCKET", "")
SCRIPTS_PREFIX = os.environ.get("SCRIPTS_PATH", "scripts/")
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
    """
    長いテキストを句読点で適切に分割してVOICEVOXのAPI制限を回避
    
    Args:
        text: 分割するテキスト
        
    Returns:
        分割されたテキストのリスト
    """
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
    MoviePy を用いて背景 + 字幕 + 音声を合成し mp4 を生成。
    各セリフごとに字幕を表示する。長尺動画対応のためリソース管理を強化。
    """
    audio_clip = None
    bg_clip = None
    text_clips = []
    video = None
    
    try:
        audio_clip = AudioFileClip(audio_path)
        total_duration = audio_clip.duration

        bg_clip = ImageClip(background_path).set_duration(total_duration)
        bg_clip = bg_clip.resize(newsize=(VIDEO_WIDTH, VIDEO_HEIGHT))

        # 各セリフの開始時間を計算
        current_time = 0
        
        for i, part in enumerate(script_parts):
            try:
                text = part.get("text", "")
                if not text:
                    continue
                
                # このセリフの音声長を推定（簡易的に文字数から計算）
                estimated_duration = min(len(text) * 0.08, 8.0)  # 最大8秒に調整
                
                if current_time + estimated_duration > total_duration:
                    estimated_duration = total_duration - current_time
                
                if estimated_duration <= 0:
                    break
                
                # 字幕クリップを作成（フォントサイズを調整）
                txt_clip = TextClip(
                    text,
                    fontsize=42,  # 少し小さくして長文対応
                    color="white",
                    font=font_path,
                    method="caption",
                    size=(VIDEO_WIDTH - 200, VIDEO_HEIGHT - 200),
                    stroke_color="black",  # 輪郭を追加して見やすく
                    stroke_width=2,
                ).set_position("center").set_start(current_time).set_duration(estimated_duration)
                
                text_clips.append(txt_clip)
                current_time += estimated_duration
                
            except Exception as e:
                print(f"Error creating subtitle for part {i}: {str(e)}")
                continue
        
        if not text_clips:
            print("Warning: No text clips created, creating video without subtitles")
            video = bg_clip.set_audio(audio_clip)
        else:
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
            threads=4,  # スレッド数を制限してメモリ使用量を抑制
        )
        
    finally:
        # クリップを解放（メモリリーク防止）
        if audio_clip:
            audio_clip.close()
        if bg_clip:
            bg_clip.close()
        for clip in text_clips:
            try:
                clip.close()
            except:
                pass
        if video:
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
                "script_s3_bucket": S3_BUCKET,
                "script_s3_key": s3_key,
            }
            put_video_history_item(item)
            
            print(f"Successfully completed! Video ID: {video_id}")

        except Exception as e:
            print(f"Error in main process: {str(e)}")
            raise
        
        finally:
            # 7. 一時ファイルのクリーンアップ（明示的な削除）
            try:
                if audio_path and os.path.exists(audio_path):
                    os.remove(audio_path)
                if video_path and os.path.exists(video_path):
                    os.remove(video_path)
                if thumbnail_path and os.path.exists(thumbnail_path):
                    os.remove(thumbnail_path)
            except Exception as e:
                print(f"Error cleaning up temporary files: {str(e)}")


if __name__ == "__main__":
    main()
