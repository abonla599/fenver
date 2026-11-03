# Fenver「模型驾驶舱」spec：上下文 / 模型卡 / 接力队列

- 日期：2026-10-31
- 状态：设计方向已确认，spec 待用户审阅
- 代码基线：`develop` `2757d308`（写作时 `perf/package-a` `d5b30a67` 与 `fix/m8-export-fallback` `2b9c57e7` 尚未合并；实现应在相关分支收口后从最新 develop 切出）
- 版本：待拍板。建议作为独立的交互批次；不与「v0.39 移动端感知只读」混写。
- 范围：网页版 + 安卓原生 + 后端模型能力元数据 + done 帧 usage 回传
- 非目标：服务端队列、跨设备同步、自动摘要压缩、真机帧率承诺、新增模型服务商

## 0. 一句话

把「选模型 / 看能力 / 管上下文 / 忙时排队」收进输入卡周围：模型名旁边一圈上下文环，点名字开模型卡，点环开上下文账本；忙时发送进接力条，正常完成自动接力，停止或报错暂停。

## 1. 背景与现状（已核实）

| 事实 | 证据（baseline develop 2757d308） |
|---|---|
| provider 已有 `max_context_k`（1–10000K，缺省 64K），`/v1/models` 已下发 | `backend/app/core/providers.py:274-292`、`:642-690`、`:700-723` |
| provider 已有 `supports_vision`；没有 `supports_thinking`、思考档位或思考请求参数 | `providers.py` 的 `_validate` / `catalog`；`streaming.py` 只被动读 `reasoning_content` |
| 上下文预算 `contextTokensK` 是**每台设备一个值**（默认 8K），按当前模型 `max_context_k` 封顶 | `static/app.js:206-214`、`:314-345`；`SettingsUi.kt:899-924` |
| 发送时用 `truncateWithin` 从最近消息往回截，超出预算静默丢最早的消息 | `static/app.js:280-298`、`:334-345`；原生 `outboundMsgs()` / `Prefs.contextTokensK` |
| 忙时不能发：web `send()` 遇到 `state.streaming` 直接 return；原生 `streamInto()` 遇到 `busy` 直接 return | `static/app.js:2286`；`ChatUi.kt:874-875` |
| 上游 usage 已记账，但 done 帧没有 usage；客户端拿不到「上次实测上下文」 | `streaming.py:209-210`、`:306-317`、`:527-530`；`stream_events.py:197-208` |
| 模型选择页原生只是名字清单；网页是模型菜单；能力与上下文散在设置页 | `ChatUi.kt:2334-2379`；`SettingsUi.kt:1395+`；`static/app.js` 模型菜单与 `ctxRange` |
| 出站消息只有 `role/content`；trace/工具结果不随历史重发；只有最新一条的附件随请求发送 | `ChatUi.kt` 的 `outboundMsgs`；`app.js` 的 `outbound()`；`main.py:664-695` |

**由此得到两条不能写错的口径：**

1. 上下文账本不能把「旧工具结果 / 旧附件」算成一个可清理的段——它们本来就不在出站消息里。能免费清掉的只有**最早的消息**；工具/记忆/系统提示的 token 只能靠 done 帧的 `prompt_tokens` 校准，不能假装客户端知道。
2. 思考开关不能先给 UI 再猜参数。没有真实 API 核实之前，模型状态只能是「未验证」，服务端不发任何思考参数。

## 2. 目标 / 不变量 / 非目标

### 2.1 目标

1. 忙时发送不丢内容：进入可见的接力队列，正常完成自动发队首，停止/报错暂停。
2. 上下文可见且可操作：环显示本会话预算使用量；点开能看分段、超预算预报、免费清理最早消息。
3. 模型能力在选模型的地方管理：思考支持/档位、模型上限、本会话预算；桌面悬停直接改，手机点开展开。
4. 思考状态诚实：已核实 / 不支持 / 未验证三态；只对已核实 provider 发参数。
5. 旧客户端、旧 API、旧配置记录不破。

### 2.2 不变量

- 不新增必须登录/授权才能用的能力；不新增系统权限。
- `done` 帧只增加可选字段，旧字段与旧客户端解析不变。
- 不把队列或上下文估算发到服务端；队列不上传。
- 不改 `data/`、`chroma_db/`、`venv/`、`version.txt`；不改 `.github/workflows/*`。
- 网页不新增内联脚本；CSP 口径不变。
- 所有模型名、描述、队列文本按纯文本渲染；不拼 HTML。
- 发版仍走 `CI → 本地全量 → tag → pre_tag_gate → 构建 → 换包 → 线上验收`；本 spec 不授权 push/tag/发版。

### 2.3 非目标

- 服务端排队、跨设备队列同步、后台自动发送。
- 跨设备同步本会话预算。
- 自动上下文摘要（深度压缩）——P2。
- 模型「显示状态」开关（截图 4 的那一列）——P1 可选，不在首版。
- 多标签页共享同一条队列的并发保证——首版明确为每标签页内存队列。
- 真机帧率/观感承诺；以真机测量为准。

## 3. 术语与口径

| 术语 | 定义 | 归属 |
|---|---|---|
| 模型上限 `maxK` | provider 记录里的 `max_context_k`，模型真正能吃的窗口 | 服务端；共享模型管理员可改，私有模型属主可改 |
| 本会话预算 `budgetK` | 这次对话最多带多少上下文；必须 ≤ `maxK` | 客户端，按「用户 + provider」存；首版不同步 |
| 客户端估算 `clientK` | 按当前分词口径估算的 persona + 出站消息 + 待发队列 | 客户端 |
| 上次实测 `lastPromptK` | 上一轮 done 帧回传的 `usage.prompt_tokens` | 服务端回传，客户端按会话记住 |
| 校准后使用 `usedK` | 有实测时按比例校准的当前用量；没有实测时等于 `clientK` | 客户端展示口径 |
| 待发队列 `queueK` | 队列里每条消息的估算之和 | 客户端 |
| 思考状态 | `verified`（已核实）/ `unsupported`（不支持）/ `unverified`（未核实） | 服务端能力字段 + 客户端展示 |
| 轻量整理 | 从**本轮发送**里去掉最早 N 条消息，历史原文不动，免费、可撤销 | 客户端 |
| 深度压缩 | 用模型摘要早期对话，替换发送上下文；P2，需等待/积分 | 服务端（P2） |

### 3.1 估算与校准

沿用现有口径（`app.js:280-298`）：

- 中日韩字符约 1 token，其余约 4 字符 1 token；每条消息加 4 token 包装开销。
- `clientK = estimate(persona) + estimate(出站消息) + estimate(最新附件文本/元数据)`；**最新附件的 token 不精确**，标「附件未计入估算」而不是瞎算。
- `lastPromptK` 来自上一轮 done 帧的 `usage.prompt_tokens`，同一会话同一模型才可用。
- 校准比例 `ratio = clamp(lastPromptK / max(1, lastClientK), 1, 4)`；`usedK = round(clientK * ratio)`。没有 `lastPromptK` 时 `usedK = clientK`，面板标「估算」；有则标「按上次实测校准」。
- 轻量整理 / 删除消息后，按新的出站消息重算 `clientK`，沿用当前 `ratio`；下一次 done 帧到达后重新校准。面板在这一拍标「估算」，不把旧的实测数当新数。
- 分段只展示客户端真知道的：角色设定、历史对话、本轮输入、待发队列；差额 `max(0, usedK - 客户端已知段之和)` 单列为「服务端注入 / 未单列」（记忆、工具、系统提示），不冒充附件或工具明细。
- 队列不计入 `usedK` 的实心部分，单独画成环外虚线弧；`usedK + queueK > budgetK` 时提前警告。

### 3.2 预算默认与迁移

- 迁移：首次打开时，把现有 `contextTokensK` 写入**当前模型**的 per-model 预算；其他模型没有记录时沿用 8K 缺省。
- 不自动把预算放大到模型上限；模型卡提供 `8K / 32K / 64K / 128K / 用满` 快捷档，由用户明确选择。
- 模型上限调低到低于当前预算时，预算自动夹到新上限并提示一次；不静默保留越界值。
- 首版预算只在本机（web localStorage / 原生 Prefs），不上服务端；换设备不同步，作为已知限制。

## 4. 交互设计

### 4.1 输入卡的三个入口

- **模型名**：点开模型选择/模型卡；当前模型名 + 倍率 + 一个小 chevron。
- **上下文环**：24dp，位于模型名左侧；填充 = `usedK / budgetK`；外圈虚线 = `queueK`；超过预算时环变警示色并出现红色缺口。点开上下文账本。
- **停止/发送**：忙时是停止键；空闲时是发送键。忙时点发送不丢内容，进接力条。

### 4.2 模型卡

**桌面**：模型选择是双栏浮层，左列表右详情；鼠标悬停左行，右侧详情即时切换；点击锁定。右栏内联可改，不跳设置页。

**手机**：底部弹层；列表行点击展开同一张卡；底部固定「模型管理」。

每张卡包含：

1. 头像、名称、服务商、倍率、描述。
2. 能力徽章：`思考已核实 / 思考未验证 / 不支持思考`、`XK 上下文`、`视觉`、`共享 · 管理员可改`。
3. 思考：开关 + 模型自定义档位（不写死低/中/高）。只有 `verified` 可操作；`unsupported` 显示原因；`unverified` 显示「先核实参数，不猜」。开关/档位是**每用户本机偏好**（按 provider 记），共享模型的能力字段仍只读；服务端只提供能力与默认档。
4. 模型上限：滑杆 + 数值 + 刻度；量程按模型实际值生成（1K..10000K），示例刻度不是硬编码；共享模型普通用户只读，管理员可改；私有模型属主可改。
5. 本会话预算：滑杆 + 「用满」；快捷档（8/32/64/128K）超过 maxK 时夹到 maxK；与模型上限分开写清；显示「已用 XK · Y%」。
6. 主按钮「设为本轮模型」；次按钮「管理此模型」。

**权限**：

- 共享模型：能力/上限只有管理员可改；普通用户只读。
- 私有模型：属主可改；管理员通过现有管理入口可改。
- 服务端 `catalog()` 增加 `can_edit` 布尔，客户端不自己猜角色。
- `can_edit` 只约束 provider 能力/模型上限（服务端字段）；**本会话预算始终是本机设置**，普通用户对共享模型也可以调自己的预算，不受 `can_edit` 限制。

**模型管理页**：

- 模型卡是单模型的快捷视图；底部「模型管理」打开同一组字段的批量视图：桌面表格式（模型 / 思考 / 档位 / 上限 / 操作），手机卡片式。共享模型的能力/上限只有管理员可写；私有模型属主可写；普通用户只读。
- 批量视图与模型卡共用同一份 provider 字段和同一套校验，不各写一份；连接配置（base_url/Key/pricing）仍回设置页。

**设置页的边界**：

- 设置 → 模型服务继续保留：base_url、API Key、model、supports_vision、paid_by、pricing、测试、默认。
- 思考、模型上限、本会话预算从设置页迁到模型选择区；设置页不再重复一份，避免两个真相。
- 模型服务页可保留一个「模型能力在模型选择里管理」的说明链接，不再放这些输入项。

### 4.3 上下文账本

桌面：锚定在输入卡上方/模型 chip 附近的气泡；空间不足时居中降级。手机：底部弹层。

内容顺序：

1. 头部：`27.3%`、`82K / 300K`、`模型上限 1M · 队列 +5K`，小环。
2. 分段条 + 图例：角色设定 / 历史对话 / 本轮输入 / 待发队列 / 服务端注入（未单列，仅校准时出现）。每段的数和来源都写清。
3. 预报：`下一条会带上 N 条历史；不会丢弃早期消息` 或 `将丢弃最早 N 条（约 XK）`，点「查看」列出具体消息。
4. 动作：
   - **轻量整理**：从本轮发送里移除最早 N 条，历史原文不动；显示真实省下的 K；可「恢复整理前」。免费、无模型调用。
   - **深度压缩**：P2；本次不实现，按钮置灰/标 P2，点按只解释。
5. 本会话预算滑杆；与模型卡同一份状态。
6. 脚注：`估算值；上次请求实测 224.9K（含服务端注入）` 或 `仅估算，暂无实测`。

**超预算**：环变警示色；面板出现红色说明；下一次发送前要求一次确认；确认后按 `truncateWithin` 的规则丢最早消息；不再静默。

### 4.4 接力条

位于输入卡上方；队列为空时完全隐藏。

每条卡：

- 拖拽手柄（桌面 hover 出现；手机常显）+ 两行预览；
- 模型/倍率 chip + `+XK` 估算 chip + `等待当前回答结束`；
- 编辑（行内 textarea）、删除、`立即插话`；
- 队列整体有 `暂停 / 继续`；
- 底部一句：`当前回答结束后自动发送队首；停止或报错会暂停`。

交互：

- 忙时发送 → 入队、清空输入、toast「已加入接力队列」；不打断当前回答。
- 正常完成 → 自动发队首；发送后下一条继续等下一轮。
- 手动停止 / error 帧 / cannot_resume → 队列暂停，显示「继续队列 / 清空」。
- 立即插话 → 二次确认「会停止当前回答，已生成部分保留」；确认后停当前轮、发该条，剩余队列暂停。
- 编辑中的卡片不能发送；发送中的卡片不能编辑/删除/重排。
- 删除/清空需可撤销？首版删除即时（有 toast），不做撤销；编辑可保存/取消。

## 5. 队列状态机

| 当前状态 | 事件 | 结果 |
|---|---|---|
| 空闲 | 发送 | 正常发起流，不入队 |
| 流式中 | 发送 | 入队；输入清空；队列可见 |
| 流式中 | done completed（且会话/身份仍匹配、队列未暂停） | 300ms 后发队首；保持流式 |
| 流式中 | 停止 / error / cannot_resume | 队列暂停；不自动发 |
| 暂停 | 继续 | 若当前空闲且会话匹配，发队首；若仍在流式，等当前结束 |
| 任意 | 切会话 | 队列按会话留在内存；不向另一个会话发送；回来显示待发/暂停 |
| 任意 | 切身份 | 清空当前身份的内存队列；其他身份的队列不可见 |
| 任意 | 模型被删/不可用 | 暂停并提示「发送前先选一个可用模型」 |
| 任意 | 队列条目带图片，但当前模型 `supports_vision=false` | 暂停；提示换视觉模型或移除附件 |
| 任意 | 附件失效 | 该条标错；暂停；允许编辑移除附件或删除 |
| 任意 | 队列满（20 条 / 20k 字） | 拒绝入队并提示；不丢已有条目 |
| 任意 | 进程重启 | 首版内存队列丢失；不做静默恢复（P1 再评估加密持久化） |
| 立即插话 | 确认 | 调现有 stop/cancel；当前流结束后发该条；剩余暂停 |
| 发送中 | 用户再次点发送 | 忽略（发送中条目已认领，防重复） |

**自动接力的前提**：当前会话/身份仍匹配、队列未暂停、页面/App 在前台（web `visibilityState === "visible"`；原生前台）。后台或切走不自动发；回到前台显示「待发送 / 继续」，由用户点一下再发。

**首版队列只活在进程内**：切会话可保留（内存 map keyed by session），重启不恢复；多标签页各自一份，不共享。持久化（native 加密 Prefs / web localStorage）列入 P1，需单独安全评审，避免把用户原文新增一份本地明文。

## 6. 数据与 API

### 6.1 provider 记录新增字段

```json
{
  "id": "p_ab12cd34",
  "label": "DeepSeek-V4.1-Flash",
  "model": "deepseek-v4.1-flash",
  "supports_vision": true,
  "max_context_k": 1000,
  "supports_thinking": true,
  "thinking_levels": ["快速", "均衡", "深入"],
  "thinking_default": "均衡",
  "thinking_profile": "none"
}
```

- 老记录兜底：`supports_thinking=false`、`thinking_levels=[]`、`thinking_default=""`、`thinking_profile="none"`。
- 校验：`max_context_k` 1–10000；`thinking_levels` 0–3 个、短标签、去重；`supports_thinking=true` 时 levels/default 必须自洽；`thinking_profile` 必须在服务端白名单内；`supports_thinking=false` 时 profile 必须 `none`。
- `thinking_profile` 是**服务端白名单枚举**，不是任意 JSON/headers；白名单里只放已经过真实 API 核实的 profile。因此 `profile != "none"` 本身就是「已核实」的事实，不需要再存一个可被客户端伪造的 verified 布尔。
- 权限：共享模型能力字段仅管理员可写；私有模型的属主可写 `supports_thinking`/levels 并从白名单选 profile；普通用户只读。`catalog()` 增加 `supports_thinking`、`thinking_levels`、`thinking_default`、`thinking_status`、`can_edit`；`thinking_profile` 不进普通目录。
- 管理端/属主编辑自己的私有模型时需要 profile 选项列表：由服务端只读端点/管理响应提供（id、label、verified、允许的档位映射），客户端不硬编码；共享模型的普通用户拿不到该列表。
- `thinking_levels` 是**有序 UI 标签**；服务端按索引映射到 profile 定义的上游取值，标签本身不发给上游。

### 6.2 `/v1/models` 新增目录字段

```json
{
  "id": "p_ab12cd34",
  "name": "DeepSeek-V4.1-Flash",
  "max_context_k": 1000,
  "supports_vision": true,
  "supports_thinking": true,
  "thinking_levels": ["快速", "均衡", "深入"],
  "thinking_default": "均衡",
  "thinking_status": "verified",
  "can_edit": false
}
```

`thinking_status` 由服务端派生（实现：`app/core/providers.py::_thinking_status`）：`supports_thinking=false` = `unsupported`；`thinking_profile` 等于 `none`（含缺省）= `unverified`；`thinking_profile` 命中服务端白名单**且那一条自己带 `verified: true` 旗标** = `verified`；其余一律 `unverified`。客户端不根据名字猜。

> 口径收口（Task 8 的裁决，Task 9 修复轮写回本行）：本行原文写的是「`supports_thinking=true` 且 `thinking_profile != "none"` = verified」，代码不是这么做的，而且代码更诚实。判据多了一格：**白名单条目必须自带 `verified` 旗标**。理由是把「不是 none」当成已核实，等于让盘上任何一条没人验过的 profile（老记录、手工写坏的、回滚留下的）都够格让界面说"这个模型的思考参数已核实"——那句话会直接决定用户能不能打开思考开关、这一轮要不要按上游的推理参数计费。§6.1 里「`profile != "none"` 本身就是已核实的事实，不需要再存一个 verified 布尔」那一句以本行为准，已被这条取代。**Task 12 往 `THINKING_PROFILES` 加配方时，必须同时给它 `verified: True`（真实 API 核实通过之后），否则界面只会显示「参数未核实」，开关永远点不动。**

`none` 在白名单里也标了 `verified: True`（"不带任何思考参数"这件事本身是确定的），但它被单独排除在 `verified` 之外：`profile == none` 永远只意味着"没配思考参数"，不意味着"思考已核实"。

`verified` 的准确含义是「该 profile 的参数形状已核实，且这条 provider 记录声明支持」，**不是**对每个模型做了逐模型实测；管理端选择 profile 时仍要按 provider 文档核对模型。不要把「profile 已核实」写成「这个模型一定支持思考」。

### 6.3 ChatRequest 增加可选的思考档位

```python
class ChatRequest(BaseModel):
    ...
    thinking_level: Optional[str] = None
```

- 省略 = 不传思考参数/使用模型默认。
- 服务端校验：provider 必须有 `verified` profile，且 level 在 `thinking_levels` 内；否则 400，人话 `该模型不支持思考` / `该模型不支持这个思考强度`。
- 旧客户端不传 → 行为与今天完全一致。
- 非流式 `pipeline` 与流式 `stream_chat_events` 必须走同一个映射函数，不能只修一条路。
- 参数映射只在 P0c 对**真实核实过**的 profile 开启；候选（不是既成事实）：
  - `openai_reasoning_effort`：`reasoning_effort`；
  - `dashscope_enable_thinking`：`extra_body.enable_thinking`（及可选的思考预算）；
  - 具体字段与档位映射以真实 API 验证结果为准，验证前 profile 保持 `none`/未核实。
- 现有 `reasoning_content` → thinking 帧的展示链不变；不新增帧类型。

### 6.4 done 帧增加可选 usage

```json
{
  "type": "done",
  "status": "completed",
  "message_id": "m_...",
  "model": "...",
  "full_text": "...",
  "usage": {
    "prompt_tokens": 224900,
    "completion_tokens": 1200,
    "total_tokens": 226100,
    "reasoning_tokens": 300,
    "cached_tokens": 0
  }
}
```

- 来源：`stream_chat_events` 的 `billed`。新增可选 `usage_out: dict` 形参，`finally` 里把 `billed` 拷进去；`main.py` 在 done 帧里带 `usage`。计费调用 `usage.record_call` 不改、不重复记。
- 旧客户端忽略新键；`done` 原有字段逐字不变。
- 只有 token 计数，不含任何内容。
- 客户端按会话记住最近一次 `lastPromptK` 与发起时的 `lastClientK`，用于校准；不新增服务端会话字段。

### 6.5 客户端预算存储

- Web：`localStorage` 的 `fv.contextBudget.<userId>.v1`（按身份隔离），JSON `{ "<providerId>": 300 }`；首次迁移读旧 `contextTokensK` 写入当前 provider，迁移成功后删除旧键；测试锁住只迁移一次、不会双读。
- 原生：`Prefs` 的 JSON map，键名同 `fv.contextBudget.<userId>.v1`（存 SharedPreferences 时可用短键 + 身份前缀）；迁移 `Prefs.contextTokensK`。
- 读取顺序：per-model map → 旧全局值（仅迁移一次）→ 8K 缺省；始终 `min(budget, maxK)`。
- `lastPromptK`/`lastClientK` 按「会话 id + provider id」在内存记住；换模型或换会话不拿旧模型的数据校准。
- 思考偏好也是本机、按「用户 + provider」：`thinkingEnabled` + `thinkingLevel`；默认取 provider 的 `thinking_default`，该值不在 capabilities 里时回落第一个档；只在 `thinking_status=verified` 时生效。服务端不存用户的开关/档位。
- 不新增网络请求；不写 `data/`。

## 7. 三端实现边界

### 7.1 后端

- `providers.py`：字段、默认值、校验、catalog、public view、权限；写路径只有 `upsert`，别在路由里另写一份。
- `main.py`：`ChatRequest.thinking_level`；`_prepare_chat` 解析/校验；流式生产把 `usage_out` 接进 done；非流式 pipeline 同步支持思考档位（如果非流式路径也接收该字段）。
- `streaming.py`：`usage_out`；思考参数映射（P0c）；保持同步生成器与线程池约束。
- `stream_events.py`：`done_frame(..., usage=None)`；旧字段不动。
- 不改计费口径；`reasoning_tokens` 仍含在 completion 内，不额外加减。

### 7.2 网页

- `app.js`：模型菜单改双栏/移动弹层；上下文环/账本；接力条；预算 map；队列状态机；`done.usage` 解析。
- `style.css`：环、账本、接力条、模型卡；沿用 `--fv-*`；新增样式最终给深/浅两套 token，不新增第三强调色。
- `index.html`：必要的容器/模板；不新增内联脚本。
- `api.js`：ChatRequest 增加可选 `thinking_level`；done 事件透出 `usage`；旧字段不动。
- 设置页：移除上下文滑杆（迁到模型卡）；模型服务表单去掉能力/上限输入，保留连接配置；上下文账本里的预算滑杆与模型卡共用一份状态。
- 静态测试：模型卡/账本/队列的源码锁；`estimateTokens`、`truncateWithin`、校准、队列状态机用 Node 真跑纯函数（仓库已有 Node 跑 app.js 纯函数的先例）。

### 7.3 安卓原生

- `ChatUi.kt`：模型 chip + 环；`ModelPickerScreen` 升级为模型卡/管理；`streamInto` 忙时入队；队列条；账本弹层；`done` usage 解析。
- `SettingsUi.kt`：移除上下文滑杆/模型能力输入；模型服务保留连接配置；深链到模型卡。
- `Prefs.kt`：预算 map 迁移；队列首版放内存（`remember`/会话级 map），不落盘。
- `Api.kt`：请求 `thinking_level`；`ChatEvent.Done` 增可选 `usage`；旧服务端无该字段不报错。
- 静态锁：模型卡三态、环、队列状态机、停止暂停、会话隔离、终稿解析不回归；Kotlin `compileDebugKotlin` 必须过。

## 8. 权限与安全

1. 共享模型能力/上限仅管理员写；私有模型属主写；普通用户只读。服务端二次校验，不靠 UI 隐藏。
2. `thinking_profile` 白名单 + 服务端映射；客户端只传档位字符串；不把任意 JSON/headers 透传给上游。
3. 模型名、描述、队列文本全部纯文本渲染；网页不拼 `innerHTML`；链接只认 http(s)。
4. 队列首版只在内存；不新增本地明文持久化；不写日志、不进审计 detail；切身份清空。
5. 上下文估算只在客户端；不把队列/原文发给服务端做估算；done usage 只带计数。
6. 网页不新增内联脚本；新增 JS 走现有 app.js/api.js，避免 CSP 哈希与 SW 清单漂移。
7. 原生不新增系统权限；不在 App 退到后台时自动发队列。
8. `max_context_k` 的写路径继续走 `providers._validate` 的唯一入口；不绕过 SSRF/占位符/密钥校验。

## 9. 验收标准

### 9.1 P0a：usage + 上下文环/账本 + 接力队列

- done 帧带 `usage` 时旧字段逐字不变；旧客户端解析不报错；无 usage 时行为与今天一致。
- `estimateTokens` / `truncateWithin` / 校准比例有 Node 真跑用例；边界：空消息、单条超预算、lastUsage 缺失、比例 clamps。
- 环的填充 = `min(100, usedK/budgetK)`；队列虚线只算队列；超预算/队列超预算时警示；调低预算触发确认。
- 账本分段之和 + 服务端注入差额 = usedK；不出现「附件与工具」这种假段；最新附件标「未计入估算」。
- 轻量整理只减少本轮发送的最早消息，历史列表不变；可恢复；省下的 K 与真实重算一致。
- 队列：忙时入队不打断；completed 自动发队首；stop/error/cannot_resume 暂停；切会话不串；切身份清空；重启丢失（首版明确）；编辑/删除/重排/立即插话；发送中防重复；队列满拒绝。
- Web：静态锁 + Node 纯函数；原生：静态锁 + `compileDebugKotlin`。
- 全量测试零 failed/errors；不新增运行时资产。

### 9.2 P0b：模型卡 + 能力/上限管理

- provider 新字段老记录兜底；`/v1/models` 字段齐全；`can_edit` 按角色正确。
- 共享模型普通用户只读、管理员可改；私有模型属主可改；服务端拒绝越权写。
- 桌面双栏悬停切换、点击锁定；手机底部弹层展开；44dp 触控目标；键盘/焦点可用。
- 设置页不再重复能力/预算输入；模型卡与账本共用预算状态，不出现两个值。
- 静态锁：三态徽章、权限、预算上限夹取、迁移旧 `contextTokensK`。

### 9.3 P0c：思考参数

- 先做真实 API 核实（用户授权一次小额调用）：用已配置 provider 发一条极小 prompt，确认请求被接受且返回 `reasoning_content`/`reasoning_tokens`，全程不打印密钥；能确认的 profile 才进白名单并允许记录选择；不能确认的保持 `unverified`。
- 核实失败/上游拒绝时，profile 保持 `none`，UI 显示未验证；不把失败当「不支持」——两者是不同状态。
- 已核实：`thinking_level` 合法 → 请求带上映射参数；fake client 断言参数形状；`unsupported/unverified` → 不发参数；非法档位 → 400 人话。
- 流式与非流式两条路都走同一个映射函数；旧客户端不传字段行为不变。
- 不新增帧类型；`reasoning_content` 仍走现有 thinking 帧。

### 9.4 P2：深度压缩

- 服务端摘要端点；原文不丢；摘要只影响发送上下文；成本/时间提示；失败可回退；测试锁住摘要不覆盖历史。
- 明确不在首版；验收另开。

## 10. 分阶段与依赖

| 阶段 | 内容 | 依赖 |
|---|---|---|
| P0a | done usage、预算 map、上下文环/账本、接力队列 | 无新模型字段；可先落 |
| P0b | provider 字段、`/v1/models`、模型卡/管理、设置页迁移 | P0a 的预算/环 |
| P0c | 思考 profile、真实核实、请求映射 | P0b；用户授权小额核实 |
| P1 | 额外 provider、显示状态、队列持久化、跨设备预算 | 安全评审 |
| P2 | 深度压缩 | P0c/P1 稳定后 |

**同版建议**：P0a + P0b 可以作为首版同批交付（队列/环/模型卡一起才有完整体验）；P0c 在核实通过后紧随，若核实顺利也可并入首版。P0c 不能为了赶同版而跳过真实核实。

**基线依赖**：`perf/package-a`（流式渲染/滚动改动）与 `fix/m8-export-fallback`（H-1/L-1/M-7/M-8）尚未合并。实现前先按发版顺序收口这些分支，再从最新 develop 切特性分支；不要把本 spec 的实现继续叠在未合并树上。

## 11. 风险与待确认

1. **思考参数未核实**：DeepSeek/Qwen 的真实参数名、档位映射、是否接受 `stream_options.include_usage`，必须以一次真实 API 调用为准；未核实前 UI 只能显示未验证。
2. **估算 vs 实测**：`prompt_tokens` 含服务端记忆/工具/系统提示；客户端只能校准，不能精确拆段。账本必须标来源，不假装知道。
3. **轻量整理的真实收益**：出站只有消息文本；旧附件/工具结果本来不在出站里。首版的免费动作是「不带最早 N 条」，不是截图 6 的「摘要早期内容」；深度压缩是 P2。
4. **队列首版不持久化**：重启丢失、多标签页各自一份；这是有意的安全/复杂度取舍，P1 再评估。
5. **权限**：共享模型上限/能力被管理员改动会影响所有使用者；写入前需二次确认，客户端展示「共享」。
6. **预算默认**：迁移沿用 8K，不自动放大；用户要更大窗口需在模型卡明确选择，避免静默涨成本。
7. **版本归属**：待拍板；不要和 v0.39 移动端感知只读混写。
8. **测试环境**：真机帧率、浏览器实测、CI Linux 全量仍需独立验证；本 spec 不把它们写成已确认。

## 12. 附录

### 12.1 队列项形状（客户端内存）

```json
{
  "id": "q_1730000000_1",
  "text": "再把结论压缩成三条，发我邮箱。",
  "attachmentIds": [],
  "createdAt": 1730000000000,
  "modelIdAtEnqueue": "p_ab12cd34",
  "sending": false
}
```

- `text` 只在客户端内存；不上传、不落盘（首版）。
- `modelIdAtEnqueue` 只用于展示；实际发送用发送时的当前模型；模型不可用时暂停提示。
- 队列上限 20 条 / 20k 字；超出拒绝入队。

### 12.2 账本分段示例

| 段 | 来源 | 示例 |
|---|---|---|
| 角色设定 | persona 估算 | 4K |
| 历史对话 | 出站消息估算 | 58K |
| 本轮输入 | 当前输入估算 | 8K |
| 待发队列 | 队列估算（虚线） | 5K |
| 服务端注入 / 未单列 | `usedK - 已知段`，仅校准时 | 12K |

最新一条消息的附件不单列 token 估算：在「本轮输入」旁标「附件未计入估算」，因为图片/文档的 token 随模型与内容而变；不要用文件大小瞎换算。

### 12.3 文案表

- 入队：`已加入接力队列`
- 自动发：`当前回答结束后自动发送`
- 暂停：`队列已暂停，不会自动发送`
- 插话确认：`会停止当前回答（已生成部分保留），并立即发送这条消息。`
- 超预算：`已超出预算 XK：下一条会从最早的消息开始丢弃。`
- 轻量整理：`已从本轮发送移除最早 N 条：省 XK；历史原文未动。`
- 未核实：`思考参数尚未核实，先不发猜测值。`
- 不支持：`该模型不支持思考。`
- 共享只读：`共享模型由管理员设定；你可以调本会话预算。`

### 12.4 现有代码参考（baseline develop 2757d308）

- `backend/app/core/providers.py` `DEFAULT_MAX_CONTEXT_K` / `normalize_max_context_k` / `_validate` / `catalog`
- `backend/app/main.py` `ChatRequest` (`:343-361`)、`_prepare_chat` (`:664-695`)
- `backend/app/core/streaming.py` `stream_chat_events` (`:165-178`)、`billed` (`:209-210`)、`usage.record_call` (`:527-530`)
- `backend/app/core/stream_events.py` `done_frame` (`:197-208`)
- `backend/app/web/static/app.js` `contextTokensK` (`:206-214`)、`estimateTokens`/`truncateWithin` (`:280-298`)、`modelCapK` (`:314-317`)、`outbound` (`:334-345`)、`send` 的 streaming 闸 (`:2286`)
- `android-native/.../ui/ChatUi.kt` `modelCapK` (`:591`)、`streamInto` 的 busy 闸 (`:874-875`)、`ModelPickerScreen` (`:2334-2379`)
- `android-native/.../ui/SettingsUi.kt` 上下文滑杆 (`:899-924`)、模型服务页 (`:1395+`)

行号只用于定位 baseline；分支合并/重构后以符号为准，不在实现里抄死数字。
