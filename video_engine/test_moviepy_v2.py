#!/usr/bin/env python3
"""
MoviePy 2.0 新文法のテスト
"""

import sys
import os
sys.path.append('.')

try:
    from moviepy import ImageClip, CompositeVideoClip
    import numpy as np
    
    print("=== MoviePy 2.0 新文法テスト ===")
    
    # テスト用画像配列を生成
    test_array = np.random.randint(0, 255, (100, 100, 3), dtype=np.uint8)
    
    # 新文法でクリップを作成
    clip = ImageClip(test_array)
    
    # 新文法のメソッドをテスト
    clip = clip.with_duration(5.0)
    clip = clip.with_start(1.0)
    clip = clip.with_position((50, 50))
    
    print("✅ ImageClipの新文法メソッドが正常に動作")
    print(f"  - duration: {clip.duration}")
    print(f"  - start: {clip.start}")
    print(f"  - size: {clip.size}")
    
    # subclip -> clipped のテスト
    if hasattr(clip, 'clipped'):
        clipped_clip = clip.clipped(start_time=1.0, end_time=3.0)
        print("✅ clipped()メソッドが正常に動作")
        print(f"  - clipped duration: {clipped_clip.duration}")
    else:
        print("❌ clipped()メソッドが見つかりません")
    
    # with_audioのテスト
    if hasattr(clip, 'with_audio'):
        print("✅ with_audio()メソッドが利用可能")
    else:
        print("❌ with_audio()メソッドが見つかりません")
    
    print("\n=== テスト完了 ===")
    
except ImportError as e:
    print(f"❌ MoviePyインポートエラー: {e}")
except Exception as e:
    print(f"❌ テスト実行エラー: {e}")
    print(f"エラータイプ: {type(e).__name__}")
