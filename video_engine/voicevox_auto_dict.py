import requests
import json
import time
from typing import Dict, Any

def _get_registered_words(api_url: str) -> Dict[str, Dict[str, Any]]:
    """VOICEVOXに登録済みの単語一覧を取得"""
    try:
        response = requests.get(f"{api_url}/dict", timeout=30)
        if response.status_code == 200:
            return response.json()
        else:
            print(f"[VOICEVOX DICT] Failed to get registered words: {response.status_code}")
            return {}
    except Exception as e:
        print(f"[VOICEVOX DICT] Error getting registered words: {e}")
        return {}

def _register_word(api_url: str, surface: str, pronunciation: str, accent_associative_list: list = None) -> bool:
    """VOICEVOXに単語を登録"""
    if accent_associative_list is None:
        accent_associative_list = [[0, 0]]
    
    try:
        word_data = {
            "surface": surface,
            "pronunciation": pronunciation,
            "accent_associative_list": accent_associative_list
        }
        
        response = requests.post(f"{api_url}/dict", params={"word": json.dumps(word_data)}, timeout=30)
        if response.status_code == 200:
            print(f"[VOICEVOX DICT] Registered word: {surface} -> {pronunciation}")
            return True
        else:
            print(f"[VOICEVOX DICT] Failed to register word: {surface}, status: {response.status_code}")
            return False
    except Exception as e:
        print(f"[VOICEVOX DICT] Error registering word: {surface}, error: {e}")
        return False

def sync_pronunciation_dict(pronunciation_dict: Dict[str, str], api_url: str) -> None:
    """
    発音辞書を同期する
    
    Args:
        pronunciation_dict: 単語と読み方の辞書 {'単語': '読み方'}
        api_url: VOICEVOX API URL
    """
    if not pronunciation_dict:
        print("[VOICEVOX DICT] No pronunciation dictionary provided")
        return
    
    print(f"[VOICEVOX DICT] Syncing {len(pronunciation_dict)} words...")
    
    # 登録済みの表記一覧を取得
    registered = _get_registered_words(api_url)
    existing_surfaces = {info["surface"] for info in registered.values()}
    
    added = 0
    skipped = 0
    failed = 0
    
    # 未登録のワードのみ追加
    for surface, pronunciation in pronunciation_dict.items():
        if surface in existing_surfaces:
            skipped += 1
            print(f"[VOICEVOX DICT] Skip existing word: {surface}")
            continue
        
        if _register_word(api_url, surface, pronunciation):
            added += 1
            # 少し待機してAPI負荷を分散
            time.sleep(0.1)
        else:
            failed += 1
    
    print(f"[VOICEVOX DICT] Sync completed: Added {added}, Skipped {skipped}, Failed {failed}")

if __name__ == "__main__":
    # 単体実行用
    import os
    
    api_url = os.environ.get("VOICEVOX_API_URL", "http://localhost:50021")
    
    # テスト用の辞書
    test_dict = {
        "テスト": "てすと",
        "API": "えーぴーあい",
    }
    
    sync_pronunciation_dict(test_dict, api_url)
