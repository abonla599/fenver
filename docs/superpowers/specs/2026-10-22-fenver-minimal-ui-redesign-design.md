# Fenver v0.36.0 极简原生 UI 重设计 spec

- 日期：2026-10-22
- 状态：设计已定稿，待用户审阅
- 版本：v0.36.0（versionCode 67）
- 范围：安卓原生 App + 官网/PWA + 桌面图标 + 启动/切换/聊天流畅度
- 约束：功能不增不减，接口与数据格式不变

## 0. 一句话

把现有「深蓝黑 + 薄荷/紫罗兰渐变 + 光晕/玻璃」的网页移植风，收敛成一套以中性表面、单强调色、发丝线和 180ms 动效为核心的极简原生 Material 3 界面；两端共用同一套 token，图标简化为「气泡 + 星芒」的几何标记，并用真实代码改动解决启动、切换和流式聊天的卡顿。

## 1. 目标与不变量

### 1.1 目标

1. App 与官网/网页版视觉同源，观感统一为极简原生风。
2. 冷启动、页面/弹层切换、聊天长列表滚动与输入的手感明显改善。
3. 桌面图标与 App 内品牌标记在同一套几何语言下重做。
4. 所有页面、入口、操作、接口、数据格式、权限保持不变。

### 1.2 不变量

- 不改 API、请求/响应形状、鉴权、任务/审批/续跑、提醒、更新通道。
- 不新增权限，不新增网络请求类型，不新增用户可见功能入口。
- 官网六节结构、页面 id、CSP、Service Worker 清单、唯一 APK 出口 `/site/android.apk` 保持。
- 现有契约锁钉住的文案与行为保留：关于三句、日程 28 条、trace 标题与秒数、审批/任务/续跑文案、shell 桥 10 个方法名。
- 版本号唯一真相仍是 `android-native/app/build.gradle`。

### 1.3 非目标

- 不做新功能、新页面、新设置项。
- 不引入网络字体或新的大体积图片；继续用系统字体。
- 不改官网文案语义与信息架构，只改视觉、排版、动效与性能实现。
- PWA 首屏主题闪烁本版不新增内联脚本（避免 CSP/SW 额外风险），沿用现有 `applyTheme` 行为。

## 2. 现状与问题（已核实）

### 2.1 设计 token 已漂移

- Android `Theme.kt:41-89` 逐 token 抄 `static/style.css:6-68`，颜色同源。
- Android 有 183 处写死 `fontSize=`、87 处直接写 `RoundedCornerShape`，只有 6 处使用 `MaterialTheme.typography`。
- 官网 `site.css:15-136` 有独立的字号/缓动/圆角 token；PWA 没有字号/间距/缓动 token。
- 圆角三套：Android 8/10/14/18/24，PWA `18 + 硬编码`，官网 `10/18/24/999`。

### 2.2 视觉重量来自装饰而非层级

- 官网两层 `position:fixed` 无限渐变动画、全屏 grain、7 个 blur 面、16 处内联渐变。
- Android 全屏双径向光晕、渐变字标、发送键彩色光晕、玻璃层、彩色阴影。
- 低端机掉帧的主要风险是 blur、无限 transform、大面积阴影，而不是列表本身。

### 2.3 手感热点

- 启动：`MainActivity:27-35` 在主线程做 Keystore 初始化、读 reminders、扫 APK 目录；`ChatUi.kt:543` 的 models/tasks 串行。
- 设置/抽屉：`SettingsSheet`、抽屉、二级页均为条件挂载，打开即重置状态并重新拉数据。
- 聊天：`ChatUi.kt:806` 每片流式更新整段字符串，`RichText` 在组合内重切全文（`2705-2741`）；`LazyColumn` 无 key（`1238`）；`WindowInsets.ime.getBottom` 在 ChatScreen 作用域读取（`1094`）。
- 图片：附件在 `LaunchedEffect` 主线程 `decodeByteArray`（`2629-2651`）。

### 2.4 图标

- `drawable-nodpi/app_icon.png` 192×192 不透明渐变圆角块；五桶 mipmap 为同源缩放。
- adaptive icon 前景无 `<monochrome>`；底色渐变 `#0E7C86→#7030E0` 与品牌强调色分家。
- 图标生成脚本 `tools/render-app-icon.ps1` 被契约测试钉住；改配色需同批改测试。

## 3. 设计原则

1. **内容优先**：层级靠字号、字重、间距、发丝线，不靠光晕和阴影。
2. **单强调色**：薄荷绿只用于主操作、选中态、焦点与关键状态；紫罗兰只保留在 AI/星芒身份点。
3. **平面化**：默认无阴影、无 blur；只有弹层/浮层允许一层极轻阴影。
4. **动效克制**：只做 120/180/240ms 三档，不做无限循环装饰。
5. **两端同源**：颜色、字号、间距、圆角、缓动以一套 token 为权威，测试锁住漂移。
6. **先保证可读与可点**：正文对比度 AA，触控目标 ≥44dp，长文行长受控。

## 4. Design tokens

### 4.1 颜色

深色（默认）：

| token | 值 | 用途 |
|---|---|---|
| `bg` | `#0A0B0E` | 窗口/页面底 |
| `surface` | `#121317` | 卡片、输入区、弹层 |
| `surface2` | `#181A20` | 用户气泡、悬停行、次级容器 |
| `surface3` | `#1F222A` | 按压态、选中行底 |
| `line` | `#262A31` | 1px 发丝线 |
| `text` | `#F2F3F5` | 主文本 |
| `text2` | `#A1A6B0` | 次文本 |
| `text3` | `#6F747E` | 辅助/占位 |
| `accent` | `#3DD598` | 主操作、选中、焦点 |
| `accentPressed` | `#35C98C` | 按压 |
| `accentSoft` | `rgba(61,213,152,.12)` | 选中底、轻提示 |
| `ai` | `#7C83FF` | AI/星芒身份点，仅少量使用 |
| `danger` | `#F87171` | 错误/危险 |
| `codeBg` | `#08090C` | 代码块底 |

浅色：

| token | 值 |
|---|---|
| `bg` | `#F7F8FA` |
| `surface` | `#FFFFFF` |
| `surface2` | `#EEF0F4` |
| `surface3` | `#E4E7EC` |
| `line` | `#E3E6EB` |
| `text` | `#16181D` |
| `text2` | `#5F6672` |
| `text3` | `#8A919C` |
| `accent` | `#0A7D5E` |
| `accentPressed` | `#086A4F` |
| `accentSoft` | `#E6F6F0` |
| `ai` | `#5566F0` |
| `danger` | `#DC2626` |
| `codeBg` | `#F3F4F6` |

规则：

- 主按钮、发送键、开关、选中行、链接、焦点环用 `accent`。
- 紫罗兰不再用于大面积渐变、按钮、背景光晕；只用于 AI 状态、星芒图标与极少数品牌点。
- 错误用 `danger`；警告用 `text` + `danger` 图标或 `accent` 提示，不新增第三强调色。
- 代码块背景用 `codeBg`，不再直接取旧的 `WebTokens.CodeBg`。

### 4.2 字体

系统字体栈，不下载字体。Compose 使用 `FontFamily.Default`，代码使用 `FontFamily.Monospace`。

| token | 字号/行高 | 字重 | 用途 |
|---|---|---|---|
| `display` | 28/36 | SemiBold | 空状态大标题、登录品牌 |
| `headline` | 22/30 | SemiBold | 页面主标题 |
| `title` | 17/24 | SemiBold | 顶栏、卡片标题 |
| `body` | 16/24 | Normal | 消息、正文 |
| `bodySmall` | 14/20 | Normal | 次要说明 |
| `label` | 13/18 | Medium | 行标签、按钮、chip |
| `caption` | 12/16 | Normal | 时间、辅助信息 |
| `code` | 13/20 | Normal/Mono | 代码块、trace 技术行 |

契约钉住的 13sp/6dp/8dp/10dp（粘贴胶囊、日程、trace 等）保留原值，映射到 `label` 或对应 token；不为了统一而改动已被行为锁钉住的尺寸。

### 4.3 间距、圆角、层级

- 间距阶梯：4 / 8 / 12 / 16 / 20 / 24 / 32dp。
- 页面左右边距 16dp；分组间距 24dp；卡片内边距 16dp；行内左右 16dp。
- 行高：设置/抽屉 52dp；触控目标最小 44dp；聊天顶栏 56dp。
- 圆角：4 / 8 / 12 / 16 / 24dp；卡片 16、输入 12、底部弹层顶部 24、chip 999。
- 阴影：默认无；弹层/浮层一层 `0 8 24 rgba(0,0,0,.24)`；浅色 `0 8 24 rgba(16,24,40,.10)`。
- 分隔：1px `line`；不再用嵌套卡片制造层级。

### 4.4 动效

- `quick` 120ms、`standard` 180ms、`emphasized` 240ms。
- 标准 easing：`cubic-bezier(0.2, 0, 0, 1)`；仅强调动效使用 `cubic-bezier(0.16, 1, 0.3, 1)`。
- 页面切换：180ms 淡入 + 8dp 垂直位移；返回反向。
- 弹层/设置：180ms 淡入 + 12dp 上移；Dialog 窗口动画关闭，内容自行动画。
- 按压缩放 0.98，120ms。
- 禁止无限循环装饰；流式 trace/思考点保留功能性循环，但限制在局部 100ms 节流。
- `prefers-reduced-motion` / 系统动画关闭时：时长 0ms，只保留透明度变化。

## 5. 组件库

### 5.1 Compose 组件

新增 `ui/theme/FenverComponents.kt`（或按现有文件就近放置），提供：

- `FenverSurface`：bg/surface/surface2 + 1px line + radius + padding。
- `FenverRow`：leading 20dp 图标、title/subtitle、trailing、minHeight 52/44、可选 divider。
- `FenverButton`：primary/secondary/ghost/danger；高度 44；primary 使用 accent；无渐变。
- `FenverTextField`：单行/多行；surface2 底、12 圆角、focus 1px accent；统一 10 处输入框写法。
- `FenverTopBar`：返回/标题/动作；56dp；标题 ellipsis；动作 44dp。
- `FenverSheet`：底部弹层容器；顶部 24 圆角；surface；一层轻阴影；无拖拽关闭。
- `FenverChip`：模型 chip、状态 chip；999 圆角、label 字号。
- `FenverCard`：消息/审批/任务/trace 的通用表面；1px line、16 圆角、无阴影。

迁移规则：

- 新增代码只用组件与 `MaterialTheme` token；不再写 `Color(0x...)`（`Theme.kt` 与图标例外）。
- 逐步清掉 183 处 `fontSize=` 与 87 处 `RoundedCornerShape`；确实需要局部尺寸时用 4dp 阶梯并加注释说明原因。
- 保留 `Icons.kt` 的自绘图标轮廓；统一 20dp 尺寸、1.5dp 线宽、`LocalContentColor` 着色，不再各屏写不同大小。

### 5.2 Web 组件与 token

- `static/style.css` 是唯一 token 权威：新增 `--fv-bg/--fv-surface/--fv-surface-2/--fv-line/--fv-text/--fv-text-2/--fv-text-3/--fv-accent/--fv-accent-soft/--fv-ai/--fv-danger/--fv-radius-*/--fv-space-*/--fv-dur-*/--fv-ease`。
- 官网 `site.css` 改为引用同值，不再手抄；命名统一为 `--fv-*`，保留旧变量作为过渡别名直到全站迁移完成（同一版内删除）。
- 新增 `.fv-surface/.fv-row/.fv-btn/.fv-input/.fv-topbar/.fv-sheet/.fv-chip/.fv-card`，PWA 与官网共用。
- 去掉 `backdrop-filter`、大面积 `box-shadow`、`background-attachment: fixed` 与无限 `@keyframes` 背景动画。
- 过渡统一 `var(--fv-dur-standard) var(--fv-ease)`；`prefers-reduced-motion` 下全部关闭。

## 6. 页面设计

### 6.1 App 外壳与聊天

- 外壳：纯 `bg`，去掉全屏双径向光晕；状态栏/导航栏透明，edge-to-edge；窗口底色继续由 `themes.xml` + `Theme.kt` 管，避免键盘闪白。
- 顶栏：56dp；左侧菜单 44dp；标题 17sp SemiBold，超长省略；右侧模型 chip（点击仍打开模型选择）+ 现有菜单动作。不新增动作。
- 消息区：
  - 助手消息：`surface` 底 + 1px line，16 圆角，无渐变；正文 16/24。
  - 用户消息：`surface2` 底 + 1px line，16 圆角，文字 `text`；不再用深青→靛紫渐变。
  - 时间/状态：`caption`，`text3`。
  - 流式 trace：`FenverCard`；默认折叠规则不变，流式中展开；步进图标沿用现有自绘三态；省略标记文案不变。
- 输入区：单张 `surface` 卡，12 圆角，1px line；附件、语音、输入、发送同一行；发送键 40dp 实心 accent 圆形，无光晕；附件面板用 `surface` 卡。
- 回底按钮：`surface` + 1px line，40dp；无阴影。
- 断网/错误条：`danger` 文本 + 轻 `danger` 底，不用大面积红。

### 6.2 会话抽屉

- 全高 `surface`；顶部品牌字 `Fenver`（单色 `text`，不再渐变）+ 当前账号行。
- 行高 48dp；活动行 `accentSoft` 底 + 左侧 2dp accent 竖条；行标题 15sp，副标题 caption。
- 保留：新建会话、会话列表、记忆、设置、切换/删除账号等全部入口与动作。
- 打开抽屉不再触发新请求；列表数据在 ChatScreen 启动时预取，打开时 stale-while-revalidate。

### 6.3 设置

- 一级页：`FenverTopBar` + 分组列表；分组标题 13sp `text3`；行 52dp；行间 1px line；不再用 nested card。
- 分组：账户 / 模型 / 对话 / 应用 / 关于。行文案与顺序保持契约要求（版本、检查更新、外观等行不得改名/移位到破坏锁的位置）。
- 二级页：统一顶栏 + 行式表单；模型/账户/角色/记忆/提醒/日程/反馈全部保留。
- 控件：开关使用 accent；选择器用 `FenverRow` + chevron；主按钮 44dp；表单错误 `danger`。
- 状态：模型/记忆/日程数据缓存上提到 ChatScreen 作用域；关闭设置不丢；再次打开先显示缓存再后台刷新；子页表单草稿在同一设置会话内保留。

### 6.4 登录/注册/找回

- 单列居中，最大宽 360dp；`surface` 面板 + 1px line；去掉拱形渐变与彩色光晕。
- 品牌字 `Fenver` 用单色 text，不再渐变；保留现有品牌位与无障碍名。
- 输入 48dp；主按钮 48dp 实心 accent；找回题/注册步骤/错误提示全部保留。
- 键盘弹起时窗口底色跟随主题，不出现白闪。

### 6.5 日程/语音/更新/审批/任务/trace

- 日程：去掉 `.sched-panel` 的底/模糊/阴影；用 `surface` + 1px line；日期/周切换与 28 条文案不变；不新增颜色字面量（遵守 `test_schedule_ui_contract.py`）。
- 语音：底部 `FenverSheet`；波形用 accent；去掉渐变 blob；录音/取消/发送逻辑不变。
- 更新：`FenverSheet`；版本行、更新说明、进度、安装按钮保留；进度条 accent。
- 审批卡/任务卡：`FenverCard`；标题/状态/操作不变；按钮 primary/secondary；不做大面积彩色底。
- trace：`FenverCard`；步骤行 13sp；成功/失败/进行中用现有图标 + accent/danger/text3；省略文案不变。

### 6.6 官网与 PWA

官网：

- 保留六节 `can-do/shell-only/start/shots/not-now/faq`、四张截图、FAQ、下载按钮与全部链接。
- 去掉 `html::before/body::before` 的两层无限渐变动画、`body::after` 全屏 grain、卡片的 `backdrop-filter` 与重阴影。
- 背景改为纯 `bg` + 一层极淡的静态色阶（不做动画）；卡片 `surface` + 1px line；标题用单色 text + accent 高亮一个词，不再整段渐变。
- 字号继续用 clamp，但基色/圆角/间距/缓动改为 `--fv-*`；CTA 用实心 accent。
- 保留首屏内联主题脚本与 CSP 哈希机制；不新增内联脚本。

PWA：

- 保留全部 id、设置行结构、登录、聊天、任务/审批/trace、更新卡与主题切换。
- 去掉背景氛围光、glass blur、无限动画；消息气泡改 `surface2`/`surface` + 1px line；输入区单卡；发送键实心 accent。
- 设置行 ≥44dp；保留 44dp 触控锁。
- 流式 Markdown 用 `requestAnimationFrame` 批处理，避免每帧整段重渲染。
- 主题切换沿用现有 `data-theme` + localStorage；不新增首屏内联脚本。

## 7. 图标

### 7.1 概念

保留「对话气泡 + 星芒/节点」识别，去掉渐变、内部细线和装饰光点，改成几何化、单色/双色图层：

- 背景：圆角方形，实心 `#121317`（深色）或 `#FFFFFF`（浅色自适应由系统蒙版决定；桌面图标用深色底）。
- 气泡：1.5–2px 等效描边，圆角矩形 + 左下角小尾巴；颜色 `#F2F3F5` 或透明镂空。
- 星芒：4 角几何星，accent mint `#3DD598`；节点用实心圆点，最多 2 个，保证 48px 下可辨。
- 不放文字、不放渐变、不放阴影。

### 7.2 资源

- 五桶 `mipmap-mdpi/hdpi/xhdpi/xxhdpi/xxxhdpi`：`ic_launcher.png`、`ic_launcher_round.png`、`ic_launcher_foreground.png`，尺寸保持现有契约（48/72/96/144/192；前景 108/162/216/324/432），RGBA。
- `mipmap-anydpi-v26/ic_launcher.xml` 与 `ic_launcher_round.xml`：保留 `@drawable/ic_launcher_background` + `@mipmap/ic_launcher_foreground`，新增 `<monochrome android:drawable="@mipmap/ic_launcher_foreground" />`。
- `drawable/ic_launcher_background.xml`：改为实心 `#121317`，删除旧渐变。
- `drawable-nodpi/app_icon.png`：512×512，App 内品牌位使用；替换现有 192。
- `backend/app/web/static/icon.png`：1024×1024，PWA manifest 保持。
- `tools/render-app-icon.ps1`：保留 `Get-SparkPath`/`Add-RoundedRect` 函数名与 ASCII-only；更新调色板与几何；一条命令重生成全部尺寸。
- 同步更新 `test_native_trace_polish_and_icon_v0300_contract.py:239-290`：新色值、尺寸、monochrome、脚本断言。
- 品牌字仍为 `Fenver`；`GradientText("Fenver")` 改为单色品牌文本时，同批更新 `test_brand_surface.py:58-73` 的锚点，但保留「两处品牌位」与无障碍名。

## 8. 性能设计

### 8.1 冷启动与首屏

- `MainActivity` 的 Keystore 初始化、reminders 读取、APK 清扫移到 `Dispatchers.IO`；不在 `onCreate` 阻塞首帧。
- `ChatScreen` 的 models/tasks/sessions 用 `coroutineScope { async }` 并行；先渲染缓存/骨架，不等待网络。
- `Prefs` 读取保持同步且轻量；首帧不发起新权限请求。
- 启动阶段禁止新增 `runBlocking`。

### 8.2 页面与弹层切换

- 设置子页用 `AnimatedContent`（180ms）包裹；页面切换保留滚动位置。
- 抽屉、设置、模型页、语音卡的条件挂载改为带统一 `AnimatedVisibility`；Dialog 窗口动画关闭。
- 设置数据缓存上提；打开不再重置/重拉；后台刷新失败保留旧数据并显示轻提示。
- 二级页表单草稿在同一设置会话内用 page-keyed holder 保留。

### 8.3 聊天列表与流式

- `LazyColumn` 使用稳定 key（消息 id）与 `contentType`；单条操作不重建整表。
- `RichText` 按消息 `remember(message.id, message.text)` 缓存解析结果；流式文本用 `derivedStateOf` + 50ms 合并，只重组当前消息行。
- trace 更新 100ms 批量；内部用不可变列表，不在每片 `toList()`。
- IME inset 只在输入区读取；`imePadding` 只在根部应用一次，去掉双 padding。
- 附件解码移到 `Dispatchers.IO`，按目标尺寸采样，缓存有上限（LRU）。
- 保持流式协议、停止/续跑、审批、任务卡行为不变。

### 8.4 官网/PWA

- 删除无限背景动画、grain、`background-attachment: fixed`、大面积 blur；阴影降到一层。
- 流式 Markdown 用 `requestAnimationFrame` 批处理；只在内容变化时重渲染消息体。
- 长页面使用 `content-visibility: auto` 的区块只在必要时启用，避免破坏锚点/搜索。
- PWA 首次渲染不再先画一层深色背景再换主题（本版不新增脚本；沿用现有行为）。

### 8.5 测量与判据

- 冷启动：同一台设备上用 `adb shell am start -W -n xyz.fenever.assistant.native/.MainActivity` 各跑 3 次冷启动，取中位数；v0.36.0 不高于 v0.35.5。
- 滚动/流式：`adb shell dumpsys gfxinfo xyz.fenever.assistant.native framestats` 记录 30s 会话列表滚动 + 一次 100+ token 流式回复；janky frame 比例不高于 v0.35.5，且无单帧 >32ms（若设备本身达不到，记录基线并如实标注）。
- 官网：Chrome mobile Lighthouse Performance 不低于改造前；流式期间无 >50ms long task（Performance 面板记录）。
- 如果设备/浏览器不可用：该项标「待确认」，只报代码级证据，不宣称数值改善。

## 9. 契约与测试迁移

### 9.1 必须保持原样

- 文案与行为：关于三句、日程 28 条、trace 标题/秒数、审批/任务/续跑、401 文案、shell 桥 10 方法名、通知门、provider authority。
- 页面 id/锚点、CSP、SW ASSETS、唯一 APK 出口、APK 版本/签名纪律。
- 品牌名 `Fenver` 的所有出现位与无障碍名。

### 9.2 同批更新

| 文件 | 原因 |
|---|---|
| `test_brand_surface.py:58-73` | 渐变字改单色品牌文本，锚点从 `GradientText` 改为新组件；两处品牌位仍必须存在 |
| `test_native_trace_polish_and_icon_v0300_contract.py:239-290` | 图标色值/几何/monochrome/脚本调色板 |
| `test_about_page_contract.py` | 关于页布局/行槽变化时同步锚点，三句逐字不变 |
| `test_v0342_single_version_row_contract.py` | 版本/检查更新/外观行的槽位变化时同步 |
| `test_schedule_ui_contract.py` | 只改样式锚点；28 条文案与 `Color(0x` 禁令不变 |
| `test_voice_naming_and_icons_v0234_contract.py` | 图标尺寸/线宽变化时同步，仍禁 emoji |
| `test_web_pwa.py` | 设置行 44dp、id、层栈、reduced-motion、auth-band/panel 双 token 等 |
| `test_website.py` | 六节 id、四图、外链、themeBtn、site.js 约束 |
| `test_changelog_page.py` | 唯一 APK 出口、无 github 可点链、头条 |
| `test_asset_versioning.py` / `test_v0350_csp_hash.py` | 新增/修改静态资源与内联脚本哈希 |
| 五处版本契约测试 | versionCode 67 / versionName 0.36.0 |

### 9.3 新增锁

- `test_design_tokens_parity.py`：从 `Theme.kt` 与 `static/style.css` 读取共享 token（bg/surface/surface2/line/text/text2/text3/accent/accentSoft/ai/danger），逐值断言两端相等；命名映射写死在测试里，缺失即红。
- 图标 monochrome 锁：两份 adaptive XML 必须含 `<monochrome>`，且前景/背景引用与现有形状约束成立。
- 性能源码锁（按现有项目风格，明确射程）：
  - `MainActivity` 不含 `runBlocking`。
  - `ChatUi` 的 `LazyColumn` 使用 `key =`。
  - `ChatScreen` 作用域不直接读 `WindowInsets.ime.getBottom`。
  - 流式文本解析不在 `LazyColumn` item 的组合体内直接对全文重切（允许 helper 内 `remember`）。
  - 这些锁只证明形状，不证明帧率；帧率由 8.5 的真机测量负责。

## 10. 交付与发版

### 10.1 分支与提交

分支：`feat/v0.36.0-minimal-ui`。提交顺序：

1. design tokens + parity 锁 + 主题/窗口底色。
2. Compose 组件库 + 聊天/抽屉。
3. 设置/登录/日程/语音/更新/审批/任务/trace。
4. 官网 + PWA 样式与 token。
5. 图标资源 + 生成脚本 + 图标锁。
6. 性能改造（启动/切换/流式/图片/IME）。
7. 版本契约、发版说明、changelog、seed、marker。

### 10.2 预览门

- 先在本地构建 v0.36.0 APK；用 staging 后端（端口 8010）预览官网，不 swap 生产。
- 交付：逐屏截图（深/浅各一套）+ 官网截图 + 本地 APK；用户确认观感后再发版。
- 用户确认前不打 tag、不推 main、不换生产包。

### 10.3 正式发版

按既有链路：CI 全绿 → 本地全量 → 本地 tag → `pre_tag_gate --pre-build` → `version.txt` → PyInstaller staging → 产物验收 → DryRun → 正式 swap → 线上验收 → 终闸 → 推 tag → Release。APK 先落盘验摘要，再改两份清单。

## 11. 验收标准

### 11.1 视觉

- 逐屏截图：聊天、抽屉、设置一级、设置二级、登录、日程、语音、更新、审批/任务、trace、官网首页、changelog、PWA 聊天/设置。
- 360dp 宽不出现文本截断/重叠；横屏/大屏不拉伸变形。
- 深/浅两套均通过 AA 对比度；触控目标 ≥44dp。
- 禁用动画时无非必要运动。

### 11.2 功能

- 本地全量测试 0 failed / 0 errors。
- 原生 JVM 测试、官网测试、CSP/SW/资产版本测试全绿。
- 手工回归：登录/注册/找回、发消息/流式/停止/续跑、附件/语音、会话切换/删除、设置各子页、日程、提醒、更新检查、审批、任务卡。

### 11.3 性能

- 8.5 的启动/滚动/流式/官网指标；设备不可用则标待确认。
- 代码级锁：启动无 `runBlocking`、列表有 key、流式不全文重切、IME 读取收口。

## 12. 风险与回滚

| 风险 | 缓解 |
|---|---|
| 一次性 diff 过大、审查困难 | 分支内按 7 个提交切分；每个提交只做一类事；预览门先给用户看 |
| 契约锁大量变红 | 第 9 节逐文件同批更新；改锁前先确认文案/行为未变 |
| 官网 CSP/SW 失效 | 不新增内联脚本；资源改动同批跑 `test_v0350_csp_hash.py` / `test_asset_versioning.py` |
| 图标测试/桌面缓存 | 五桶重生成 + monochrome + PWA 1024；真机安装确认 |
| 性能指标不升反降 | 8.5 基线对比；不达标先回退该项，不带病发版 |
| 视觉观感不接受 | 预览 APK/截图门；未确认不发版 |

回滚：保留 v0.35.5 APK、`dist/run_backend_old-*`、运行时清单旧值；tag `v0.36.0` 未推前可本地删除；已发 Release 的回滚按既有退版流程，不自动 un-rename。

## 13. 明确不做

- 不新增/删除功能、权限、接口、数据字段。
- 不改 shell 桥方法名单与 caps 键。
- 不引入网络字体/大图/新依赖（除现有 Compose/AndroidX）。
- 不改官网信息架构、六节 id、唯一 APK 出口。
- 不做 PWA 首屏主题脚本（本版）。
- 不重写业务状态机，只做状态缓存与渲染路径优化。
