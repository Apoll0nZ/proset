# Black Background Issue - Analysis and Fix

## Problem
黒塗りの背景画像が使用されているケースが発生していました。

## Root Cause Analysis

背景動画の生成パイプライン：
```
create_background_clip()
    ↓
download_random_background_video() ← ここで失敗
    ↓ (失敗)
process_background_video_for_hd()
    ↓ (失敗)
create_fallback_background()
    ↓
ColorClip(0, 0, 0) → 黒塗り背景
```

### 失敗ポイント

#### 1. S3ダウンロード失敗の原因候補
```
理由A: assets/ フォルダが存在しない
       → S3_BUCKET/assets/ が作成されていない

理由B: s*.mp4 ファイルがない
       → assets/ にはあるがファイル名が s*.mp4 でない
       → 例: background.mp4, video.mp4 など

理由C: S3へのアクセス権限がない
       → AWS_ACCESS_KEY_ID/SECRET が無効
       → IAMポリシーが不足

理由D: S3_BUCKET 環境変数が設定されていない
       → SCRIPT_S3_BUCKET env var missing
```

#### 2. 動画処理失敗の原因候補
```
理由A: MP4ファイルが破損している
理由B: Video codec がサポートされていない
理由C: ffmpeg がインストールされていない
理由D: VideoFileClip のロード失敗
```

## Solutions Implemented

### 1. 詳細なエラーログ追加
**ファイル**: `video_engine/render_video.py`

#### create_background_clip() の改善
```python
# BEFORE: 失敗理由不明
except Exception as e:
    print(f"[BACKGROUND ERROR] Failed: {e}")
    return ColorClip(...)  # 黒塗り

# AFTER: 詳細な段階別ログ
[Step 1 FAILED] No background video downloaded from S3
[Step 1 FAILED] Downloaded file does not exist
[Step 2 FAILED] Background processing returned None
[Fallback] Creating solid color background...
```

#### download_random_background_video() の改善
```python
# 失敗時に詳細情報を提示
[BACKGROUND] ERROR: No objects found in assets/ folder
[BACKGROUND] Please ensure S3_BUCKET is correctly configured

[BACKGROUND] WARNING: No s*.mp4 files found
[BACKGROUND] Available MP4 files in assets/:
                        - assets/background.mp4
                        - assets/video001.mp4
```

#### process_background_video_for_hd() の改善
```python
# 各ステップで失敗を検出
[BACKGROUND] ERROR: File does not exist: ...
[BACKGROUND] ERROR: Failed to load video: ...
[BACKGROUND] WARNING: Failed to trim video: ...

# 失敗時は None を返す（例外ではなく）
→ フォールバック処理へ移行
```

### 2. フォールバック背景の改善
```python
# BEFORE: 純粋な黒
color = (0, 0, 0)  # Pure black

# AFTER: ダークグレー（より見やすい）
color = (20, 20, 20)  # Dark gray

# さらに改善の可能性
color = (30, 30, 40)  # Dark blue-gray (more professional)
```

### 3. S3診断ツール
**ファイル**: `diagnose_s3_background.py`

実行コマンド：
```bash
python3 diagnose_s3_background.py
```

出力例：
```
=== 正常な場合 ===
3. Contents of assets/ folder:
   Found 5 objects:

   s*.mp4 files (Target for background):
      ✓ assets/scene1.mp4 (150.25 MB)
      ✓ assets/sample_bg.mp4 (200.50 MB)
   Total s*.mp4 files: 2
   ✓ Background videos available: 2

=== エラーがある場合 ===
   ✗ NO s*.mp4 FILES FOUND!
   This is the root cause of black backgrounds.

   SOLUTION:
   - Upload MP4 files starting with 's' to S3
   - Example: assets/sample1.mp4, assets/scene1.mp4
```

## How to Fix Black Backgrounds

### ステップ1: 原因を特定
```bash
# ローカルテスト環境で実行
python3 diagnose_s3_background.py

# または GitHub Actions のログを確認
# "[BACKGROUND] ERROR: No objects found in assets/ folder"
# "[BACKGROUND] WARNING: No s*.mp4 files found"
```

### ステップ2: S3 を確認・修正

#### ケースA: assets/ フォルダがない
```bash
# AWS CLIで確認
aws s3 ls s3://your-bucket/assets/

# フォルダを作成
aws s3api put-object --bucket your-bucket --key assets/
```

#### ケースB: s*.mp4 ファイルがない
```bash
# 現在のファイルを確認
aws s3 ls s3://your-bucket/assets/

# s で始まるMP4ファイルをアップロード
aws s3 cp your_background_video.mp4 s3://your-bucket/assets/scene1.mp4
aws s3 cp another_video.mp4 s3://your-bucket/assets/sample_bg.mp4
```

#### ケースC: 権限がない
```bash
# AWS IAMでS3权限を確認
# 必要な権限:
# - s3:ListBucket
# - s3:GetObject
```

### ステップ3: 検証
```bash
# 背景が正しく読み込まれているか確認
python3 diagnose_s3_background.py

# ✓ Background videos available: 2
# のメッセージが表示されたら OK
```

## Code Flow - After Fix

```
render_video.py main()
    ↓
create_background_clip()
    ├─ Step 1: download_random_background_video()
    │    ├─ S3接続確認
    │    ├─ assets/ フォルダ確認
    │    ├─ s*.mp4 ファイル検索
    │    ├─ ファイルサイズ確認
    │    └─ ダウンロード実行
    │        ├─ SUCCESS: bg_clip を返す
    │        └─ FAILED → None を返す
    │
    ├─ Step 2 (if Step 1 failed): process_background_video_for_hd()
    │    ├─ ファイル存在確認
    │    ├─ VideoFileClip ロード
    │    ├─ フレーム率・位置設定
    │    └─ 機能確認
    │        ├─ SUCCESS: bg_clip を返す
    │        └─ FAILED → None を返す
    │
    └─ Fallback (if Steps 1-2 failed):
        create_fallback_background()
            └─ ColorClip(20, 20, 20) ダークグレー
```

## Logging Output

### 正常時
```
=== Creating Background Clip ===
[Step 1] Attempting to download background video from S3...
[BACKGROUND] Listing s*.mp4 files in s3://bucket/assets/
[BACKGROUND] Found 3 total objects in assets/ folder
[BACKGROUND] Selected from 1 available s*.mp4 files: assets/scene1.mp4
[BACKGROUND] Downloading from S3: s3://bucket/assets/scene1.mp4
[BACKGROUND] Successfully downloaded: 150.25 MB

[Step 2] Processing background video: /tmp/tmpXXXX/scene1.mp4
[BACKGROUND] Loading video with VideoFileClip
[BACKGROUND] VideoFileClip loaded successfully
[BACKGROUND] Duration: 30.00s, Size: (1920, 1080)
[BACKGROUND] SUCCESS: Background clip is functional
[SUCCESS] Background clip created successfully
```

### エラー時
```
=== Creating Background Clip ===
[Step 1] Attempting to download background video from S3...
[BACKGROUND] ERROR: No objects found in assets/ folder
[Step 1 FAILED] No background video downloaded from S3
[Fallback] Creating solid color background...
[SUCCESS] Fallback background created: 1920x1080, duration=30.00s, color=RGB(20, 20, 20)
```

## Files Modified

1. **video_engine/render_video.py**
   - Lines 835-876: Enhanced `create_background_clip()`
   - Lines 879-909: New `create_fallback_background()`
   - Lines 1175-1253: Enhanced `download_random_background_video()`
   - Lines 1314-1383: Enhanced `process_background_video_for_hd()`

2. **diagnose_s3_background.py** (NEW)
   - S3 diagnostics and troubleshooting tool

## Testing

```bash
# ローカルで診断実行
python3 diagnose_s3_background.py

# GitHub Actionsのログで確認
# [BACKGROUND] SUCCESS: Background clip is functional
```

## Summary

黒背景が出現する原因は S3 に背景動画ファイルが存在しない、またはキー名が `s*.mp4` パターンに一致していません。

改善内容：
- ✅ エラー原因を明確にするログを追加
- ✅ フォールバック背景を改善（黒 → ダークグレー）
- ✅ S3 診断ツールで問題を特定可能に

黒背景が出現した場合は、`diagnose_s3_background.py` を実行して原因を確認し、必要に応じて S3 に背景動画ファイルをアップロードしてください。
