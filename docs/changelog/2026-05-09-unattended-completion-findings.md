# Unattended Completion 知見まとめ（2026-05-09）

## 結論

**対策は全て実装・デプロイ済み。ただし事象が再現していないため、対策の有効性は未検証。**

- 6時間の連続テストでハングは再現しなかった
- しかし auth_expired ログは 0 件 = 根本原因（AgentCore再起動→401）のトリガー自体が発生していない
- 「対策が問題を防いだ」のではなく「問題が起きなかった」だけ
- 対策の有効性を確認するには、401 が実際に発生した際に早期リターンが発動することを観測する必要がある

## 実装した対策と効果

| # | 事象 | 対策 | ファイル | 効果 |
|---|---|---|---|---|
| 1 | MCP接続切断 | MCPReconnect クラス（自動再接続 max 8回） | `agent/mcp_reconnect.py` | 接続切断時に自動復旧 |
| 2 | SSEエラー非表示 | strandsParser.js にエラーハンドリング | `web-ui/src/services/strandsParser.js` | エラーがUIに表示される |
| 3 | ツール実行ハング | _TOOL_TIMEOUT=360s でキャンセル | `agent/streaming.py` | 360秒で強制打ち切り |
| 4 | httpxタイムアウト | timeout 120→360 | `agent/mcp_clients.py` | Code Interpreter 300s対応 |
| 5 | Bedrock read_timeout | read_timeout 120→300 | `agent/factory.py` | adaptive thinking対応 |
| 6 | max_tokens超過 | max_tokens=128000 + adaptive thinking | `agent/model_profiles.py` | 出力打ち切り解消 |
| 7 | コールドスタート | WebUI fetch に 502/503/504 リトライ（最大2回） | `web-ui/src/services/agentCoreService.js` | 自動復旧 |
| 8 | 401永久リトライ | ExceptionGroup展開 + 401検知で即return | `agent/modes/separated/composer.py` | 無駄なリトライ回避 |
| 9 | compose_slidesハング | per-group ハードタイムアウト（slides×90s×3） | `agent/modes/separated/composer.py` | 異常時に物理打ち切り |

## デバッグ基盤

| 機能 | 場所 | 用途 |
|---|---|---|
| _compose_state 共有変数 | `composer.py` モジュールレベル dict | streaming.py keepalive で出力、ハング箇所特定 |
| compose_yield ログ | `composer.py` progress_q 出力時 | CloudWatch で進捗追跡 |
| keepalive + last_compose | `streaming.py` | SSE keepalive に compose 状態を載せる |
| prefetch_start/end ログ | `composer.py` | MCP prefetch の所要時間計測 |
| composer_start/end ログ | `composer.py` | LLM推論の所要時間計測 |

## ロードテスト結果

### テスト1: 5枚（Parallel Agents ON）
- 結果: ✅ PASS、5枚正常生成、約6分

### テスト2: 30枚（Well-Architected Framework）
- 結果: ✅ PASS、31枚全生成、約49分、ハングなし

### テスト3: 5テーマ×30枚 繰り返し（6時間連続）
- 結果: ✅ ALL PASS、ハング再現せず
- CloudWatch: auth_expired 0件、hard_timeout_fired 0件、reconnect 0件

## 設定値

| 設定 | 値 | 根拠 |
|---|---|---|
| read_timeout | 300s | Opus adaptive thinking が120s超かかる |
| mcp timeout | 360s | Code Interpreter sessionTimeout=300s + 60sマージン |
| _TOOL_TIMEOUT | 360s | MCP timeout と同じ。SSEハングのフォールバック |
| max_retries (reconnect) | 8回 | backoff合計55s。コールドスタート15-30sをカバー |
| max_tokens | 128000 | Opus 4.7/4.6 の max output |
| HARD_TIMEOUT_MULTIPLIER | 3 | slides×90s×3。正常の2.5倍余裕 |
| WebUI fetch retry | 2回 | 3s/6s backoff。コールドスタート対策 |

## 残課題

| 課題 | 優先度 | 備考 |
|---|---|---|
| 会話履歴の画像間引き | 中 | Too much media の根本対策 |
| CloudFront signed URL 期限切れ | 低 | プレビュー表示のみ影響、生成には無関係 |
| AgentCore再起動の原因特定 | 低 | 対策済みのため影響なし。再現待ち |
| OTel ログバッチサイズ超過 | 低 | 非致命的。ログ一部欠落の可能性 |

## ワーカー構成

| workspace | ロール | Agent | 用途 |
|---|---|---|---|
| workspace:11 | テスター | kiro-sa-tester | ブラウザE2Eテスト |
| workspace:12 | デバッガー | kiro-sa-dev | CloudWatchログ調査 |
| workspace:13 | dev | kiro-sa-dev | コード実装 |
| workspace:14 | レビュアー | kiro-sa-dev | コードレビュー |

## オーケストレーション運用改善

1. **共有コンテキスト方式**: `/tmp/shared-context.md` に背景・環境・タスクを集約
2. **ファイル経由指示**: `cmux send` の改行分割問題を回避するため、指示は `/tmp/task-*.md` に書いてパスを送る
3. **並列実行**: テスター（E2E）とデバッガー（ログ監視）を並行で動かす
