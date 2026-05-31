# MCP 再接続 設計ドキュメント

## 目的

MCP サーバーとの接続が切れた場合に、自動的に再接続してツール呼び出しを復旧する。

## 前提

- MCP サーバーは 3 つ:
  - **Presentation Maker** (`mcp_agentcore_runtime`): AgentCore Runtime 経由の Streamable HTTP。長時間 SSE 接続。`required=True`
  - **AWS Knowledge** (`mcp_aws_knowledge`): AWS MCP の Streamable HTTP。`required=False`
  - **AWS Pricing** (`mcp_aws_pricing`): ローカル stdio プロセス。`required=False`
- 3 つとも接続が切れる可能性がある（SSE 切断、プロセス死亡）
- メイン Agent と Composer Agent の 2 種類が MCP ツールを呼ぶ
- Composer Agent は ThreadPool で最大 10 並列で動く
- WebUI に再接続状態を SSE で通知する

## スコープ

**現在の実装は Presentation Maker のみを再接続対象とする。**

理由:
1. Presentation Maker は `required=True` — 切れると Agent が機能しない
2. AWS Knowledge / Pricing は `required=False` — 切れても Agent は動き続ける（一部ツールが使えないだけ）
3. Presentation Maker は長時間 SSE 接続を維持するため、切断リスクが最も高い
4. 実際に本番で切断が確認されたのは Presentation Maker のみ（2026-05-05, 05-06）

### 将来の拡張: 全サーバー対応の選択肢

| 案 | 設計 | メリット | デメリット |
|---|---|---|---|
| A: MCPReconnect を複数インスタンス化 | サーバーごとに MCPReconnect を 1 つ生成。`after_tool_hook` で失敗したツールがどのサーバーに属するか特定して対応する MCPReconnect を呼ぶ | サーバーごとに独立した factory_fn / リトライ設定が可能 | ツール→サーバーの逆引きが必要（旧実装の `_find_entry_for_tool` 問題が再発する） |
| B: MCPReconnect を 1 つのまま、entries リストで管理 | `MCPReconnect` が `entries: list` を持ち、`after_tool_hook` で該当 entry を特定して再接続 | 1 インスタンスで全管理。イベント通知が統一 | 旧実装に逆戻り。ツール→サーバー特定の複雑さが戻る |

**推奨**: 現時点では対応不要。AWS Knowledge / Pricing が切れて問題になった場合に案 A で対応する。その際、ツール→サーバーの逆引きは「サーバー初期化時にツール名セットを保存する」方式で行う（`tool_map` への動的アクセスは禁止）。

## 想定するエラーとリトライ設計

### 想定エラー

| エラー | 原因 | 復旧時間 | 頻度 |
|---|---|---|---|
| SSE 接続切断 | ネットワーク一時障害、ロードバランサーの idle timeout | 即時〜数秒 | 高（30 分以上のセッションで発生） |
| MCP サーバーのコンテナ再起動 | AgentCore Runtime のスケーリング、デプロイ | 15〜30 秒 | 中（デプロイ時、idle timeout 後の再起動） |
| MCP サーバーの一時的な過負荷 | 並列リクエスト集中 | 数秒 | 低 |

### 再接続しないエラー

| エラー | 理由 |
|---|---|
| 認証エラー（401/403） | JWT が無効。リトライしても同じ結果 |
| ツールのビジネスロジックエラー（File not found 等） | MCP 接続は正常。再接続は無意味 |
| ValidationException（Too much media 等） | Bedrock API のエラー。MCP とは無関係 |

### リトライパラメータの根拠

- `max_retries=8`: コンテナ再起動（最大 30 秒）をカバーするため。5 回（25 秒）では MCP Server 起動に間に合わず全リトライ失敗した事例あり
- 間隔 `min(2^attempt, 10)`: 1+2+4+8+10+10+10+10 = 55 秒
- jitter なし: `_lock` で排他するため thundering herd は発生しない
- 合計最大待ち時間: 約 55 秒。ユーザー体感としては keepalive が流れ続けるため WebUI は固まらない
- 401（JWT 期限切れ）検知時: 即座にリトライ停止（リトライしても同じ結果なので待ち時間を浪費しない）

## アーキテクチャ

```
MCPReconnect (1 インスタンス、factory.py で生成)
│
├── .client            現在の共有クライアント（メイン Agent 用）
├── .new_client()      独立クライアントを生成（Composer 用）
├── .reconnect(agent)  共有クライアントを作り直す（メイン Agent 用）
├── .after_tool_hook   メイン Agent の AfterToolCallEvent hook
├── .before_tool_hook  メイン Agent の BeforeToolCallEvent hook
└── .drain_events()    WebUI 通知用イベントを取り出す
```

## 2 つの再接続フロー

### フロー 1: メイン Agent

```
ツール呼び出し失敗
  → after_tool_hook 発火
  → is_mcp_error() で MCP 接続エラーか判定
  → reconnect(agent) で共有クライアントを作り直し + tool_registry 差し替え
  → 次のツール呼び出しは新クライアント経由
```

### フロー 2: Composer Agent

```
Composer 全体の実行が失敗 (except Exception)
  → _group_mcp.stop()
  → _group_mcp = mcp_reconnect.new_client()
  → composer.tool_registry.process_tools([_group_mcp])
  → Composer を再実行
```

### なぜ別フローか

| | メイン Agent | Composer |
|---|---|---|
| 検知タイミング | ツール単位の失敗後（hook） | Agent 全体の実行失敗後（except） |
| 対象 | 共有クライアント 1 つ | グループごとの独立クライアント |
| tool_registry | メイン Agent のもの | 各 Composer のもの |
| リトライ単位 | 次のツール呼び出し | Composer 全体を再実行 |

共通化すべきは「新しいクライアントを作る」部分のみ（`new_client()`）。

## MCPReconnect クラス仕様

### コンストラクタ

```python
MCPReconnect(factory_fn, jwt_token, max_retries=8)
```

- `factory_fn`: `(jwt_token: str) -> MCPClient` — MCP クライアント生成関数
- `jwt_token`: 認証トークン
- `max_retries`: reconnect のリトライ回数

### メソッド

| メソッド | 用途 | スレッドセーフ |
|---|---|---|
| `new_client() -> MCPClient` | 独立クライアントを生成して返す | ✅（factory_fn が冪等なら） |
| `reconnect(agent) -> bool` | 共有クライアントを作り直す。成功で True | ✅（_lock で排他） |
| `after_tool_hook(event)` | メイン Agent の hook。MCP エラー検知 → reconnect | - |
| `before_tool_hook(event)` | 再接続中のツール呼び出しをブロック | - |
| `drain_events() -> list[dict]` | 溜まった通知イベントを返してクリア | ✅ |
| `has_pending_events() -> bool` | 通知イベントがあるか | - |

### is_mcp_error 判定ロジック

以下のいずれかに該当すれば MCP 接続エラーと判定する:

1. `event.exception` の型名に `MCPClient` / `MCPSession` / `ToolProvider` を含む
2. エラーテキストに以下のキーワードを含む:
   - `connection`（`closed`, `refused`, `reset` 等）
   - `session is not running`
   - `failed to start mcp`
   - `eof`, `broken pipe`
   - `timed out`, `timeout`
   - `remote protocol error`, `read error`
   - `502`, `503`, `504`

以下は MCP エラーと判定**しない**（`_PERMANENT_KEYWORDS` で除外）:
- `unauthorized`, `forbidden`（認証エラー — リトライしても無駄）
- `certificate`, `ssl`（TLS エラー）
- ツールの実行結果がビジネスロジック上のエラー（ファイルが見つからない等）

### reconnect(agent) の動作

1. `_lock` を取得（再接続中の二重実行を防止）
2. 最大 `max_retries` 回ループ:
   a. `{"type": "reconnecting", "attempt": N, "max_retries": N}` イベントを発行
   b. `factory_fn(jwt_token)` で新クライアント生成
   c. 成功: `agent.tool_registry.unload_tool_provider(old)` → `process_tools([new])` → `{"type": "reconnected"}` → return True
   d. 失敗かつ 401/Unauthorized: `{"type": "auth_expired"}` → return False（即停止）
   e. 失敗（その他）: `sleep(min(2^attempt, 10))` → 次のリトライ
3. 全リトライ失敗: `{"type": "failed"}` イベントを発行、return False

### before_tool_hook の動作

- `_lock` が取得されている（= 再接続中）なら、ロック解放まで待つ（最大 60 秒）
- タイムアウトした場合はそのままツール呼び出しを続行（失敗するが、ブロックし続けるよりまし）

## factory.py での組み立て

```python
mcp_reconnect = MCPReconnect(
    factory_fn=mcp_agentcore_runtime,  # MCP クライアント生成関数
    jwt_token=jwt_token,
)
mcp_reconnect.set_client(mcp_servers[0])

# メイン Agent の hooks
agent.hooks.add_callback(AfterToolCallEvent, mcp_reconnect.after_tool_hook)
agent.hooks.add_callback(BeforeToolCallEvent, mcp_reconnect.before_tool_hook)

# Composer 用
composer_mcp_factory = mcp_reconnect.new_client
```

## streaming.py での通知

```python
if mcp_reconnect.has_pending_events():
    for ev in mcp_reconnect.drain_events():
        yield ev
```

---

## テスト仕様

### 1. new_client

| テスト | 入力 | 期待結果 |
|---|---|---|
| 正常生成 | factory_fn が MCPClient を返す | 新しい MCPClient が返る |
| factory_fn が例外 | factory_fn が ConnectionError を raise | 例外がそのまま伝播 |
| 共有クライアントに影響しない | new_client() を呼ぶ | self._client は変わらない |

### 2. reconnect(agent)

| テスト | 入力 | 期待結果 |
|---|---|---|
| 1 回目で成功 | factory_fn が成功 | True、client 更新、tool_registry 差し替え、"reconnected" イベント |
| 2 回目で成功 | 1 回目 raise、2 回目成功 | True、attempt=2 で成功 |
| 全リトライ失敗 | factory_fn が常に raise | False、"failed" イベント、client は古いまま |
| 二重呼び出し防止 | 2 スレッドから同時に reconnect | 1 つだけ実行、もう 1 つはロック待ち後に新 client を見て return |
| agent.tool_registry 更新 | 成功時 | unload_tool_provider(old) + process_tools([new]) が呼ばれる |

### 3. after_tool_hook

| テスト | 入力 | 期待結果 |
|---|---|---|
| MCP エラー → reconnect 発動 | result.status="error", text="Connection closed" | reconnect が呼ばれる |
| MCPClientInitializationError → reconnect 発動 | event.exception が MCPClientInitializationError | reconnect が呼ばれる |
| 非 MCP エラー → 何もしない | result.status="error", text="File not found" | reconnect は呼ばれない |
| 成功 → 何もしない | result.status="success" | reconnect は呼ばれない |
| result が dict でない → 何もしない | result = "string" | reconnect は呼ばれない |

### 4. before_tool_hook

| テスト | 入力 | 期待結果 |
|---|---|---|
| 再接続中でない → 即座に return | _lock が空き | ブロックしない |
| 再接続中 → ブロック | _lock が取得済み | ロック解放まで待つ |
| タイムアウト → 続行 | _lock が 60 秒以上取得済み | タイムアウト後に return |

### 5. is_mcp_error

| テスト | エラーテキスト / 例外型 | 期待結果 |
|---|---|---|
| "Connection to the MCP server was closed" | True |
| "the client session is not running" | True |
| MCPClientInitializationError 型 | True |
| "The read operation timed out" | True |
| "EOF on transport" | True |
| "broken pipe" | True |
| "File not found: /tmp/slides.json" | False |
| "ValidationException: Too much media" | False |
| "unauthorized: invalid token" | False |
| result.status != "error" | False |

### 6. drain_events / has_pending_events

| テスト | 操作 | 期待結果 |
|---|---|---|
| 初期状態 | has_pending_events() | False |
| reconnect 後 | has_pending_events() | True |
| drain 後 | drain_events() → has_pending_events() | False |
| drain は全イベントを返す | reconnect 2 回 → drain | ["reconnecting", "reconnected", ...] |

### 7. Composer 統合（結合テスト相当）

| テスト | シナリオ | 期待結果 |
|---|---|---|
| Composer が new_client で独立クライアント取得 | new_client() 呼び出し | 共有 client と別インスタンス |
| Composer リトライ時に new_client で再生成 | 1 回目失敗 → new_client() → 2 回目成功 | 新クライアントで成功 |
| Composer の失敗がメイン Agent に影響しない | Composer の _group_mcp が死ぬ | mcp_reconnect.client は生きたまま |
