import os
import random
import json
import time
import base64
from typing import List, Dict, Any, Optional, Tuple
from PIL import Image, ImageDraw, ImageFont, ImageFilter  # type: ignore
import requests
from io import BytesIO

"""
テックガジェットスタイル（2chスレタイ風）サムネイル生成スクリプト。

レイアウト:
- 上部70%: 記事関連画像2枚を6:4〜3:7のランダム比率で並列配置
- 下部30%: 鮮やかな黄色の背景（座布団）
- メイン字幕: 2chスレタイ風フレーズ、極太ゴシック、黒・赤・青を使い分け
- サブ/煽り字幕: 白文字・黒縁取り、斜め配置
"""

THUMBNAIL_WIDTH = 1280
THUMBNAIL_HEIGHT = 720
TOP_AREA_HEIGHT = int(THUMBNAIL_HEIGHT * 0.7)  # 上部70%
BOTTOM_AREA_HEIGHT = THUMBNAIL_HEIGHT - TOP_AREA_HEIGHT  # 下部30%

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
        os.environ.get("THUMBNAIL_FONT_MAIN", ""),
    ]
    
    for font_path in possible_fonts:
        if font_path and os.path.exists(font_path):
            print(f"[DEBUG] Found thumbnail font: {font_path}")
            return font_path
    
    # どれも見つからない場合はデフォルト
    print("[DEBUG] No Japanese thumbnail font found, using default")
    return ""

# けいふぉんとを優先
KEIFONT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "keifont.ttf")

def resolve_thumbnail_font(env_key: str) -> str:
    env_font = os.environ.get(env_key, "")
    if env_font and os.path.exists(env_font):
        print(f"[DEBUG] Selected thumbnail font path: {env_font}")
        return env_font
    if os.path.exists(KEIFONT_PATH):
        print(f"[DEBUG] Selected thumbnail font path: {KEIFONT_PATH}")
        return KEIFONT_PATH
    return find_japanese_font()

# フォントパス（クロスプラットフォーム対応）
FONT_PATH_MAIN = resolve_thumbnail_font("THUMBNAIL_FONT_MAIN")
FONT_PATH_SUB = resolve_thumbnail_font("THUMBNAIL_FONT_SUB")

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL_NAME = os.environ.get("GEMINI_MODEL_NAME", "gemini-2.5-flash-lite")
GEMINI_API_VERSION = os.environ.get("GEMINI_API_VERSION", "v1")
THUMBNAIL_GEMINI_TEXT_FILTER = os.environ.get("THUMBNAIL_GEMINI_TEXT_FILTER", "1").lower() not in ("0", "false", "off")
THUMBNAIL_GEMINI_MAX_CANDIDATES = max(2, int(os.environ.get("THUMBNAIL_GEMINI_MAX_CANDIDATES", "8")))
THUMBNAIL_GEMINI_RANDOM_POOL = max(2, int(os.environ.get("THUMBNAIL_GEMINI_RANDOM_POOL", "4")))


def _get_mime_type_from_path(image_path: str) -> Optional[str]:
    ext = os.path.splitext(image_path.lower())[1]
    if ext in [".jpg", ".jpeg"]:
        return "image/jpeg"
    if ext == ".png":
        return "image/png"
    if ext == ".webp":
        return "image/webp"
    if ext == ".gif":
        return "image/gif"
    if ext == ".bmp":
        return "image/bmp"
    return None


def _analyze_image_text_density_with_gemini(image_path: str) -> Optional[Dict[str, Any]]:
    if not THUMBNAIL_GEMINI_TEXT_FILTER:
        return None
    if not GEMINI_API_KEY:
        return None
    if not os.path.exists(image_path):
        return None

    mime_type = _get_mime_type_from_path(image_path)
    if not mime_type:
        return None

    try:
        with open(image_path, "rb") as f:
            image_b64 = base64.b64encode(f.read()).decode("utf-8")
    except Exception as e:
        print(f"[THUMBNAIL] Failed to read image for Gemini text check: {image_path} ({e})")
        return None

    url = (
        f"https://generativelanguage.googleapis.com/{GEMINI_API_VERSION}/models/"
        f"{GEMINI_MODEL_NAME}:generateContent?key={GEMINI_API_KEY}"
    )
    prompt = (
        "この画像がYouTubeサムネ背景に向くか判定してください。"
        "文字・ロゴ・UI・スクリーンショット・看板など、読める文字情報が目立つ画像は不適です。"
        "JSONのみで返答: "
        "{\"text_ratio\": 0-100の整数, \"text_heavy\": true/false, \"keep\": true/false}"
    )
    payload = {
        "contents": [
            {
                "parts": [
                    {"text": prompt},
                    {"inline_data": {"mime_type": mime_type, "data": image_b64}},
                ]
            }
        ],
        "generationConfig": {
            "temperature": 0.0,
            "max_output_tokens": 120,
            "response_mime_type": "application/json",
        },
    }

    max_retries = 2
    for attempt in range(max_retries):
        try:
            response = requests.post(url, json=payload, timeout=(5, 20))
            if response.status_code in (429, 503):
                if attempt < max_retries - 1:
                    time.sleep(1.2)
                    continue
                return None
            if response.status_code != 200:
                print(f"[THUMBNAIL] Gemini text check failed: {response.status_code}")
                return None

            data = response.json()
            candidates = data.get("candidates", [])
            if not candidates:
                return None
            parts = candidates[0].get("content", {}).get("parts", [])
            raw_text = "\n".join(p.get("text", "") for p in parts if p.get("text")).strip()
            if not raw_text:
                return None

            try:
                parsed = json.loads(raw_text)
            except Exception:
                start = raw_text.find("{")
                end = raw_text.rfind("}")
                if start == -1 or end == -1 or end <= start:
                    return None
                parsed = json.loads(raw_text[start : end + 1])

            text_ratio = int(parsed.get("text_ratio", 50))
            text_ratio = max(0, min(100, text_ratio))
            text_heavy = bool(parsed.get("text_heavy", text_ratio >= 35))
            keep = bool(parsed.get("keep", not text_heavy))
            return {
                "text_ratio": text_ratio,
                "text_heavy": text_heavy,
                "keep": keep,
            }
        except Exception as e:
            print(f"[THUMBNAIL] Gemini text check error: {e}")
            if attempt < max_retries - 1:
                time.sleep(1.0)
            else:
                return None

    return None


def _select_thumbnail_image_paths(candidate_paths: List[str], count: int = 2) -> List[str]:
    unique_paths: List[str] = []
    seen = set()
    for path in candidate_paths:
        if not path or path in seen or not os.path.exists(path):
            continue
        seen.add(path)
        unique_paths.append(path)

    if not unique_paths:
        return []
    if len(unique_paths) <= count:
        return unique_paths

    # 先に軽量なヒューリスティックで候補を絞り、Gemini評価コストを抑える
    ranked_by_basic = sorted(unique_paths, key=lambda p: calculate_image_score(p), reverse=True)
    gemini_targets = ranked_by_basic[: min(len(ranked_by_basic), THUMBNAIL_GEMINI_MAX_CANDIDATES)]

    scored: List[Tuple[str, float, bool, int]] = []
    for path in gemini_targets:
        base_score = float(calculate_image_score(path))
        analysis = _analyze_image_text_density_with_gemini(path)
        if analysis:
            text_ratio = int(analysis.get("text_ratio", 50))
            text_heavy = bool(analysis.get("text_heavy", text_ratio >= 35))
            # 文字が少ないほど加点、文字だらけは大きく減点
            final_score = base_score + (100 - text_ratio) / 20.0 - (6.0 if text_heavy else 0.0)
        else:
            text_ratio = 50
            text_heavy = False
            final_score = base_score
        scored.append((path, final_score, text_heavy, text_ratio))

    scored.sort(key=lambda x: (x[2], -x[1], x[3]))

    # 上位候補からランダムに選び、毎回同じ組み合わせになりにくくする
    pool_size = min(len(scored), max(count, THUMBNAIL_GEMINI_RANDOM_POOL))
    top_pool = [path for path, _, _, _ in scored[:pool_size]]
    selected = random.sample(top_pool, count) if len(top_pool) >= count else top_pool[:]
    if len(selected) < count:
        for path in ranked_by_basic:
            if path not in selected:
                selected.append(path)
                if len(selected) >= count:
                    break

    print(f"[THUMBNAIL] Selected {len(selected)} images from top-{pool_size} pool after Gemini text-density filter")
    return selected[:count]


def download_image(url: str, max_size: tuple = (640, 480)) -> Optional[Image.Image]:
    """URLから画像をダウンロードしてリサイズ。"""
    try:
        print(f"[DEBUG] Downloading image from URL: {url}")
        resp = requests.get(url, timeout=10)
        if resp.status_code != 200:
            print(f"[DEBUG] Failed to download image: HTTP {resp.status_code}")
            return None
        img = Image.open(BytesIO(resp.content))
        print(f"[DEBUG] Image loaded: mode={img.mode}, size={img.size}")
        img = img.convert("RGBA")  # RGBAに変換して透過をサポート
        img.thumbnail(max_size, Image.Resampling.LANCZOS)
        print(f"[DEBUG] Image resized to: {img.size}")
        return img
    except Exception as e:
        print(f"[DEBUG] Error downloading image: {e}")
        return None


def calculate_image_score(image_path: str) -> int:
    """画像のスコアを計算（サムネイル優先度の簡易判定）"""
    score = 0
    basename = os.path.basename(image_path).lower()

    # キーワードスコア
    if any(kw in basename for kw in ['iphone', 'android', 'samsung', 'google', 'apple', 'xiaomi', 'oppo', 'vivo', 'huawei', 'honor']):
        score += 5
    if any(kw in basename for kw in ['product', 'official', 'device', 'pro']):
        score += 3

    # ファイルサイズスコア
    try:
        file_size = os.path.getsize(image_path)
        if file_size > 200_000:
            score += 3
        elif file_size > 100_000:
            score += 2
    except Exception:
        pass

    return score


def select_images_from_video(image_schedule: List[Dict], s3_bucket: str = None) -> List[str]:
    """
    動画で使用した画像からランダムに2枚を選択

    優先順位:
    1. ローカルに存在する画像をランダムに選択
    2. S3から画像をダウンロードしてランダムに選択
    3. 空リストを返す（フォールバックへ）
    """
    if not image_schedule:
        return []

    temp_dir = os.path.join(os.path.dirname(__file__), "temp")
    os.makedirs(temp_dir, exist_ok=True)

    # ステップ1: ローカルファイルをチェック
    local_images = []
    for item in image_schedule:
        path = item.get('path', '')
        if not path:
            continue

        local_path = path if os.path.isabs(path) else os.path.join(temp_dir, os.path.basename(path))
        if os.path.exists(local_path):
            local_images.append(local_path)

    # 文字量の少ない画像を優先して2枚を選択
    if len(local_images) >= 2:
        selected_images = _select_thumbnail_image_paths(local_images, 2)
        print(f"[SUCCESS] Selected {len(selected_images)} local images for thumbnail")
        return selected_images
    elif len(local_images) == 1:
        print(f"[SUCCESS] Using 1 available local image for thumbnail")
        return local_images

    # ステップ2: S3から画像をダウンロード
    if s3_bucket:
        print(f"[INFO] Downloading images from S3 (need {2 - len(local_images)} more)...")
        try:
            import boto3
            s3_client = boto3.client('s3')

            # 利用可能なS3画像を収集
            s3_images = []
            for item in image_schedule[:10]:  # 上位10件を試行
                path = item.get('path', '')
                if not path:
                    continue

                filename = os.path.basename(path)
                s3_key = f"temp/{filename}"
                local_path = os.path.join(temp_dir, f"thumbnail_{filename}")

                # 既にローカルにある場合はスキップ
                if os.path.exists(local_path):
                    s3_images.append(local_path)
                    continue

                try:
                    s3_client.download_file(s3_bucket, s3_key, local_path)
                    if os.path.exists(local_path):
                        s3_images.append(local_path)
                        print(f"[S3] Downloaded: {s3_key}")
                except Exception as e:
                    print(f"[S3] Download failed for {s3_key}: {e}")
                    continue

            # S3からダウンロードした画像も含めて、文字量の少ない画像を優先選択
            all_available_images = local_images + s3_images
            if len(all_available_images) >= 2:
                selected_images = _select_thumbnail_image_paths(all_available_images, 2)
                print(f"[SUCCESS] Selected {len(selected_images)} images for thumbnail")
                return selected_images
            elif len(all_available_images) == 1:
                print(f"[SUCCESS] Using 1 available image for thumbnail")
                return all_available_images

        except Exception as e:
            print(f"[ERROR] S3 client initialization failed: {e}")

    if local_images:
        print(f"[SUCCESS] Using {len(local_images)} images for thumbnail")

    return local_images[:2]


def create_dark_blue_background(width: int, height: int) -> Image.Image:
    """ダークブルー (#1a1a2e) の背景画像を生成"""
    color = (26, 26, 46)  # #1a1a2e in RGB
    return Image.new("RGB", (width, height), color)


def create_placeholder_image(width: int, height: int, color: tuple = (200, 200, 200)) -> Image.Image:
    """プレースホルダー画像を生成。"""
    img = Image.new("RGB", (width, height), color)
    draw = ImageDraw.Draw(img)
    # 中央にグリッドパターンを描画
    for i in range(0, width, 40):
        draw.line([(i, 0), (i, height)], fill=(180, 180, 180), width=1)
    for i in range(0, height, 40):
        draw.line([(0, i), (width, i)], fill=(180, 180, 180), width=1)
    return img


def get_article_images(
    topic_summary: str,
    meta: Optional[Dict] = None,
    used_image_paths: List[str] = None,
    require_images: bool = False,
    max_retries: int = 3,
) -> Tuple[Image.Image, Image.Image]:
    """
    記事関連画像を2枚取得（サムネイル生成時に独立して画像検索を行う）。
    """
    img1 = None
    img2 = None

    # 先に動画で使用した画像を優先して再利用
    image_paths = used_image_paths or []
    if image_paths:
        for path in image_paths:
            if not path or not os.path.exists(path):
                continue
            try:
                loaded = Image.open(path).convert("RGBA")
            except Exception as e:
                print(f"[THUMBNAIL] Failed to load used image: {path} ({e})")
                continue
            if img1 is None:
                img1 = loaded
                print(f"[THUMBNAIL] Using video image for img1: {path}")
            elif img2 is None:
                img2 = loaded
                print(f"[THUMBNAIL] Using video image for img2: {path}")
            if img1 is not None and img2 is not None:
                return img1, img2
    
    # サムネイル生成時に独立して画像検索を行う（リトライあり）
    for attempt in range(1, max_retries + 1):
        try:
            import sys
            sys.path.append(os.path.dirname(os.path.abspath(__file__)))
            from render_video import search_images_with_playwright, download_image_from_url
            import asyncio

            async def search_thumbnail_images():
                # トピック要約からキーワードを抽出して画像検索
                keywords = []
                if topic_summary:
                    # 簡単なキーワード抽出
                    words = topic_summary.split()[:3]  # 最初の3単語を使用
                    keywords = [word for word in words if len(word) > 2]

                # メタ情報からキーワードを抽出
                if meta and 'source_url' in meta:
                    source_url = meta['source_url']
                    if 'apple' in source_url.lower():
                        keywords.insert(0, 'Apple')
                    elif 'microsoft' in source_url.lower():
                        keywords.insert(0, 'Microsoft')
                    elif 'google' in source_url.lower():
                        keywords.insert(0, 'Google')

                # キーワードがなければデフォルトを使用
                if not keywords:
                    keywords = ['technology', 'innovation']

                print(f"[THUMBNAIL] Searching images with keywords: {keywords}")

                # 画像検索
                for keyword in keywords[:2]:  # 最大2つのキーワードで試行
                    try:
                        images = await search_images_with_playwright(keyword, max_results=3)
                        if images:
                            print(f"[THUMBNAIL] Found {len(images)} images for keyword: {keyword}")

                            # 最初の2枚をダウンロード
                            downloaded_paths = []
                            for img in images[:2]:
                                try:
                                    path = download_image_from_url(img['url'])
                                    if path and os.path.exists(path):
                                        downloaded_paths.append(path)
                                        print(f"[THUMBNAIL] Downloaded image: {os.path.basename(path)}")
                                except Exception as e:
                                    print(f"[THUMBNAIL] Failed to download image: {e}")
                                    continue

                            # 画像を読み込んで返す
                            if len(downloaded_paths) >= 2:
                                found1 = Image.open(downloaded_paths[0]).convert("RGBA")
                                found2 = Image.open(downloaded_paths[1]).convert("RGBA")
                                print(f"[THUMBNAIL] Successfully loaded 2 images for thumbnail")
                                return found1, found2
                            elif len(downloaded_paths) == 1:
                                found1 = Image.open(downloaded_paths[0]).convert("RGBA")
                                print(f"[THUMBNAIL] Successfully loaded 1 image for thumbnail")
                                return found1, None
                    except Exception as e:
                        print(f"[THUMBNAIL] Failed to search with keyword '{keyword}': {e}")
                        continue

                return None, None

            # 非同期関数を実行（既存のイベントループがある場合は別スレッドで実行）
            try:
                running_loop = asyncio.get_running_loop()
            except RuntimeError:
                running_loop = None

            if running_loop and running_loop.is_running():
                import concurrent.futures

                def _run_in_thread():
                    return asyncio.run(search_thumbnail_images())

                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(_run_in_thread)
                    found1, found2 = future.result()
            else:
                found1, found2 = asyncio.run(search_thumbnail_images())

            if found1 and img1 is None:
                img1 = found1
            elif found1 and img2 is None:
                img2 = found1
            if found2 and img2 is None:
                img2 = found2

            if img1 is not None and img2 is not None:
                return img1, img2

        except Exception as e:
            print(f"[THUMBNAIL] Error in independent image search (attempt {attempt}/{max_retries}): {e}")
            continue
    
    # 動画生成で使用した画像からフォールバック（文字量の少ない画像を優先）
    if image_paths and (img1 is None or img2 is None):
        # 存在する画像パスのみを収集（重複を避ける）
        available_paths = []
        used_paths = set()  # 使用済みパスを追跡
        
        for path in image_paths:
            if path in used_paths:
                continue  # 既に使用済みのパスはスキップ

            # 画像が削除済みの場合は除外
            if not os.path.exists(path):
                print(f"[WARNING] Image file does not exist (already deleted): {path}")
                continue
            
            available_paths.append(path)
            used_paths.add(path)  # 使用済みとしてマーク

        selected_paths = _select_thumbnail_image_paths(available_paths, 2)
        print(f"[DEBUG] Selected {len(selected_paths)} images from {len(available_paths)} available images")
        
        for path in selected_paths:
            try:
                loaded = Image.open(path).convert("RGBA")
                if img1 is None:
                    img1 = loaded
                    print(f"[DEBUG] Loaded video image 1: {path}")
                elif img2 is None:
                    img2 = loaded
                    print(f"[DEBUG] Loaded video image 2: {path}")
            except Exception as e:
                print(f"[DEBUG] Failed to load video image: {e}")
    
    if require_images and (img1 is None or img2 is None):
        raise RuntimeError("Failed to obtain required thumbnail images")

    # IT系汎用背景素材（チップ風）をフォールバックに使用
    fallback_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "background.png")
    fallback_image = None
    if os.path.exists(fallback_path):
        try:
            fallback_image = Image.open(fallback_path).convert("RGBA")
            print(f"[DEBUG] Loaded fallback image: {fallback_path}")
        except Exception as e:
            print(f"[DEBUG] Failed to load fallback image: {e}")
            print("[DEBUG] Using dark blue background as fallback")
            fallback_image = create_dark_blue_background(1920, 1080).convert("RGBA")
    else:
        print("[DEBUG] Fallback image not found, using dark blue background")
        fallback_image = create_dark_blue_background(1920, 1080).convert("RGBA")
    
    # 画像がない場合はフォールバック素材を使用
    if img1 is None and fallback_image is not None:
        img1 = fallback_image.copy()
        print(f"[DEBUG] Using fallback image for img1")
    if img2 is None and fallback_image is not None:
        img2 = fallback_image.copy()
        print(f"[DEBUG] Using fallback image for img2")

    # プレースホルダー画像を生成（RGBAで透過対応）
    if img1 is None:
        img1 = create_placeholder_image(640, 480, (100, 150, 200)).convert("RGBA")
        print(f"[DEBUG] Created placeholder img1: size={img1.size}, mode={img1.mode}")
    if img2 is None:
        img2 = create_placeholder_image(640, 480, (200, 100, 150)).convert("RGBA")
        print(f"[DEBUG] Created placeholder img2: size={img2.size}, mode={img2.mode}")
        
    return img1, img2


def draw_text_with_outline(
    draw: ImageDraw.Draw,
    text: str,
    position: tuple,
    font: ImageFont.FreeTypeFont,
    fill: str = "white",
    outline_color: str = "black",
    outline_width: int = 3,
):
    """縁取り付きテキストを描画。UTF-8対応。"""
    try:
        # テキストをUTF-8で安全に処理
        if isinstance(text, bytes):
            text = text.decode('utf-8', errors='ignore')
        else:
            text = str(text)
        
        x, y = position
        # 縁取りを描画（8方向）
        for adj_x in [-outline_width, 0, outline_width]:
            for adj_y in [-outline_width, 0, outline_width]:
                if adj_x == 0 and adj_y == 0:
                    continue
                draw.text((x + adj_x, y + adj_y), text, font=font, fill=outline_color, encoding='unic')
        # メインテキストを描画
        draw.text(position, text, font=font, fill=fill, encoding='unic')
    except Exception as e:
        print(f"[DEBUG] Text drawing failed: {e}, using fallback")
        # フォールバック：ASCIIのみで描画
        try:
            ascii_text = text.encode('ascii', errors='ignore').decode('ascii')
            draw.text(position, ascii_text, font=font, fill=fill)
        except:
            # 最終フォールバック：プレースホルダー
            draw.text(position, "THUMBNAIL", font=font, fill=fill)


def create_thumbnail(
    title: str,
    topic_summary: str,
    thumbnail_data: Dict[str, Any],
    output_path: str,
    meta: Optional[Dict] = None,
    used_image_paths: List[str] = None,
    require_images: bool = False,
    max_image_retries: int = 3,
) -> None:
    """
    テックガジェットスタイル（2chスレタイ風）サムネイルを生成。
    
    Args:
        title: 動画タイトル
        topic_summary: トピック要約
        thumbnail_data: Geminiから生成されたサムネイルデータ
            - main_text: メイン字幕（2chスレタイ風）
            - sub_texts: サブ/煽り字幕のリスト
        output_path: 出力画像パス
        meta: メタ情報（source_url等を含む）
    """
    # キャンバス作成
    img = Image.new("RGB", (THUMBNAIL_WIDTH, THUMBNAIL_HEIGHT), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    
    # 上部70%エリア: 画像2枚をランダム比率で配置
    ratio = random.uniform(0.3, 0.7)  # 3:7 〜 7:3 の範囲でランダム
    left_width = int(THUMBNAIL_WIDTH * ratio)
    right_width = THUMBNAIL_WIDTH - left_width
    
    img1, img2 = get_article_images(
        topic_summary,
        meta,
        used_image_paths,
        require_images=require_images,
        max_retries=max_image_retries,
    )
    
    # 画像をリサイズして配置
    img1_resized = img1.resize((left_width, TOP_AREA_HEIGHT), Image.Resampling.LANCZOS)
    img2_resized = img2.resize((right_width, TOP_AREA_HEIGHT), Image.Resampling.LANCZOS)
    print(f"[DEBUG] Resized images: img1={img1_resized.size}, img2={img2_resized.size}")
    
    # 透過画像を正しく貼り付け（第3引数にmaskを指定）
    img.paste(img1_resized, (0, 0), img1_resized)
    img.paste(img2_resized, (left_width, 0), img2_resized)
    print(f"[DEBUG] Pasted images at positions: (0,0) and ({left_width},0)")
    
    # 下部30%エリア: 黄色背景（座布団）
    yellow_color = (255, 220, 0)  # 鮮やかな黄色
    draw.rectangle(
        [(0, TOP_AREA_HEIGHT), (THUMBNAIL_WIDTH, THUMBNAIL_HEIGHT)],
        fill=yellow_color
    )
    
    # フォント読み込み（日本語フォントを優先）
    try:
        # テキスト長に応じてフォントサイズを動的に調整
        main_text_length = len(thumbnail_data.get("main_text", title))
        if main_text_length > 30:
            main_font_size = 56  # 長いテキストは小さめ
        elif main_text_length > 20:
            main_font_size = 64
        else:
            main_font_size = 72  # 短いテキストは大きめ
            
        print(f"[DEBUG] Loading main font from: {FONT_PATH_MAIN}, size={main_font_size}")
        main_font = ImageFont.truetype(FONT_PATH_MAIN, main_font_size)
        print(f"[DEBUG] Main font loaded successfully")
    except Exception as e:
        print(f"[DEBUG] Failed to load main font: {e}")
        try:
            fallback_path = "/System/Library/Fonts/Hiragino Sans GB.ttc"
            print(f"[DEBUG] Trying fallback font: {fallback_path}")
            main_font = ImageFont.truetype(fallback_path, main_font_size)
            print(f"[DEBUG] Fallback main font loaded")
        except Exception:
            print(f"[DEBUG] Using default font")
            main_font = ImageFont.load_default()
    
    try:
        # サブ字幕サイズはメインと同サイズに揃える
        sub_font_size = main_font_size
        print(f"[DEBUG] Loading sub font from: {FONT_PATH_SUB}")
        sub_font = ImageFont.truetype(FONT_PATH_SUB, sub_font_size)
        print(f"[DEBUG] Sub font loaded successfully")
    except Exception as e:
        print(f"[DEBUG] Failed to load sub font: {e}")
        try:
            fallback_path = "/System/Library/Fonts/Hiragino Sans GB.ttc"
            print(f"[DEBUG] Trying fallback font: {fallback_path}")
            sub_font = ImageFont.truetype(fallback_path, sub_font_size)
            print(f"[DEBUG] Fallback sub font loaded")
        except Exception:
            print(f"[DEBUG] Using default font")
            sub_font = ImageFont.load_default()
    
    # メイン字幕（下部中央、2chスレタイ風）
    main_text = thumbnail_data.get("main_text", title)
    if not main_text:
        main_text = title
    
    # テキストが長すぎる場合は2行に分割
    if len(main_text) > 20:
        # 20文字前後で2行に分割
        mid_point = len(main_text) // 2
        # 空白や句読点で分割を試みる
        for i in range(mid_point, max(0, mid_point-5), -1):
            if main_text[i] in [' ', '、', '。', '・', ' ']:
                main_text_line1 = main_text[:i]
                main_text_line2 = main_text[i+1:]
                break
        else:
            # 適切な分割点がなければ均等に分割
            main_text_line1 = main_text[:mid_point]
            main_text_line2 = main_text[mid_point:]
    else:
        main_text_line1 = main_text
        main_text_line2 = ""
    
    # テキスト色をランダムに選択（黒・赤・青）
    main_colors = ["black", "red", "blue"]
    main_color = random.choice(main_colors)
    
    # テキストサイズを調整（2行対応）
    if main_text_line2:
        # 2行の場合は各行のサイズを計算
        bbox1 = draw.textbbox((0, 0), main_text_line1, font=main_font)
        bbox2 = draw.textbbox((0, 0), main_text_line2, font=main_font)
        text_width = max(bbox1[2] - bbox1[0], bbox2[2] - bbox2[0])
        text_height = (bbox1[3] - bbox1[1]) + (bbox2[3] - bbox2[1]) + 10  # 行間10px
    else:
        # 1行の場合
        bbox = draw.textbbox((0, 0), main_text_line1, font=main_font)
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]
    
    # 中央配置（上に寄せる）
    text_x = (THUMBNAIL_WIDTH - text_width) // 2
    text_y = TOP_AREA_HEIGHT + (BOTTOM_AREA_HEIGHT - text_height) // 3  # 1/3の位置に配置して上に寄せる
    
    # 極太ゴシック風に描画（縁取り付き、2行対応）
    if main_text_line2:
        # 2行で描画
        line1_y = text_y
        line2_y = text_y + (draw.textbbox((0, 0), main_text_line1, font=main_font)[3] - draw.textbbox((0, 0), main_text_line1, font=main_font)[1]) + 10
        
        draw_text_with_outline(
            draw, main_text_line1, (text_x, line1_y), main_font,
            fill=main_color, outline_color="white", outline_width=4
        )
        draw_text_with_outline(
            draw, main_text_line2, (text_x, line2_y), main_font,
            fill=main_color, outline_color="white", outline_width=4
        )
    else:
        # 1行で描画
        draw_text_with_outline(
            draw, main_text_line1, (text_x, text_y), main_font,
            fill=main_color, outline_color="white", outline_width=4
        )
    
    # サブ/煽り字幕（条件付き表示）
    sub_texts = thumbnail_data.get("sub_texts")
    
    # sub_textsが空またはNoneの場合は描画を完全にスキップ
    if sub_texts and len(sub_texts) > 0:
        # 最初の1つのみ使用（最大20文字に設定）
        sub_text = sub_texts[0]
        if len(sub_text) > 20:
            sub_text = sub_text[:20]
        
        try:
            # 高解像度での描画準備（2倍サイズで作成して後で縮小することでアンチエイリアスを効かせる）
            sub_bbox = draw.textbbox((0, 0), sub_text, font=sub_font)
            sub_text_width = sub_bbox[2] - sub_bbox[0]
            sub_text_height = sub_bbox[3] - sub_bbox[1]
            
            padding = 12
            bg_width = sub_text_width + padding * 2
            bg_height = sub_text_height + padding * 2
            
            scale_factor = 2
            high_res_width = bg_width * scale_factor
            high_res_height = bg_height * scale_factor
            
            sub_img = Image.new("RGBA", (high_res_width, high_res_height), (0, 0, 0, 0))
            sub_draw = ImageDraw.Draw(sub_img)
            
            high_res_font_size = sub_font.size * scale_factor
            high_res_font = ImageFont.truetype(FONT_PATH_SUB, high_res_font_size)
            
            # 座布団（白背景）と枠線の描画
            sub_draw.rectangle([(0, 0), (high_res_width, high_res_height)], fill="white")
            border_color = (100, 150, 255)
            sub_draw.rectangle([(0, 0), (high_res_width, high_res_height)], outline=border_color, width=2)
            
            text_color = random.choice(["black", "red"])
            high_res_padding = padding * scale_factor
            sub_draw.text((high_res_padding, high_res_padding), sub_text, font=high_res_font, fill=text_color, encoding='unic')
            
            # --- 【修正ポイント】角度を -10度 or 10度 に設定 ---
            angle = random.choice([-10, 10])
            
            # 回転処理
            rotated_sub_img = sub_img.rotate(angle, expand=True, fillcolor=(0, 0, 0, 0), resample=Image.Resampling.BICUBIC)
            
            # リサイズして元のスケールに戻す
            final_sub_img = rotated_sub_img.resize(
                (rotated_sub_img.width // scale_factor, rotated_sub_img.height // scale_factor), 
                Image.Resampling.LANCZOS
            )
            
            # 配置位置の計算
            # 横軸(X): 画面の左右端100pxを空けた範囲でランダム
            x_min = 100
            x_max = max(x_min + 1, THUMBNAIL_WIDTH - final_sub_img.width - 100)
            sub_x_random = random.randint(x_min, x_max)

            # 縦軸(Y): 下部のメイン背景(黄色帯)にかからない上部エリア内でランダム
            y_min = 20
            y_max = TOP_AREA_HEIGHT - final_sub_img.height - 20
            if y_max < y_min:
                adjusted_sub_y = max(0, TOP_AREA_HEIGHT - final_sub_img.height)
            else:
                adjusted_sub_y = random.randint(y_min, y_max)
            
            # 貼り付け
            img.paste(final_sub_img, (sub_x_random, adjusted_sub_y), final_sub_img)
            
            print(f"[DEBUG] Subtitle placed: angle={angle}, pos=({sub_x_random}, {adjusted_sub_y})")
            
        except Exception as e:
            print(f"[DEBUG] Subtitle rendering error: {e}")
    else:
        print("[DEBUG] No sub_texts provided, skipping subtitle rendering")
    
    # 保存
    img.save(output_path, "PNG", quality=95)
    print(f"サムネイルを生成しました: {output_path}")


if __name__ == "__main__":
    # テスト用
    test_data = {
        "main_text": "これマジでヤバい",
        "sub_texts": ["これマジ？", "逝ったああ", "やばすぎる"]
    }
    create_thumbnail(
        title="テストタイトル",
        topic_summary="テスト要約",
        thumbnail_data=test_data,
        output_path="test_thumbnail.png"
    )
