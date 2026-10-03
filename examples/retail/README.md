# ACME（零售）

零售示例仅使用内置组件，在同一份商品目录上同时运行两个智能体：商城前台可以搜索和比较商品、制定方案、将商品加入购物车、准备结账，并在重启后保留记忆；商家门户可以展示晨间摘要、准备补货、商品信息修正和促销变更，并通过预览卡片应用这些变更。SDK 控制台和参考 MCP 服务器默认加载的也是这个后端。

## 运行

```bash
python scripts/run_demo.py retail               # API :8000 + 商城前台 :3000
python scripts/run_demo.py retail --merchant     # API :8000 + 商家门户 :3100
python scripts/run_demo.py retail --all          # 两个 Web 应用共用一个 API
```

也可以先在 `examples/` 中运行 `npm ci`，然后自行启动各个组件：

```bash
uvicorn retail.api.main:app --app-dir examples --reload --port 8000
(cd examples/retail/storefront-web && npm run dev)     # :3000
(cd examples/retail/merchant-web && npm run dev)       # :3100
```

聊天功能需要在仓库根目录的 `.env` 或环境变量中设置 `ANTHROPIC_API_KEY`；浏览商品目录和商家门户组件不需要该密钥。设置 `MERCHANT_REQUIRE_HOST_APPROVAL=0` 后，可以通过聊天中的批准来应用变更；默认情况下，需要点击预览卡片上的按钮来应用变更。

### 可选 PostgreSQL 会话和购物车

安装 `requirements-postgres.txt` 并设置 `COMMERCE_DATABASE_URL` 后，Messages API 使用
`demo_common/postgres.py` 的会话存储和 `api/postgres_retail.py` 的购物车后端。
`api/migrations/002_carts.sql` 定义购物车；共享会话表由 `demo_common/migrations/001_sessions.sql`
定义。启动时执行迁移，关闭时释放连接池。未设置该变量时保持内存模式。

运行方式、事务边界及验证命令见 [`docs/postgres.md`](../../docs/postgres.md)。
此模式持久化购物会话及购物车、在数据库事务内限制数量，并保证直加接口的请求幂等；不恢复中断的 Agent 轮次。
商品目录、商家状态和文件记忆保持原有实现。

## 试用

商城前台（`scripts/smoke_chat.py --vertical retail` 会运行对应的三轮对话）：

1. 下个月我准备第一次带伴侣和 6 岁的孩子去露营。我们需要一顶帐篷——不要太重，最好不超过 250 美元。
2. 帮我比较最合适的两个选项——我最关心空间大小和搭建是否方便。
3. 家庭款听起来不错。把它加入购物车，再提醒我一下退货政策，以防万一。

商家门户（运行 `scripts/smoke_chat.py --vertical retail --merchant`；在卡片上批准变更之前，第三轮请求会被拒绝，最后两轮在批准后继续执行）：

1. 今天早上有哪些事情需要我关注？
2. 按照当前的销售速度，为海洋主题墙贴补充足够未来一个月销售的库存，并修正该商品描述，补上其中缺失的信息。所有变更上线前都先展示给我看。
3. 看起来没问题——批准补货。
4. 儿童房装饰最近似乎很受欢迎。调取数据看看——本月海底世界系列的表现真的超过店内其他商品吗？
5. 过去两周销售额为什么发生变化——是哪些品类或商品造成的，分别影响了多少？

以下均为单条提示词，每条都在新的会话中运行：

| 界面 | 提示词 | 理想回答 |
|---|---|---|
| 商城前台 | 帮我订购一套和之前在你们这里买过的相同的阻力带。 | 从订单历史中找到该套装，将一套加入购物车，并说明当前价格与当时的购买价格不同。 |
| 商城前台 | 我之前在你们这里订购的瑜伽垫还能退货吗？还没有使用过。 | 读取订单和退货政策，从送达日期起计算 30 天，并说明退货期限已经结束；不会创建退货申请。 |
| 商城前台 | 我需要一个能给笔记本电脑充电的通用旅行转换插头。 | 只搜索一次并展示一张商品卡片，即 65 W 转换插头，无需提出澄清问题。 |
| 商城前台 | 两对情侣第一次周末自驾露营，四个人都没有装备。帮我们配齐所需物品，总价控制在 600 美元以内。 | 按四人需求制定方案（一顶家庭帐篷、四个睡袋、一个炉具和一个冷藏箱），计算总价，说明总价超过 600 美元，并指出删掉或共用哪些物品可以将预算降到目标以内。 |
| 商家门户 | 哪个品类导致了上周销售额的变化，变化了多少？ | 读取快照和每日序列：儿童房品类是数据中唯一单独列出的品类，其增长额超过了全店增长额，因此其他品类整体有所下滑；同时说明数据没有提供完整的品类拆分。 |
| 商家门户 | 海洋主题墙贴的商品信息缺少墙面覆盖范围和材质。帮我补上。 | 读取商品信息，发现记录中没有这两个值，因此会向用户询问，而不是凭空把材质或覆盖范围写入页面。 |

## 此示例特有的内容

- `api/mock_retail.py`：`MockRetail`，即基于样例数据实现的 `StorefrontBackend`，以及商品详情页展示的价格和评论摘要。
- `api/mock_merchant.py`：`MockRetailMerchant`，即基于同一商品目录实现的 `MerchantBackend`；已应用的变更会写回其中，`execute_analysis_query` 则通过同一状态的只读 SQLite 视图为分析委托智能体提供数据。
- `api/agent_config.py`：两个智能体的配置。分析功能默认开启；设置 `MERCHANT_ANALYSIS_CODE_EXECUTION=1` 会添加托管沙箱，`MERCHANT_ANALYSIS_MODEL` 可以覆盖委托智能体使用的模型。
- `api/main.py`：商城前台基于文件的记忆存储（`data/.memory-store.json`）、商品详情扩充逻辑，以及“加入购物车”按钮的路由。
- `api/cart_add.py`：`POST /api/cart/add` 要求 `operation_id`（UUID）、`product_id` 和可选的 `quantity`（默认 1）。同次重试复用 ID，新操作用新 ID；相同 ID 换参数返回 409。SQL 的 `api/migrations/003_cart_add_operations.sql` 账本与购物车及按钮 note 同事务提交；内存模式只在当前进程缓存结果。前端保留失败且结果未知的按钮操作 ID，成功或明确拒绝后才生成下一个 ID；刷新页面不保留待重试操作。
- `storefront-web/lib/api.test.cjs`：运行 `npm test --workspace=acme-retail-storefront-web` 验证按钮请求 ID 和失败分类。
- `api/merchant.py`：概览页面中的 KPI 趋势和洞察卡片。
- `storefront-web/`、`merchant-web/`：此示例的卡片、视图和设计令牌，构建于 `../web-shared/` 之上。

## 数据

`data/catalog.json`、`users.json`、`orders.json`、`policies.json` 和 `memory-seed.json` 为商城前台提供数据；`merchant_metrics.json`、`merchant_inventory.json`、`merchant_campaigns.json` 和 `merchant_messages.json` 为商家门户提供数据。有四种商品包含可选规格（床垫可选尺寸，枕套套装可选尺寸和颜色，有色润肤霜可选色号，加重毯可选重量）：商品目录以紧凑形式定义这些变体，其余内容由 `demo_common` 按照 [`docs/backends.md`](../../docs/backends.md) 中的说明派生生成。

`storefront-web/public/products/` 中的商品照片是 CC0 品类图片，来源列在同目录的 `IMAGE-CREDITS.md` 中；没有照片的商品会显示为色块。

会话和身份由 [`../demo_common/`](../demo_common/) 中的共享宿主代码处理：一个会话 ID 代表一个演示用户档案或唯一的商家身份。
