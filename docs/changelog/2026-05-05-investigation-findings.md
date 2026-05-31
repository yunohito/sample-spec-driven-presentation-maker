# 調査結果: Composer 接続切断 & OTel ロギング (2026-05-05)

## 1. Composer 接続切断の調査

### 症状

WebUI で Composer（`compose_slides`）が並行スライド生成中に、一部のグループが `Getting preview — Failed` で失敗する。ユーザーが「何が起きた？」と打ち込むと Agent が再開して成功する。

### CloudWatch ログから確認した事実

| 時刻 (JST) | レイヤー | イベント |
|---|---|---|
| 16:25:56〜16:38:47 | MCP Server | `run_python` が直列で 21 回処理。実行時間: 12.8s → 14.5s → ... → 38.0s（単調増加） |
| 16:38:47 | MCP Server | 最後の `tool_ok: tool=run_python, duration=38.0s` |
| 16:38:47〜16:39:20 | MCP Server | PingRequest が 1.5 秒間隔で正常に処理され続ける |
| 16:39:20 以降 | MCP Server | PingRequest が 2 秒間隔で正常に処理され続ける（コンテナは生きている） |
| 16:39:21 | Agent | `Tool error: tool=run_python, exception_type=None, error=Connection to the MCP server was closed` |
| 16:39:43〜16:41:38 | Agent | `MCPClientInitializationError: the client session is not running` × 8 回 |
| 16:41:54 | Agent | `stream_agent completed (keepalives=140)` |

### 確定した事実

1. **MCP Server コンテナは終了していなかった** — 接続切断後も Ping に応答し続けている
2. **MCP Server のツール呼び出しは完全に直列** — 10 グループ並行でも 1 つずつしか処理されない
3. **切断されたのは Composer の MCPClient のみ** — メインの MCPClient（Ping を送っている方）は正常
4. **MCP 自動再接続は発動しなかった** — Composer Agent 内部のエラーはメインの MCPReconnectHandler の対象外

### MCP Server が直列処理になる原因（コードから確認）

- `run_python` は `def run_python()`（同期関数）
- FastMCP は同期関数を `await fn()` で直接呼ぶ（`asyncio.to_thread` を使わない）
- → `run_python` 実行中、MCP Server のイベントループがブロックされる
- → 他のリクエストの SSE レスポンスを返せない

### Strands SDK の `Connection to the MCP server was closed` 発生メカニズム（ソースコードから確認）

```python
# strands/tools/mcp/mcp_client.py の _invoke_on_background_thread 内
async def run_async() -> T:
    invoke_event = asyncio.create_task(coro)
    tasks = [invoke_event, close_future]
    done, pending = await asyncio.wait(tasks, return_when=FIRST_COMPLETED)
    if done.pop() == close_future:
        raise RuntimeError("Connection to the MCP server was closed")
```

`close_future` が完了する条件: `_async_background_thread` の `async with self._transport_callable()` が例外で抜けた時（= Streamable HTTP トランスポートが切断された時）

### 未確定の部分

- Composer の MCPClient がリクエストを送信してから切断されるまでの正確な時間
- トランスポートが切断された具体的な理由（httpx タイムアウト？MCP Server 側の SSE ストリーム無応答？）
- `httpx.Timeout(120, read=300)` のどのパラメータが該当するか

### 仮説（蓋然性が高いが未確定）

Composer のグループ B が `run_python` を MCP Server に送信 → MCP Server はグループ A の `run_python`（38 秒）を処理中でイベントループがブロック → グループ B のリクエストは TCP レベルでは受け付けられるが、HTTP レスポンスが返らない → Streamable HTTP の SSE ストリームが開かれたまま無応答 → 一定時間後にクライアント側のトランスポートが切断 → `close_future` 完了 → `RuntimeError("Connection to the MCP server was closed")`

---

## 2. OTel / CloudWatch Logs の調査

### AgentCore Runtime の OTel 構成

- Agent / MCP Server ともに `CMD ["opentelemetry-instrument", "python", ...]` で起動
- `aws-opentelemetry-distro>=0.10.0` が ADOT auto-instrumentation を提供
- AgentCore Runtime がデフォルトで ADOT 環境変数を設定し、ログ・トレースを CloudWatch に送信

### 発生していた問題

| 問題 | 原因 |
|---|---|
| `Failed to export span batch code: 400` が 5 秒ごとに出続ける | CloudWatch Transaction Search の送信先が `XRay`（`CloudWatchLogs` ではない）。トレースバッチが 1MB 超え |
| `Upload too large: 1073006 bytes exceeds limit of 1048576` | 会話履歴全体がスパン属性に含まれ、バッチサイズが 1MB を超える |

### 試した設定と結果

| 設定 | Agent ログ | MCP Server ログ | トレース 400 エラー |
|---|---|---|---|
| なし（デフォルト） | ✅ 出る（OTel JSON ラッパー付き） | ✅ 出る | ❌ 5 秒ごとに出る |
| `DISABLE_ADOT_OBSERVABILITY=true` | ❌ 出なくなる | ✅ 出る | ✅ 消える |
| `OTEL_TRACES_EXPORTER=none` | ✅ 出る | ✅ 出る | ✅ 消える |

### 結論

**`OTEL_TRACES_EXPORTER=none` が正解。** ADOT のログ転送パイプラインは維持しつつ、問題のトレースエクスポートだけ無効化する。

### `DISABLE_ADOT_OBSERVABILITY=true` の挙動（ドキュメントから確認）

> Setting this variable to `true` unsets the AgentCore runtime's default ADOT environment variables, ensuring that none of the default ADOT configurations are set.

目的は「他の observability プラットフォーム（Langfuse 等）を使う場合」。ADOT のログ転送も含めて全て無効化されるため、Agent のアプリケーションログが CloudWatch に出なくなる。

---

## 3. MCP Server ツールログの実装

### 問題

MCP Server 側のログは `Processing request of type CallToolRequest` としか記録されず、ツール名・引数・所要時間・エラー内容が分からなかった。

### 実装

```python
# mcp-server/server.py
_original_tm_call_tool = mcp._tool_manager.call_tool

async def _logged_tm_call_tool(name, arguments, **kwargs):
    logger.info("tool_start: tool=%s", name)
    t0 = time.time()
    try:
        result = await _original_tm_call_tool(name, arguments, **kwargs)
        logger.info("tool_ok: tool=%s, duration=%.1fs", name, time.time() - t0)
        return result
    except Exception as e:
        logger.error("tool_fail: tool=%s, duration=%.1fs, error=%s", name, time.time() - t0, str(e)[:500])
        raise

mcp._tool_manager.call_tool = _logged_tm_call_tool
```

### なぜ `mcp.call_tool` のモンキーパッチが効かなかったか

FastMCP は初期化時に `self._mcp_server.call_tool()(self.call_tool)` で低レベルサーバーにバウンドメソッドを登録する。後から `mcp.call_tool = ...` で差し替えても、低レベルサーバーが持つ参照は変わらない。`mcp._tool_manager.call_tool` をラップすることで、内部呼び出しパスで確実にログが出る。

---

## 4. Agent レベルエラーの UI 表示

### 問題

Bedrock API エラー（`ValidationException: Too much media` 等）が発生すると、`basic_agent.py` が `{"status": "error", "error": "..."}` を SSE に送出するが、`strandsParser.js` にハンドラがなく UI に何も表示されなかった。

### 実装（コミット `012f8ec`、PR #118）

```javascript
// strandsParser.js
if (json.status === 'error' && json.error) {
    const msg = json.error;
    let errorMessage;
    if (msg.includes("Too much media") || msg.includes("too long")) {
        errorMessage = "⚠️ This conversation is too long...";
    } else if (msg.includes("ThrottlingException") || ...) { ... }
    // ...
}
```

### 未マージだった理由

`fix/webui-agent-error-display` ブランチに残ったまま main にマージされていなかった。本セッションでマージ + WebUI デプロイで解消。

---

## 5. Cognito パスワードリセット問題

### 症状

ユーザーがこれまでのパスワードでログインできなくなった。

### 原因（CloudTrail から確認）

5/3 17:54 JST に CloudFormation が `UpdateUserPoolClient` を呼んだ際、`ExplicitAuthFlows` パラメータが**送信されなかった**。Cognito の `UpdateUserPoolClient` API は省略されたパラメータをデフォルト値にリセットするため、`ALLOW_USER_PASSWORD_AUTH` が消えた。

### 時系列

| 時刻 | イベント | ExplicitAuthFlows |
|---|---|---|
| 5/3 11:21 | カスタムリソース更新 | ✅ USER_PASSWORD_AUTH 含む |
| 5/3 17:54 | CloudFormation 更新 | ❌ 送信なし → デフォルトにリセット |
| 5/3 18:18 | カスタムリソース更新（PR #112） | ✅ USER_PASSWORD_AUTH 含む（復旧） |

### 教訓

CDK の `UserPoolClient` リソースで OAuth スコープを変更すると、CloudFormation テンプレート上で `ExplicitAuthFlows` が変更対象に含まれない場合がある。カスタムリソース（`UpdateCognitoCallbackUrls`）が全パラメータを明示的に設定するため、デプロイ後に自動復旧する。
