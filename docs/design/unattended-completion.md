# 「放置して完成」を阻害する事象と対処

## 背景

本プロジェクトの Agent は、ユーザーの指示を受けてスライドを自律的に生成する。
1 回のリクエストで 30 枚のスライド生成 + レビュー + 修正を行うため、処理時間は 30〜60 分に及ぶことがある。

この間に以下のような一時的障害が発生する:
- MCP Server との接続切断（SSE の idle timeout、ネットワーク障害）
- Bedrock API の推論遅延（adaptive thinking による長時間推論）
- MCP Server のコールドスタート（コンテナ再起動）
- JWT トークンの期限切れ（Cognito アクセストークン 1 時間）

## 目標

ユーザーが指示を出したら、プレゼンテーションが完成するまで放置できること。
時間がかかっても構わないが、途中で止まってユーザーの介入が必要になるのは NG。

## 設計方針

1. **タイムアウトは「正当な処理の最大時間 + マージン」で設定する** — 短すぎると正常な処理を殺し、長すぎるとハング検知が遅れる
2. **リトライは「一時的障害」に対してのみ行う** — 認証エラー（401）やバリデーションエラーはリトライしても無駄なので即停止
3. **リトライ不可能な場合はエラーを表示する** — 黙って固まるのが最悪。エラーを表示すればユーザーが対処できる

## 事象一覧

リモートリポジトリ（GitHub main）の実装を正とした場合に発生する問題と、ローカルで実施した対処。

### 1. MCP 接続切断 → 全ツール失敗

| 項目 | 内容 |
|---|---|
| リモートの状態 | MCP 再接続の仕組みが存在しない。接続が切れたら全ツールが失敗してそのまま |
| 症状 | Agent は生きているが全ツールが `MCPClientInitializationError` で失敗し続ける。**新しいチャットセッションを作り直す必要がある** |
| ローカルの対処 | `mcp_reconnect.py` を新規作成。`MCPReconnect` クラスで MCP エラー検知 → 自動再接続（max_retries=8、backoff 合計 55 秒）。再接続が正しく発動する |
| 残課題 | なし |

### 2. Too much media → WebUI 固まる

| 項目 | 内容 |
|---|---|
| リモートの状態 | `streaming.py` にストリーム全体の例外処理がない。例外が発生すると SSE が途切れるだけで WebUI に通知されない |
| 症状 | WebUI がローディングのまま永久に固まる。**新しいチャットセッションを作り直す必要がある** |
| ローカルの対処 | `streaming.py` の `except Exception` で `yield {"status": "error", "error": str(e)}` を追加。エラーが WebUI に表示される |
| 残課題 | 会話履歴の画像間引き（根本対策）が未実装。発生した場合はユーザーが新チャットを開始する必要あり |

### 3. MCP Server の run_python 応答が Agent に届かない → 永久ハング

| 項目 | 内容 |
|---|---|
| リモートの状態 | ツール実行中のタイムアウト機構がない |
| 症状 | WebUI はくるくる回り続けるが応答が返らない（34 分間ハング）。**ユーザーがメッセージを送り直す必要がある** |
| ローカルの対処 | `streaming.py` に `_TOOL_TIMEOUT=360` を追加。`in_tool=True` が 360 秒続いたらキャンセル → エラー表示 |
| 残課題 | タイムアウト後の自動リトライは Strands SDK の制約で不可。ユーザーがメッセージを送り直す必要あり |

### 4. MCP クライアントの httpx タイムアウト → エラー表示で停止

| 項目 | 内容 |
|---|---|
| リモートの状態 | `mcp_clients.py` の `timeout=120` が Code Interpreter の実行時間（最大 300 秒）に対して短い。再接続の仕組みもない。`strandsParser.js` にエラーハンドリングがないためエラーメッセージも表示されない |
| 症状 | SSE が途切れるか、Agent が例外で停止。WebUI はローディングのまま固まる。**ユーザーがメッセージを送り直すか新チャットを作り直す必要がある** |
| ローカルの対処 | `timeout=360` に変更 + `mcp_reconnect.py` で `"timed out"` 検知 → 再接続 → Bedrock が同じツールを再呼び出し + `strandsParser.js` にエラー表示追加。**ユーザー介入不要** |
| 残課題 | なし |

### 5. Bedrock API の read_timeout → エラー表示で停止

| 項目 | 内容 |
|---|---|
| リモートの状態 | `factory.py` の `read_timeout=120`。boto3 `retries=adaptive 5回` は設定済みだが、`ReadTimeoutError` 発生後に `strandsParser.js` にエラーハンドリングがないためエラーメッセージが表示されない |
| 症状 | boto3 が自動リトライするが、全リトライ失敗時は SSE が途切れて WebUI が固まる。**ユーザーがメッセージを送り直す必要がある** |
| ローカルの対処 | `read_timeout=300` に変更 + `streaming.py` の `except Exception` でエラー yield + `strandsParser.js` にエラー表示追加。300 秒以内なら正常完了。超えた場合は boto3 が自動リトライ（5 回）。全リトライ失敗時はエラー表示され**ユーザーがメッセージを送り直す必要がある** |
| 残課題 | なし |

### 6. max_tokens 超過 → Agent 停止

| 項目 | 内容 |
|---|---|
| リモートの状態 | `model_profiles.py` で Opus 4.7 は `CLAUDE_EXTENDED_THINKING`（`temperature=None` のみ）、Opus 4.6 は `CLAUDE_ADAPTIVE_THINKING`（`temperature=1.0` のみ）。`max_tokens` 未設定（Bedrock デフォルト 4096）。`additional_request_fields`（thinking 設定）も未設定 |
| 症状 | 「Agent has reached an unrecoverable state due to max_tokens limit」と表示。**ユーザーがモデルを変えるかメッセージを送り直す必要がある** |
| ローカルの対処 | Opus 4.7/4.6 を `CLAUDE_ADAPTIVE_THINKING` に統一し、`max_tokens=128000` + `additional_request_fields={"thinking": {"type": "adaptive"}}` + `temperature=None` を設定。**ユーザー介入不要** |
| 残課題 | なし |

### 7. コールドスタートタイムアウト → エラー表示で停止

| 項目 | 内容 |
|---|---|
| リモートの状態 | WebUI にリトライ機構がない |
| 症状 | Agent コンテナが idle timeout で終了後、新コンテナ起動が間に合わず AgentCore Runtime がタイムアウトを返す。WebUI はエラーハンドリングがないためローディングのまま固まるか無反応。**ユーザーがリロードまたは新チャットを作り直す必要がある** |
| ローカルの対処 | 未実装 |
| 残課題 | `agentCoreService.js` でタイムアウト時に自動リトライ（最大 2 回）を実装すべき |

### 8. JWT 期限切れ → 再接続が全て 401 で失敗

| 項目 | 内容 |
|---|---|
| リモートの状態 | 再接続の仕組みがない。MCP 接続が切れたら事象 1 と同じく全ツール失敗で停止 |
| 症状 | 事象 1 と同じ。WebUI がローディングのまま固まるか無反応。**新しいチャットセッションを作り直す必要がある** |
| ローカルの対処 | 再接続の仕組み（事象 1）を前提に、401 検知で即座にリトライ停止 + `auth_expired` イベント発行。ハング対策（_TOOL_TIMEOUT）で 1 時間超のリクエストを防止し、JWT 期限切れ自体が発生しにくくなった |
| 残課題 | WebUI で `auth_expired` を検知して「メッセージを送り直してください」と表示 |

### 9. Agent プロセス停止（原因不明）

| 項目 | 内容 |
|---|---|
| リモートの状態 | 検知手段がない |
| 症状 | ログが途絶え、コンテナが再起動。**新しいチャットセッションを作り直す必要がある** |
| ローカルの対処 | SIGTERM ハンドラ、GeneratorExit キャッチ、ping ハンドラ、version ログ等の検知手段を追加 |
| 残課題 | 防止策なし。再現待ち |

## まとめ: 現在の「放置完了」阻害要因

| 事象 | ユーザー介入が必要？ | 対処状況 |
|---|---|---|
| MCP 接続切断 | ❌ 不要（自動再接続） | ✅ |
| httpx タイムアウト | ❌ 不要（自動リトライ） | ✅ |
| Bedrock read_timeout | △ 通常不要（全リトライ失敗時は送り直し） | ✅ |
| max_tokens 超過 | ❌ 不要（設定で解消） | ✅ |
| Too much media | ⚠️ 新チャット必要 | △（エラー表示のみ。画像間引き未実装） |
| SSE ハング（360 秒） | ⚠️ メッセージ送り直し | △（タイムアウトはするが自動リトライ不可） |
| コールドスタート | ⚠️ リロードまたは新チャット | ❌（WebUI リトライ未実装） |
| JWT 期限切れ | ⚠️ メッセージ送り直し | △（ハング防止で発生しにくい） |
| Agent プロセス停止 | ⚠️ 新チャット必要 | ❌（防止策なし） |

## 「放置完了」を実現するために残っている課題

1. **会話履歴の画像間引き** — Too much media を根本的に防止
2. **WebUI の自動リトライ** — コールドスタートと SSE ハング後の自動復旧
3. **Agent プロセス停止の防止** — 原因不明のため対策困難

## 変更ファイル一覧（リモート未反映）

| ファイル | 変更内容 |
|---|---|
| `agent/mcp_reconnect.py` | 新規作成（MCPReconnect クラス、161 行） |
| `agent/factory.py` | MCPReconnect 組み立て、`read_timeout=300`、`retries` 追加、`composer_mcp_factory` 変更 |
| `agent/mcp_clients.py` | `timeout` 120→360 |
| `agent/streaming.py` | `try/except` 追加、`_TOOL_TIMEOUT=360`、keepalive rss_kb、`reconnect_handler` パラメータ |
| `agent/basic_agent.py` | SIGTERM ハンドラ、ping ハンドラ、version ログ、GeneratorExit キャッチ、session_start ログ |
| `agent/model_profiles.py` | `max_tokens=128000`、adaptive thinking、Opus 4.7 マッピング変更 |
| `agent/modes/separated/composer.py` | compose ログ、MCP 再生成 + backoff |
| `agent/resilience.py` | `fingerprint_repeat_limit` 3→5、エラーのみカウント |
| `agent/Dockerfile` | COPY 行に `mcp_reconnect.py` 追加 |
| `web-ui/src/services/strandsParser.js` | `json.error` ハンドリング追加 |
| `tests/test_mcp_reconnect.py` | 新規作成（33 テスト） |
| `tests/conftest.py` | `agent/` を sys.path に追加 |
| `docs/design/mcp-reconnect.md` | 新規作成（MCP 再接続設計） |
| `docs/design/unattended-completion.md` | 新規作成（本ドキュメント） |
| `docs/changelog/2026-05-05-agent-hang-investigation.md` | 新規作成（調査知見） |

## 設定値の根拠

| 設定 | 値 | 根拠 |
|---|---|---|
| `read_timeout=300` | 300s | Opus 4.7 の adaptive thinking が複雑なタスク（28 スライドの outline 構造化等）で 120 秒超かかることを確認。CloudTrail で `outputTokens=4096` 到達時に 120s 超を観測。300s あれば大半のケースをカバー |
| `mcp timeout=360` | 360s | MCP Server の Code Interpreter セッションタイムアウトが 300s（`sandbox.py` の `sessionTimeoutSeconds=300`）。正当な処理は必ず 300s 以内に完了する。300 + マージン 60s で設定 |
| `_TOOL_TIMEOUT=360` | 360s | MCP timeout と同じ根拠。httpx タイムアウトが効かないケース（SSE ストリーム上でレスポンスが来ない）のフォールバック。実際に 34 分間ハングした事例あり |
| `max_retries=8` | 8 回 | backoff: 1+2+4+8+10+10+10+10 = 合計 55s。MCP Server のコールドスタート（15〜30s）をカバー。5 回（合計 25s）では MCP Server 起動に間に合わず全リトライ失敗した事例あり |
| `max_tokens=128000` | 128K | Opus 4.7/4.6 の max output tokens が 128K（AWS 公式ドキュメント）。未設定時のデフォルト 4096 では `run_python` のコード生成が途中で打ち切られることを CloudTrail で確認（`outputTokens=4096` ちょうどで停止） |
| `thinking: adaptive` | — | Opus 4.7 は adaptive のみ対応（`thinking.type: "enabled"` + `budget_tokens` は 400 エラー。公式: "only supports thinking.type: adaptive"）。Opus 4.6 も adaptive 推奨（`budget_tokens` は deprecated） |
| `temperature=None` | 渡さない | Opus 4.7 は `temperature`/`top_p`/`top_k` が非対応（公式: "Sampling parameters no longer supported"）。Opus 4.6 も thinking 有効時は temperature 非対応 |
| `fingerprint_repeat_limit=5` | 5 | MCP 再接続リトライで同じツールが複数回呼ばれるため、3 では誤検知する。エラーのみカウントに変更したうえで 5 に引き上げ |
| `max_tool_calls=300` | 300 | 30 スライド生成で各スライド 5〜10 回のツール呼び出し。150 では正常な処理で上限に達する |
