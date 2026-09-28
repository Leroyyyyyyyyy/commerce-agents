# NOTES — Commerce Agents 精读笔记

格式固定为「现象 → 规则」成对：**现象**是实测到的具体结果，**规则**是可迁移的约束。编号接着总学习笔记（`agent-engineering-log/NOTES.md`，1–88 条），从 89 开始，HANDOFF 里的「NOTES 99」之类引用都指这里。新的 commerce-agents 笔记只写进这个文件。

### 89. 确定性测试通过，不代表模型行为已经被评测

**现象**:在 Commerce Agents 中运行购物车护栏、商家护栏、turn loop、会话和 host 五个测试文件，得到 **71 passed**；但 `plugins/commerce-builder/skills/commerce-evals/SKILL.md` 明确写着仓库没有提供 eval harness。已有测试能验证给定调用如何执行，不能据此知道真实模型会不会选对工具、遵守预算或正确引用上一轮卡片。

**规则**:Agent 验证要分开两层：确定性测试验证执行器和约束，真实模型 trials 验证决策行为。前者的通过率不能写成后者的任务成功率；评测规范也不等于已经存在可运行的评测系统。

(证据强度:本地实测 71 条通过 + 阅读仓库评测 skill；未做真实模型行为评测)

### 90. 限量、幂等和跨进程一致性是三个不同保证

**现象**:Commerce Agents 的 `gated_add_to_cart` 在 session 对应的 `asyncio.Lock` 内执行读购物车、计算允许增量和写回；现有并发测试中两个请求各加 20、上限 24，最终数量为 **24**。锁存放在进程内 `WeakValueDictionary` 中；默认 `SessionStore` 的状态和历史也存放在内存 dict。

**规则**:数量上限保证“最终不超过阈值”，不保证“重试同一请求不会再加一次”；进程内锁协调同一事件循环的任务，不协调多个 worker。迁移共享数据库时，要分别设计最终状态约束、操作幂等键、跨 worker 的事务并发控制；只换会话存储或只增加一把锁，都不能自动得到其余保证。

(证据强度:单进程上限测试已通过；锁和存储范围由源码确认；多 worker 行为尚未实测)

### 91. 用户消息写入会话，和进入 Agent 循环，不是同一步

**现象**:`POST /api/chat` 的 handler 只把请求转给 `StorefrontHost.chat()`。`chat()` 里先调 `append_user_turn(record, request.message, ...)`，把用户消息 `append` 进 `record.messages`，然后才返回 `stream_turn(...)`。`stream_turn` 本身立刻返回 `StreamingResponse`；真正的 `agent.stream_turn(record.messages, session, record.state)` 写在内部的 `event_stream()` 生成器里，要等 SSE 开始被消费才会跑。会话落盘更晚：`BackgroundTask(write_back)` 在流结束之后才 `sessions.save(record)`。

**规则**:流式接口上要拆开三件事：**写进当前请求的内存会话**（handler 同步完成）、**进入 Agent 循环**（生成器被迭代时才开始）、**持久化**（流结束后的 background task）。`host.chat()` 返回不等于模型已经开始跑；用户这轮话在进入循环之前就已经在 `record.messages` 里了。

(证据强度:读 `storefront.py:111-119,204-206` 与 `host.py:141-172,216-221`；未跑真实对话)

### 92. 记忆提取和会话写回都是后置，调度器不同，没有先后保证

**现象**:`host.py:stream_turn` 的 `event_stream()` 是 `try / except / else`。`else` 只在 `agent.stream_turn(...)` 正常耗尽、没有进 except 时执行，里面是 `spawn_background(agent.update_memory(...))`：`create_task` 后把 task 放进 `_background_tasks` 防 GC，**不 await**。`sessions.save(record)` 在 `BackgroundTask(write_back)` 里，Starlette 在 **HTTP 流结束之后**才跑。认证失败或其它 except 会 yield 一条 `error` SSE，**不走 else**，但 `write_back` 仍然会执行，因为 `background=` 在构造 `StreamingResponse` 时就挂上了。

**规则**:后置动作要问三件事：**谁调度、等不等待、失败了还跑不跑**。记忆提取是 event-loop 上的 fire-and-forget 任务，会话写回是响应生命周期上的 BackgroundTask。前者启动早于写回（在生成器 `else` 里、最后一个 SSE 已经 yield 之后），但不保证先完成；下一轮 `_prefetch` 也可能还看不到这轮刚抽出的 facts。异常路径上「不提取、仍写回」是刻意的：transcript 要保住，记忆提取不能在失败轮次上再打一次模型。

(证据强度:读 `host.py:77-85,170-221` 与 `test_host.py:77-92`；Python `try/else` 语义；未测客户端断连时 `CancelledError` 是否仍跑 `write_back`)

### 93. 用户消息已经在 `messages` 里之后，循环第一件事是 prefetch，不是模型调用

**现象**:`ShoppingAgent.stream_turn` 进 `for round_index` 之前，先 `await self._prefetch(session)`：`asyncio.gather` 并行拉 preferences / account / cart / memory facts，单个失败被 `fetched()` 吃掉变成 `None`，turn 继续。然后才 `build_dynamic_context` → `build_system_blocks` → 建 executor → `first_forced_tool`。循环默认 `range(max_tool_iterations + 1)`（8+1=9 次模型调用上限）；最后一轮 `tool_choice=none`。流式过程中 `EagerDispatcher` 在 `content_block_stop` 且 JSON 可解析时就开始 `execute`，与模型写同一轮剩余内容重叠。`close_on_presentation` 默认开：一轮里有成功的 `present_suggestions` 且全部 `ends_clean`，就 `break`，不再要收尾模型调用。测试里 `present_guide` + chips 只打了 **1 次**模型。外层 `finally` 调 `close_open_tool_uses`：host 在 `tool_call` 处 `aclose()` 会补一条 interrupted result；已经等到 `tool_result` 再关，则保留真实结果。

**规则**:一轮 turn 不是「调模型 → 调工具」两步，而是 **prefetch（读侧并行）→ 若干轮（流式生成 ∥ 工具执行）→ compact → yield `turn_complete`**。问「循环第一件事」要答 prefetch；问「工具何时开始跑」要答 block 关闭且 JSON 可读的那一刻，不是整轮 `get_final_message()` 之后。问「为什么有的展示轮没有第二次模型调用」要答 `close_on_presentation`，不是模型自己停了。

(证据强度:读 `orchestrator.py:121-277`、`turn.py:174-223,344-377,520-527,544-572`；`test_orchestrator.py` 的 chips 1-call 与中途 `aclose` 配对测试；prefetch 失败降级测试。未跑真实模型对话)

### 94. 模型提出工具调用 ≠ 工具执行；结果靠一条 user 消息回灌

**现象**:`stream_turn` 的闭环是固定四步，函数名对得上：
1. `client.messages.stream()` 的 `final.content` 里筛 `block.type == "tool_use"` 得到 `tool_uses`（提出）。
2. `EagerDispatcher(executor.execute, ...)` 再 `await dispatcher.collect(tool_uses)`；`collect` 要么 await 已启动的 task，要么当场 `_execute(name, args)`，而 `_execute` 就是构造时传入的 `executor.execute`（执行）。模型自己不跑工具。
3. `messages.append({"role": "user", "content": [tool_result_block(...) for ...]})`：同一轮所有结果打进**一条** user 消息（回灌）。错误也走这条路，`tool_result_block` 带 `is_error`。
4. `for round_index` 下一轮 `build_request_messages(messages)` 把刚 append 的结果发给模型。

`present_guide` + `present_suggestions` 的 FakeClient 测试实测 `len(agent.client.calls) == 1`（本轮复跑 1 passed）。

**规则**:Messages API 的 `tool_use` 是 assistant **输出里的意图**；执行权在编排层的 `executor.execute`。结果必须占 **user 位** 且与 `tool_use` 一一配对，下一轮模型才能读到——这不是风格问题，缺配对就是 400。问「工具在哪执行」不能答「模型调了」。

(证据强度:读 `orchestrator.py:193-270`、`turn.py:183-218,530-536`；FakeClient 测试 1 passed。未跑真实模型对话)

### 95. 三个循环出口不对称：前两个跳过 collect，第三个在 append 之后

**现象**:`for round_index in range(max_tool_iterations + 1)`（默认 8+1=9）。出口有三处，位置不同：
- `if not tool_uses or force_text: break` 在 `dispatcher.collect` **之前**。最后一轮 `force_text` 时 `tool_choice={"type":"none"}`，即便模型吐了 tool_use 也不会执行。
- `close_on_presentation and round_closes_turn(...)` 在 `outcome_events` + `tool_result_block` append **之后**。`round_closes_turn` 要求本轮有 `present_suggestions`（`CHIPS_TOOL`），且每个 call 都过 `executor.ends_clean`（必须是展示工具、未拒绝、结果就是 displayed_text）。
- for 自然耗尽被 `force_text` 那一支提前接住，不会落到「执行完第 9 轮工具再停」。

**规则**:「循环何时结束」要答**哪一个 break、break 时工具跑过没有**。没有 tool_use / 触顶强制文本 = 本轮不执行工具；chips 收尾 = 工具已经执行并写回历史，只是不再打收尾模型。`search_products` 这类非展示工具过不了 `ends_clean`，即使同轮有 chips 也不会 `round_closes_turn`。

(证据强度:读 `orchestrator.py:166-270`、`turn.py:204-212,520-527`、`presentation.py:21`；未构造 search+chips 同轮的反例测试)

### 96. `streamed` 是这一轮的累积器，`stream` 才是模型连接

**现象**:`stream_turn` 里先 `streamed = StreamedRound(...)`，再 `async with client.messages.stream(...) as stream`，然后 `streamed.relay(stream, ...)`。`StreamedRound` 身上有两类字段：构造时传入的 `specs` / `partial_tools` / `state` / `eager_frames`；`relay` 喂事件后填上的 `blocks`（text / thinking / tool_use）、`tools`（每个工具的 `name`、`id`、参数 JSON 的 `buffer`、`closed`）、`usage`、`abandoned`。

**规则**:问「streamed 里有什么」答这一轮已经拆出来的内容块和进行中的工具参数，不答 SDK 的流对象。模型连接是 `stream`；`streamed` 只是把它记下来的那一份。`relay` 内部怎么出 `text_delta` / `ui_partial` 留到「流式执行与结构化 UI」。

(证据强度:读 `orchestrator.py:196-213`、`turn.py:226-377`。未跑真实模型流)

### 97. 搜索写入 `seen_products` 是加购白名单；写购物车先过 gate

**现象**:`_search_products` 在 `backend.search_products` 之后调用 `state.remember_products`，按 `product_id` 写入 `ShoppingSessionState.seen_products`（`remember` 上限 200，超出删最旧）。`_add_to_cart` 调用 `gated_add_to_cart`：`check_provenance` 发现 id 不在 `seen_products` 就返回 `ToolOutcome.held("provenance")`，`backend.add_to_cart` 不执行；有未选规格的 family 同样 held 在 `"options"`。通过之后才在会话锁里读当前购物车、按 `max_quantity_per_item` 截断，再写 backend。`test_gates.py` 两个并发各加 20、上限 24，最终 `item_count == 24`。`_search_policies` 只 `backend.search_policies` 再 `_fenced`，不写 `state`。

**规则**:模型报出的 `product_id` 不是授权。读工具把服务端返回过的记录记进 provenance；写工具在 gate 里查这份名单，再碰购物车。跳过 gate 会同时跳过「没见过的 id」「还没选规格」和「写后超限」。政策文本没有可加购的 id，所以只进 fence、不进白名单。

(证据强度:读 `executor.py:132-166,213-216`、`gates.py:40-44,102-130`、`types.py:194-202`、`commerce_common/types.py:15-25`；`test_gates.py:105-131` 已有 20+20→24。本轮未新跑测试)

### 98. 工具名到函数的表在构造时冻结；`execute` 保证失败也是 `ToolOutcome`

**现象**:`ShoppingToolExecutor.handlers()` 返回 `"search_products": self._search_products` 这类映射。`BaseToolExecutor.__init__` 把它展开进 `self._handlers`，并加上 `save_memory` / `recall_memories`。`execute` 只 `try: dispatch`。`dispatch` 丢掉 `status` 之后，按 absent → skill → 展示组件 → delegate → `self._handlers.get(name)` 分发。未知名字返回 `ToolOutcome.error("Unknown tool: ...")`。handler 抛出的异常被 `execute` 收成 `ToolOutcome.error`，`domain_error` 先处理 `Unavailable` / `NotOffered`。

**规则**:问「`search_products` 怎么找到函数」答构造时写入的 `_handlers["search_products"]`。`execute` 的职责是：无论分发失败还是 handler 抛错，这一轮都拿到 `ToolOutcome`，工具异常结束不了 turn。

(证据强度:读 `execution.py:146-150,214-243`、`executor.py:106-128`。未跑测试)

### 99. 三种工具失败落在三层；只有 `execute` 把异常收住，当前 turn 才会继续

**现象**:`dispatch` 在丢掉 `status` 之后按固定顺序返回：`_absent` → `load_skill` → `components`/`_extensions` → `_delegates` → `_handlers` → `ToolOutcome.error("Unknown tool: ...")`。未知名字（测试 `teleport_products`）在最后一支 **return**，不抛。参数校验在 handler 里：`_search_products` 对 `filters` 调 `parse_argument`，pydantic `ValidationError` 被包成 `InvalidArguments` 抛出；`dispatch` 不接，`execute` 的第一个 `except` 收成 `"{name} arguments were invalid — ..."`。`filters.sort="cheapest"` 实测走这条。backend 自己 `CartItem.model_validate` 抛出的 `ValidationError` 不是 `InvalidArguments`，落到 `except Exception`，文案是 `temporarily unavailable`，且不含 `arguments were invalid`。`RuntimeError("backend down")` 同样进这条；`domain_error` 只先认 `Unavailable` / `NotOffered`。`execute` 不往外抛，所以 `dispatcher.collect` 能返回和 `tool_use` 等长的 outcome，`stream_turn` 无条件 `append` 一条带 `is_error` 的 user `tool_result`，for 循环进入下一轮 `messages.stream`。若 `execute` 抛出，`collect` 抛出，257 行的 append 跳过，当前 turn 退出；外层 `finally` 的 `close_open_tool_uses` 只给已落盘的 `tool_use` 补一条中断结果，避免下次请求 400，不把这一轮交回模型。

**规则**:问「失败在哪处理」要答层：未知工具在 `dispatch` 的 return；模型参数错在 `parse_argument` → `execute` 的 `InvalidArguments`；backend 异常在 `execute` 的 `except Exception`（`domain_error` 优先）。handler 正常返回的 `ToolOutcome.error` / `held` 不进这两个 except。当前 turn 能让模型读到错误并重试，是因为 `execute` 把异常变成 outcome；`close_open_tool_uses` 只管「turn 已经死了，历史还要能配对」。

(证据强度:读 `execution.py:54-61,214-243`、`executor.py:132-138`、`orchestrator.py:249-272`、`turn.py:210-218,530-583`；`test_executor.py` 的 `test_unknown_tool_and_backend_failure_are_soft_errors` 与 `test_only_the_models_own_arguments_are_reported_as_invalid`。同日第二遍用临时探针实测：`buy_now` → `Unknown tool`；`filters.sort="cheapest"` 经 `execute` 得 invalid 文案，直调 `dispatch` 抛 `InvalidArguments`；backend 抛 `ConnectionError("db down at 10.0.0.5")` → `temporarily unavailable`，原始异常文本不进 tool_result)

### 100. 一个名字同时命中多个分支时，`dispatch` 的 if 顺序决定结果；absent 必须在最前

**现象**:`ShoppingAgentConfig(enable_cart=False)` 时，`checkout` 同时在 `executor._absent` 和 `executor.components` 里（实测两个都是 True）。`execute("checkout", {})` 得 `is_error=True`、`checkout is not something this store offers; say so plainly and do not suggest it.`，不是 `Displayed to the customer.`，也不是 `Unknown tool`。原因是 `dispatch` 第一个 if 就是 `name in self._absent`，return 之后展示组件分支走不到。

**规则**:分发表不是互斥的，同一个名字可以同时出现在几张表里，这时靠 if 的先后顺序定优先级。「配置关掉」是权限判断，必须排在所有「怎么执行」的分支之前；如果排在 `components` 后面，一个关掉的展示工具照样会渲染。审一个分发函数，先问「有没有名字能同时命中两支」。

(证据强度:2026-09-24 用临时脚本对真实 `ShoppingToolset(...).executor` 跑出上述结果；读 `execution.py:231-243`)

### 101. 加购的数量上限算的是「这一行还剩多少空间」，不是「这次最多加多少」；add 没有「购物车里已有」这条后门

**现象**:用 stub backend 直调 `gated_add_to_cart`（`max_quantity_per_item=24`）：没 `remember_products` 就加 `p-1` → `blocked="provenance"`，stub 的 `add_to_cart` 调用次数 `writes=0`。remember 后加 20 → `x20`；再加 10 → `Added p-1 x4 (capped at the per-item limit of 24)`；再加 1 → `is_error=True`「already at the per-item limit」。remember 一个带 `options` 的 family 再加 → `blocked="options"`。上限在 `gates.py:120`：`min(requested, max(0, 24 - 已有数量))`，已有数量来自锁内 `get_cart`。`requested = max(1, quantity)`，传 0 或负数也按 1 加。对比 `gated_update_cart_item:143` 直接 `min(requested, 24)`，因为 update 是「设成」不是「加上」。update/remove 走 `_check_provenance_or_cart`：id 不在 `seen_products` 但在购物车里也放行；add 只看 `seen_products`。

**规则**:上限要对「写入后的结果」算，不能对「这次请求」算——加法语义必须先读当前值，所以读和写要在同一把锁里，否则两个并发的 +20 各自看到 0。放行名单按操作语义区分：改/删一个已存在的行，「它在购物车里」本身就是来源证明（可能是上个 session 加的）；新增一行没有这个证据，只能靠本 session 读工具留下的记录。

(证据强度:2026-09-24 scratchpad 探针实测上述 5 个结果；读 `gates.py:40-44,102-130,133-183`、`executor.py:141,150,158-166`、`commerce_common/types.py:21-25`)

### 102. 动态 context 放在哪里，决定了它一变要让多少缓存失效

**现象**:`build_system_blocks` 返回两个 system block：静态文本带 `cache_control`，动态 context（购物车、页面、时钟、记忆）不带。另有两个断点：`with_tool_cache_control` 放在最后一个 tool 上，`build_request_messages` 放在最新一条消息上（每轮往后挪）。请求顺序是 `tools → system → messages`，所以动态 block 夹在「静态断点」和「全部历史」之间。购物车一变：tools + 静态 prompt 仍然命中缓存，消息断点的前缀里包含这个 block，所以整段历史（包括很长的搜索结果）要重新处理一次。`context_clock` 用 `now.replace(minute=0, ...)` 把时钟截到整点，因为精确到分钟的话，几乎每轮都会变，每轮都要重读历史。文章（anatomy 中文版 243 行）的建议不一样：把 volatile 内容放在请求最末尾，作为最新 user turn 里的一个数据块，这样历史前缀不受影响。

**规则**:缓存是前缀匹配，一个字节变了，它后面的所有断点都会失效。所以要按变化频率从低到高排列内容，一段内容放在哪里，就决定了它变化时要付出多大代价。「静态前缀保住了」和「历史命中了」是两个不同的断点，要分开回答。这份代码拿缓存换了别的东西：状态只存一份最新的，不会在每个 user turn 里留下一份过期快照（这一点是我从设计上推断的，代码注释没写）。面试时解释实际代码的取舍，不要背「volatile 放末尾」。

(证据强度:读 `commerce_common/prompt_assembly.py` 全文、`orchestrator.py:111-119`；`tests/test_turn_loop.py:144,236,304` 断言了断点位置。缓存命中/失效是 API 行为，没有用真实请求的 `cache_read_input_tokens` 实测)

### 103. `tool_choice` 会影响 messages 那段缓存的 key，所以强制调用工具的轮次不加滚动断点

**现象**:2026-09-26 用 `.venv/bin/python` 直调 `commerce_common.prompt_assembly` 做探针。`build_system_blocks("STATIC", "CTX")` 返回两个 block，只有第一个带 `cache_control`。`build_request_messages(msgs)` 把最后一条 `"find tents"` 从字符串改成 `[{"type":"text",...,"cache_control":...}]`，同时删掉 assistant 那条消息上旧的 marker；原来的 `msgs` 和 deepcopy 比较结果是 `True`，没有被改。只有一条消息时不加断点；传 `rolling_breakpoint=False` 时也不加。`orchestrator.py:178-183` 只在 `tool_choice["type"] == "auto"` 时传 True：第一轮 `first_forced_tool` 强制调工具，最后一轮是 `none`，这两种都不加。动态 context 每个 turn 在 `orchestrator.py:135` 构建一次，同一个 turn 的多轮工具调用共用同一份字节。

**规则**:缓存命中要求前缀字节一致，同时请求参数也要一致；`tool_choice` 就是会改变 messages 段缓存 key 的参数。在 `tool_choice` 和后续轮次不同的轮次上写缓存，写进去的内容后面读不到，只是白付写入费。所以放断点前要问两件事：前缀以后还会不会原样出现？这个缓存以后还有没有请求能读到？另外，缓存标记只加在请求副本上，不写回会话历史，否则旧的 marker 会越积越多，超过单次请求允许的断点数量。

(证据强度:2026-09-26 scratchpad 探针实测上述输出；读 `prompt_assembly.py:59-120`、`orchestrator.py:130-190`。「`tool_choice` 影响缓存 key」来自代码注释，没有用真实请求的 `cache_read_input_tokens` 验证)

### 104. 真实 trace：动态 context 没变时整段历史都命中缓存；强制工具的那一轮只命中 tools + 静态 prompt

**现象**:2026-09-26 用真实模型跑 `scripts/smoke_chat.py --vertical retail`（3 个 turn，SMOKE PASSED），用 `traces/record_retail.py` 包一层，把 SSE 事件存成 JSON。orchestrator 每轮的日志（`input` / `cache_read` / `cache_write`）：
- turn 1 round 0：`input=612 read=0 write=9660`。9660 就是 tools + 静态 prompt（①②）；动态 context + 第一条用户消息只有一条消息，不加滚动断点，所以记为未缓存的 612。round 1–4：`input=2`，`read` 从 9660 一路涨到 12558，每轮只写新增的那段。
- turn 2 round 0：`read=12873 write=139`。12873 = turn 1 最后一轮的 12558 + 315，**整段历史都命中**，只写了新的用户消息。中间跑过一次记忆提取（haiku），context 的字节仍然没变。
- turn 3 的消息里有 "returns"，`first_forced_tool` 返回 `search_policies`，所以 round 0 是强制调用：`input=3924 read=9660 write=0`。只命中 ①②，前两轮的历史（3924 tokens）按未缓存输入计费，也没有写缓存。round 1 恢复 auto：`read=13374`，正好等于 turn 2 结束时的 13012 + 362。
- turn 3 里 `add_to_cart` 改了购物车，但这个 turn 的 context 在开头就构建好了，所以购物车的变化要到下一个 turn 才会影响缓存。这次只跑了 3 个 turn，没有观察到。

**规则**:判断缓存命中要看每一轮（round）的数字，不能只看 turn 的汇总。`turn_complete` 里的 `cache_read` 是整个 turn 各轮的累加（turn 1 是 45344），看不出哪一轮没命中。强制工具的轮次会让整段历史按原价计费一次，代价随历史长度线性增长；下一个 auto 轮能接回之前的缓存。注意：这组数据证明的是「强制轮不加断点的代价」，**不能**证明代码注释里「`tool_choice` 会影响缓存 key」这个理由。要证明那个理由，得在强制轮上也放一个断点，再看能不能命中。

(证据强度:2026-09-26 真实请求实测，模型 `claude-sonnet-5`，trace 见 `traces/retail-3turn.json`，每一轮的数字见 `traces/README.md`；`first_forced_tool` 用探针对三条消息逐一确认)

### 105. 购物车和记忆都会改变动态 context；中转 endpoint 下，连 tools + 静态 prompt 也会随机不命中

**现象**:2026-09-26 第二次真实运行，在 retail 对话后加了第 4 个 turn（"What else should we pack…"，`first_forced_tool` 返回 None，SMOKE PASSED），同时把每个 turn 的动态 context 记录下来，按字段比对：
- turn 1→2：context 完全相同。turn 2 round 0 `read=13016 write=399`，历史命中。
- turn 2→3：`saved_memory` 多了一条 `camping_conditions`。它是 turn 1 之后那次记忆提取写的：那次提取 `stop=tool_use`（调用了保存工具），耗时 8.4 秒，turn 2 的 prefetch 在它完成之前就读了记忆，所以这条记忆**晚了一个 turn** 才进 context（NOTES 92 讲的时序问题）。turn 2 之后那次提取是 `end_turn`，没有保存。第一次运行时两次提取都是 `end_turn`，一条都没存，所以 context 没变。这和时序无关（之前我把它归因成时序，说错了）。
- turn 3→4：`cart` 从 0 件变成 1 件（AR-1202）。turn 4 round 0（auto）：`read=9660 write=5473 input=2`。①② 命中，context 之后的全部历史被**重新写入缓存**。对比 NOTES 104 里强制轮的情况：强制轮是把历史当未缓存 input 计费（3924），没有写缓存；auto 轮有断点，历史会按缓存写入价（1.25 倍）重写一遍，下一轮就能读。
- 代码解释不了的异常：turn 1 round 4 `read=0 write=13096`；turn 3 round 0 `read=0`；turn 4 round 1 只读到 9797（前一轮已经写到了 15135）。tools 和静态 prompt 的字节在初始化时就固定了，所以 ①② 不应该失效。另外 turn 2 round 0 读到的 13016 正好是 turn 1 round 3 的前缀，round 4 写入的 13096 没有被读到。第一次运行没有出现这类情况。

**规则**:动态 context 里任何一个字段变了都会让 ③ 失效，不只是购物车；后台记忆写入会在不确定的时间点改变 context。要知道哪个字段变了，就把两个 turn 的 context 解析成字段再比，别靠猜。**缓存行为也取决于请求被发到哪里**：`examples/retail/.env` 用的是第三方中转 endpoint。缓存是按组织/账号隔离的，如果中转把请求分发到多个上游账号，同样的前缀就可能随机不命中。这一条**是推断，还没证实**。验证办法是用官方 endpoint 把同一段对话跑两遍，看 `read=0` 的异常还会不会出现。在这之前，用这套环境测出来的缓存数字，不能当成代码本身的缓存行为。

(证据强度:2026-09-26 真实请求，trace 见 `traces/retail-4turn.json`，里面带每个 turn 的 context；context 按字段 diff 已实测；中转分发的解释是推断)

### 106. skill 的索引放在不变的前缀里，正文作为工具结果追加；压缩时正文和普通工具结果一样会被清掉

**现象**:skill 的 `name` 出现在 `load_skill` 的 `skill_name.enum` 里（`tools/registry.py:95`），`name` + `description` 由 `index_block()` 渲染进静态 prompt（`prompt.py:163`）。两者都在初始化时构建一次，`SkillRegistry` 按名字排序，所以字节固定。正文由 `_load_skill` 返回 `ToolOutcome(body)`（`execution.py:245-253`），作为 tool_result 进入 messages。真实 trace（`traces/retail-4turn.json`）：turn 1 加载 `search-discovery`，turn 4 加载 `planning-goals`，加载后的下一轮 `cache_read` 仍然包含 9660，①② 没有失效。`compact_history`（`turn.py:124-150`）在上一次调用的 prompt 达到 100k tokens 时触发，从最旧的 tool_result 开始替换成 `CLEARED_RESULT`，直到历史缩小到一半；它只判断 `type == "tool_result"`，没有为 `load_skill` 做例外。

**规则**:按需加载的内容要放在追加的位置，不能放进前缀：只有索引常驻，并且字节固定；正文作为工具结果追加，只会让前缀变长，不会改动已有的前缀。代价是正文在历史里的地位和普通工具结果一样，压缩时会被清掉；要恢复只能靠 prompt 规则让模型重新加载（`prompt.py:161`「on whichever turn it arrives」）。gate 用到的状态存在 session state 里，不受压缩影响。

(证据强度:读代码 + trace 里的缓存数字。「清掉后模型会重新加载」是推断，没有做超过 100k tokens 的实测)

### 107. 同一次对话里的东西有三种寿命：按 session 存在进程内存、按 session 存在 backend 内存、按用户存在文件

**现象**:2026-09-26 探针：进程 1 通过 `POST /api/session` 拿到 session id，进程 2 重新 import retail app，带着这个 id 请求，得到 `401 {"detail":"Unknown session"}`。原因是 `SessionStore` 把状态文档存在 `_states` dict、把 transcript 存在 `_transcripts` dict（`sessions.py`），都在进程内存里。`seen_products` 是 `ShoppingSessionState` 的字段，属于状态文档，所以一起丢了。购物车在 mock backend 的 `SessionCarts._lines` 里（`storefront_fixtures.py:429`），按 `session_id` 存在内存里，重启后同样没了；而且即使不重启，开一个新 session 也是空购物车。长期记忆不一样：retail 用的是 `JsonFileMemoryStore(data/.memory-store.json)`（`retail/api/main.py:40`），按 `user_id` 存在文件里。文件里的 `current_project` 是 2026-09-21 另一个 session 写的，今天两次运行的 context 里都有它；`camping_conditions` 带着第二次运行的 `source_session_id`。

**规则**:问「重启后还剩什么」要按两个维度分别回答：**键是什么**（session 还是用户）、**存在哪里**（进程内存、backend 还是文件）。对话历史、`seen_products` 和购物车跟着 session 走，并且只在内存里；长期记忆跟着用户走，存在文件里。所以要做持久化，得分别处理两处：`SessionStore` 底部的六个存储方法（`write_state` 的版本检查对应数据库里的 `UPDATE … WHERE version = ?`）和 backend 的购物车，只换一处不够。副作用：记忆会跨运行保留，所以两次 trace 并不独立，第二次运行一开始就带着以前的记忆。

(证据强度:2026-09-26 双进程探针实测 401；记忆文件内容和时间戳实测；读 `sessions.py` 全文、`storefront_fixtures.py:429-441`、`types.py:194-202`、`retail/api/main.py:9-40`)

### 108. `purge_generation` 只防「清空」和提取之间的竞态；单条删除靠快照去重，换个说法就会写回

**现象**:`extract_and_store`（`memory.py:519-536`）调用模型之前读一次 `purge_generation`（521 行），同时读出已有记忆的快照（522 行）；模型返回后再读一次 generation（533 行），变了就什么都不写。只有 `clear` 会让 generation 加一，`delete_fact` 不会。`extract_facts` 用快照去重：同一个 key、同样的值会被丢掉；同一个 key、换了措辞的值会被当作「更新」保留（471-515 行）。提取只读最近一轮对话（`orchestrator.py:284`）。2026-09-27 用假模型客户端（延迟 0.3 秒）加临时 `JsonFileMemoryStore` 实测 5 种情况：
- 提取期间清空全部，模型原样提出或换个说法：都没有写回。
- 提取期间删除一条，模型原样提出：**没有写回**，因为快照里还有这条，被当作重复丢掉。
- 提取期间删除一条，同一个 key、换个说法：**写回了**。
- 提取开始之前删除一条，模型原样提出：**写回了**，因为快照里已经没有这条。

**规则**:乐观检查（先记版本，做完耗时操作再比一次）只能发现会改变版本号的操作，所以「清空全部」受保护，「删一条」不受保护。单条删除能不能保住，取决于快照去重有没有碰巧命中；删除发生在快照之前，或者模型换了个说法，被删的记忆都会回来，而且不需要并发，删除后的下一轮对话里还提到它就够了。要堵住，得把「用户删过什么」记下来，提取时跳过；检查和写入也要放进同一个事务（`UPDATE … WHERE generation = ?`），才能关掉 533→535 行之间的窗口。

(证据强度:2026-09-27 scratchpad 假客户端探针实测上述 5 个结果；读 `memory.py:88-98, 423-536`、`orchestrator.py:284`。533→535 行之间的窗口没有测。本条替代同日的初版，初版把「删一条且模型原样提出」错判成会写回)

### 109. 长任务 goal 的「做完没有」不是代码判断的：代码只负责续跑和硬停，完成由模型按审计 prompt 自己声明

**现象**:2026-09-27 读 Codex 的 goal 扩展（`openai/codex` commit `67a7096`，`codex-rs/ext/goal/`）。目标存在 SQLite 表 `thread_goals`（每个线程一行：`goal_id, objective, status, token_budget, tokens_used, time_used_seconds`）。`runtime.rs` 的 `continue_if_idle()` 在线程空闲时重新读库，`status == active` 就注入一条续跑消息，用 `start_turn_if_idle(turn_trigger="goal")` 开下一轮。全部代码里没有任何「检查任务是否完成」的逻辑。完成的唯一途径是模型调用 `update_goal(status="complete")`，判断标准写在 `templates/goals/continuation.md` 的 Completion audit 里：先假定没完成，把目标拆成需求，逐条找权威证据（文件、命令输出、测试、PR 状态），证据弱或间接都算没完成；禁止把成功重新定义成已做完的那部分，也禁止「预算快用完就标 complete」。`update_goal` 的 status 枚举只有 `complete / blocked / paused`（`spec.rs`），`tool.rs:250` 拒绝其他值；`budget_limited`、`usage_limited` 和恢复只能由系统或用户设置。

**规则**:「完成」是语义判断，代码写不出来，所以交给模型；harness 能做的是**限制模型能声明什么**，并**抬高声明的门槛**。分工是：模型拥有「完成 / 阻塞 / 暂停」三个声明，代码拥有所有硬性终止（预算、额度、报错、空转），模型无权改写。审计 prompt 的写法值得抄：把「完成」定义成需要举证的主张（先假定没完成），而不是「没发现剩余工作」。这和本仓库「merchant 写操作只能经 host 审批才生效」是同一个思路：模型能改哪些状态，由代码划定。

(证据强度:2026-09-27 读 `runtime.rs` 全文、`steering.rs`、`spec.rs`、`tool.rs` 相关段、`templates/goals/*.md`、`state/goals_migrations/*.sql`。没有运行 Codex)

### 110. goal 的异常兜底：每种终止都有明确的触发条件和目标状态，「阻塞」要连续 3 轮才算

**现象**:`runtime.rs` 的 `stop_active_goal_for_turn` 按原因映射状态：本轮报错 → `blocked`；额度用尽 → `usage_limited`；执行环境不可用 → `blocked`；自动续跑**连续 3 轮空回复**（`accounting.rs:217-229`：只统计自动触发的轮次，最终消息为空并且本轮没有任何工具调用等活动）→ `blocked`。任何一次有工具结果，计数器就清零（`accounting.rs:116`）。预算：每轮累计 token，子 agent 的用量也计入（`record_descendant_token_usage`）；超出预算后系统设为 `budget_limited`，注入 `budget_limit.md`，要求「不开始新的实质工作，总结进度，给出下一步」。模型自己标 `blocked` 也有门槛：同一个阻塞条件要连续出现 3 轮，措辞变了也算同一个；用户恢复后重新计数。续跑 prompt 还要求模型把上一轮归类为「有进展 / 已验证的等待 / 无进展」，其中只复述状态、只列计划都算无进展。

**规则**:自动续跑的循环必须有**不依赖模型配合**的退出条件，否则模型空转就会一直烧钱；「连续 N 次无活动就停」是最便宜的一种。退出条件分两层：代码层兜底（空转、报错、预算），prompt 层防过早放弃（阻塞要连续 3 轮、困难不算阻塞）。两层方向相反：一层防止跑不停，一层防止太早停。「进展」必须按外部状态有没有改变来定义，不能按模型说了什么来定义。

(证据强度:读 `runtime.rs:300-380`、`accounting.rs` 相关函数、`continuation.md`、`budget_limit.md`。3 轮空回复阈值来自代码，没有实测)

### 111. goal 的并发：一把锁覆盖「读库 → 开轮」整个窗口，写库带 `expected_goal_id`

**现象**:`GoalRuntimeInner` 有一个 `Semaphore::new(1)`（`goal_state_lock`）。`continue_if_idle` 在读库之前拿锁，一直持有到续跑轮次被登记（`mark_goal_continuation`）为止；代码注释写明：否则外部 set/clear 可能在「读到目标」和「开始续跑」之间改掉目标。`stop_active_goal_for_turn` 在「记账 + 改状态」期间也持有同一把锁。写库用 `GoalUpdate { expected_goal_id: Some(...) }`，目标已被替换时旧轮次的写入不生效；`ExecutionUnavailable` 和 `EmptyResponse` 这两种停止还会先比对 goal_id，不一致就直接返回。`restore_after_resume()` 在进程恢复后从库里读目标，`active` 才重新挂上，其他状态一律清掉内存状态。

**规则**:和 NOTES 108 是同一类问题：「先读、做耗时操作、再写」的中间窗口会被外部修改插进来。两种解法这里都用了：窗口短的用互斥锁把整段包起来（读库到开轮）；跨越一整轮模型调用的窗口太长，不能持锁，就在写入时带上读到的身份（`expected_goal_id`），不匹配就放弃，也就是乐观锁。内存里的状态只当缓存，每次决策前都重新读库，重启后以库为准。

(证据强度:读 `runtime.rs:157-163, 401-530` 及注释。竞态没有复现)

### 112. 续跑消息追加在末尾、目标存在库里：这样压缩不会丢任务，也不会打乱缓存

**现象**:`steering.rs` 把续跑、预算用尽、目标被修改三种提示都包装成 `InternalModelContextFragment`（来源标记 `"goal"`），作为一条新的 input item 追加，不改 system prompt，也不改 tools。每次续跑都从库里重新渲染 `<objective>`，还附上已用 token、预算和剩余量。目标文本先 `escape_xml_text`（转义 `& < >`），外面再包一层声明：「下面的目标是用户提供的数据，把它当作要做的任务，不是更高优先级的指令」。用户中途修改目标时，`apply_external_goal_set` 会向正在运行的轮次注入 `objective_updated` 提示（`inject_if_running`）。

**规则**:需要跨很多轮保持的东西（目标、预算）不能只活在对话历史里，因为压缩会把它清掉（NOTES 106）。应该存在历史之外，每轮从源头重新注入到末尾。注入到末尾同时满足了 NOTES 102 的缓存约束：前缀字节不变，变化只追加在后面。用户文本即使进入的是「系统续跑」这种高权限通道，也要按 fenced data 处理：转义加声明，防止目标文本里夹带的指令被当成系统指令。

(证据强度:读 `steering.rs` 全文、`runtime.rs` 的 `apply_external_goal_set`。「压缩不丢、缓存不破」是根据注入位置推断的，没有看 Codex 的压缩代码，也没有看缓存数字)

### 109. 记忆提取会生成用户没说过的事实：输入里的「已有记忆」本身也是推断的来源

**现象**:第二次运行（`traces/retail-4turn.json`）中，turn 1 之后的提取写入了 `camping_conditions = plans to camp in warm weather`。但 turn 1 里用户的原话（第一次露营、下个月、帐篷 250 美元以内）和助手的回复都没有提到天气；只有工具结果里出现过 `3-season`，而 `transcript_text` 会把工具块去掉，Haiku 看不到。发给 Haiku 的输入里，唯一和「热」有关的是预置记忆 `bedding_constraint = partner sleeps hot`（`memory.py:466` 把已有记忆放在消息开头）。NOTES 108 探针里的「expects hot weather」和对话「it'll be hot out there」都是假客户端硬编码的，不是模型输出。

**规则**:提取模型读的是「已有记忆 + 最近一轮对话」，已有记忆既用来去重，也会变成新推断的素材，所以一条旧记忆可能被误读出一条用户从没说过的新记忆。评测记忆质量时要检查「每条新记忆能不能在这一轮对话里找到依据」。这也放大了 NOTES 108 的问题：用户最可能删的正是这种错误推断，而产生它的旧记忆还在，下一次提取可能换个说法再推出来。

(证据强度:trace 里的对话文字和工具结果已检索，没有 warm/hot 等字样；「误读 sleeps hot」是推断，拿不到那次 Haiku 请求的原文。换措辞写回有没有真的发生在真实 Haiku 上，没有验证)
