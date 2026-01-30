# DynamoDBテーブル設計変更案

## 現在のテーブル設計
```
- url (文字列) - パーティションキー
- content_hash (文字列)
- processed_at (文字列)
- status (文字列)
- title (文字列)
- ttl (数値)
```

## 新しいテーブル設計（scoreフィールド追加）
```
- url (文字列) - パーティションキー
- content_hash (文字列)
- processed_at (文字列)
- status (文字列) - "evaluated", "selected"
- title (文字列)
- ttl (数値)
- score (数値) - 新規追加：0-100の評価点数
```

## 変更点
1. **scoreフィールドの追加**：Geminiによる評価点数（0-100）を保存
2. **statusの意味合い変更**：
   - "evaluated"：評価済み（基準点以上・以下を問わず）
   - "selected"：動画化済み

## 移行スクリプト（既存データ対応）
既存データはscoreフィールドなしで保存されており、新しいロジックではscoreがない場合に0.0として扱うため、即時の移行は不要。

## インデックス考慮事項
将来的に「score + processed_at」でのクエリを最適化する場合、GSI（Global Secondary Index）の追加を検討：
- パーティションキー：status
- ソートキー：score
- プロジェクション：processed_at, url, title
