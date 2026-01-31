#!/usr/bin/env python3
"""
動画生成品質チェックスクリプト
生成された動画が真っ黒でないかを自動検証
"""

import os
import sys
import cv2
import numpy as np
from pathlib import Path

def check_video_file_size(video_path, min_size_mb=1):
    """動画ファイルのサイズチェック"""
    try:
        file_size = os.path.getsize(video_path)
        file_size_mb = file_size / (1024 * 1024)
        
        print(f"[TEST] ファイルサイズ: {file_size_mb:.2f} MB")
        
        if file_size_mb < min_size_mb:
            print(f"[ERROR] ファイルサイズが小さすぎます: {file_size_mb:.2f} MB < {min_size_mb} MB")
            return False
        
        print(f"[PASS] ファイルサイズが正常です: {file_size_mb:.2f} MB")
        return True
        
    except Exception as e:
        print(f"[ERROR] ファイルサイズチェック失敗: {e}")
        return False

def calculate_frame_brightness(frame):
    """フレームの平均輝度を計算"""
    # グレースケールに変換
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    # 平均輝度を計算（0-255）
    brightness = np.mean(gray)
    return brightness

def check_video_frames(video_path, test_times=[3, 10, 30], min_brightness=10):
    """動画フレームの輝度チェック"""
    try:
        cap = cv2.VideoCapture(video_path)
        
        if not cap.isOpened():
            print(f"[ERROR] 動画ファイルを開けません: {video_path}")
            return False
        
        # 動画情報を取得
        fps = cap.get(cv2.CAP_PROP_FPS)
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        duration = total_frames / fps
        
        print(f"[TEST] 動画情報: FPS={fps}, 総フレーム数={total_frames}, 長さ={duration:.2f}s")
        
        all_passed = True
        
        for time_sec in test_times:
            if time_sec > duration:
                print(f"[SKIP] {time_sec}秒 > 動画長({duration:.2f}s)、チェックをスキップ")
                continue
            
            # 指定時間のフレーム番号を計算
            frame_number = int(time_sec * fps)
            
            # フレームをシーク
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame_number)
            ret, frame = cap.read()
            
            if not ret:
                print(f"[ERROR] {time_sec}秒のフレームを読み込めません")
                all_passed = False
                continue
            
            # 輝度を計算
            brightness = calculate_frame_brightness(frame)
            print(f"[TEST] Frame at {time_sec}s brightness: {brightness:.1f} / 255")
            
            # 輝度チェック
            if brightness < min_brightness:
                print(f"[ERROR] {time_sec}秒のフレームが真っ黒です: brightness={brightness:.1f} < {min_brightness}")
                all_passed = False
            else:
                print(f"[PASS] {time_sec}秒のフレームは正常です: brightness={brightness:.1f}")
        
        cap.release()
        return all_passed
        
    except Exception as e:
        print(f"[ERROR] フレームチェック失敗: {e}")
        return False

def main():
    """メイン処理"""
    video_path = "video.mp4"
    
    print("=== 動画品質自動チェック ===")
    print(f"[INFO] チェック対象: {video_path}")
    
    # ファイル存在確認
    if not os.path.exists(video_path):
        print(f"[ERROR] 動画ファイルが存在しません: {video_path}")
        sys.exit(1)
    
    # 1. ファイルサイズチェック
    size_ok = check_video_file_size(video_path)
    if not size_ok:
        print("[FAIL] ファイルサイズチェックに失敗しました")
        sys.exit(1)
    
    # 2. フレーム輝度チェック
    frames_ok = check_video_frames(video_path)
    if not frames_ok:
        print("[FAIL] フレーム輝度チェックに失敗しました")
        sys.exit(1)
    
    print("\n[PASS] すべての品質チェックに合格しました")
    print("✅ 動画は正常に生成されています")
    sys.exit(0)

if __name__ == "__main__":
    main()
