import os
import random
from typing import Dict, Any, Optional, Tuple
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

# フォントパス（システムフォントを使用、なければデフォルト）
FONT_PATH_MAIN = os.environ.get(
    "THUMBNAIL_FONT_MAIN",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"  # フォールバック
)
FONT_PATH_SUB = os.environ.get(
    "THUMBNAIL_FONT_SUB",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
)


def download_image(url: str, max_size: tuple = (640, 480)) -> Optional[Image.Image]:
    """URLから画像をダウンロードしてリサイズ。"""
    try:
        resp = requests.get(url, timeout=10)
        if resp.status_code != 200:
            return None
        img = Image.open(BytesIO(resp.content))
        img = img.convert("RGB")
        img.thumbnail(max_size, Image.Resampling.LANCZOS)
        return img
    except Exception:
        return None


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


def get_article_images(topic_summary: str, meta: Optional[Dict] = None) -> Tuple[Image.Image, Image.Image]:
    """
    記事関連画像を2枚取得（実装は簡易版）。
    実際の運用では、記事のOGP画像や関連画像を取得する処理を追加。
    """
    # ここではプレースホルダー画像を生成
    # 実運用では、meta.source_url からOGP画像を取得する処理を追加
    img1 = create_placeholder_image(640, 480, (100, 150, 200))
    img2 = create_placeholder_image(640, 480, (200, 100, 150))
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
    """縁取り付きテキストを描画。"""
    x, y = position
    # 縁取りを描画（8方向）
    for adj_x in [-outline_width, 0, outline_width]:
        for adj_y in [-outline_width, 0, outline_width]:
            if adj_x == 0 and adj_y == 0:
                continue
            draw.text((x + adj_x, y + adj_y), text, font=font, fill=outline_color)
    # メインテキストを描画
    draw.text(position, text, font=font, fill=fill)


def create_thumbnail(
    title: str,
    topic_summary: str,
    thumbnail_data: Dict[str, Any],
    output_path: str,
    meta: Optional[Dict] = None,
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
    
    img1, img2 = get_article_images(topic_summary, meta)
    
    # 画像をリサイズして配置
    img1_resized = img1.resize((left_width, TOP_AREA_HEIGHT), Image.Resampling.LANCZOS)
    img2_resized = img2.resize((right_width, TOP_AREA_HEIGHT), Image.Resampling.LANCZOS)
    
    img.paste(img1_resized, (0, 0))
    img.paste(img2_resized, (left_width, 0))
    
    # 下部30%エリア: 黄色背景（座布団）
    yellow_color = (255, 220, 0)  # 鮮やかな黄色
    draw.rectangle(
        [(0, TOP_AREA_HEIGHT), (THUMBNAIL_WIDTH, THUMBNAIL_HEIGHT)],
        fill=yellow_color
    )
    
    # フォント読み込み（フォールバック付き）
    try:
        main_font_size = 72
        main_font = ImageFont.truetype(FONT_PATH_MAIN, main_font_size)
    except Exception:
        try:
            main_font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", main_font_size)
        except Exception:
            main_font = ImageFont.load_default()
    
    try:
        sub_font_size = 36
        sub_font = ImageFont.truetype(FONT_PATH_SUB, sub_font_size)
    except Exception:
        try:
            sub_font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", sub_font_size)
        except Exception:
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
    
    # 中央配置
    text_x = (THUMBNAIL_WIDTH - text_width) // 2
    text_y = TOP_AREA_HEIGHT + (BOTTOM_AREA_HEIGHT - text_height) // 2
    
    # 極太ゴシック風に描画（縁取り付き）
    draw_text_with_outline(
        draw, main_text, (text_x, text_y), main_font,
        fill=main_color, outline_color="white", outline_width=4
    )
    
    # サブ/煽り字幕（画像上に斜め配置）
    sub_texts = thumbnail_data.get("sub_texts", ["これマジ？", "逝ったああ"])
    if not sub_texts:
        sub_texts = ["これマジ？"]
    
    # 各サブテキストをランダム位置に配置
    for i, sub_text in enumerate(sub_texts[:3]):  # 最大3個
        # ランダム位置（上部エリア内）
        sub_x = random.randint(50, THUMBNAIL_WIDTH - 200)
        sub_y = random.randint(50, TOP_AREA_HEIGHT - 100)
        
        # 斜めに回転（簡易版: 実際にはImage.rotateを使用）
        # ここでは斜め配置の見た目を出すため、位置をずらす
        draw_text_with_outline(
            draw, sub_text, (sub_x, sub_y), sub_font,
            fill="white", outline_color="black", outline_width=2
        )
    
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
