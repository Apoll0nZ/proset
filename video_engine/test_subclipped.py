#!/usr/bin/env python3
"""
MoviePy v2.0 subclippedメソッドのテスト
"""

import sys
import os
sys.path.append('.')

try:
    from moviepy import AudioFileClip, VideoFileClip
    import numpy as np
    
    print("=== MoviePy v2.0 subclippedメソッドテスト ===")
    
    # テスト用オーディオクリップを作成
    test_array = np.random.randint(0, 255, (1000, 2), dtype=np.int16)
    
    # subclippedメソッドのテスト
    print("テスト1: subclippedメソッドの存在確認")
    
    # AudioFileClipのテスト
    try:
        # 一時的なテスト用クリップ（実際のファイルは不要）
        from moviepy.audio.io.AudioFileClip import AudioFileClip
        print("✅ AudioFileClipインポート成功")
        
        # subclippedメソッドの存在確認
        if hasattr(AudioFileClip, 'subclipped'):
            print("✅ AudioFileClipにsubclippedメソッドが存在")
        else:
            print("❌ AudioFileClipにsubclippedメソッドが存在しない")
            
    except Exception as e:
        print(f"❌ AudioFileClipテストエラー: {e}")
    
    # VideoFileClipのテスト
    try:
        from moviepy.video.io.VideoFileClip import VideoFileClip
        print("✅ VideoFileClipインポート成功")
        
        # subclippedメソッドの存在確認
        if hasattr(VideoFileClip, 'subclipped'):
            print("✅ VideoFileClipにsubclippedメソッドが存在")
        else:
            print("❌ VideoFileClipにsubclippedメソッドが存在しない")
            
    except Exception as e:
        print(f"❌ VideoFileClipテストエラー: {e}")
    
    print("\n=== テスト完了 ===")
    
except ImportError as e:
    print(f"❌ MoviePyインポートエラー: {e}")
except Exception as e:
    print(f"❌ テスト実行エラー: {e}")
    print(f"エラータイプ: {type(e).__name__}")
