#!/usr/bin/env python3
"""
MoviePy v2.0 TextClipの正しい引数名を確認
"""

import sys
import os
sys.path.append('.')

try:
    from moviepy import TextClip
    import inspect
    
    print("=== MoviePy v2.0 TextClip引数確認 ===")
    
    # TextClipのシグネチャを確認
    try:
        signature = inspect.signature(TextClip.__init__)
        print("TextClip.__init__ の引数:")
        for param_name, param in signature.parameters.items():
            if param_name != 'self':
                print(f"  - {param_name}: {param.default if param.default != inspect.Parameter.empty else 'Required'}")
    except Exception as e:
        print(f"シグネature取得失敗: {e}")
    
    # ヘルプを表示
    print("\nTextClipのドキュメント:")
    try:
        help(TextClip.__init__)
    except:
        print("ドキュメント取得失敗")
    
    # 簡単なテスト
    print("\n簡単なテスト:")
    try:
        # 最小限の引数でテスト
        clip = TextClip("テスト")
        print("✅ 最小限の引数で成功")
        clip.close()
    except Exception as e:
        print(f"❌ 最小限の引数で失敗: {e}")
    
    print("\n=== テスト完了 ===")
    
except ImportError as e:
    print(f"❌ MoviePyインポートエラー: {e}")
except Exception as e:
    print(f"❌ テスト実行エラー: {e}")
