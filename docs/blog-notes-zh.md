# 博客精读笔记(中文)

> 来源:Anthropic 博客 "A guide to the anatomy of effective commerce agents"(Ali Shazal / Matthew Koen,2026-09-02)
> **这不是翻译,是笔记。** 用我自己的话压缩论点,并把每条挂到本仓库的具体文件上。
> 原文请自己读一遍:https://claude.com/blog/the-anatomy-of-effective-commerce-agents

---

## 第一部分 · 架构

### 1. 一个模型,一个标准 loop

commerce agent 干的事是「简化线上目录里的买与卖」,分两类:面向消费者的(搜索、比较、组单),面向业务的(销售分析、促销、库存)。

**核心主张:一个模型跑在一个标准 agent loop 里。没有意图路由器,背后也没有一堆领域子 agent。**

→ 本仓库:`commerce-common/commerce_common/turn.py` 是唯一的 loop,两个角色共用。

### 2. 用 Skills,不用 subagent

反对「一个领域一个子 agent」的理由:**商务对话是跨多个意图和多轮的、紧耦合的单一会话。** 交接会丢状态、token 翻几倍、延迟加几秒。

Skill 的好处是:指令**加载进已经持有全部历史的主 agent**,不发生交接。

**什么时候 subagent 才合理**,原文给了两种:
1. 窄而自足的任务(比如 deep research)
2. 真正的移交——对方领域有自己的、合规的专属 agent

**判据是「谁拥有这段对话」**,不是「任务复不复杂」。

→ 本仓库:`shopping-agent/skills/` 和 `merchant-agent/skills/` 各五个流程,一个 `SKILL.md` 一个。
→ 唯一的例外是 merchant 的分析 delegate(`merchant_agent/analysis.py`),它正好符合上面第 1 种:窄、自足、返回一个 schema 校验过的结果。

### 3. 什么进 prompt,什么进 skill

**经验法则:覆盖三分之一以上流量的内容进 system prompt,其余进 skill。**
**安全、法务、品牌规则永远进 prompt**,不管流量占比。

参考实现里的划法:
- shopping prompt 装:grounding、购物车与 checkout 语义、展示规则、商品搜索
- shopping skills:search-discovery / purchase-research / planning-goals / customer-care / memory-personalization
- merchant skills:performance-insights / catalog-listings / inventory-operations / pricing-promotions / marketing-campaigns

→ 本仓库:`shopping-agent/core/shopping_agent/prompt.py`(241 行)对照 `skills/` 五个目录。

### 4. 工具的两条原则

**① 把 agent 工具建在你已有的核心系统和逻辑之上。**
不要在工具代码里重新实现排序、购物车、库存规则——去调你本来就在跑的那套。

**② tool result 就是 context。**
只返回模型真正要拿来推理的字段;把原始响应重塑一遍;**把错误码换成指令。**

→ 本仓库:`backend.py` 是纯接口(仓库不实现任何业务逻辑),`serialization.py` / `enrichment.py` 负责重塑。
→ 「错误码换指令」的现成例子:`shopping_agent/gates.py:29` 的 provenance 报错,直接告诉模型下一步该调哪个工具。

### 5. UI 组件做成工具调用

原文的判断:让模型吐自定义标签(`<product id=...>` 那种)**规模一大就不работает**——可靠性、prompt 膨胀、以及对话历史变得无法解析。

改成:**每个 UI 元素是一个带类型的工具调用**(`present_products`、`present_itinerary`)。

**代价说得很清楚**:缓冲会牺牲流式粒度,除非开 `eager_input_streaming`;而开了就**放弃服务端的 schema 保证**。

→ 本仓库:`commerce_common/presentation.py` 的 `run_presentation`;`prompt_assembly.py` 的 `with_eager_input`。
→ 放弃 schema 保证之后怎么兜底:`turn.py` 的 `StreamedRound` 把解析不了的 input 直接判成 error,工具不执行。

---

## 第二部分 · 延迟与成本

> **仓库的 `docs/` 里没有任何文件对应这一部分。它只活在代码和 docstring 里。**

### 6. 任务完成延迟的三个杠杆

不是「首 token 延迟」,是**完成一个任务**的延迟。三个杠杆,而且**它们有时互相打架**:

| 杠杆 | 手段 |
|---|---|
| **更少轮次** | 预取大概率要用的 context;换更聪明的模型(每 token 更慢,但总轮次更少);开并行工具调用 |
| **更快工具** | 把业务逻辑的窟窿在后端补掉,而不是塞进工具代码;用 eager dispatch(参数一凑齐就发起执行,不等整个 JSON 收完)——原文说这能把多秒的空档压到几百毫秒 |
| **更快 token** | 靠 eval 扫出来,不靠拍脑袋 |

「换更聪明的模型反而更快」是这一节最反直觉的一条:**per-token 慢,但轮次少,总时间可能更短。**

→ 本仓库:`config.py` 的 `eager_tool_dispatch: bool = True`;grounding 的 prefetch 就是「预取 context」。

### 7. 感知延迟

两招:
- presentation 工具的参数**边生成边渲染**(第一个商品到位就先画出来)
- 逐步的进度消息(「正在找靠海的酒店」)

→ 本仓库:`presentation.py` 的 `enrich_partial` / `partial_signature`;`execution.py` 的 `split_status`(工具调用里那条 `status` 行在校验前就被摘出来发给宿主)。

### 8. Prompt caching —— 这一节是重点

**缓存读的价格是新鲜 input 的十分之一。目标命中率 90–99%。**

**缓存严格是前缀式的**,所以 context 必须分成三段、按这个顺序:

| 顺序 | 段 | 内容 | 变化频率 |
|---|---|---|---|
| 1 | **全局** | system prompt、tools | 基本不变 |
| 2 | **会话** | per-user 的上下文 | 一个 session 内基本不变 |
| 3 | **易变** | 时间戳、当前页面 | 每轮都可能变 |

**原文点名的常见错误:把时间戳或当前页放在 system prompt 顶部——静默地毁掉缓存。**
「静默」是关键词:没有报错,只是账单变贵、延迟变长。

→ 本仓库:`prompt_assembly.py` 全部 119 行都在做这件事。
→ 有一个衍生判断原文没写、但仓库做了:`context_clock()` 把时间**抹到整点**,因为渲染分钟会让这一段几乎每轮都变。

### 9. 选模型

三步:① 定指标和底线 → ② 用全套 eval 扫所有模型和 effort 档 → ③ **仔细读结果**(注意 prompt 往往是对着某个特定模型调出来的,换模型时这一层要剥掉)。

起手建议:merchant agent 用 Opus,消费者 agent 用 Sonnet——**但最终要靠测量决定**。

**关键指标是「每完成一个任务的成本」,不是「每次模型调用的成本」。**
这句和第 6 节的「更聪明的模型可能更快」是同一个道理:单价高但轮次少,总账可能更便宜。

---

## 第三部分 · 生产

### 10. Memory:存储 / 写 / 读

**存储**:数据库里的**类型化记录**,不是 markdown。merchant 的 memory **按人存,不按账号存**。

**数据处理规则**:写路径校验类型、允许用户删除、设保留期、做成 per-deployment 的开关。

**写**:**异步抽取优于让 agent 自己调 save 工具——fact 召回高 13%。**

**读**:分三层——① 常驻 context ② 每轮预取 ③ 藏在查询工具后面。

→ 本仓库:`commerce_common/memory.py`(666 行)。`memory_tier_one_cap` 就是第一层的条数上限。

### 11. 安全

**核心原则:模型只 stage,人或策略才 apply。没有任何工具调用直接动钱或改业务状态。**

其余几条:
- **写和渲染只接受服务端发出的 id** —— 同时挡住模型幻觉出来的 id 和注入进来的 id
- **交易上限按「写入之后那一行会变成什么样」来判**;同一 session 的写要串行化
- **第三方内容 sanitize + fence**,因为 fence 里的文本是**用来汇报的材料,不是拿来执行的**

→ 本仓库:`docs/safety.md`(中译在 `docs/safety-zh.md`)把这些逐条对应到了模块。

### 12. Evals:评快照,不评对话

**最重要的方法论主张:构造测试状态本身,而不是给整段模拟对话打分。**

理由很硬:**API 是无状态的,输出就是 prompt + tools + messages 的函数。** 既然如此,直接把那三样构造出来测就行,没必要跑一整段对话。

- 模型对模型的「模拟用户」eval **用来打分很差**,但**适合发现 case**
- 要测**恶劣条件**:混乱的、自相矛盾的历史
- **每个正例都要配一个反例**
- 覆盖面:核心请求、依赖上下文的请求、安全/品牌 case、界面 eval、多能力组合请求
- **每个用户流程 50–100 条**,来源是领域专家和生产日志

→ 本仓库:1105 条测试全部不打真实 API,`commerce_common/testing.py` 提供一个按脚本回放的假模型客户端。**这就是「评快照」的落地形态。**

### 13. 跨组织协作

- **所有权跟着系统走**:每个 skill / tool 有且只有一个 owner
- 每个改动**自带测试用例** + 定向 CI
- 把 agent 纳入发布日历:金丝雀发布,**每个 skill 一个 kill switch**

---

## 收尾的论点

这套架构之所以耐用,是因为**工具调的是你本来就在跑的系统**;所以换模型退化成「改一处配置 + 跑一遍 eval」。

展望:语音、主动行为、以及第三方购物 agent 通过**同一套 provenance / staging / 审批规则**接进商家系统。

---

## 读完之后,三条最值得背下来的

1. **判断该不该拆 subagent,看「谁拥有这段对话」,不看任务复杂度。**
2. **缓存是前缀式的,所以 context 必须按变化频率排序;放错顺序不会报错,只会变贵。**
3. **评快照不评对话,因为 API 无状态——输出是 prompt + tools + messages 的函数。**
