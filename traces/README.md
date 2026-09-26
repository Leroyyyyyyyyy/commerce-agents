# traces

用真实模型跑 retail smoke 对话录下的事件流，用来做机制 1 的验收和缓存分析。解读见 `NOTES.md` 第 104、105 条。

| 文件 | 内容 |
|--|--|
| `retail-3turn.json` | 第一次运行，3 个 turn：`events` |
| `retail-4turn.json` | 第二次运行，多加了第 4 个 turn：`events`，以及每个 turn 的动态 `context` |
| `record_retail.py` | 录制脚本 |

两次运行都在 2026-09-26，模型是 `claude-sonnet-5`（记忆提取用 `claude-haiku-4-5-20251001`），走的是 `examples/retail/.env` 里配置的中转 endpoint。`toolu_*` 和 session id 已替换成占位符，替换后 `tool_use` 和 `tool_result` 仍然一一配对。

## 录制

```bash
env -u ANTHROPIC_BASE_URL .venv/bin/python traces/record_retail.py traces/out.json --fourth-turn
```

Claude Code 的进程环境里预置了 `ANTHROPIC_BASE_URL`，所以要先 unset，`.env` 才会生效。每跑一次花几美分。

## 每一轮的缓存数字

事件流里的 `turn_complete` 只有整个 turn 的累计值，下面的数字来自 orchestrator 每一轮的 INFO 日志。

第一次运行（`retail-3turn.json`）：

| turn | round | input | cache_read | cache_write |
|--|--|--|--|--|
| 1 | 0 | 612 | 0 | 9660 |
| 1 | 1 | 2 | 9660 | 1560 |
| 1 | 2 | 2 | 11220 | 686 |
| 1 | 3 | 2 | 11906 | 652 |
| 1 | 4 | 2 | 12558 | 315 |
| 2 | 0 | 2 | 12873 | 139 |
| 2 | 1 | 2 | 13012 | 362 |
| 3 | 0（强制 `search_policies`） | 3924 | 9660 | 0 |
| 3 | 1 | 2 | 13374 | 1066 |

第二次运行（`retail-4turn.json`）：

| turn | round | input | cache_read | cache_write |
|--|--|--|--|--|
| 1 | 0 | 612 | 0 | 9660 |
| 1 | 1 | 2 | 9660 | 1560 |
| 1 | 2 | 2 | 11220 | 701 |
| 1 | 3 | 2 | 11921 | 1095 |
| 1 | 4 | 2 | **0** | 13096 |
| 2 | 0 | 2 | 13016 | 399 |
| 2 | 1 | 2 | 13415 | 438 |
| 3 | 0（强制 `search_policies`，`saved_memory` 已变） | 4391 | **0** | 9660 |
| 3 | 1 | 2 | 9660 | 5240 |
| 4 | 0（`cart` 已变） | 2 | 9660 | 5473 |
| 4 | 1 | 2 | **9797** | 6186 |
| 4 | 2 | 2 | 15133 | 6747 |
| 4 | 3 | 2 | 21880 | 449 |

加粗的三处无法用代码解释。目前推断是中转把请求分发到了多个上游，导致缓存不共享，还没有证实。
