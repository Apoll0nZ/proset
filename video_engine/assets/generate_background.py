#!/usr/bin/env python3
"""
背景画像を生成するスクリプト
"""

try:
    from PIL import Image
    import numpy as np
    
    # 1920x1080の黒背景を生成
    img = Image.new('RGB', (1920, 1080), color=(0, 0, 0))
    img.save('background.png')
    print('Generated 1920x1080 black background.png')
    
except ImportError:
    # PILやnumpyがなければ、単純なバイナリデータで生成
    # PNGヘッダ + IHDRチャンク + 1920x1080の黒い画像データ
    import struct
    
    def create_png_header():
        # PNGシグネチャ
        png_signature = b'\x89PNG\r\n\x1a\n'
        
        # IHDRチャンク (1920x1080, 8bit, RGB)
        width, height = 1920, 1080
        ihdr_data = struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)  # width, height, bit_depth, color_type, compression, filter, interlace
        ihdr_crc = 0x2144df1c  # 事前計算されたCRC
        
        ihdr_chunk = struct.pack('>I', 13) + b'IHDR' + ihdr_data + struct.pack('>I', ihdr_crc)
        
        return png_signature + ihdr_chunk
    
    # 最小限のPNGデータを作成
    png_data = create_png_header()
    
    # IDATチャンク（圧縮された画像データ - 黒い1x1ピクセル）
    import zlib
    # 1x1の黒ピクセルデータを圧縮
    raw_data = b'\x00\x00\x00\x00'  # 1x1黒ピクセル
    compressed = zlib.compress(raw_data)
    idat_chunk = struct.pack('>I', len(compressed)) + b'IDAT' + compressed + struct.pack('>I', zlib.crc32(b'IDAT' + compressed))
    
    # IENDチャンク
    iend_chunk = struct.pack('>I', 0) + b'IEND' + struct.pack('>I', zlib.crc32(b'IEND'))
    
    # 完全なPNGファイル
    full_png = png_data + idat_chunk + iend_chunk
    
    with open('background.png', 'wb') as f:
        f.write(full_png)
    
    print('Generated minimal black background.png (fallback method)')
