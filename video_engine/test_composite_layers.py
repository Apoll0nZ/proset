#!/usr/bin/env python3
"""
合成ロジックのテスト
"""

import sys
import os
sys.path.append('.')

try:
    from moviepy import ImageClip, CompositeVideoClip, TextClip
    import numpy as np
    
    print("=== 合成ロジックテスト ===")
    
    # テスト用の背景を作成
    bg_array = np.random.randint(0, 255, (1080, 1920, 3), dtype=np.uint8)
    bg_clip = ImageClip(bg_array).with_duration(10)
    
    # テスト用の画像を作成
    img_array = np.random.randint(0, 255, (720, 1280, 3), dtype=np.uint8)
    img_clip = ImageClip(img_array).with_start(1).with_duration(5).with_opacity(1.0)
    
    # テスト用のテキストを作成
    txt_clip = TextClip(
        text="テスト字幕",
        font_size=48,
        color="white",
        bg_color="black"
    ).with_start(2).with_duration(3).with_opacity(1.0)
    
    # レイヤー順序の確認
    all_clips = [bg_clip, img_clip, txt_clip]
    print(f"合成クリップ数: {len(all_clips)}")
    print(f"背景: start={bg_clip.start}, duration={bg_clip.duration}, size={bg_clip.size}")
    print(f"画像: start={img_clip.start}, duration={img_clip.duration}, size={img_clip.size}")
    print(f"字幕: start={txt_clip.start}, duration={txt_clip.duration}, size={txt_clip.size}")
    
    # 合成テスト
    try:
        video = CompositeVideoClip(all_clips, size=(1920, 1080))
        print("✅ 合成成功")
        print(f"最終動画サイズ: {video.size}")
        print(f"最終動画長: {video.duration}")
        
        # フレームをテスト
        frame = video.get_frame(2.5)  # 2.5秒時点
        print(f"フレーム取得成功: {frame.shape}")
        
        video.close()
        bg_clip.close()
        img_clip.close()
        txt_clip.close()
        
    except Exception as e:
        print(f"❌ 合成失敗: {e}")
        print(f"エラータイプ: {type(e).__name__}")
    
    print("\n=== テスト完了 ===")
    
except ImportError as e:
    print(f"❌ MoviePyインポートエラー: {e}")
except Exception as e:
    print(f"❌ テスト実行エラー: {e}")
    print(f"エラータイプ: {type(e).__name__}")
