# HANDOFF — commerce-agents 精读

写给没有上下文的接手者。**开工先读这份，再读同目录的 `NOTES.md`（第 89 条起）。** 新进度和新笔记只写进这两个文件，不再写进上级目录的 `HANDOFF.md` / `NOTES.md`。

## 目标与定位

用户要把这个仓库写进简历，并能扛住面试追问。定位是「基于 Anthropic 参考实现做二次开发」，不要把官方框架、文章里的数字或 demo 说成个人原创或线上经历。
总方案在上级目录的 `../COMMERCE_INTERVIEW_PLAN.md`，那个文件不在本仓库里。内容包括六个机制的阅读入口、实验、计划中的个人贡献和简历模板。

- 范围：只深入 retail + shopping + Messages API，商家审批只做对照。
- 计划中的个人主贡献：PostgreSQL 持久化购物车和会话、原子化的终态约束、请求幂等、多 worker 故障测试。验证手段是快照式行为 eval runner 和真实成本/延迟基线。**这些都还没开始做。**
- 已否决的方案：只加一把 Redis 锁。单靠它得不到持久化、业务事务和请求幂等。

## 教学方式

- 每轮追问都要出一道可证伪的预测题，先让用户答，再用探针实测。
- 能实测就实测。探针写在 scratchpad 里，用完删掉，**不要提交削弱护栏的改动**。
- 回答时说清证据强度：读过代码、跑过测试，还是只是推断。

## 已读完的调用链

```
POST /api/chat
→ StorefrontHost.chat()            # append_user_turn 先写进 record.messages
→ host.stream_turn()               # 立刻返回 StreamingResponse
→ 客户端开始消费 SSE 时才进入 event_stream()
→ ShoppingAgent.stream_turn()
    → _prefetch                    # 并行读 preferences/account/cart/memory
    → build_dynamic_context → build_system_blocks
    → for round_index:
        build_request_messages + messages.stream()
        → tool_uses → dispatcher.collect → executor.execute → dispatch → handler
        → messages.append(user, tool_result × N)
    → compact_history → turn_complete
→ else: spawn_background(update_memory)    # 不 await
→ BackgroundTask(write_back)               # 流结束后 sessions.save
```

| 主题 | 笔记 |
|--|--|
| 单测不等于行为评测；限量、幂等、跨进程是三种不同保证 | 89, 90 |
| host 层：写会话、进循环、落盘是三步；两种后置调度 | 91, 92 |
| orchestrator 循环：prefetch、tool_use 回灌、三个出口 | 93–96 |
| executor：`seen_products` 白名单、分发表、三层失败、absent 优先 | 97–100 |
| gates：加购上限按写后总量算 | 101 |
| prompt caching：三个断点、动态 context 的位置、`tool_choice` 与滚动断点 | 102, 103 |

## 最近一次进度（2026-09-26）

读了 `commerce_common/prompt_assembly.py` 里的 `build_system_blocks` / `build_request_messages`，以及 `shopping_agent/prompt.py` 的 `build_dynamic_context`。用探针实测了四件事：
- 断点位置
- 不修改会话历史
- 单条消息不加断点
- `rolling_breakpoint=False` 时不加断点

请求结构：

```
tools…⚑① │ system[0] 静态⚑② │ system[1] 动态 context │ messages…⚑③
```

动态 context 变化时，①② 仍然命中，③ 失效，整段历史重读一次。时钟截到整点，就是为了减少这种失效。

**还没有验证的**：缓存命中/失效是 API 行为，没有用真实请求的 `usage.cache_read_input_tokens` 验证过。

## 未答的预测题（下个 session 冷问，先答再实测）

1. 把 `dispatch` 里 `_absent` 那个 if 挪到 components 之后，`enable_cart=False` 时调用 `checkout` 会怎样？（答案对照 NOTES 100）
2. 一次 `remember_products` 写入 p-0..p-200 共 201 个商品后，`len(seen_products)` 和 `check_provenance(state, "p-0")` 分别是什么？
3. 上限 24，两个并发请求各加 20，如果只去掉进程内的购物车锁，最终数量是多少？

## 下一步只做一件事

补上缓存的真实请求实测：同一会话连发两轮，第一轮购物车不变，第二轮改购物车，对比两轮的 `cache_read_input_tokens` / `cache_creation_input_tokens`。
验收：用户能用这两组数字说明 ①②③ 各自是命中还是失效。

## 环境与陷阱

- Python 用 `.venv/bin/python`。shell 里没有 `python` 命令。
- Claude Code 进程预置了 `ANTHROPIC_BASE_URL`，会覆盖 `.env`，所以在 Bash 里直接跑会 401。发真实请求时要在命令里显式覆盖。
- `examples/retail/.env` 被 git 忽略，里面预设了中转 base URL，token 由用户自己填。**不要把它的内容写进任何会提交的文件。**
- 本仓库是 **公开** fork（`origin` = 用户自己的 fork，`upstream` = 官方）。提交前检查有没有私人环境细节。
- 局部验证命令，结果是 71 passed：

```bash
.venv/bin/python -m pytest shopping-agent/core/tests/test_gates.py merchant-agent/core/tests/test_gates.py tests/test_turn_loop.py examples/demo_common/tests/test_sessions.py examples/demo_common/tests/test_host.py -q
```

- 已知的未解决问题：`host.py` 的冲突分支会更新版本号后重新保存当前 turn，可能丢掉按钮排队的 note。以后改存储时要明确冲突策略。
