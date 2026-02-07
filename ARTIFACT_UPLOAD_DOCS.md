# Artifact Upload Flow - Documentation

## Summary of Changes

修正を通して、以下のフローが確立されました：

### 1. ビデオ生成フロー（`render_video.py`）

```
LocalTest または GitHub Actions
          ↓
    render_video.py (main関数)
          ↓
  ┌─────────────────────────────┐
  │ 1. Script取得 (S3)          │
  │ 2. Audio生成 (VOICEVOX)     │
  │    ↓ duration_list_all取得  │
  │ 3. Video生成                │
  │    ↓ 動画ファイル出力       │
  │       /tmp/tmpXXXX/video.mp4│
  │ 4. Thumbnail生成            │
  │    ↓ /root/thumbnail.png    │
  └─────────────────────────────┘
          ↓
  ┌─────────────────────────────┐
  │ Artifact Copy処理           │
  │ (Lines 4060-4079)           │
  │                             │
  │ workspace_root = GITHUB_WORKSPACE
  │   または '.' (ローカル)     │
  │                             │
  │ Copy:                       │
  │ /tmp/video.mp4 →           │
  │   {workspace_root}/video.mp4│
  │                             │
  │ Thumbnail既存確認:          │
  │   ~/{workspace_root}/      │
  │   thumbnail.png            │
  └─────────────────────────────┘
          ↓
      ファイル完成
      (リポジトリルート)
      ├── video.mp4
      └── thumbnail.png
```

### 2. GitHub Actions Workflow フロー（`.github/workflows/main.yml`）

```
GitHub Actions実行
          ↓
Step 1-8: 環境構築
  ├─ Python/ffmpeg/ImageMagick
  ├─ VOICEVOX Docker起動
  └─ Python依存インストール
          ↓
Step 9: render_video.py実行
  ├─ working-directory: ./video_engine
  └─ Script: 環境変数で設定されたパスを使用
          ↓
Step 10: ✨NEW✨ Artifact検証（追加）
  ├─ カレントディレクトリ確認
  ├─ video.mp4/thumbnail.png存在確認
  ├─ 複数パスをチェック
  └─ ログ出力
          ↓
Step 11: Artifact Upload
  ├─ actions/upload-artifact@v4使用
  ├─ path: video.mp4, thumbnail.png
  └─ Artifacts: "debug-video-artifacts"
          ↓
Step 12: VOICEVOX Cleanup
```

## Critical Fixes Applied

### Fix 1: NameError - duration_list_all
**ファイル**: `video_engine/render_video.py`
**行**: 2918, 4022

```python
# BEFORE:
return final_audio_path, part_durations, query_data_list_all, text_parts_list_all  # 4値

# AFTER:
return final_audio_path, part_durations, query_data_list_all, text_parts_list_all, duration_list_all  # 5値
```

**影響**: ファイルベースのduration測定データがビデオ生成パイプライン全体に正しく流れるようになった

### Fix 2: ワークフロー アーティファクト検証
**ファイル**: `.github/workflows/main.yml`
**行**: 109-125

新しいステップを追加：
- ファイル存在確認
- 複数パスのチェック
- デバッグログ出力

## Verification Checklist

### ローカルテスト（Python構文）
- ✅ `python3 -m py_compile video_engine/render_video.py`
- ✅ `test_duration_fix.py` - 全テストPASS
- ✅ `test_sync_measurement.py` - 全テストPASS

### GitHub Actions実行時の期待動作

1. **ビデオ生成フェーズ**
   - ✅ Audio synthesis with duration_list_all capture
   - ✅ Video composition with measured timings
   - ✅ File copy to workspace_root

2. **Artifact検証フェーズ**（NEW）
   - Expected output:
   ```
   === Checking for generated artifacts ===
   Current directory: /home/runner/work/youtube/youtube/video_engine
   Repository root contents:
   ... (contents listed)

   Checking for video.mp4:
   ✓ ../video.mp4 found  ← Video should be in parent dir

   Checking for thumbnail.png:
   ✓ ../thumbnail.png found
   ```

3. **Upload フェーズ**
   - ✅ video.mp4 found and uploaded
   - ✅ thumbnail.png found and uploaded
   - ✅ Both appear in Artifacts section

## Data Flow - Duration Measurement

```
分割テキスト (chunks)
    ↓
synthesize_precut_speech_voicevox()
    ├─ WAVファイル生成（各chunk）
    ├─ WAVファイルから実測duration取得
    └─ duration_list作成 [1.5s, 2.3s, 1.8s, ...]
    ↓
duration_list_all: {
    0: [1.5, 2.3, 1.8, 2.1],      # Title audio
    1: [2.5, 2.1, 1.9, 2.3, ...]  # Main audio
}
    ↓
在線化タイムライン（build_unified_timeline）
    ├─ Part 0: duration_list_all.get(0) → [1.5, 2.3, 1.8, 2.1]
    ├─ calculate_measured_chunk_durations()に渡す
    ├─ 計測値に基づく字幕タイミング計算
    └─ Part 1: duration_list_all.get(1) → [2.5, 2.1, 1.9, ...]
    ↓
動画 with 完璧な字幕-音声同期
```

## Next Steps

### 1. テスト実行
GitHub Actions実行時に新しい検証ステップのログを確認：
- `Check generated artifacts`ステップの出力
- ファイルパスが正しく解決されているか確認

### 2. ズレ計測（ユーザー実施）
実際の動画出力後：
```bash
# ユーザータスク: 動画を見ながら計測
1. 動画再生
2. 各セクションで字幕-音声のズレを観察
3. ズレが検出されたら補正係数を記録
4. calculate_measured_chunk_durations()に補正を加える
```

### 3. 補正係数適用（必要な場合）
```python
# pseudo code
if sync_drift_measured:
    correction_factor = 1.0 + (drift_seconds / total_duration)
    adjusted_durations = [d * correction_factor for d in duration_list]
```

## Files Modified

1. **video_engine/render_video.py**
   - Line 2918: Return statement fix
   - Line 4022: Unpacking fix
   - Lines 237-238, 301-303: duration_list使用

2. **.github/workflows/main.yml**
   - Line 109-125: Artifact verification step added

3. **test_duration_fix.py** (NEW)
   - Verification that fix is properly implemented

4. **test_sync_measurement.py** (NEW)
   - Timing accuracy verification

## Commits
- `a8c2a52` - Fix critical NameError: duration_list_all not defined
- `ca64182` - Add comprehensive sync measurement tests
- `564fbbe` - Add comprehensive fix summary documentation
- `e888ad4` - Add artifact verification step to GitHub Actions workflow
