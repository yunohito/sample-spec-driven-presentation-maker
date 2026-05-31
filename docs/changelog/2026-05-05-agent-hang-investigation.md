# Agent 停止・ハング調査（2026-05-05）

## 確定した問題と修正

### 問題 1: Too much media で WebUI が固まる（確定・修正済み）

**原因**: 会話履歴にプレビュー画像（base64）が蓄積し、Bedrock の 100 枚上限を超えると `ValidationException: Too much media: 0 document pages + 103 images > 100` が発生。`streaming.py` の `except Exception` がエラーを握りつぶし、WebUI にエラーが届かずローディングのまま固まる。

**修正**: `streaming.py` の `except Exception` で `yield {"status": "error", "error": str(e)}` を追加。WebUI に「This conversation is too long for the model to process. Please start a new chat to continue.」と表示される。

**再現条件**: 20 枚デッキで compose → preview → 修正 → preview を繰り返すと、数ターンで 100 枚に達する。

**根本対策（未実装）**: 会話履歴から古いプレビュー画像を間引く。

### 問題 3: MCP 切断後に再接続が発動しない（確定・修正済み）

**原因**: `mcp_reconnect.py` の `_find_entry_for_tool` が `entry.client.tool_map` にアクセスして例外 → `except Exception: continue` で握りつぶし → `None` を返す → 再接続ロジックが発動しない。MCP セッションが死んだ瞬間に `tool_map` も死ぬため、ツール→サーバーの紐付けが解決できなくなる。

**症状**: Agent は生きているが全ツール呼び出しが `MCPClientInitializationError: the client session is not running` で失敗し続ける。WebUI には「MCP サーバーとの接続が切れており、ツールが使用できない状態です」と表示される。

**修正**: `MCPServerEntry` に `tool_names: set` キャッシュを追加。初回ツール呼び出し成功時にキャッシュが構築され、以降は MCP セッションが死んでもキャッシュから entry を特定でき、再接続が正しく発動する。

**ログの時系列（01:30 の事例）**:
1. `01:30:10` — MCP SDK: `GET stream disconnected, reconnecting in 1000ms...`
2. `01:30:29` — `Tool error: tool=import_attachment, exception_type=None, error=Connection to the MCP server was closed`
3. `01:30:35〜01:31:16` — 以降すべて `MCPClientInitializationError` で失敗（再接続が発動していない）

**PR #119 を取り下げ**: このバグが含まれていたため閉じた。修正を含めて再作成予定。

### 問題 2: 18:28 の停止（未解決）

**事象**: Agent が ConverseStream 正常完了 + Memory 保存成功の直後にログが途絶え、コンテナが再起動した。

**確定事実**:
- MCP Server 正常完了、Bedrock API 正常完了（CloudTrail で確認）
- OOM ではない（CloudWatch メモリメトリクス安定）
- OTel context detach エラーは無害（同じエラーが出ても 30 分以上正常動作を確認）
- 15 分アイドルタイムアウトではない（30 分以上動作を確認）
- SIGTERM ログなし、GeneratorExit ログなし
- `Too much media` ではない（CloudTrail に ValidationException が記録されていない）

**残る可能性**:
- AgentCore Runtime 内部の一過性障害
- 再現していないため、これ以上の原因特定は困難

---

## 今回デプロイした変更

| ファイル | 変更内容 |
|---|---|
| `agent/factory.py` | メイン Agent に `retries={"max_attempts": 5, "mode": "adaptive"}` 追加 |
| `agent/basic_agent.py` | SIGTERM ハンドラ、`@app.ping` ハンドラ（HEALTHY_BUSY）、`session_start: mode=` ログ、GeneratorExit キャッチ |
| `agent/streaming.py` | メモリ使用量ログ（rss_kb）、クライアント切断防御（GeneratorExit/ConnectionError）、**エラーイベント yield 追加** |
| `agent/mcp_reconnect.py` | **`_find_entry_for_tool` に `tool_names` キャッシュ追加**（MCP 切断後も再接続が発動するように修正） |
| `infra/lib/agent-stack.ts` | `OTEL_TRACES_EXPORTER: "none"`（変更なし） |

---

## AgentCore Runtime の動作仕様

### /ping エンドポイント

- `Healthy`: アイドル状態。ドキュメント上は 15 分続くと自動終了
- `HealthyBusy`: 処理中。終了されない
- 実測: `Healthy` を返し続けても SSE 接続中は kill されなかった（ドキュメントと異なる）
- 設計判断: ドキュメントに従い `HealthyBusy` を返す。実測の挙動に依存しない

### カスタム ping ハンドラが必要な理由

`bedrock-agentcore` SDK（`BedrockAgentCoreApp`、PyPI: `bedrock-agentcore`、GitHub: `aws/bedrock-agentcore-sdk-python`）のデフォルトは `add_async_task()` で登録されたタスク数で判定する。Strands Agent の `stream_async` は SDK の async task 管理を使わないため、デフォルトでは処理中でも `Healthy` を返す。`_cancel_events`（アクティブセッション数）で判定するカスタムハンドラが必要。

### OTel の制約

- `OTEL_PYTHON_DISABLED_INSTRUMENTATIONS=all` は効かない（AgentCore Runtime が OTel を強制有効化）
- `OTEL_TRACES_EXPORTER=none` はトレース送信を止めるが、OTel SDK 自体は動き続ける
- `DISABLE_ADOT_OBSERVABILITY=true` はアプリケーションログも消えるので使用禁止
- OTel context detach エラーは無害（ERROR レベルだがプロセスは死なない）
- OTel ログエクスポーターが `Failed to export logs batch code: 400` を出すことがある（ログバッチが大きすぎる）。アプリケーションログの欠落を引き起こす可能性がある

### presigned URL

- CloudFront signed URL の有効期間: **15 分（900 秒）**
- 長時間セッション（30 分以上）では期限切れが発生する
- 期限切れ自体は Agent の動作に影響しない（WebUI の画像表示のみ）

### セッションとコンテナのルーティング

- `X-Amzn-Bedrock-AgentCore-Runtime-Session-Id` ヘッダーでセッションがコンテナに固定される（sticky session）
- 同じ sessionId は同じコンテナに行く。アプリケーション側から切り替える手段はない
- コンテナが終了済みの場合、次のリクエストで新コンテナに割り当てられる
- **既存セッションで新コンテナを使う方法**: `stopRuntimeSession` API を呼んでセッションを終了 → 次のメッセージで新コンテナに割り当て

### stopRuntimeSession API

```bash
curl -s -X POST "https://bedrock-agentcore.us-east-1.amazonaws.com/runtimes/${ENCODED_ARN}/stopruntimesession?qualifier=DEFAULT" \
  -H "Authorization: Bearer ${ACCESS_TOKEN}" \
  -H "Content-Type: application/json" \
  -H "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id: ${SESSION_ID}"
```

- **認証**: Cognito OAuth トークン（SigV4 は不可。`Authorization method mismatch` エラーになる）
- セッションが既に終了済みの場合: HTTP 404 `Session not found or has been terminated`
- WebUI の DevTools コンソールから呼ぶのが最も手軽（WebUI が既にトークンを保持）
- DynamoDB テーブル `SdpmData-DecksTable1391E269-FNDU6FOKF630` の `chatSessionId` フィールドからフル sessionId を取得可能

---

## WebUI のモード制御

```
Parallel agents OFF → mode=single（Spec/Vibe 選択不可）
Parallel agents ON  → Spec: mode=separated, Vibe: mode=vibe
```

コード: `agentMode === "vibe" ? "vibe" : (parallelAgents ? "separated" : "single")`

ModeSelector は `parallelAgents && <ModeSelector ...>` で表示制御されている。

---

## 検知手段（デプロイ済み）

| 検知対象 | 手段 | ログメッセージ |
|---|---|---|
| セッション開始・モード | `basic_agent.py` | `session_start: session=xxx, mode=vibe` |
| クライアント切断 | `basic_agent.py` + `streaming.py` | `Client disconnected (GeneratorExit)` |
| AgentCore による kill | `basic_agent.py` | `Received signal SIGTERM` |
| メモリ推移 | `streaming.py` keepalive | `rss_kb=472888` |
| ping 状態変化 | `basic_agent.py` | `ping_response: HEALTHY_BUSY, active_sessions=1` |
| Bedrock エラー | `streaming.py` | `stream_agent unexpected error` + WebUI にエラー表示 |
| AgentCore 内部エラー | CloudWatch `/aws/spans` | `error_type: InvocationError.Internal` |

---

## ログ確認コマンド集

### Agent ログ

```bash
# セッション開始・完了・エラー
aws logs filter-log-events \
  --log-group-name "/aws/bedrock-agentcore/runtimes/sdpm_agent-20nCH0F8Em-DEFAULT" \
  --start-time $(date -v-30M +%s000) \
  --filter-pattern "?session_start ?stream_agent ?keepalive ?SIGTERM ?GeneratorExit ?disconnect ?\"Tool error\" ?\"unexpected error\"" \
  --limit 30 --no-cli-pager --region us-east-1
```

### MCP Server ログ

```bash
aws logs filter-log-events \
  --log-group-name "/aws/bedrock-agentcore/runtimes/sdpm-rICI8O3LbR-DEFAULT" \
  --start-time $(date -v-10M +%s000) \
  --filter-pattern "?tool_start ?tool_ok ?tool_fail" \
  --limit 20 --no-cli-pager --region us-east-1
```

### CloudTrail（Bedrock API エラー）

```bash
aws cloudtrail lookup-events \
  --lookup-attributes AttributeKey=EventName,AttributeValue=ConverseStream \
  --start-time "2026-05-05T14:00:00Z" --end-time "2026-05-05T14:15:00Z" \
  --no-cli-pager --region us-east-1
```

### メモリメトリクス

```bash
aws cloudwatch get-metric-data \
  --metric-data-queries '[{"Id":"mem","MetricStat":{"Metric":{"Namespace":"AWS/Bedrock-AgentCore","MetricName":"MemoryUsed-GBHours","Dimensions":[{"Name":"Service","Value":"AgentCore.Runtime"}]},"Period":60,"Stat":"Sum"}}]' \
  --start-time "2026-05-05T09:00:00Z" --end-time "2026-05-05T09:35:00Z" \
  --no-cli-pager --region us-east-1
```

### Transaction Search スパン

```bash
aws logs filter-log-events \
  --log-group-name "aws/spans" \
  --start-time $(date -v-30M +%s000) \
  --filter-pattern "?error_type ?InvocationError" \
  --limit 10 --no-cli-pager --region us-east-1
```

---

## 未解決・今後の対応

### 高優先度

| 項目 | 状態 |
|---|---|
| 会話履歴の画像間引き（Too much media 根本対策） | 未実装。Strands SDK の `agent.messages` から古いプレビュー画像を間引く |
| タイムアウト + リトライポリシーの統一設計 | 未実装。下記参照 |

#### タイムアウト + リトライポリシー

全リクエストパスにタイムアウトとリトライの仕組みが必要。現状の抜けを整理する。

| リクエストパス | タイムアウト | リトライ | 問題 |
|---|---|---|---|
| WebUI → AgentCore Runtime（SSE） | なし（fetch 無制限） | なし | コールドスタートタイムアウトでユーザーに再試行を求める |
| Agent → Bedrock ConverseStream | `read_timeout=120` | boto3 adaptive 5回 | `ModelTimeoutException` がリトライ対象外 |
| Agent → MCP Server（ツール実行中） | MCP SDK の `timeout=120` が効かない | なし | **MCP SSE 接続が切れるとレスポンスが届かず Agent が永久にハングする** |
| Agent → MCP Server（ツール完了後） | — | MCPReconnect 5回 | 対応済み |
| Composer → MCP Server | `timeout=120` | Composer リトライ 2回 + `new_client()` | 対応済み |
| MCP Server → S3/DynamoDB | boto3 デフォルト | boto3 デフォルト | — |
| WebUI → API Gateway（履歴取得等） | 未確認 | 未確認 | 未調査 |

**対処方針**:

1. **WebUI → AgentCore**: タイムアウトエラー時に自動リトライ（最大 2 回、間隔 3 秒）。コールドスタート（10〜30 秒）をカバー
2. **Agent → Bedrock**: `ModelTimeoutException` を Strands SDK の event loop でキャッチしてリトライするか、`streaming.py` でキャッチして再実行。ただし thinking が長すぎるタスクは何度リトライしても同じ結果になる可能性があるため、最大 1 回に制限
3. **Agent → MCP Server（ツール実行中ハング）**: Agent 側で 360 秒のタイムアウトを設ける。タイムアウト後にツール呼び出しをエラーとして扱い、`after_tool_hook` → MCP 再接続 → 次のツール呼び出しは新接続で成功。MCP Server の Code Interpreter セッションタイムアウトが 300 秒（`mcp-server/tools/sandbox.py` の `sessionTimeoutSeconds=300`）なので、360 秒あれば正当な処理は全て完了する
4. **WebUI → API Gateway**: 標準的な fetch リトライ（5xx 時に 1 回リトライ）を追加

#### なぜ MCP SDK の `timeout=120` が効かないか

MCP の Streamable HTTP は SSE ストリーム上でリクエスト/レスポンスを多重化する。httpx の `timeout` は HTTP 接続レベルのタイムアウトであり、SSE ストリーム上の個別メッセージ（ツールのレスポンス）の到着を監視しない。SSE 接続自体が `GET stream disconnected, reconnecting` で再接続され続けるため、httpx は「接続は生きている」と判断してタイムアウトしない。
| presigned URL 期限切れ時の WebUI ハンドリング | 未実装。403 検知でトークンリフレッシュ or 再取得 |

### 中優先度

| 項目 | 状態 |
|---|---|
| WebUI「Stopped」誤表示の改善 | 未実装。下記参照 |
| 18:28 の停止原因 | 未解決。再現待ち。SIGTERM/GeneratorExit/spans で次回は検知可能 |
| ping ログの頻度削減 | 未実装。状態変化時のみログ出力に変更する |
| テンプレート名エイリアス（aws-brand-black → aws-brand-dark） | 未実装 |

#### WebUI「Stopped」誤表示の改善

**問題**: `ComposeCard.tsx` の `isHardStopped` 条件（`!isActive && !hasError && !status && state.agents.length > 0`）は、「Agent が本当に停止した」と「正常完了したが SSE で結果イベントが欠落した」を区別できない。後者の場合、リロードすれば正しい結果が表示される。

**原因**: 長時間の compose_slides 実行中（750 秒等）に SSE 接続が維持されているが、最終の tool_result イベントが WebUI に届かないケースがある。OTel ログエクスポーターの `Upload too large` エラーと同時刻に発生しており、ログバッチ処理がイベント送信に影響している可能性。

**改善案**: `isHardStopped` になったら、チャット履歴 API（`GET /chat/<sessionId>`）を呼んで最終メッセージを確認する。

- 最終メッセージに Agent の応答（text ブロック）がある → 正常完了。履歴から結果を復元して表示
- 最終メッセージが tool_use のまま（応答なし） → 本当に停止。「Stopped」を表示

これにより、SSE イベント欠落時もリロードなしで正しい結果が表示される。本当の停止時は従来通り「Stopped」が表示される。

### 低優先度

| 項目 | 状態 |
|---|---|
| コールドスタートタイムアウトの自動リトライ | 未実装。下記参照 |
| Composer 接続切断（run_python 待ち行列問題） | 再現待ち。新ログで次回は原因確定可能 |
| OTel ログエクスポーターの 400 エラーによるログ欠落 | 対処不可（AgentCore Runtime が制御） |

#### コールドスタートタイムアウトの自動リトライ

**問題**: コンテナが idle timeout で終了した後、新コンテナの起動（コールドスタート）が間に合わず、AgentCore Runtime がタイムアウトエラーを返す。WebUI は「The model took too long to respond」と表示するが、実際にはモデルは呼ばれていない（CloudTrail に ConverseStream の記録なし、Agent ログに session_start なし）。

**改善案**: `agentCoreService.js` の `invokeAgentCore` で、レスポンスがタイムアウトエラーの場合に自動リトライする（最大 2 回、間隔 3 秒）。コールドスタートは通常 10〜30 秒で完了するため、リトライすれば成功する。

**注意**: `ModelTimeoutException`（Bedrock API が thinking で時間切れ）と区別が必要。区別方法は Agent ログに `session_start` が出ているかどうか。WebUI 側では区別困難なため、一律リトライして 2 回目も失敗したらユーザーに表示する方式が現実的。

---

## 再発時の開始プロンプト

```
docs/changelog/2026-05-05-agent-hang-investigation.md を読んでください。

Agent が再び応答不能になりました。以下を順に確認してください：

1. Agent ログで最後の session_start / keepalive / SIGTERM / GeneratorExit / "unexpected error" を確認
2. MCP Server ログで最後の tool_start / tool_ok / tool_fail を確認
3. CloudTrail で ConverseStream の ValidationException を確認
4. /aws/spans ロググループで InvocationError を確認
5. CloudWatch メモリメトリクスを確認

停止時刻: [ここに時刻を記入]
セッション ID: [WebUI の URL やログから取得]

上記の結果から、以下のどれに該当するか判定してください：
- ValidationException (Too much media) → 画像間引き未実装が原因。新しいチャットで回避
- MCPClientInitializationError が連続 → MCP 再接続失敗。tool_names キャッシュが空の可能性（初回ツール呼び出し前に切断）
- GeneratorExit ログあり → クライアント切断が原因
- SIGTERM ログあり → AgentCore が kill した
- "unexpected error" ログあり → streaming.py でキャッチされたエラー（WebUI に表示されるはず）
- /aws/spans に error_type あり → AgentCore 内部エラー
- 何も出ずにログ途絶 → SIGKILL または未知の原因（サポート問い合わせ）
```

---

## 関連ファイル

- `agent/basic_agent.py` — エントリポイント、SIGTERM ハンドラ、ping ハンドラ、mode ログ
- `agent/streaming.py` — SSE ストリーミング、keepalive、クライアント切断防御、エラー yield
- `agent/factory.py` — Agent 生成、Bedrock リトライ設定
- `agent/mcp_reconnect.py` — MCP 自動再接続、Tool error ログ
- `agent/modes/separated/composer.py` — Composer Agent、composer_tool_send ログ
- `mcp-server/server.py` — tool_start/tool_ok/tool_fail ログ
- `infra/lib/agent-stack.ts` — 環境変数設定
- `web-ui/src/services/agentCoreService.js` — mode 送信ロジック
- `web-ui/src/services/strandsParser.js` — エラーイベントのパース・表示
- `web-ui/src/components/chat/ChatPanel.tsx` — ModeSelector 表示条件、invokeAgentCore 呼び出し
- `api/index.py` — presigned URL 生成（`_cf_signed_url`, `expires_in=900`）
