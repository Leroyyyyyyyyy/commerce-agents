# HANDOFF — commerce-agents 精读

写给没有上下文的接手者。**开工先读这份，再读同目录的 `NOTES.md`（第 89 条起）。** 新进度和新笔记只写进这两个文件，不再写进上级目录的 `HANDOFF.md` / `NOTES.md`。

## 目标与定位

用户要把这个仓库写进简历，并能扛住面试追问。定位是「基于 Anthropic 参考实现做二次开发」，不要把官方框架、文章里的数字或 demo 说成个人原创或线上经历。
总方案在上级目录的 `../COMMERCE_INTERVIEW_PLAN.md`，那个文件不在本仓库里。内容包括六个机制的阅读入口、实验、计划中的个人贡献和简历模板。

- 范围：只深入 retail + shopping + Messages API，商家审批只做对照。
- 个人主贡献主线：PostgreSQL 会话/购物车持久化及事务限量已有可选实现，真实双进程测试通过。**请求幂等及完整的重放/中断恢复仍未实现。** 验证线有首个购物车 case 和最小 eval runner；真实模型行为基线、成本/延迟比较尚未完成。用户明确要求先进入 PostgreSQL，不等待行为基线。
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
| prompt caching：三个断点、动态 context 的位置、`tool_choice` 与滚动断点、真实 trace、中转下的异常、skills | 102–106 |
| 会话与记忆的寿命；删除记忆与后台提取的竞态 | 107, 108 |
| 对照阅读：Codex `/goal` 的续跑、完成声明、兜底、并发与注入位置 | 109–112 |

## PostgreSQL 当前进度

用户要求记下四项改造并开始实现：①会话持久化；②购物车持久化；③短事务内限量；④请求幂等。见 NOTES 116–120。用户曾把 ③④ 理解为异步，已解释：同步 worker 也会并发竞争，顺序重试也会重复写；`async` 不替代事务或幂等。

本轮落地 **①②③**：
- `examples/demo_common/postgres.py`：可选同步连接池、迁移 checksum 和 advisory lock；`PostgresSessionStore.require/save` 覆盖 public API，以同一事务保存 state/version/history。拒绝独立 `write_state/write_messages`，避免拆开提交。transcript 第一版是整条 JSONB 数组；不宣称大规模存储优化。
- `examples/demo_common/migrations/001_sessions.sql`、`examples/retail/api/migrations/002_carts.sql`：会话、父购物车、商品行；删除会话级联删车，CHECK 和组合外键限制每行数量，父行锁保护总行数。
- `examples/retail/api/postgres_retail.py`：继承 fixture backend，只替换购物车；同步 SQL 通过线程调用。每次写锁父购物车，按持久化的配置限量；跨 worker 配置不一致时拒绝，不能静默放宽。
- core 的可选 `try_atomic_add_to_cart` / `CartAddition`：provenance/options 仍先检查；SQL add 不再先做进程内 read/check，返回实际增量给 gate。没有能力的原后端保持 fallback。原静态 prompt/tool schemas 未改。
- `examples/demo_common/storefront.py` 支持注入 sessions/startup/shutdown；`retail/api/main.py` 在 `COMMERCE_DATABASE_URL` 非空时选 SQL。启动开 pool、迁移，退出关闭；不设置变量时内存模式仍可用，实测阻止 driver import 后仍能 import 内存 app。
- `host.py` 不再在冲突后强行用新版本覆盖整条 turn：仅对可验证的 pending-note-only 变化 rebase，保留按钮 note；另一条历史或 state 写入则日志记录并拒绝覆盖。**不是任意并发 chat 的合并/续跑方案。**

运行与边界统一见 `docs/postgres.md`；可选依赖 `requirements-postgres.txt`，本地服务 `compose.postgres.yaml`。没有修改任何 `.env`，默认仍用内存。验收使用独立临时数据库，不包含模型请求。

最新验证：全仓 **1151 passed, 1 skipped**（真实 SQL 20 条 + 无数据库 extension tests 5 条），lint/format/`scripts/check.py`/diff-check 通过。真实两个解释器各 +20 → 实际增量 20、4 → 最终 24；争抢最多 1 行也不超限；两组并发测试另重复 3 次通过。新解释器可恢复已保存会话、provenance 和购物车；还测了 SQL 约束、提交前回滚、取消等待后写线程仍提交、HTTP session/chat/cart/reset。

**明确未实现**：④操作账本和稳定请求 ID；SQL 持久记忆/商家状态；同一 session 并发 chat 调度；模型调用中的 checkpoint。已经提交但回复丢失时再 add 仍会重复加，测试明确得到 2。`to_thread` 的等待被取消不代表 SQL 被取消，测试得到外层 CancelledError 但最终车里 1 件。

## 已完成的缓存阅读（2026-09-26）

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

同日用真实模型跑了 `smoke_chat.py --vertical retail`（SMOKE PASSED），每一轮的缓存数字见 NOTES 104：
- turn 2 整段历史都命中（read 12873，write 139）。
- turn 3 被 `search_policies` 强制调用，round 0 只命中 ①②，3924 tokens 的历史按原价计费。

trace 和每一轮的缓存数字在 `traces/` 目录，录制脚本是 `traces/record_retail.py`，用法见 `traces/README.md`。

## 未答的预测题（下个 session 冷问，先答再实测）

1. 把 `dispatch` 里 `_absent` 那个 if 挪到 components 之后，`enable_cart=False` 时调用 `checkout` 会怎样？（答案对照 NOTES 100）
2. 一次 `remember_products` 写入 p-0..p-200 共 201 个商品后，`len(seen_products)` 和 `check_provenance(state, "p-0")` 分别是什么？
3. 上限 24，两个并发请求各加 20，如果只去掉进程内的购物车锁，最终数量是多少？

## 下一步只做一件事

机制 6 已新增 `scripts/eval_retail.py`、`evals/retail/cases/add-known-product.json` 和使用说明 `evals/retail/README.md`。首个 case：已见 AR-1202、空购物车，只加 AR-1202 x1。`score_cart` 直接检查 backend 购物车的精确 ID/数量、无额外商品；不锁死工具顺序。

本轮验证：`tests/test_retail_eval.py` **22 passed**；全仓 lint、format、`scripts/check.py` 通过。全仓 pytest 在显式设置官方 `ANTHROPIC_BASE_URL` 后 **1126 passed, 1 skipped**；未设置时两条官方 endpoint 断言受环境覆盖而失败。没有修改这些平台测试，没有发真实模型请求。

runner 每个 trial 新建 backend/session/transcript/内存记忆，不导入 retail app、不提取后台记忆。超时/异常单独记 `error`，即使购物车写入已成功也不能判通过；报告保存快照、指纹、事件、终态和 verdict，默认输出到被 gitignore 的 `evals/retail/reports/`，拒绝覆盖已有基线。当前 case schema 只支持 seen_products、初始 cart、turns 和 cart_exact，不支持全部 skill 中列出的 scorer，也不测 HTTP/SSE 或持久化。

机制 3 暂时跳过（用户决定）；机制 4、5 已完成（NOTES 102–108）。

用户改了推进顺序：已先完成 PostgreSQL 持久化/事务限量初版。**下一步只做④：先给直加购物车接口定义稳定操作 ID 和重放契约，再加事务内操作账本及唯一约束。** 不要先用商品参数 hash 或模型 tool_use ID 做去重；同一重试复用 ID，不同操作即使参数相同也用新 ID。
- 首先覆盖 `POST /api/cart/add`（单操作、没有模型重生成）；明确 scope/session、请求 payload 冲突处理、并发重放、提交前失败、提交后响应丢失。完成记录与车写入同事务；按钮 note 与会话版本冲突也需考虑，不能重试时再次加车。
- chat 请求去重是另一层：需定义 ingress 请求 ID、正在执行/已完成/中断状态，不能把一次 regenerated turn 当成原 turn 自动重跑；不能拿直加接口的幂等冒充整轮 Agent 恢复。
- 行为基线仍是未完成任务，可随时用默认 MockRetail 路径补跑；eval runner 不导入 retail main，设置 SQL 模式不会自动让它变成 PostgreSQL eval。
- 运行：`.venv/bin/python scripts/eval_retail.py --trials 3`；真实请求需先确认有效 endpoint，遵循 demo 环境加载优先级，不能把凭证写进报告。可用 `--model` 固定模型、`--output` 固定报告路径。
- 计划书末尾的真实 trace 起步任务已完成，不必重跑起点。
- 用户已答对：只断言总件数无法发现错商品；进一步明确需要商品 ID、逐项数量且无额外商品。下轮让用户合上源码重写 `score_cart`，再读 `run_trial`。
- 读 `plugins/commerce-builder/skills/commerce-evals/SKILL.md`，第一遍聚焦 The case shape、Authoring rules、Scorers；先跑通一个 case，再扩到 10–15 条，暂不加 LLM judge。
- 第一版 10–15 个用例：预算约束、没见过的商品 ID、超出数量上限、注入攻击等。检查最终的购物车状态和 UI 数据，不检查模型怎么措辞。
- 验收：用当前代码跑出一份基线报告，每个用例跑多次，记录通过率和波动。

机制 1 的端到端调用链验收三问，用户还没有自己答过。

## 环境与陷阱

- Python 验证命令统一使用 `.venv/bin/python`。
- demo 凭证加载顺序是进程环境 > 示例 `.env` > 仓库根 `.env`；发真实请求前确认 endpoint 和凭证来自预期配置。
- `.env` 和本地评测报告被 git 忽略，不提交凭证、连接密码或未经检查的原始模型输出。
- 本仓库是公开 fork；提交到 `origin`，不把个人环境说明或私有链接写进仓库。
- 局部验证命令，结果是 71 passed：

```bash
.venv/bin/python -m pytest shopping-agent/core/tests/test_gates.py merchant-agent/core/tests/test_gates.py tests/test_turn_loop.py examples/demo_common/tests/test_sessions.py examples/demo_common/tests/test_host.py -q
```

- 全仓确定性测试可运行：`ANTHROPIC_BASE_URL=https://api.anthropic.com .venv/bin/python -m pytest -q`。平台测试用 stub credentials 构造客户端而不发请求；固定 endpoint 是为了防止环境影响断言，不是运行真实 eval 的凭证配置。
- 原 `host.py` 强制覆盖导致按钮 note 丢失的路径已改为保守 rebase；另一条 transcript/state 写入和压缩后无法确认基线的冲突仍不自动合并，SSE 已输出的回合不能改成 HTTP 409，只记录日志而不覆盖。需要后续 session 调度/请求状态设计。
- SQL 集成测试需显式设置独立测试库 `COMMERCE_TEST_DATABASE_URL`；每个测试创建随机 schema 并仅删除自己的 schema。不设置时 20 条 SQL 测试会 skip，不能把 skip 当数据库验证通过。平台单测仍需固定官方 `ANTHROPIC_BASE_URL` 防环境影响。
- 临时验收容器已清理；使用新模式按 `docs/postgres.md` 配置本地服务与 `COMMERCE_DATABASE_URL`，不要在交接或报告里写连接密码。
