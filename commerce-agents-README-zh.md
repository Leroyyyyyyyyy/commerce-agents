# Claude Commerce Agents(中文翻译)

> 原文:`/Users/dld/AIeatwld/commerce-agents/README.md`(anthropics/commerce-agents,Apache-2.0)
> 术语保留英文的地方是刻意的:这些词在面试里要直接说英文。

---

# Claude Commerce Agents

基于 Claude 构建的两个商务 agent:一个 **shopping agent(购物 agent)**,由商家嵌入自家 app 供顾客使用;一个 **merchant agent(商家 agent)**,供商家员工打理后台。每个 agent **只定义一次**(prompt、skills、工具契约、gates),然后能跑在三条执行路径上——Messages API、Claude Agent SDK、Managed Agents;四个可运行的垂直行业示例展示了这两个 agent 跑在同一套库上。

> [!NOTE]
> 这里出现的公司、品牌、商品、人物全是虚构的,唯一的公司叫 ACME。
> **没有任何东西会真的下单、扣卡、或改动线上商品**:`checkout` 只是把购物车渲染出来交给宿主应用去完成,而每一次商家侧的写操作都会**暂存(staged)**,直到有人批准。业务规则、鉴权、合规,都是部署方自己的事。

## 快速开始:跑起示例

需要 Python 3.11+ 和 Node 22。clone、安装、填 key、跑一个垂直行业:

```bash
git clone https://github.com/anthropics/commerce-agents.git && cd commerce-agents
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt       # 本仓库的七个包 + 它们锁定版本的依赖
cp .env.example .env                  # 填入 ANTHROPIC_API_KEY
(cd examples && npm ci)               # 八个 web app 共用一个 npm workspace
python scripts/run_demo.py retail     # API :8000 + 店面 :3000
```

`--merchant` 启动商家后台而不是店面,`--all` 两个都启。四个垂直行业:`retail`(:3000,后台 :3100)、`travel`(:3001,:3101)、`telecom`(:3002,:3102)、`entertainment`(:3003,:3103);每个的 README 都列了可以在两个界面上试的 prompt。

## 快速开始:构建你自己的

这个 Claude Code 插件能基于这几个包、对着你自己的系统脚手架出一个 agent,也能 review 你已有的 agent。按上面的方式把仓库 clone 下来后(插件会把它当作参考实现来读):

```bash
claude plugin marketplace add anthropics/commerce-agents
claude plugin install commerce-builder@claude-commerce-agents
claude
/scaffold-commerce-agent a shopping assistant for our store
```

这个命令会问你的技术栈、把方案复述一遍给你确认、然后把项目建出来;`/add-commerce-flow` 和 `/author-commerce-evals` 接着往下做,`/review-commerce-agent` 则是从一个**已经存在的** agent 开始([`plugins/commerce-builder/`](commerce-agents/plugins/commerce-builder/))。每个命令在请求与其 description 匹配时也会自动触发,所以不点名也行。

## 两个 agent

**shopping agent** 负责搜索、比价、做规划、填购物车、回答订单和政策问题,并记住顾客告诉它的事。它的五条 flow 就是 [`shopping-agent/skills/`](commerce-agents/shopping-agent/skills/) 里的 skills;部署方需要针对自己的商品目录、购物车、订单、政策系统实现 [`StorefrontBackend`](commerce-agents/shopping-agent/core/shopping_agent/backend.py)。

**merchant agent** 负责解释经营表现、维护商品列表、处理库存与订单告警、定价与促销、起草营销活动;**每一次写操作都是一条暂存变更(staged change)**,由宿主的审批界面来真正应用。它的五条 flow 是 [`merchant-agent/skills/`](commerce-agents/merchant-agent/skills/) 里的 skills;部署方需要针对自己的分析、目录、库存、定价、活动系统实现 [`MerchantBackend`](commerce-agents/merchant-agent/core/merchant_agent/backend.py)。

## 目录布局

| 目录 | 内容 | pip 包名,`import` 名 |
|---|---|---|
| [`commerce-common/`](commerce-agents/commerce-common/) | 两个角色共用的东西:config、fencing(围栏)、memory、skills、grounding、presentation、executor 框架、events | `commerce-common`,`commerce_common` |
| [`shopping-agent/core/`](commerce-agents/shopping-agent/core/) | 购物侧类型、`StorefrontBackend`、prompt、工具契约、gates、executor | `shopping-agent-core`,`shopping_agent` |
| [`shopping-agent/runtime-messages-api/`](commerce-agents/shopping-agent/runtime-messages-api/) | `ShoppingAgent`,跑在 Messages API 上的 turn loop | `shopping-agent-runtime`,`shopping_agent_runtime` |
| [`shopping-agent/runtime-agent-sdk/`](commerce-agents/shopping-agent/runtime-agent-sdk/) | 跑在 Agent SDK 上的购物 agent,带一个控制台 | `shopping-agent-sdk`,`shopping_agent_sdk` |
| [`shopping-agent/managed-agents/`](commerce-agents/shopping-agent/managed-agents/) | 给 Managed Agents 用的 manifest 和店面 MCP server | — |
| [`merchant-agent/core/`](commerce-agents/merchant-agent/core/) | 商家侧类型、`MerchantBackend`、prompt、工具契约、变更护栏、gates、executor | `merchant-agent-core`,`merchant_agent` |
| [`merchant-agent/runtime-messages-api/`](commerce-agents/merchant-agent/runtime-messages-api/) | `MerchantAgent` 和跑在 Messages API 上的分析委派(analysis delegate) | `merchant-agent-runtime`,`merchant_agent_runtime` |
| [`merchant-agent/runtime-agent-sdk/`](commerce-agents/merchant-agent/runtime-agent-sdk/) | 跑在 Agent SDK 上的商家 agent,带一个会要求审批的控制台 | `merchant-agent-sdk`,`merchant_agent_sdk` |
| [`merchant-agent/managed-agents/`](commerce-agents/merchant-agent/managed-agents/) | 给 Managed Agents 用的 manifest、商家 MCP server、定时摘要 | — |
| [`examples/`](commerce-agents/examples/) | 四个垂直行业,共享的宿主代码(`demo_common/`)、共享的 web 代码(`web-shared/`) | — |
| [`plugins/commerce-builder/`](commerce-agents/plugins/commerce-builder/) | 那个 Claude Code 插件 | — |
| [`docs/`](commerce-agents/docs/) | `safety.md`(被代码强制执行的规则)、`backends.md`(怎么映射你自己的系统)、`deployment.md`(跑在其他平台上) | — |
| [`tests/`](commerce-agents/tests/) | 跨包的测试套件;每个包另有自己的 `tests/` | — |
| [`scripts/`](commerce-agents/scripts/) | `install.sh`、`run_demo.py`、`smoke_chat.py`、`screenshot_tour.py`、`check.py`、`deploy_managed_agent.sh`、`verify_all.py` | — |

## 跑一个 agent 的三种方式

**Messages API。** 这是**参考实现的那个循环**;示例们都是围着它搭的宿主应用:

```python
from pathlib import Path

from shopping_agent import ShoppingAgentConfig
from shopping_agent_runtime import ShoppingAgent

agent = ShoppingAgent(backend=your_backend, skills_dir=Path("shopping-agent/skills"),
                      config=ShoppingAgentConfig(brand_name="Your Store"))
async for event in agent.stream_turn(messages, session, state):
    ...   # text_delta、tool_call、ui、cart_update(商家侧是 change_update)、turn_complete
await agent.update_memory(messages, session)   # 记忆抽取;只有这条路径有
```

示例宿主通过 `X-Session-Id` 请求头接收 session id。

**Agent SDK。** 同一套 prompt、skills、工具,但**循环由 SDK 来跑**;宿主预取(prefetch)grounding 需要的只读数据,而且**这条路径在一轮结束后不会再跑任何东西**:

```bash
python shopping-agent/runtime-agent-sdk/main.py --once "a two-person tent under $250"
python merchant-agent/runtime-agent-sdk/main.py          # 用 y/N 批准暂存的变更
```

**Managed Agents。** 一个托管的 agent,用同样的 skills 和契约,调用你自己的 MCP server:

```bash
scripts/deploy_managed_agent.sh shopping-agent/managed-agents/shopping-agent   # 或 merchant-agent/...;加 --live 才真正部署
```

## 安全

fencing(围栏)、provenance gates(来源校验闸)、caps(上限)、记忆校验、商家审批闸,**都跑在工具调用内部**,因此在三条路径上都成立;grounding、分析预算、记忆抽取则是 runtime 层的特性。[`docs/safety.md`](commerce-agents/docs/safety.md) 逐条列出了每条规则、它所在的模块和路径,以及一个部署方**最先**该补上什么;注意示例**没有任何鉴权**,MCP server 只绑定在 loopback 上。

## 四个垂直行业

| 示例 | 店面(Storefront) | 后台(Portal) |
|---|---|---|
| [`examples/retail/`](commerce-agents/examples/retail/) ACME | 用内置组件做搜索、比较、方案、购物车、结账、记忆 | 摘要、暂存的补货与商品信息修正、跑在一个 SQL 视图上的分析委派 |
| [`examples/travel/`](commerce-agents/examples/travel/) ACME Travel | 按日期绑定的库存,以及一个 `present_itinerary` 扩展 | 入住率日历、按日期区间调价 |
| [`examples/telecom/`](commerce-agents/examples/telecom/) ACME Mobile | 账户上下文、套餐矩阵、服务端撰写的费用披露 | 套餐结构、会说明"影响到哪些号码"的调价、受保护的监管费用 |
| [`examples/entertainment/`](commerce-agents/examples/entertainment/) ACME Tickets | 限时占位(hold)、候补、转让、场馆座位图、全包价费用披露 | 活动售卖节奏、释放占位以真正放出库存、保留费用结构的调价 |

每个示例的 README 里都有一节 `Try`:`scripts/smoke_chat.py` 会跑的那些轮次,以及一些单条 prompt 和"一个好回答应该做到什么"。

## 验证

```bash
ruff check . && ruff format --check . && pytest && python scripts/check.py
python scripts/verify_all.py                        # 上面那行 + 部署 dry run + web 构建
python scripts/smoke_chat.py --vertical travel      # 跑一段真实对话;需要 key
```

`requirements-dev.txt` 额外加了 pytest 和 ruff。CI 会在两个 Python 版本上用它安装、构建那八个 web app,并检查这些包名**始终没有被注册到公共 index 上**(锁定文件是从各自目录安装的,绝不走 index)。**要确认缓存是否命中**,读 `turn_complete` 里的 `cache_read_input_tokens`,或者读每次模型调用打在对应 runtime logger 上的那行日志——**第二轮还是 0,说明前缀变了**。

## 部署到别的平台

几个 runtime 都接受任意 `anthropic` client(通过 `client=` 传入),SDK 版的 runtime 则从 CLI 环境里取平台;[`docs/deployment.md`](commerce-agents/docs/deployment.md) 覆盖了 GCP Vertex AI、AWS Bedrock、Microsoft Foundry 和各种网关。

## MCP 连接器

**一个都不自带**;两个 agent 都是通过 backend 接口去够到你的系统。在某个官方连接器就是权威数据源的地方,它才是集成目标:分析数仓(Snowflake、BigQuery、Databricks、Amplitude)、财务(Stripe、Square、PayPal、QuickBooks)、投递(Slack、Google Drive、Gmail)。某个电商平台自己的、管目录/购物车/结账的 MCP server,是**在服务端从某个 backend 方法里调用**的;在 Managed Agents 上,manifest 会把它挂在该角色自己的 server 旁边,而 provenance gates 依然挡在每一次写操作前面。

## 改造成你自己的

- **backend 方法。** 每个方法都在**服务端**、用宿主为该 session 持有的凭证去调你的服务;模型只读到结果。**某条步骤顺序固定的 flow,要在 backend 里强制这个顺序**(而不是靠 prompt)。
- **读 backend 指南。** [`docs/backends.md`](commerce-agents/docs/backends.md) 讲了身份与凭证、有序 flow、结账、带选项的商品,以及你的平台**给不出来**的那些字段怎么办。
- **同一套接口也覆盖别的业务形态。** 在市场(marketplace)场景里,卖家是搜索的一个维度,merchant agent 代表 session 指明的那个运营方行事。在按账户或合同定价的场景里,报出的价格就是该 session 账户的价格。如果你没有自己的结账,就把购物车关掉,或者把它交给报价单、采购单、或一个托管的结账 URL。
- **结账是交接出去的。** 结账卡片链接到你自己的结账路由,或者平台的托管结账 URL(在市场场景里每个卖家一个)。**backend 返回这个 URL、宿主来渲染,模型永远看不到它。**
- **从小处起步。** 一个购物侧试点可以只实现搜索和商品详情、其余全部 stub 掉;**一个 stub 掉的方法返回"不可用"结果,并且不改变 prompt 的任何一个字节**。一个商家侧试点可以只实现那八个读方法、让写操作一律拒绝;这样摘要和指标照样能跑,完全不需要写路径。
- **把你没有的东西关掉。** 业务上压根不存在的系统(引流页面没有购物车、没有订单跟踪),就是一个关掉的 `enable_*` 开关——它会在**每一条路径上**移除对应的工具、prompt 行和 grounding 规则;把依赖它的 flow 挪到 `skills/_staged/` 下面停放。商家侧 config 对商品编辑、库存、定价、活动有同样的开关。
- **加你自己的东西。** 一条 flow 就是 `skills/` 下的一个目录 + 一个 `SKILL.md`。领域专属 UI 是一个 `PresentationExtension`(四个垂直行业一共带了七个)。两侧 config 上的 `brand_name`、`assistant_name`、`brand_voice` 用来设定身份。

## 许可证

Copyright 2026 Anthropic PBC。基于 [Apache License 2.0](commerce-agents/LICENSE) 授权。
**这是一份参考实现;它不做维护,也不接受贡献。**
