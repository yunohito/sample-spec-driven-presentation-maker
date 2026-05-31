# 2026-05-04: Agent レベルエラーの UI 表示

## 背景

Agent が Bedrock API エラー（`ValidationException: Too much media` 等）で失敗した場合、`basic_agent.py` が `{"status": "error", "error": "..."}` を SSE ストリームに送出するが、`strandsParser.js` にハンドラがなく、UI に何も表示されなかった。ユーザーから見ると「何を打っても無反応」になる。

## 変更内容

### strandsParser.js

`json.status === "error"` のハンドリングを追加。エラーメッセージをカテゴリ別にユーザーフレンドリーなメッセージに変換してチャットに表示する。

| エラーカテゴリ | 検知パターン | 表示メッセージ |
|---|---|---|
| 会話上限 | `Too much media`, `too long` | ⚠️ This conversation is too long for the model to process. Please start a new chat to continue. |
| スロットリング | `ThrottlingException`, `throttl` | ⚠️ The service is temporarily busy. Please wait a moment and try again. |
| モデルタイムアウト | `ModelTimeoutException`, `timed out`, `timeout` | ⚠️ The model took too long to respond. Please try again. |
| モデル未準備 | `ModelNotReadyException`, `not ready` | ⚠️ The model is not ready yet. Please wait a moment and try again. |
| サービス不可 | `ServiceUnavailable` | ⚠️ The service is temporarily unavailable. Please try again later. |
| その他 | 上記以外 | ⚠️ {原文のエラーメッセージ} |

### ChatPanel.tsx

catch ブロックでリトライ可能なエラーと会話上限エラーを区別し、適切なガイダンスを表示。

## 変更ファイル

| ファイル | 変更内容 |
|---|---|
| `web-ui/src/services/strandsParser.js` | `{"status": "error"}` SSE イベントのハンドリング追加 |
| `web-ui/src/components/chat/ChatPanel.tsx` | エラーカテゴリ別のガイダンス表示 |

## コミット

- `012f8ec` fix(web-ui): surface agent-level errors in chat UI
- ブランチ `fix/webui-agent-error-display` から `main` にマージ
- `deploy_webui.sh` で WebUI のみデプロイ済み
