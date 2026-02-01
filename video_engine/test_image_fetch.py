#!/usr/bin/env python3
"""
画像取得プロセスのデバッグ用スクリプト
Playwrightによる画像検索とダウンロードのロジックを単体テストする
"""

import os
import sys
import tempfile
import time
import hashlib
from typing import List, Dict, Optional
from urllib.parse import urlparse

# video_engineモジュールをインポートするためのパス設定
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

def test_search_images_with_playwright(keyword: str, max_results: int = 5) -> List[Dict[str, str]]:
    """PlaywrightでGoogle画像検索（デバッグ強化版）"""
    try:
        from playwright.sync_api import sync_playwright
        
        print(f"[DEBUG] 検索キーワード: '{keyword}'")
        print(f"[DEBUG] 最大取得数: {max_results}")
        
        with sync_playwright() as p:
            try:
                # ヘッドレスブラウザ起動
                print("[DEBUG] ブラウザを起動します...")
                browser = p.chromium.launch(headless=True)
            except Exception as e:
                print(f"[ERROR] ブラウザ起動失敗: {e}")
                return []
            
            try:
                context = browser.new_context()
                page = context.new_page()
                
                # ブロック回避のため軽く待機
                time.sleep(0.5)
                
                # Google画像検索ページへ
                search_url = f"https://www.google.com/search?q={keyword}&tbm=isch"
                print(f"[DEBUG] 検索URL: {search_url}")
                
                print("[DEBUG] ページにアクセスします...")
                page.goto(search_url)
                
                print("[DEBUG] ページの読み込みを待機します...")
                page.wait_for_load_state('networkidle')
                
                # 検索結果が表示されるまで待機
                page.wait_for_timeout(2000)
                page.wait_for_load_state('networkidle')
                
                # ページのタイトルを確認
                page_title = page.title()
                print(f"[DEBUG] ページタイトル: {page_title}")
                
                # 画像要素を収集
                print("[DEBUG] 画像要素を検索します...")
                image_elements = page.query_selector_all('img[src]')
                print(f"[DEBUG] 見つかったimg要素の数: {len(image_elements)}")
                
                # 別のセレクタも試す
                all_images = page.query_selector_all('img')
                print(f"[DEBUG] 全てのimg要素の数: {len(all_images)}")
                
                # aタグ内の画像も検索
                a_tags = page.query_selector_all('a[href]')
                print(f"[DEBUG] aタグの数: {len(a_tags)}")
                
                images = []
                processed_urls = set()
                
                for i, img in enumerate(image_elements[:max_results * 2]):  # 余裕を持って探索
                    try:
                        src = img.get_attribute('src')
                        alt = img.get_attribute('alt') or ''
                        
                        print(f"[DEBUG] 画像要素 {i+1}: src={src[:100] if src else 'None'}, alt={alt[:50]}")
                        
                        if src and src.startswith('http'):
                            # base64でない画像URLのみ処理
                            if 'base64' not in src and src not in processed_urls:
                                processed_urls.add(src)
                                
                                # URLからファイル拡張子をチェック
                                parsed_url = urlparse(src)
                                path = parsed_url.path.lower()
                                
                                # 画像拡張子のチェック
                                valid_extensions = ['.jpg', '.jpeg', '.png', '.webp', '.avif', '.gif', '.bmp']
                                is_valid_image = any(ext in path for ext in valid_extensions)
                                
                                # encrypted-tbn0.gstatic.comのURLも許可（Google画像検索のサムネイル）
                                is_google_thumbnail = 'encrypted-tbn0.gstatic.com' in src
                                
                                # Content-Typeに画像関連のキーワードが含まれる場合も許可
                                has_image_keyword = any(img_type in src.lower() for img_type in ['jpg', 'jpeg', 'png', 'webp', 'gif'])
                                
                                if is_valid_image or is_google_thumbnail or has_image_keyword:
                                    images.append({
                                        'url': src,
                                        'title': f'Image {i+1} for {keyword}',
                                        'thumbnail': src,
                                        'alt': alt,
                                        'is_google_thumbnail': is_google_thumbnail
                                    })
                                    print(f"[DEBUG] 有効な画像を追加: {src[:100]}... (google_thumbnail: {is_google_thumbnail})")
                                    
                                    if len(images) >= max_results:
                                        break
                    except Exception as e:
                        print(f"[DEBUG] 画像要素 {i+1} の処理でエラー: {e}")
                        continue
                
                # もし画像が見つからない場合は、別のアプローチを試す
                if not images:
                    print("[DEBUG] 標準的なimg要素で画像が見つからなかったため、別の方法を試します...")
                    
                    # data-src属性を持つ要素を検索
                    data_src_elements = page.query_selector_all('img[data-src]')
                    print(f"[DEBUG] data-srcを持つimg要素の数: {len(data_src_elements)}")
                    
                    for i, img in enumerate(data_src_elements[:max_results]):
                        try:
                            src = img.get_attribute('data-src')
                            if src and src.startswith('http'):
                                images.append({
                                    'url': src,
                                    'title': f'Data-src Image {i+1} for {keyword}',
                                    'thumbnail': src,
                                    'alt': img.get_attribute('alt') or ''
                                })
                                print(f"[DEBUG] data-srcから画像を追加: {src[:100]}...")
                        except Exception as e:
                            print(f"[DEBUG] data-src要素 {i+1} の処理でエラー: {e}")
                            continue
                
                browser.close()
                print(f"[DEBUG] 最終的に見つかった画像の数: {len(images)}")
                return images
                
            except Exception as e:
                print(f"[ERROR] ブラウザ操作中のエラー: {e}")
                try:
                    browser.close()
                except:
                    pass
                return []
            
    except ImportError:
        print("[ERROR] Playwrightがインストールされていません")
        print("インストールコマンド: pip install playwright")
        print("ブラウザインストールコマンド: playwright install chromium")
        return []
    except Exception as e:
        print(f"[ERROR] 画像検索全体のエラー: {e}")
        print(f"[ERROR] エラータイプ: {type(e).__name__}")
        return []


def test_download_image_from_url(image_url: str, filename: str = None, temp_dir: str = None) -> Optional[str]:
    """URLから画像をダウンロードしてテスト（デバッグ強化版）"""
    try:
        import requests
        
        if not image_url:
            print("[ERROR] 画像URLが空です")
            return None
        
        print(f"[DEBUG] ダウンロード対象URL: {image_url}")
        
        # URLの検証
        if image_url.lower().endswith(".svg"):
            print(f"[DEBUG] SVGファイルはスキップします: {image_url}")
            return None
        
        if not filename:
            # URLからファイル名を生成
            url_hash = hashlib.md5(image_url.encode()).hexdigest()[:8]
            ext = os.path.splitext(image_url.split("?")[0])[1].lower()
            if ext not in [".jpg", ".jpeg", ".png", ".webp", ".avif", ".gif"]:
                # 拡張子がない場合はContent-Typeから判断
                ext = ".jpg"  # デフォルト
            filename = f"test_image_{url_hash}{ext}"
        
        if not temp_dir:
            temp_dir = tempfile.mkdtemp()
        
        local_path = os.path.join(temp_dir, filename)
        print(f"[DEBUG] 保存先パス: {local_path}")
        
        print("[DEBUG] HTTPリクエストを送信します...")
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        
        response = requests.get(image_url, timeout=30, headers=headers)
        print(f"[DEBUG] HTTPステータスコード: {response.status_code}")
        print(f"[DEBUG] Content-Type: {response.headers.get('Content-Type', 'Unknown')}")
        print(f"[DEBUG] Content-Length: {response.headers.get('Content-Length', 'Unknown')} bytes")
        
        response.raise_for_status()
        
        # 画像をローカルに保存
        with open(local_path, 'wb') as f:
            f.write(response.content)
        
        file_size = os.path.getsize(local_path) if os.path.exists(local_path) else 0
        print(f"[DEBUG] 保存完了: ファイルサイズ={file_size} bytes")
        
        # 画像のフォーマット検証
        try:
            from PIL import Image
            with Image.open(local_path) as img:
                print(f"[DEBUG] 画像検証成功: format={img.format}, mode={img.mode}, size={img.size}")
                img.verify()
                print(f"[DEBUG] 画像検証完了: 有効な画像ファイルです")
        except ImportError:
            print("[DEBUG] PILがインストールされていないため、画像検証をスキップします")
        except Exception as e:
            print(f"[ERROR] 画像検証失敗: {e}")
            os.remove(local_path)
            return None
        
        print(f"[DEBUG] 画像ダウンロード成功: {local_path}")
        return local_path
        
    except requests.exceptions.HTTPError as e:
        print(f"[ERROR] HTTPエラー: {e}")
        print(f"[ERROR] レスポンス: {e.response.text[:200] if hasattr(e, 'response') and e.response else 'No response'}")
        return None
    except requests.exceptions.Timeout as e:
        print(f"[ERROR] タイムアウトエラー: {e}")
        return None
    except Exception as e:
        print(f"[ERROR] 画像ダウンロード全体のエラー: {e}")
        print(f"[ERROR] エラータイプ: {type(e).__name__}")
        return None


def main():
    """メインテスト実行関数"""
    print("=" * 60)
    print("画像取得プロセス デバッグテスト")
    print("=" * 60)
    
    # テスト用キーワード
    test_keywords = [
        "AI技術",
        "テクノロジー",
        "コンピュータ",
        "software development"
    ]
    
    temp_dir = tempfile.mkdtemp()
    print(f"[DEBUG] 一時ディレクトリ: {temp_dir}")
    
    for keyword in test_keywords:
        print(f"\n{'=' * 40}")
        print(f"テストキーワード: {keyword}")
        print(f"{'=' * 40}")
        
        # 画像検索テスト
        images = test_search_images_with_playwright(keyword, max_results=3)
        
        if images:
            print(f"\n[SUCCESS] {len(images)}個の画像が見つかりました")
            
            # 画像ダウンロードテスト
            for i, image_info in enumerate(images):
                print(f"\n--- 画像 {i+1} のダウンロードテスト ---")
                downloaded_path = test_download_image_from_url(
                    image_info['url'], 
                    temp_dir=temp_dir
                )
                
                if downloaded_path:
                    print(f"[SUCCESS] ダウンロード成功: {downloaded_path}")
                else:
                    print(f"[FAIL] ダウンロード失敗: {image_info['url']}")
        else:
            print("[FAIL] 画像が見つかりませんでした")
    
    print(f"\n{'=' * 60}")
    print(f"テスト完了。一時ディレクトリ: {temp_dir}")
    print(f"ダウンロードされたファイル:")
    for file in os.listdir(temp_dir):
        file_path = os.path.join(temp_dir, file)
        if os.path.isfile(file_path):
            size = os.path.getsize(file_path)
            print(f"  - {file} ({size} bytes)")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
