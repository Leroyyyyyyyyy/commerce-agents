# HANDOFF — commerce-agents 精读

写给没有上下文的接手者。**开工先读这份，再读同目录的 `NOTES.md`（第 89 条起）。** 新进度和新笔记只写进这两个文件，不再写进上级目录的 `HANDOFF.md` / `NOTES.md`。

## 目标与定位

用户要把这个仓库写进简历，并能扛住面试追问。定位是「基于 Anthropic 参考实现做二次开发」，不要把官方框架、文章里的数字或 demo 说成个人原创或线上经历。
总方案在上级目录的 `../COMMERCE_INTERVIEW_PLAN.md`，那个文件不在本仓库里。内容包括六个机制的阅读入口、实验、计划中的个人贡献和简历模板。

- 范围：只深入 retail + shopping + Messages API，商家审批只做对照。
- 个人主贡献主线：PostgreSQL 会话/购物车持久化及事务限量已有可选实现，真实双进程测试通过。**直加接口的请求幂等已实现；整轮 Agent 重放/中断恢复仍未实现。** 验证线有首个购物车 case 和最小 eval runner；真实模型行为基线、成本/延迟比较尚未完成。用户明确要求先进入 PostgreSQL，不等待行为基线。
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

代码已提交并推送到 `origin/main`，交付 commit 为 `3b6384f`（eval 初版与 PostgreSQL 第一阶段）。随后教学笔记更新尚未包含在该 commit 中。

第一阶段落地 **①②③**（已提交的持久化实现）：
- `examples/demo_common/postgres.py`：可选同步连接池、迁移 checksum 和 advisory lock；`PostgresSessionStore.require/save` 覆盖 public API，以同一事务保存 state/version/history。拒绝独立 `write_state/write_messages`，避免拆开提交。transcript 第一版是整条 JSONB 数组；不宣称大规模存储优化。
- `examples/demo_common/migrations/001_sessions.sql`、`examples/retail/api/migrations/002_carts.sql`：会话、父购物车、商品行；删除会话级联删车，CHECK 和组合外键限制每行数量，父行锁保护总行数。
- `examples/retail/api/postgres_retail.py`：继承 fixture backend，只替换购物车；同步 SQL 通过线程调用。每次写锁父购物车，按持久化的配置限量；跨 worker 配置不一致时拒绝，不能静默放宽。
- core 的可选 `try_atomic_add_to_cart` / `CartAddition`：provenance/options 仍先检查；SQL add 不再先做进程内 read/check，返回实际增量给 gate。没有能力的原后端保持 fallback。原静态 prompt/tool schemas 未改。
- `examples/demo_common/storefront.py` 支持注入 sessions/startup/shutdown；`retail/api/main.py` 在 `COMMERCE_DATABASE_URL` 非空时选 SQL。启动开 pool、迁移，退出关闭；不设置变量时内存模式仍可用，实测阻止 driver import 后仍能 import 内存 app。
- `host.py` 不再在冲突后强行用新版本覆盖整条 turn：仅对可验证的 pending-note-only 变化 rebase，保留按钮 note；另一条历史或 state 写入则日志记录并拒绝覆盖。**不是任意并发 chat 的合并/续跑方案。**

运行与边界统一见 `docs/postgres.md`；可选依赖 `requirements-postgres.txt`，本地服务 `compose.postgres.yaml`。没有修改任何 `.env`，默认仍用内存。验收使用独立临时数据库，不包含模型请求。

第一阶段验证：全仓 **1151 passed, 1 skipped**（真实 SQL 20 条 + 无数据库 extension tests 5 条），lint/format/`scripts/check.py`/diff-check 通过。真实两个解释器各 +20 → 实际增量 20、4 → 最终 24；争抢最多 1 行也不超限；两组并发测试另重复 3 次通过。新解释器可恢复已保存会话、provenance 和购物车；还测了 SQL 约束、提交前回滚、取消等待后写线程仍提交、HTTP session/chat/cart/reset。

**当前明确未实现**：Agent/chat 操作身份和整轮恢复；SQL 持久记忆/商家状态；同一 session 并发 chat 调度；模型调用中的 checkpoint。没有操作 ID 的 Agent backend add 仍会重复加，测试得到 2。`to_thread` 的等待被取消不代表 SQL 被取消；直加接口现在靠账本解决已提交后的重试。

### ④直加请求幂等

用户要求先写测试再实现，只覆盖 retail `POST /api/cart/add`，不改整轮 Agent。先用旧路径实测红灯：同一 ID 重试为 2 件、换参数仍 200，再实现以下代码：
- `examples/retail/api/cart_add.py`：`RetailCartAddRequest` 要求 UUID `operation_id`，拒绝未知字段；默认 quantity=1，ID 的不同拼写规范化。同次重试复用 ID，新操作即使参数相同也换 ID。SQL 路径调用 `PostgresRetail.direct_add_once`；内存 demo 只有进程内结果缓存。
- `examples/retail/api/migrations/003_cart_add_operations.sql`：保存规范化 payload、HTTP status/body；主键 `(session_id, operation_id)`，会话删除级联删账本。新迁移由 main 启动加载，未编辑已应用的 001/002。
- `examples/retail/api/postgres_retail.py`：锁 session、先查完成记录，未完成时用同一连接调用共享 `gated_add_to_cart`（事务 adapter）和原有父购物车限量逻辑。cart、结果和按钮 note 同事务提交；note 使用实际增量。追加最新 session 文档的 note 并增加 version，不覆盖 messages，也不修改依赖加载的旧 record，所以退出时不会用旧副本再次保存。
- 完成重试返回原 status/body（旧购物车快照，不是实时车）；同 ID 换 product_id/quantity 返回 409。业务 400 也缓存；401/422 和提交前异常不创建完成记录。账本保留至 session 删除。
- `examples/demo_common/storefront.py` 仅抽出按钮错误/安全 note 格式化供两条路径共用，其他 vertical 的接口契约不变。Agent add/update/remove 仍用原逻辑，没有 operation ID。
- 零售前端 `lib/api.ts` 发送 ID 并区分未知结果（网络/解析/5xx）与确定拒绝。`components/ProductTile.tsx:AddButton` 按按钮意图保留 ID；不同按钮即使同商品也不同 ID，成功或确定拒绝后再加用新 ID。`onAdd` 类型经 presentation 组件传递。待重试 ID 只在组件内存里，刷新页面不保留；没有宣称浏览器重启恢复。
- `tests/test_retail_cart_idempotency.py`：20 条真实 SQL 测试，覆盖四项验收、并发 HTTP/双进程、跨 pool、gate/业务拒绝重放、规范化、SQL 唯一约束/级联删除、提交前失败、提交后响应失败和竞争会话 note。SQL fixtures 复用 `tests/test_postgres_retail.py` 的独立 schema。原 backend 重复加测试更名，明确保留 Agent 非幂等边界。
- `examples/retail/storefront-web/lib/api.test.cjs`：6 条 Node stub 测试（TS transpile + fetch/hook stub），验证 ID 生命周期、不同按钮和失败分类；不是浏览器 E2E。

本轮验证：全仓 **1172 passed, 1 skipped**，其中真实 SQL **40 条实际运行**；lint/format/`scripts/check.py`/diff-check 通过；零售前端 6 tests 和 Next.js production build 通过。未调用模型，未修改 `.env`。验收临时数据库容器已清理。运行说明与重放边界见 `docs/postgres.md`，笔记 124–125。

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

用户改了推进顺序：PostgreSQL ①②③ 和直加接口④均已完成。**用户已要求将④代码、测试、文档和笔记提交并推送到 `origin/main`。下一步讲解已交付的④实现；不要自行扩到整轮 Agent 恢复。** 可证伪预测题：第一次 +1 成功后，另一个新 ID 再 +1；重试第一次 ID 的响应里是 1 还是 2，GET 购物车又是多少？证据见原响应重放测试和 NOTES 124。
同一重试复用 ID，不同操作即使参数相同也用新 ID；不能拿商品参数 hash 或重新生成的模型 tool_use ID 当操作身份。
- 本轮用户要求结合代码解释 PostgreSQL 实现：按 main 的模式选择、session 的 version/state/history、cart 父行锁与实际增量、subprocess 双进程和提交前故障测试讲解。提交后的 caller baseline 更新见 NOTES 121；没有再次运行 SQL/模型请求。
- 已结合 `tests/test_postgres_retail.py:WORKER` / `test_two_processes_cannot_exceed_quantity_or_line_cap` 解释 Popen、独立 pool、ready/go 启动屏障与终态断言。纠正证据表述：统一放行不保证 SQL 步骤确实重叠，现有测试没有锁等待断言；见 NOTES 122。本轮没有重跑 SQL。
- 未答预测题：把第一个 worker 的启动从 Popen 改成 run、其他 barrier 代码不变，会怎样？先答再用无数据库的临时进程探针验证。
- 用户已预测“加购提交后，会话 save 失败会撤销购物车”，并用真实 PostgreSQL 临时探针纠正：cart 1→1；save 抛 TypeError；session version 仍为 1、messages/seen_products 均为空。两个事务不互相撤销，见 NOTES 123。探针和临时容器已清理，没有业务代码改动或模型请求。
- 已覆盖 `POST /api/cart/add` 的 session scope、payload 冲突、并发重放、提交前失败、提交后响应丢失，以及按钮 note/会话版本冲突；不要再把这些写成待实现。
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
- SQL 集成测试需显式设置独立测试库 `COMMERCE_TEST_DATABASE_URL`；每个测试创建随机 schema 并仅删除自己的 schema。不设置时 40 条 SQL 测试会 skip，不能把 skip 当数据库验证通过。平台单测仍需固定官方 `ANTHROPIC_BASE_URL` 防环境影响。
- 临时验收容器已清理；使用新模式按 `docs/postgres.md` 配置本地服务与 `COMMERCE_DATABASE_URL`，不要在交接或报告里写连接密码。
