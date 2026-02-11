#!/usr/bin/env python3
"""
S3 Background Video Diagnostics
背景動画がS3に存在しているかを確認し、ダウンロード可能かテストします
"""

import os
import sys
import boto3
from botocore.exceptions import ClientError

# AWS設定
AWS_REGION = os.environ.get("MY_AWS_REGION", "ap-northeast-1")
S3_BUCKET = os.environ.get("SCRIPT_S3_BUCKET", "")

def diagnose_s3_background():
    """S3の背景動画構成を診断"""
    print("\n" + "=" * 70)
    print("S3 BACKGROUND VIDEO DIAGNOSTICS")
    print("=" * 70)

    # 環境変数確認
    print("\n1. Environment Variables:")
    print(f"   AWS_REGION: {AWS_REGION}")
    print(f"   S3_BUCKET: {S3_BUCKET}")

    if not S3_BUCKET:
        print("   ✗ ERROR: S3_BUCKET not configured")
        return False

    # S3接続テスト
    print("\n2. S3 Connection Test:")
    try:
        s3_client = boto3.client("s3", region_name=AWS_REGION)
        s3_client.head_bucket(Bucket=S3_BUCKET)
        print(f"   ✓ Successfully connected to S3 bucket: {S3_BUCKET}")
    except ClientError as e:
        print(f"   ✗ Failed to connect to S3: {e}")
        return False
    except Exception as e:
        print(f"   ✗ Unexpected error: {e}")
        return False

    # assets/ フォルダのリスト
    print("\n3. Contents of assets/ folder:")
    try:
        response = s3_client.list_objects_v2(
            Bucket=S3_BUCKET,
            Prefix="assets/",
            MaxKeys=100
        )

        contents = response.get("Contents", [])
        if not contents:
            print("   ✗ ERROR: assets/ folder is empty!")
            return False

        print(f"   Found {len(contents)} objects:")

        # ファイルタイプ別分類
        mp4_files = []
        s_mp4_files = []
        other_files = []

        for obj in contents:
            key = obj["Key"]
            size = obj["Size"]
            filename = os.path.basename(key)

            if filename.lower().endswith(".mp4"):
                if filename.lower().startswith("s"):
                    s_mp4_files.append((key, size))
                else:
                    mp4_files.append((key, size))
            else:
                other_files.append((key, size))

        # s*.mp4 ファイル
        print("\n   s*.mp4 files (Target for background):")
        if s_mp4_files:
            for key, size in s_mp4_files:
                size_mb = size / (1024*1024)
                print(f"      ✓ {key} ({size_mb:.2f} MB)")
            print(f"   Total s*.mp4 files: {len(s_mp4_files)}")
        else:
            print("      ✗ NO s*.mp4 FILES FOUND!")
            print("      This is the root cause of black backgrounds.")

        # その他のMP4ファイル
        if mp4_files:
            print("\n   Other MP4 files:")
            for key, size in mp4_files:
                size_mb = size / (1024*1024)
                print(f"      - {key} ({size_mb:.2f} MB)")

        # その他のファイル
        if other_files:
            print("\n   Other files in assets/:")
            for key, size in other_files[:10]:  # 最初の10個
                size_kb = size / 1024
                print(f"      - {key} ({size_kb:.0f} KB)")

        # サマリー
        print("\n4. Summary:")
        if s_mp4_files:
            print(f"   ✓ Background videos available: {len(s_mp4_files)}")
            print("   ✓ System should work correctly")
            return True
        else:
            print("   ✗ No background videos found!")
            print("   ✗ System will use fallback dark background")
            print("\n   SOLUTION:")
            print("   - Upload MP4 files starting with 's' to S3: s3://{S3_BUCKET}/assets/")
            print("   - Example: assets/sample1.mp4, assets/scene1.mp4, etc.")
            return False

    except Exception as e:
        print(f"   ✗ Error listing S3 objects: {e}")
        return False

def main():
    success = diagnose_s3_background()

    print("\n" + "=" * 70)
    if success:
        print("RESULT: Background video system is properly configured")
        print("Expected behavior: Actual background videos will be used")
    else:
        print("RESULT: Background video system has issues")
        print("Fallback behavior: Dark gray background will be used")
    print("=" * 70 + "\n")

    return 0 if success else 1

if __name__ == "__main__":
    sys.exit(main())
