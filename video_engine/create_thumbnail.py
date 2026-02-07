import os
import random
from typing import List, Dict, Any, Optional, Tuple
from PIL import Image, ImageDraw, ImageFont, ImageFilter  # type: ignore
import requests
from io import BytesIO

"""
PC猫スタイル（2chスレタイ風）サムネイル生成スクリプト。

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


def get_article_images(topic_summary: str, meta: Optional[Dict] = None, used_image_paths: List[str] = None) -> Tuple[Image.Image, Image.Image]:
    """
    記事関連画像を2枚取得（動画生成で使用した画像から再利用）。
    """
    # 使用された画像パスのリストを取得
    image_paths = used_image_paths or []
    
    img1 = None
    img2 = None
    
    # 動画生成で使用した画像から2枚をスコアリングして選択
    if image_paths and len(image_paths) >= 2:
        import random
        
        def get_domain_score(image_path: str) -> int:
            """ドメインに基づいてスコアを付与"""
            preferred_domains = [
                'apple.com',
                'microsoft.com',
                'google.com',
                'amazon.com',
                'verge.com',
                'cnet.com',
                'techcrunch.com',
                'engadget.com',
                'arstechnica.com',
                'wired.com',
                'theverge.com',
                'zdnet.com',
                'pcmag.com',
                'tomshardware.com',
                'anandtech.com'
            ]
            
            # パスからURLを抽出（S3パスの場合はファイル名から推測）
            url_lower = image_path.lower()
            
            # 優先ドメインのスコアリング
            for domain in preferred_domains:
                if domain in url_lower:
                    print(f"[SCORE] High score for preferred domain: {domain} in {image_path}")
                    return 10
            
            # 一般的な企業ドメイン
            if any(company in url_lower for company in ['nvidia', 'amd', 'intel', 'qualcomm', 'samsung']):
                return 8
            
            # ニュースメディア系
            if any(media in url_lower for media in ['news', 'reuters', 'bloomberg', 'wsj']):
                return 7
            
            # 公式サイト
            if any(official in url_lower for official in ['official', 'press', 'media']):
                return 6
            
            # その他
            return 3
        
        # スコアリングして上位2枚を選択（重複を避ける）
        scored_paths = []
        used_paths = set()  # 使用済みパスを追跡
        
        for path in image_paths:
            if path in used_paths:
                continue  # 既に使用済みのパスはスキップ
            
            score = get_domain_score(path)
            # 同点の場合はランダム性を加える
            score += random.randint(0, 2)
            scored_paths.append((score, path))
            used_paths.add(path)  # 使用済みとしてマーク
        
        # スコアで降順にソートして上位2枚を選択
        scored_paths.sort(key=lambda x: x[0], reverse=True)
        selected_paths = [path for score, path in scored_paths[:2]]
        
        print(f"[DEBUG] Selected {len(selected_paths)} images with scoring: {[(score, path) for score, path in scored_paths[:2]]}")
        
        try:
            img1 = Image.open(selected_paths[0]).convert("RGBA")
            print(f"[DEBUG] Loaded video image 1: {selected_paths[0]}")
        except Exception as e:
            print(f"[DEBUG] Failed to load video image 1: {e}")
        
        if len(selected_paths) > 1:
            try:
                img2 = Image.open(selected_paths[1]).convert("RGBA")
                print(f"[DEBUG] Loaded video image 2: {selected_paths[1]}")
            except Exception as e:
                print(f"[DEBUG] Failed to load video image 2: {e}")
    
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
) -> None:
    """
    PC猫スタイル（2chスレタイ風）サムネイルを生成。
    
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
    ratio = random.uniform(0.6, 0.7)  # 6:4 〜 7:3 の範囲
    left_width = int(THUMBNAIL_WIDTH * ratio)
    right_width = THUMBNAIL_WIDTH - left_width
    
    img1, img2 = get_article_images(topic_summary, meta, used_image_paths)
    
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
        main_font_size = 72
        print(f"[DEBUG] Loading main font from: {FONT_PATH_MAIN}")
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
        sub_font_size = 36
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
    main_text = thumbnail_data.get("main_text", title[:20])
    if not main_text:
        main_text = title[:20]
    
    # テキスト色をランダムに選択（黒・赤・青）
    main_colors = ["black", "red", "blue"]
    main_color = random.choice(main_colors)
    
    # テキストサイズを調整
    bbox = draw.textbbox((0, 0), main_text, font=main_font)
    text_width = bbox[2] - bbox[0]
    text_height = bbox[3] - bbox[1]
    
    # 中央配置（上に寄せる）
    text_x = (THUMBNAIL_WIDTH - text_width) // 2
    text_y = TOP_AREA_HEIGHT + (BOTTOM_AREA_HEIGHT - text_height) // 3  # 1/3の位置に配置して上に寄せる
    
    # 極太ゴシック風に描画（縁取り付き）
    draw_text_with_outline(
        draw, main_text, (text_x, text_y), main_font,
        fill=main_color, outline_color="white", outline_width=4
    )
    
    # サブ/煽り字幕（条件付き表示）
    sub_texts = thumbnail_data.get("sub_texts")
    
    # sub_textsが空またはNoneの場合は描画を完全にスキップ
    if sub_texts and len(sub_texts) > 0:
        # 最初の1つのみ使用
        sub_text = sub_texts[0]
        
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
            
            # --- 【修正ポイント】配置位置の計算 ---
            # 横軸(X): 画面の左右端100pxを空けた範囲でランダム
            x_min = 100
            x_max = max(x_min + 1, THUMBNAIL_WIDTH - final_sub_img.width - 100)
            sub_x_random = random.randint(x_min, x_max)
            
            # 縦軸(Y): メイン字幕とサブタイトルの中央が揃うように配置
            # 下部エリアの中央（text_y）から、サブタイトルの高さの半分だけ上に配置
            adjusted_sub_y = text_y - (final_sub_img.height // 2) - 20
            
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
