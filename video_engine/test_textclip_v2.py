#!/usr/bin/env python3
"""
MoviePy v2.0 TextClip text=引数のテスト
"""

import sys
import os
sys.path.append('.')

try:
    from moviepy import TextClip
    import numpy as np
    
    print("=== MoviePy v2.0 TextClip text=引数テスト ===")
    
    # フォントパスの取得
    font_path = "/Users/zapoll0n/Documents/app/youtube/video_engine/assets/keifont.ttf"
    if not os.path.exists(font_path):
        font_path = None  # デフォルトフォントを使用
    
    print(f"使用フォント: {font_path or 'デフォルト'}")
    
    # テスト1: 従来の書き方（第一引数にテキスト）
    print("\nテスト1: 従来の書き方 TextClip('テキスト', ...)")
    try:
        clip1 = TextClip(
            "テストテキスト",
            fontsize=24,
            color="black",
            font=font_path
        )
        print("✅ 従来の書き方で成功")
        clip1.close()
    except Exception as e:
        print(f"❌ 従来の書き方で失敗: {e}")
        print(f"エラータイプ: {type(e).__name__}")
    
    # テスト2: v2.0の書き方（text=を明示）
    print("\nテスト2: v2.0の書き方 TextClip(text='テキスト', ...)")
    try:
        clip2 = TextClip(
            text="テストテキスト",
            fontsize=24,
            color="black",
            font=font_path
        )
        print("✅ v2.0の書き方で成功")
        clip2.close()
    except Exception as e:
        print(f"❌ v2.0の書き方で失敗: {e}")
        print(f"エラータイプ: {type(e).__name__}")
    
    # テスト3: 複雑なTextClip（字幕風）
    print("\nテスト3: 複雑なTextClip（字幕風）")
    try:
        clip3 = TextClip(
            text="字幕テスト",
            fontsize=48,
            color="black",
            font=font_path,
            method="caption",
            size=(1700, None),
            stroke_color="yellowgreen",
            stroke_width=1,
            bg_color="white"
        )
        print("✅ 複雑なTextClipで成功")
        clip3.close()
    except Exception as e:
        print(f"❌ 複雑なTextClipで失敗: {e}")
        print(f"エラータイプ: {type(e).__name__}")
    
    print("\n=== テスト完了 ===")
    
except ImportError as e:
    print(f"❌ MoviePyインポートエラー: {e}")
except Exception as e:
    print(f"❌ テスト実行エラー: {e}")
    print(f"エラータイプ: {type(e).__name__}")
