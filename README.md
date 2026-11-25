# 食序

食序把餐次、菜品、付款和时间依据保存在本机 SQLite 数据库中。MCP stdio 与本机维护页都调用同一台 `127.0.0.1` 服务，因此网页关闭时 MCP 仍可使用，之后从网页能看到同一份记录。

## 环境

- Windows 与 Python 3.12。
- `requirements.txt` 固定使用本机已验证的 MCP Python SDK `mcp==2.3.0`。
- 首次安装：`py -3.12 -X utf8 -B -m pip install -r requirements.txt`

默认数据目录是 `I:\data\Shixu`。数据库 `shixu.sqlite3`、本机 MCP 令牌和运行状态文件在目录根部，后台服务日志在 `logs\service.log`，手动备份默认保存到 `backups` 子目录。令牌只用于本机 MCP 与启动器的内部通信，不进入 MCP 响应或日志。

## 打开维护页

双击 `F:\桌面\程序\APP\食序.lnk`，或运行项目里的 `scripts\start.cmd`。脚本会启动或复用本机服务，等数据库迁移完成且服务身份确认后，再打开 `http://127.0.0.1:43127/` 的今日工作台。若固定端口被其他程序占用，启动器会显示错误。

从快捷方式启动后，Windows 任务栏右下角会出现食序托盘图标。如果图标被 Windows 收起，可点击通知区域的 `^` 找到它。右键图标时，菜单会在指针旁弹出。选择“打开食序网页”可再次打开今日工作台，选择“退出食序”会停止本机服务并移除托盘图标。

今日工作台比较外食、外卖和做饭，同时显示提醒、厨房库存、今天的菜品与消费、菜单和本周餐次。记录页 `/records` 可以补记、更正餐次。一次保存可以录入多道菜和多笔付款。候选可在 `details.dishes` 保存多道菜，选择方案记餐时会逐项带入，可在保存前增删。调用 `meal_create` 时可带 `recommendation_source`，同一次提交会写入实际餐次和推荐来源。

推荐度是 0–100 的规则分。时间占 40 分，费用占 30 分，喜好占 20 分，近期重复与跳过占 10 分。比较时间和金额时按范围上限，设置截止或预算后，余量会影响分数。待核实方案最高 59%，已排除方案 0%。MCP 调用 `recommend` 返回 `recommendation_score` 与分项，传 `ranking="score"` 可按工作台的顺序排列。

饭点和只读日志位于工作台设置中。允许浏览器通知后，保持页面打开即可弹出通知。页面关闭时，正在运行的本机服务仍会记录到期事件。服务重启或电脑恢复后，每条规则只补最近一次到期事件，已处理事件不会重复出现。状态与日志默认收起，展开后可刷新，右键标题或点击“隐藏”可以隐藏面板，用“恢复状态与日志”重新打开。

如需在命令行中停止服务，可在相同环境中运行：

```powershell
py -3.12 -X utf8 -B -m app.launch --stop
```

停止命令先核对本地实例锁、实例标识和独立令牌，再让本机服务停止接收新请求并等待正在处理的请求完成。默认端口和默认数据目录分别为 `43127` 与 `I:\data\Shixu`。

## MCP stdio 接入

[版本与 AI 对接约定](version.md)列出当前 MCP 服务和数据库版本，以及记餐、更正与回执核对规则。

MCP 客户端通过标准 stdio 启动入口脚本；脚本使用绝对路径定位项目，不依赖宿主当前工作目录。当前工作区的实测启动值为：

```json
{
  "command": "C:\\Users\\Lenovo\\AppData\\Local\\Programs\\Python\\Python312\\python.exe",
  "args": [
    "-X",
    "utf8",
    "-B",
    "F:\\桌面\\程序\\shixu\\scripts\\mcp_stdio.py"
  ],
  "cwd": "C:\\Windows\\System32"
}
```

这是通用 stdio 客户端的启动字段示例，不代表某个宿主的专有配置格式。入口使用绝对路径定位项目；以上正是从 `C:\Windows\System32` 启动的实测工作目录，宿主若不支持 `cwd` 字段也可省略。入口会在 stdio stdout 上只输出 MCP 协议；服务子进程与 MCP 日志写到本地服务日志或 stderr。stdio MCP 会自动启动或复用共享服务，不要求先打开维护页。

首次使用时，可让 AI 先调用 `meal_create` 保存一餐，再调用 `meal_list` 查回。金额字段按分传入，例如 28 元为 `2800`：

```json
{
  "request_id": "每次新增时生成的唯一编号",
  "date": "2026-11-24",
  "meal_type": "lunch",
  "mode": "delivery",
  "meal_amount_cents": 2800,
  "items": [{"name": "牛肉饭"}]
}
```

```json
{
  "date_from": "2026-11-24",
  "date_to": "2026-11-24",
  "limit": 10,
  "offset": 0
}
```

`meal_create` 的动作名由 MCP 工具名确定，以上第一个对象是它的 `payload`；第二个对象是 `meal_list` 的 `payload`。写入结果未知时，先用原 `request_id` 调用 `request_status` 核对，不生成新的编号重试。业务错误保持 `{ok:false,error:{code,message,details}}`，成功保持 `{ok:true,result}`。

### 店铺关联与频次统计

新增餐次可用 `store` 关联店铺，名称必填，分店可省略；`meal_update` 的 `changes.store` 省略时保留原关联，传 `null` 时清除。`meal_list` 会读回 `store_id`、名称和分店。旧记录不会从 `location`、备注或付款信息推测店铺；原有 `location`、金额与请求回执仍保留。

```json
{
  "request_id": "每次新增时生成的唯一编号",
  "date": "2026-11-24",
  "meal_type": "lunch",
  "mode": "eat_out",
  "items": [{"name": "牛肉饭"}],
  "store": {"name": "示例饭店", "branch": "市中心店"}
}
```

`meal_stats` 是只读聚合工具。`date_from` 和 `date_to` 是可选日期边界，按餐次日期包含首尾；省略两端可查询全部历史。`mode_counts` 总是包含外食、外卖和做饭三种方式，即使次数为零。常吃菜和店铺列表每餐最多计一次，并带各自的最近餐次日期与已记录结束时刻；总体也返回最近餐次。只有记录中有可确定的 `timing.ended_at` 才会返回具体时刻，缺失或同日无法判定时只返回日期和空时刻，不推测钟点。

`top_limit`（默认 10，范围 1–200）只限制常吃菜/店铺排行榜的条数，不影响总餐次、模式次数、选定菜/店计数或奶茶购买笔数。`dish` 可选菜名，`store` 可选店名与分店，用于额外查询该菜或店的次数和最近餐次；没有匹配时次数为 0、最近餐次为 `null`。

奶茶按笔数统计为两部分：独立 `milk_tea` 记录每条一笔；餐次付款中 `kind="milk_tea"` 的付款行每条一笔。独立记录自身的付款行不会再重复加入统计。

数据库从 v2 迁移至 v3，新增 `stores` 表和可空餐次外键。旧餐次保持未关联，不批量猜补店铺。

### 本机联测结果

标准 stdio 联测用 MCP Python SDK `2.3.0` 从 `C:\Windows\System32` 启动绝对路径入口。1.1.1 列出 114 个工具，在隔离库复验 v6 迁移、原记录和回执查询，并逐组验证 11 组代表流程与补齐的边界。1.0.0 的历史联测还覆盖了无网页记餐、去重查回、改价、备份恢复和本机饭点事件。QQ 和 CuteDuck 留待接入。

## 本机 HTTP 与会话

- 网页通过 `GET /` 加载工作台、通过 `GET /records` 加载记录页。两页均通过 `POST /api/action` 使用 `{action,payload}` 调用业务白名单。受浏览器会话保护的 `local_panel_status` 读取本机端口和最近最多 50 行服务日志。
- 网页服务只监听 `127.0.0.1`。请求必须使用准确的本机 Host；业务 POST 还要求准确 Origin、`Sec-Fetch-Site: same-origin` 和首页发放的 HttpOnly、SameSite=Strict 会话 cookie。
- `GET /healthz` 仅供启动器使用，要求本机令牌并返回 `app_id`、协议版本、端口、进程号、实例 ID 和就绪状态。启动器还核对应用进程持有的 Windows 实例锁与本机实例标记，之后才复用该服务。
- MCP 工具从 `app.services.TOOL_SCHEMAS` 读取静态 JSON Schema，并经仅监听回环的令牌保护 RPC 调用 `BusinessService.dispatch`；不会暴露任意命令、文件或 SQL 接口。

## 隔离本机验收

需要隔离数据时，同时设置 `SHIXU_DATA_DIR` 和 `SHIXU_PORT`。两者由 stdio 入口与后台服务共同继承：

```powershell
$env:SHIXU_DATA_DIR = 'H:\AIcode\HexiPlan\runs\shixu-v1-20261124-01a20b4b\tmp\shixu-stdio-check'
$env:SHIXU_PORT = '43128'
& 'C:\Users\Lenovo\AppData\Local\Programs\Python\Python312\python.exe' -X utf8 -B 'F:\桌面\程序\shixu\scripts\mcp_stdio.py'
```

关闭同一隔离实例时，保留相同环境变量并运行 `py -3.12 -X utf8 -B -m app.launch --stop`；再用相同变量重跑 stdio 命令即可重新启动。不要为了验收指向或停止默认用户数据库。

## 统计、推荐与厨房 MCP 工具

- 消费统计：`spending_summary`、`period_compare`、`meal_frequency`、`budget_set`、`budget_status`、`meal_tags_set`、`diet_review`。预算与标签分别保存在周期预算和餐次标签表中；支出结果区分已记录付款、食用成本及未知金额，不把估算当作实际付款。
- 推荐记录与行程：`recommendation_defaults_get/set`、`recommendation_history_list`、`recommendation_meal_link_set`、`recommendation_preference_set/list`、`recommendation_skip_record/list`、`recommendation_time_profile_set/list`、`meal_event_set/list`、`recommendation_countdown`。普通 `recommend` 仍为只读；只有明确调用关联餐次或记录跳过的工具时才写入反馈。
- 菜谱：`recipe_import_preview` 先整理用户提供的菜谱，`recipe_import_commit` 在确认预览后保存新版本；`recipe_scale` 按目标份数缩放已知数量和单位。无单位或无法换算的用量会标为待确认，不推测数值。版本、来源和反馈可通过 `recipe_version_list/get`、`recipe_feedback_add/list` 查询；普通 `recipe_list` 也会附带最近仍适用的反馈。
- 厨房：`inventory_batch_upsert/list/adjust` 管理库存批次，`leftover_upsert/list/history_list` 管理剩菜，`menu_plan_upsert/list` 管理计划餐次，`shopping_list_build/get/mark_bought` 合并并维护购物清单。

`recommend` 默认不使用库存。设置 `use_kitchen_stock=true` 后，它会附带指定厨房位置（`kitchen_location`）的有效批次和可用剩菜。未指定位置时汇总所有位置；响应会保留数量、单位、批次来源和保质日期。显式传入的 `available_ingredients` 优先。只有数量已知且单位可比、库存足够时才标记为匹配，其余情况会显示不足或待确认。

购物清单的「已买入库」按清单剩余数量登记。数量未知时填写实买数量和单位。部分采购或补录购买日期可用 `shopping_purchase_record` 记录。购物清单项重新打开时保留已登记的采购，重新标记不会重复入库已覆盖的数量。无购物清单时可直接使用 `inventory_batch_upsert`。

每批新食材可直接填写本批到期日 `expires_on`，也可输入 `shelf_life_days` 保存保质天数并按购买日计算。只填写本批日期时，已保存的天数不变。后续入库同一种食材、同一存放位置时，可留空沿用保存的天数。位置留空单独匹配。三种采购入口都按 `recorded_on`（省略时为今天）重新计算，缺少日期和已存天数时返回 `SHELF_LIFE_REQUIRED`。MCP 客户端可用 `inventory_shelf_life_get` 查询，用 `inventory_shelf_life_set` 单独保存或清除。已有批次的到期日继续按原记录使用。剩菜登记的 `use_by` 默认保留至制作日的次日。

做饭的 `recommend`、`meal_options` 和 `recipe_scale` 发现仍有余量的过期食材或可用状态的过期剩菜时，返回 `kitchen_alerts`，包含名称、登记期限和余量。MCP 客户端应在给出做饭建议前把提醒告诉用户。期限当天仍可计入库存，从次日起排除。

## 饭点、周月报与场合

- 用 `meal_reminder_set` 设置餐次与本机时间。星期一为 0，星期日为 6，省略 `weekdays` 即每天。用 `meal_reminder_list` 取得当前版本，再传 `rule_id` 和 `expected_version` 修改或停用。用 `meal_reminder_pending` 看未处理事件，用 `meal_reminder_ack` 确认处理。`meal_reminder_claim` 用于多个浏览器页面之间去重弹窗。
- `meal_report` 接受 `period="week"` 或 `"month"`，可指定 `anchor`。返回周期内的方式次数、常吃的菜和店、奶茶笔数、最近 30 条餐次、付款和预算。`meal_diet_insights` 返回近期有依据的标签和一般建议，并区分手动确认与规则推断。
- 先用 `store_search` 确认店铺 ID，再用 `store_profile_get/set` 查询或记录环境、适合的场合及备注。保存候选时可在 `details.store_id` 引用店铺。调用 `recommend` 时传 `occasion`，例如“周末聚餐”，会返回记录匹配、未列出或待核实的依据。合并店铺时会保留场合的并集，环境和备注优先采用保留店铺已有内容。

## 菜价与可选菜谱来源

- 用 `price_snapshot_add` 保存食材、整包规格、整包金额（分）、地区、日期与来源，通过 `price_snapshot_list` 查询。`recipe_price_estimate` 按所选地区和日期取最近价格，分别计算本次购买整包装的金额和按耗用量分摊的成本。传入 `use_stock=true` 可扣除同单位的有效库存。缺失价格或用量时，返回已知部分和缺项。
- `recommend` 可传 `price_region` 将菜价估算纳入做饭候选比较。设置 `use_kitchen_stock=true` 后，足量库存可将采购付款估成 0 元，仍计入食用成本。已记录的费用依据优先，估算会附食材的价格来源和日期。
- 百度搜索与模型生成各自默认关闭。`recipe_provider_status` 返回开关与配置就绪状态。百度搜索需要 `SHIXU_ENABLE_BAIDU_RECIPE=1` 和 `SHIXU_BAIDU_API_KEY`。启用后 `recipe_baidu_search` 只发送本次搜索词，返回可以核对的链接和摘录。[百度千帆搜索接口](https://cloud.baidu.com/doc/qianfan-api/s/wmwbk70bv)
- 生成需要 `SHIXU_ENABLE_RECIPE_MODEL=1`、`SHIXU_RECIPE_MODEL` 和 `SHIXU_RECIPE_MODEL_URL`（Chat Completions 完整地址）。需要认证时设置 `SHIXU_RECIPE_MODEL_KEY`。`recipe_model_preview` 只发送本次提示词和份数，返回标记为 `ai_generated` 的菜谱预览。调用 `recipe_import_commit` 时提交返回的导入参数、预览签名及明确确认即可保存。
- 环境变量由后台服务启动时继承，参数变更后重新启动服务。外部请求可能按提供方规则计费。两条适配器已用模拟响应验证解析和预览流程，真实账号调用需要本机配置后单独核对。

服务启动时将既有 SQLite 数据逐步迁移至当前版本，沿用原数据库的餐次、付款行、请求回执和店铺关联。当前版本见[版本记录](version.md)。

## 备份与恢复

服务从旧版升级时会将 SQLite 数据结构迁移到 v9，保留当前数据库中的请求回执。若升级前食序后台已启动，先按该实例的数据目录和端口运行 `py -3.12 -X utf8 -B -m app.launch --stop`。从旧默认目录 `%LOCALAPPDATA%\Shixu` 搬迁时，先用 `SHIXU_DATA_DIR` 指向旧目录停止服务，把数据库、日志和本机令牌迁到新目录，再使用默认配置启动。

服务运行时也可以做一致的 SQLite 快照。运行下列命令后，快照以时间戳命名，保存到当前数据目录的 `backups` 子目录。也可以在命令末尾传入尚不存在的绝对文件路径。

```powershell
py -3.12 -X utf8 -B 'F:\桌面\程序\shixu\scripts\backup_db.py'
```

恢复前先为当前数据库另做备份，再用 `app.launch --stop` 停止服务。将当前 `shixu.sqlite3` 改名留存，把所选快照复制到 `I:\data\Shixu\shixu.sqlite3`，然后运行 `scripts\start.cmd`。服务首次就绪时会执行所需迁移，可打开页面或调用 `meal_list` 回读已恢复数据。使用自定义数据目录时，在整个恢复流程中保持相同的 `SHIXU_DATA_DIR` 和 `SHIXU_PORT`，并将快照复制到该目录的 `shixu.sqlite3`。

## MCP 1.1 整餐、单点和选配

用 `dish_catalog_set` 维护菜品 ID 和别名，用 `dish_offer_set` 维护可单点的门店菜品、份量、时间、配料、价格和来源。门店可以用已有 `store_id`，也可以用 `store` 提供已知店名和分店。单点价以分录入，表示一份独立可选的菜。没有单点价的套餐成员保留整餐出处，单品金额返回未知。要使用厨房库存和包装价，给做饭选项提供每份的 `ingredient_amounts`，并在比较时传 `use_kitchen_stock` 和价格地区 `price_region`。

`meal_options` 的三种调用示例：

```json
{"kind":"whole","slots":[{"role":"main"},{"role":"side"},{"role":"soup"}],"constraints":{"budget_cents":3000}}
```

```json
{"kind":"single","query":"青菜"}
```

```json
{"kind":"complement","locked":[{"offer_id":"主菜的单点报价 ID","quantity":1}],"slots":[{"role":"side"}],"addition_budget_cents":1000}
```

整餐查询省略 `slots` 时比较已存候选与菜谱。传入 `slots` 时，按单点档案组合。自动选配按同一方式和已知门店匹配，做饭可组合本机菜品。可以同时传 `locked`、`selection` 和 `slots`，保持主菜份数、加入已选菜，并补足另外指定的菜。`addition_budget_cents` 用于新增菜的付款，`constraints.budget_cents` 用于整餐。总价未知时仍会返回已知小计，已知小计超预算会排除。做饭组合共同核算库存和包装数，能确认锁定菜与完整菜单两种结账估算时，差额用于追加预算。工序重叠方式未知时，组合按区间比较。超过搜索上限会标记 `bounded`。

`meal_options_compare` 给同一次查询传多组条件。评分权重 `time`、`cost`、`preference`、`variety` 可调整，合计为 100。用 `recommendation_defaults_set` 可按餐次保存权重和预算，显式传入的条件优先。过敏限制、时间和预算仍会先决定可行状态，同店同方式的菜品评价及近期实吃记录参与评分。结果可带预算与截止时间余量。

## MCP 1.1 台账操作

| 功能 | 工具 |
| --- | --- |
| 从历史建立候选、按菜评价 | `candidate_from_meal`、`meal_item_profile_set` |
| 更正历史、拆并餐、调整奶茶归属 | `meal_change_history`、`meal_change_preview`、`meal_change_commit` |
| 按已记录的进度比较截止时间 | `meal_journey_remaining` |
| 菜谱差异、替换、排程和配料依据 | `recipe_versions_compare`、`recipe_adjust_preview`、`recipe_work_plan`、`recipe_ingredients_verify` |
| 批次预留、消费、释放、盘点与剩菜 | `stock_change_preview`、`stock_change_commit`、`stock_reservation_list`、`leftover_take` |
| 菜单改期、部分采购、清单查询 | `menu_plan_reschedule`、`shopping_purchase_record`、`shopping_list_list` |
| 退款、分账、采购分摊和剩菜成本 | `meal_money_record`、`meal_money_details` |
| 模糊查餐、导入核对、导出和数据检查 | `meal_history_query`、`meal_import_inspect`、`meal_data_export`、`meal_data_check` |
| 接口和数据库版本 | `shixu_status` |

更正和库存批量写入先预览，提交同一参数、预览返回的 `fingerprint`、稳定 `request_id` 和 `confirm:true`。发现表单数据已变更时重新预览。库存预留会影响推荐与购物清单计算。部分采购写入实买数量并增加库存，可同时记录该包装的实际价格。跨餐分摊和剩菜成本用于追踪食用成本，采购付款保留原记录。

- 要从历史恢复某笔付款，用 `meal_change_preview` 的 `restore_payment`，给出餐次 ID、付款 ID 和历史版本。可只恢复金额，并保留后续写的备注。合并有重叠菜品或付款的餐次时，通过 `move_item_ids`、`move_payment_ids` 指定迁入的行，其余行保留在已归档的来源餐次。
- 旧菜谱版本可直接作为调整草稿来源。默认按新菜谱预览，显式提供 `target_expected_version` 时按该菜谱的当前版本预览。更新原版本的配料核对资料时传最新的 `expected_updated_at`。
- 购物清单的 `quantity_to_buy` 是原始需求，`quantity_purchased` 和 `remaining_quantity` 分别是实买量和当前缺口。费用详情省略 `period_start` 时，按该餐所属周或月读取预算。
- `meal_import_inspect` 返回已有记录和同批中可能重复的导入行。`meal_data_export` 支持餐次、独立奶茶、库存和菜品档案，返回 JSON 或 CSV 数据。
