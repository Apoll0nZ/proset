#!/usr/bin/env python3
"""
修正したMoviePy v2.0 TextClipのテスト
"""

import sys
import os
sys.path.append('.')

try:
    from moviepy import TextClip
    import numpy as np
    
    print("=== 修正後MoviePy v2.0 TextClipテスト ===")
    
    # フォントパスの取得
    font_path = "/Users/zapoll0n/Documents/app/youtube/video_engine/assets/keifont.ttf"
    if not os.path.exists(font_path):
        font_path = None  # デフォルトフォントを使用
    
    print(f"使用フォント: {font_path or 'デフォルト'}")
    
    # テスト1: セグメントテキスト（修正後）
    print("\nテスト1: セグメントテキスト（修正後）")
    try:
        clip1 = TextClip(
            text="概要",
            font_size=28,
            color="white",
            font=font_path,
            bg_color="red",
            size=(250, 60)
        )
        print("✅ セグメントテキストで成功")
        print(f"  - サイズ: {clip1.size}")
        print(f"  - 持続時間: {clip1.duration}")
        clip1.close()
    except Exception as e:
        print(f"❌ セグメントテキストで失敗: {e}")
        print(f"エラータイプ: {type(e).__name__}")
    
    # テスト2: 字幕テキスト（修正後）
    print("\nテスト2: 字幕テキスト（修正後）")
    try:
        clip2 = TextClip(
            text="字幕テスト",
            font_size=48,
            color="black",
            font=font_path,
            method="caption",
            size=(1700, None),
            stroke_color="yellowgreen",
            stroke_width=1,
            bg_color="white"
        )
        print("✅ 字幕テキストで成功")
        print(f"  - サイズ: {clip2.size}")
        print(f"  - 持続時間: {clip2.duration}")
        clip2.close()
    except Exception as e:
        print(f"❌ 字幕テキストで失敗: {e}")
        print(f"エラータイプ: {type(e).__name__}")
    
    # テスト3: with_*メソッドの組み合わせ
    print("\nテスト3: with_*メソッドの組み合わせ")
    try:
        clip3 = TextClip(
            text="完全テスト",
            font_size=36,
            color="blue",
            font=font_path
        ).with_position((100, 100)).with_duration(5.0).with_start(1.0)
        print("✅ with_*メソッドの組み合わせで成功")
        print(f"  - サイズ: {clip3.size}")
        print(f"  - 開始時間: {clip3.start}")
        print(f"  - 持続時間: {clip3.duration}")
        clip3.close()
    except Exception as e:
        print(f"❌ with_*メソッドの組み合わせで失敗: {e}")
        print(f"エラータイプ: {type(e).__name__}")
    
    print("\n=== テスト完了 ===")
    
except ImportError as e:
    print(f"❌ MoviePyインポートエラー: {e}")
except Exception as e:
    print(f"❌ テスト実行エラー: {e}")
    print(f"エラータイプ: {type(e).__name__}")
