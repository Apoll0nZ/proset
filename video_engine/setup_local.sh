#!/bin/bash

echo "=== ローカル環境セットアップ ==="

# 1. Python依存関係のインストール
echo "1. Python依存関係をインストールします..."
pip install --upgrade pip

# MoviePy 2.0以上をインストール
pip install "moviepy>=2.0.0"

# その他の依存関係
pip install -r requirements.txt

# Playwrightブラウザ
pip install playwright
playwright install chromium

echo "✅ Python依存関係のインストール完了"

# 2. ImageMagickの確認
echo "2. ImageMagickの状態を確認します..."
if command -v convert &> /dev/null; then
    echo "✅ ImageMagickがインストールされています"
    convert -version
else
    echo "❌ ImageMagickがインストールされていません"
    echo "macOSの場合: brew install imagemagick"
    echo "Ubuntuの場合: sudo apt-get install imagemagick"
fi

# 3. VOICEVOXの接続確認
echo "3. VOICEVOXの接続を確認します..."
if curl -s -f http://localhost:50021/speakers > /dev/null 2>&1; then
    echo "✅ VOICEVOXがlocalhost:50021で実行中です"
else
    echo "❌ VOICEVOXに接続できません"
    echo "Dockerで起動する場合:"
    echo "docker run -d -p 50021:50021 voicevox/voicevox_engine:cpu-ubuntu20.04-latest"
fi

echo "=== セットアップ完了 ==="
