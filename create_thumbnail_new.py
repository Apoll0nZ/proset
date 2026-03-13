import os
import random
import json
import time
import base64
from typing import List, Dict, Any, Optional, Tuple
from PIL import Image, ImageDraw, ImageFont, ImageFilter
import requests
from io import BytesIO

"""
テックガジェットスタイル サムネイル生成スクリプト（v2）

レイアウト:
- 背景: 画像1枚フルブリード（1280x720全面）
- 上部: タイトル文字（赤 or 黄・黒縁・極太）+ 上部グラデーションオーバーレイ
- 下部: サブタイトル（白・黒縁のみ・帯なし）
- 色: color_index を外部で管理し赤・黄を交互に使用
"""

THUMBNAIL_WIDTH = 1280
THUMBNAIL_HEIGHT = 720

# メインタイトルカラー: 赤・黄を交互に使う
# color_index % 2 == 0 → 赤, == 1 → 黄
MAIN_TEXT_COLORS = [
    (255, 40, 40),   # 赤
    (255, 220, 0),   # 黄
]

# クロスプラットフォーム対応のフォント検出
def find_japanese_font() -> str:
    possible_fonts = [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Black.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/System/Library/Fonts/Hiragino Sans GB.ttc",
        "/System/Library/Fonts/STHeiti Light.ttc",
        "C:\\Windows\\Fonts\\msgothic.ttc",
        os.environ.get("THUMBNAIL_FONT_MAIN", ""),
    ]
    for p in possible_fonts:
        if p and os.path.exists(p):
            print(f"[THUMBNAIL] Font found: {p}")
            return p
    print("[THUMBNAIL] No Japanese font found, using default")
    return ""

KEIFONT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "keifont.ttf")

def resolve_thumbnail_font(env_key: str) -> str:
    env_font = os.environ.get(env_key, "")
    if env_font and os.path.exists(env_font):
        return env_font
    if os.path.exists(KEIFONT_PATH):
        return KEIFONT_PATH
    return find_japanese_font()

FONT_PATH_MAIN = resolve_thumbnail_font("THUMBNAIL_FONT_MAIN")
FONT_PATH_SUB  = resolve_thumbnail_font("THUMBNAIL_FONT_SUB")

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL_NAME = os.environ.get("GEMINI_MODEL_NAME", "gemini-2.5-flash-lite")

def resolve_gemini_api_version(model_name: str, configured_version: Optional[str]) -> str:
    version = (configured_version or "").strip()
    if not version:
        return "v1beta" if model_name.startswith("gemini-2.") else "v1"
    if version == "v1" and model_name.startswith("gemini-2."):
        return "v1beta"
    return version

GEMINI_API_VERSION = resolve_gemini_api_version(
    GEMINI_MODEL_NAME,
    os.environ.get("GEMINI_API_VERSION"),
)
THUMBNAIL_GEMINI_TEXT_FILTER    = os.environ.get("THUMBNAIL_GEMINI_TEXT_FILTER", "1").lower() not in ("0","false","off")
THUMBNAIL_GEMINI_MAX_CANDIDATES = max(2, int(os.environ.get("THUMBNAIL_GEMINI_MAX_CANDIDATES", "8")))
THUMBNAIL_GEMINI_RANDOM_POOL    = max(2, int(os.environ.get("THUMBNAIL_GEMINI_RANDOM_POOL", "4")))


# ------------------------------------------------------------------ #
#  ユーティリティ
# ------------------------------------------------------------------ #

def _get_mime_type_from_path(image_path: str) -> Optional[str]:
    ext = os.path.splitext(image_path.lower())[1]
    return {".jpg":"image/jpeg",".jpeg":"image/jpeg",".png":"image/png",
            ".webp":"image/webp",".gif":"image/gif",".bmp":"image/bmp"}.get(ext)


def draw_text_with_outline(
    draw: ImageDraw.Draw,
    text: str,
    position: tuple,
    font: ImageFont.FreeTypeFont,
    fill,
    outline_color=(0, 0, 0),
    outline_width: int = 9,
):
    """縁取り付きテキスト描画（8方向 × outline_width ピクセル）"""
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="ignore")
    x, y = position
    for dx in range(-outline_width, outline_width + 1, 2):
        for dy in range(-outline_width, outline_width + 1, 2):
            if dx == 0 and dy == 0:
                continue
            draw.text((x + dx, y + dy), text, font=font, fill=outline_color)
    draw.text(position, text, font=font, fill=fill)


def wrap_text_to_lines(draw: ImageDraw.Draw, text: str, font, max_width: int) -> List[str]:
    """max_width を超えないよう1文字ずつ折り返す"""
    lines, current = [], ""
    for char in text:
        test = current + char
        bbox = draw.textbbox((0, 0), test, font=font)
        if bbox[2] - bbox[0] > max_width and current:
            lines.append(current)
            current = char
        else:
            current = test
    if current:
        lines.append(current)
    return lines


def get_font_and_lines(
    draw: ImageDraw.Draw,
    text: str,
    font_path: str,
    max_width: int,
    max_size: int = 88,
    min_size: int = 48,
) -> Tuple[ImageFont.FreeTypeFont, int, List[str]]:
    """テキストが2行以内に収まる最大フォントサイズとその行リストを返す"""
    for size in range(max_size, min_size - 1, -4):
        try:
            font = ImageFont.truetype(font_path, size)
        except Exception:
            continue
        lines = wrap_text_to_lines(draw, text, font, max_width)
        if len(lines) <= 2:
            return font, size, lines
    font = ImageFont.truetype(font_path, min_size)
    lines = wrap_text_to_lines(draw, text, font, max_width)
    return font, min_size, lines[:2]


# ------------------------------------------------------------------ #
#  Gemini 画像テキスト密度フィルタ（既存ロジックを維持）
# ------------------------------------------------------------------ #

def _analyze_image_text_density_with_gemini(image_path: str) -> Optional[Dict[str, Any]]:
    if not THUMBNAIL_GEMINI_TEXT_FILTER or not GEMINI_API_KEY:
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
        print(f"[THUMBNAIL] Failed to read image: {image_path} ({e})")
        return None

    url = (
        f"https://generativelanguage.googleapis.com/{GEMINI_API_VERSION}/models/"
        f"{GEMINI_MODEL_NAME}:generateContent?key={GEMINI_API_KEY}"
    )
    prompt = (
        "この画像がYouTubeサムネ背景に向くか判定してください。"
        "文字・ロゴ・UI・スクリーンショット・看板など読める文字情報が目立つ画像は不適です。"
        'JSONのみで返答: {"text_ratio":0-100,"text_heavy":true/false,"keep":true/false}'
    )
    payload = {
        "contents": [{"parts": [{"text": prompt}, {"inline_data": {"mime_type": mime_type, "data": image_b64}}]}],
        "generationConfig": {"temperature": 0.0, "max_output_tokens": 120, "response_mime_type": "application/json"},
    }
    for attempt in range(2):
        try:
            resp = requests.post(url, json=payload, timeout=(5, 20))
            if resp.status_code in (429, 503):
                if attempt == 0:
                    time.sleep(1.2)
                    continue
                return None
            if resp.status_code != 200:
                return None
            data = resp.json()
            candidates = data.get("candidates", [])
            if not candidates:
                return None
            parts = candidates[0].get("content", {}).get("parts", [])
            raw = "\n".join(p.get("text","") for p in parts if p.get("text")).strip()
            if not raw:
                return None
            try:
                parsed = json.loads(raw)
            except Exception:
                s, e2 = raw.find("{"), raw.rfind("}")
                if s == -1 or e2 <= s:
                    return None
                parsed = json.loads(raw[s:e2+1])
            tr = max(0, min(100, int(parsed.get("text_ratio", 50))))
            th = bool(parsed.get("text_heavy", tr >= 35))
            return {"text_ratio": tr, "text_heavy": th, "keep": bool(parsed.get("keep", not th))}
        except Exception as e:
            print(f"[THUMBNAIL] Gemini error: {e}")
            if attempt == 0:
                time.sleep(1.0)
    return None


def calculate_image_score(image_path: str) -> int:
    score = 0
    basename = os.path.basename(image_path).lower()
    if any(kw in basename for kw in ["iphone","android","samsung","google","apple","xiaomi","oppo","vivo","huawei","honor"]):
        score += 5
    if any(kw in basename for kw in ["product","official","device","pro"]):
        score += 3
    try:
        if os.path.getsize(image_path) > 200_000:
            score += 3
        elif os.path.getsize(image_path) > 100_000:
            score += 2
    except Exception:
        pass
    return score


def _select_best_image(candidate_paths: List[str]) -> Optional[str]:
    """
    候補リストから背景に最適な画像を1枚選ぶ。
    Gemini テキスト密度フィルタ → スコアランキング → ランダムプールから1枚。
    """
    unique: List[str] = []
    seen = set()
    for p in candidate_paths:
        if p and p not in seen and os.path.exists(p):
            seen.add(p)
            unique.append(p)
    if not unique:
        return None
    if len(unique) == 1:
        return unique[0]

    ranked = sorted(unique, key=lambda p: calculate_image_score(p), reverse=True)
    targets = ranked[:min(len(ranked), THUMBNAIL_GEMINI_MAX_CANDIDATES)]

    scored: List[Tuple[str, float, bool, int]] = []
    for path in targets:
        base = float(calculate_image_score(path))
        analysis = _analyze_image_text_density_with_gemini(path)
        if analysis:
            tr = int(analysis.get("text_ratio", 50))
            th = bool(analysis.get("text_heavy", tr >= 35))
            final = base + (100 - tr) / 20.0 - (6.0 if th else 0.0)
        else:
            tr, th, final = 50, False, base
        scored.append((path, final, th, tr))

    # 文字密度が低い順・スコア高い順に並べる
    scored.sort(key=lambda x: (x[2], -x[1], x[3]))

    pool_size = min(len(scored), max(1, THUMBNAIL_GEMINI_RANDOM_POOL))
    top_pool = [p for p, *_ in scored[:pool_size]]
    selected = random.choice(top_pool)
    print(f"[THUMBNAIL] Selected image from top-{pool_size} pool: {os.path.basename(selected)}")
    return selected


# ------------------------------------------------------------------ #
#  画像取得（既存の select_images_from_video 互換）
# ------------------------------------------------------------------ #

def select_image_from_video(image_schedule: List[Dict], s3_bucket: str = None) -> Optional[str]:
    """
    動画で使用した画像から背景に最適な1枚を選択。
    既存の select_images_from_video の1枚版。
    """
    if not image_schedule:
        return None

    temp_dir = os.path.join(os.path.dirname(__file__), "temp")
    os.makedirs(temp_dir, exist_ok=True)

    local_images = []
    for item in image_schedule:
        path = item.get("path", "")
        if not path:
            continue
        local_path = path if os.path.isabs(path) else os.path.join(temp_dir, os.path.basename(path))
        if os.path.exists(local_path):
            local_images.append(local_path)

    if local_images:
        result = _select_best_image(local_images)
        if result:
            return result

    # S3 フォールバック
    if s3_bucket:
        try:
            import boto3
            s3 = boto3.client("s3")
            s3_images = []
            for item in image_schedule[:10]:
                path = item.get("path", "")
                if not path:
                    continue
                filename = os.path.basename(path)
                local_path = os.path.join(temp_dir, f"thumbnail_{filename}")
                if not os.path.exists(local_path):
                    try:
                        s3.download_file(s3_bucket, f"temp/{filename}", local_path)
                    except Exception as e:
                        print(f"[S3] Download failed: {e}")
                        continue
                if os.path.exists(local_path):
                    s3_images.append(local_path)
            if s3_images:
                result = _select_best_image(local_images + s3_images)
                if result:
                    return result
        except Exception as e:
            print(f"[THUMBNAIL] S3 error: {e}")

    return None


def get_background_image(
    topic_summary: str,
    meta: Optional[Dict] = None,
    used_image_paths: List[str] = None,
    max_retries: int = 3,
) -> Optional[Image.Image]:
    """
    背景画像を1枚取得。
    優先順: ローカル使用済み画像 → Playwright画像検索 → None
    """
    # ローカル画像から選択
    if used_image_paths:
        best = _select_best_image(used_image_paths)
        if best:
            try:
                img = Image.open(best).convert("RGB")
                print(f"[THUMBNAIL] Background image: {os.path.basename(best)}")
                return img
            except Exception as e:
                print(f"[THUMBNAIL] Failed to open image: {e}")

    # Playwright 検索フォールバック
    for attempt in range(1, max_retries + 1):
        try:
            import sys
            sys.path.append(os.path.dirname(os.path.abspath(__file__)))
            from render_video import search_images_with_playwright, download_image_from_url
            import asyncio

            async def search():
                keywords = []
                if topic_summary:
                    keywords = [w for w in topic_summary.split()[:3] if len(w) > 2]
                if meta:
                    url = meta.get("url") or meta.get("source_url", "")
                    for brand in ["apple","microsoft","google","nvidia","samsung"]:
                        if brand in url.lower():
                            keywords.insert(0, brand.capitalize())
                            break
                if not keywords:
                    keywords = ["technology"]
                for kw in keywords[:2]:
                    try:
                        images = await search_images_with_playwright(kw, max_results=5)
                        if images:
                            for img_info in images:
                                path = download_image_from_url(img_info["url"])
                                if path and os.path.exists(path):
                                    return Image.open(path).convert("RGB")
                    except Exception as e:
                        print(f"[THUMBNAIL] Search error for '{kw}': {e}")
                return None

            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None

            if loop and loop.is_running():
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                    result = ex.submit(lambda: asyncio.run(search())).result()
            else:
                result = asyncio.run(search())

            if result:
                return result
        except Exception as e:
            print(f"[THUMBNAIL] Playwright search error (attempt {attempt}): {e}")

    return None


def create_dark_fallback(width: int, height: int) -> Image.Image:
    """フォールバック用ダーク背景"""
    img = Image.new("RGB", (width, height), (15, 15, 25))
    draw = ImageDraw.Draw(img)
    for y in range(height):
        t = y / height
        r = int(15 + 20 * t)
        g = int(15 + 15 * t)
        b = int(25 + 30 * t)
        draw.line([(0, y), (width, y)], fill=(r, g, b))
    return img


# ------------------------------------------------------------------ #
#  メイン生成関数
# ------------------------------------------------------------------ #

def create_thumbnail(
    title: str,
    topic_summary: str,
    thumbnail_data: Dict[str, Any],
    output_path: str,
    meta: Optional[Dict] = None,
    used_image_paths: List[str] = None,
    require_images: bool = False,
    max_image_retries: int = 3,
    color_index: int = 0,
) -> None:
    """
    サムネイル生成（v2 レイアウト）

    Args:
        title:             動画タイトル
        topic_summary:     トピック要約（画像検索キーワードに使用）
        thumbnail_data:    {"main_text": str, "sub_texts": [str, ...]}
        output_path:       出力ファイルパス
        meta:              メタ情報（url 等）
        used_image_paths:  動画生成で使用した画像パスリスト
        require_images:    True の場合、画像取得失敗時に例外を送出
        max_image_retries: 画像検索リトライ回数
        color_index:       0=赤, 1=黄（外部で管理して交互に渡すこと）
    """

    # === 1. 背景画像の取得 ===
    bg_img = get_background_image(
        topic_summary=topic_summary,
        meta=meta,
        used_image_paths=used_image_paths or [],
        max_retries=max_image_retries,
    )

    if bg_img is None:
        # フォールバック: assets/background.png or ダーク背景
        fallback_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "background.png")
        if os.path.exists(fallback_path):
            try:
                bg_img = Image.open(fallback_path).convert("RGB")
            except Exception:
                bg_img = None
        if bg_img is None:
            if require_images:
                raise RuntimeError("Failed to obtain required background image")
            bg_img = create_dark_fallback(THUMBNAIL_WIDTH, THUMBNAIL_HEIGHT)

    # === 2. フルブリード配置（クロップしてキャンバス全体を埋める）===
    bg_ratio = bg_img.width / bg_img.height
    target_ratio = THUMBNAIL_WIDTH / THUMBNAIL_HEIGHT

    if bg_ratio > target_ratio:
        new_h = THUMBNAIL_HEIGHT
        new_w = int(THUMBNAIL_HEIGHT * bg_ratio)
    else:
        new_w = THUMBNAIL_WIDTH
        new_h = int(THUMBNAIL_WIDTH / bg_ratio)

    bg_img = bg_img.resize((new_w, new_h), Image.Resampling.LANCZOS)
    x_off = (new_w - THUMBNAIL_WIDTH) // 2
    y_off = (new_h - THUMBNAIL_HEIGHT) // 2
    bg_img = bg_img.crop((x_off, y_off, x_off + THUMBNAIL_WIDTH, y_off + THUMBNAIL_HEIGHT))

    canvas = bg_img.copy()

    # === 3. 上部グラデーションオーバーレイ（文字読みやすさのため）===
    overlay = Image.new("RGBA", (THUMBNAIL_WIDTH, THUMBNAIL_HEIGHT), (0, 0, 0, 0))
    ov_draw = ImageDraw.Draw(overlay)
    ov_height = int(THUMBNAIL_HEIGHT * 0.50)
    for y in range(ov_height):
        t = 1.0 - (y / ov_height)
        alpha = int(175 * t * t)
        ov_draw.line([(0, y), (THUMBNAIL_WIDTH, y)], fill=(0, 0, 0, alpha))

    canvas = canvas.convert("RGBA")
    canvas = Image.alpha_composite(canvas, overlay)
    canvas = canvas.convert("RGB")

    draw = ImageDraw.Draw(canvas)

    # === 4. メインタイトル（上部・中央揃え）===
    main_text = thumbnail_data.get("main_text") or title
    main_color = MAIN_TEXT_COLORS[color_index % 2]
    padding_x = 55

    try:
        font, font_size, lines = get_font_and_lines(
            draw, main_text, FONT_PATH_MAIN,
            max_width=THUMBNAIL_WIDTH - padding_x * 2,
        )
    except Exception:
        font = ImageFont.load_default()
        font_size = 40
        lines = [main_text]

    line_height = font_size + 18
    total_text_h = len(lines) * line_height

    # 上部30%エリアの中心に配置（最低でも上から30px）
    center_y = int(THUMBNAIL_HEIGHT * 0.22)
    y_start = max(30, center_y - total_text_h // 2)

    for i, line in enumerate(lines):
        try:
            bbox = draw.textbbox((0, 0), line, font=font)
            line_w = bbox[2] - bbox[0]
        except Exception:
            line_w = len(line) * font_size // 2
        x = (THUMBNAIL_WIDTH - line_w) // 2
        y = y_start + i * line_height
        draw_text_with_outline(
            draw, line, (x, y), font,
            fill=main_color,
            outline_color=(0, 0, 0),
            outline_width=9,
        )

    # === 5. サブタイトル（下部・白・黒縁のみ・帯なし）===
    sub_texts = thumbnail_data.get("sub_texts") or []
    sub_text = sub_texts[0] if sub_texts else None

    if sub_text:
        try:
            sub_font_size = 52
            sub_font = ImageFont.truetype(FONT_PATH_SUB, sub_font_size)
            sub_bbox = draw.textbbox((0, 0), sub_text, font=sub_font)
            sub_w = sub_bbox[2] - sub_bbox[0]
            sub_h = sub_bbox[3] - sub_bbox[1]
            sub_x = (THUMBNAIL_WIDTH - sub_w) // 2
            sub_y = THUMBNAIL_HEIGHT - sub_h - 38

            draw_text_with_outline(
                draw, sub_text, (sub_x, sub_y), sub_font,
                fill=(255, 255, 255),
                outline_color=(0, 0, 0),
                outline_width=7,
            )
        except Exception as e:
            print(f"[THUMBNAIL] Sub text rendering failed: {e}")

    # === 6. 保存 ===
    canvas.save(output_path, "PNG", quality=95)
    print(f"[THUMBNAIL] Saved: {output_path}")


# ------------------------------------------------------------------ #
#  color_index 管理ヘルパー
# ------------------------------------------------------------------ #

class ThumbnailColorRotator:
    """
    動画ごとに赤・黄を必ず交互に使うためのカウンタ管理クラス。

    使い方:
        rotator = ThumbnailColorRotator()
        color_index = rotator.next()  # 0, 1, 0, 1 ...
    """
    def __init__(self, start: int = 0):
        self._index = start % 2

    def next(self) -> int:
        idx = self._index
        self._index = 1 - self._index
        return idx

    def current(self) -> int:
        return self._index


# ------------------------------------------------------------------ #
#  テスト実行
# ------------------------------------------------------------------ #

if __name__ == "__main__":
    rotator = ThumbnailColorRotator()
    test_cases = [
        {
            "title": "【iPhone 17e】廉価版はもうやめろ！Appleの衝撃戦略がヤバすぎる",
            "topic_summary": "iPhone 17e Apple 廉価版戦略",
            "thumbnail_data": {
                "main_text": "廉価版はもう終わり",
                "sub_texts": ["Appleの本当の狙いとは"]
            },
        },
        {
            "title": "【iOS 26.3.1】iPhoneユーザーは今すぐアプデせよ！",
            "topic_summary": "iOS 26.3.1 アップデート 脆弱性",
            "thumbnail_data": {
                "main_text": "今すぐアプデせよ",
                "sub_texts": ["重大な脆弱性を修正"]
            },
        },
        {
            "title": "【RTX 5050】GDDR7 9GB搭載！これが次世代の衝撃か",
            "topic_summary": "NVIDIA RTX 5050 GDDR7 GPU",
            "thumbnail_data": {
                "main_text": "次世代GPU爆誕",
                "sub_texts": ["GDDR7 9GB搭載の実力"]
            },
        },
    ]

    out_dir = os.path.join(os.path.dirname(__file__), "test_thumbnails")
    os.makedirs(out_dir, exist_ok=True)

    for i, case in enumerate(test_cases, 1):
        color_index = rotator.next()
        out = os.path.join(out_dir, f"thumb_{i}_{'red' if color_index==0 else 'yellow'}.png")
        create_thumbnail(
            title=case["title"],
            topic_summary=case["topic_summary"],
            thumbnail_data=case["thumbnail_data"],
            output_path=out,
            color_index=color_index,
        )
        print(f"  color={'赤' if color_index==0 else '黄'}")
