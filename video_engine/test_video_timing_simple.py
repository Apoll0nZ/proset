#!/usr/bin/env python3
"""
動画タイミング修正のシンプルテスト
"""

import sys
import os
sys.path.append('.')

try:
    from moviepy import ImageClip, CompositeVideoClip
    import numpy as np
    
    print("=== 動画タイミング修正テスト ===")
    
    # テスト用の背景を作成
    bg_array = np.random.randint(0, 255, (1080, 1920, 3), dtype=np.uint8)
    bg_clip = ImageClip(bg_array).with_duration(60)
    
    # テスト用の画像クリップを作成（3秒から開始）
    img_array = np.random.randint(255, 255, (720, 1280, 3), dtype=np.uint8)
    
    image_clips = []
    for i in range(3):
        start_time = 3.0 + i * 3.0  # 3秒, 6秒, 9秒から開始
        duration = 4.0  # 十分な長さ
        
        img_clip = ImageClip(img_array).with_start(start_time).with_duration(duration)
        # フェードは後で追加
        img_clip = img_clip.with_position("center")
        
        print(f"画像{i+1}: start={start_time}s, duration={duration}s, size={img_clip.size}")
        image_clips.append(img_clip)
    
    # 合成テスト
    all_clips = [bg_clip] + image_clips
    video = CompositeVideoClip(all_clips, size=(1920, 1080))
    
    print(f"最終動画長: {video.duration}s")
    
    # 各時間点でのフレームを確認
    test_times = [0, 2.5, 3.5, 6.5, 9.5, 12.5]
    for t in test_times:
        if t < video.duration:
            frame = video.get_frame(t)
            print(f"時間{t}s: フレーム取得成功 {frame.shape}")
        else:
            print(f"時間{t}s: 動画長を超えています")
    
    # クリーンアップ
    video.close()
    bg_clip.close()
    for clip in image_clips:
        clip.close()
    
    print("\n=== テスト完了 ===")
    
except Exception as e:
    print(f"❌ テスト実行エラー: {e}")
    print(f"エラータイプ: {type(e).__name__}")
