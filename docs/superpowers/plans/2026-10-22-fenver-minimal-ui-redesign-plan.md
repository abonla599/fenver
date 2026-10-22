# Fenver v0.36.0 极简原生 UI 重设计 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把安卓原生 App 与官网/PWA 统一为中性表面、单强调色、发丝线和 180ms 动效的极简原生 Material 3 界面，重做「气泡 + 星芒」图标，并在功能不变的前提下解决启动、切换和流式聊天的卡顿。

**Architecture:** 先建立一套两端同源的 design token 与 Compose/Web 组件原语，再按「聊天 → 抽屉/设置 → 登录/日程/语音/更新/状态卡 → 官网/PWA → 图标 → 性能」迁移。所有页面保留现有入口、文案语义、接口与行为；契约测试同批更新。单版本 v0.36.0，预览 APK/官网确认后才发版。

**Tech Stack:** Kotlin + Jetpack Compose (Material 3)、Python/FastAPI 静态资源与 PyInstaller、原生 HTML/CSS/JS PWA、Gradle 8.7、pytest。

**Spec:** `docs/superpowers/specs/2026-10-22-fenver-minimal-ui-redesign-design.md`

## Global Constraints

- 版本：`versionCode 67` / `versionName "0.36.0"`；版本号唯一真相是 `android-native/app/build.gradle`。
- 功能不增不减：不改 API、请求/响应形状、鉴权、任务/审批/续跑、提醒、更新通道、shell 桥 10 个方法名。
- 所有现有契约文案保留逐字：关于三句、日程 28 条、trace 标题/秒数、审批/任务/续跑、401 文案。
- 官网六节 id、CSP、SW ASSETS、唯一 APK 出口 `/site/android.apk` 保持。
- 不新增网络字体、不新增依赖、不新增权限；颜色/字号/圆角/动效只用 token。
- 所有测试用 `C:\Users\34426\ai-assistant\venv\Scripts\python.exe`；不得用全局 python。
- 生产机目录 `C:\Users\34426\ai-assistant` 同时是线上服务；实现放在独立 worktree `C:\Users\34426\ai-assistant\.qoder\worktrees\agent-ui-v0360`，同一时刻只有一个写者。
- 未拿到用户对预览 APK/官网截图的确认前：不打 tag、不换生产包、不推 main。
- 每次提交只做一类事；改完一个任务先跑该任务的聚焦测试，再进下一个。

---

## File Structure

**Native**
- `android-native/app/src/main/java/.../theme/Theme.kt` — token、色板、Typography、Shapes、窗口底色、品牌组件。
- `android-native/app/src/main/java/.../ui/FenverComponents.kt` — 新组件原语（Surface/Row/Button/TextField/TopBar/Sheet/Chip/Card）。
- `android-native/app/src/main/java/.../ui/ChatUi.kt` — 聊天壳、消息、输入区、流式、设置入口。
- `android-native/app/src/main/java/.../ui/SessionListUi.kt` — 会话抽屉。
- `android-native/app/src/main/java/.../ui/SettingsUi.kt` — 设置一级/二级页。
- `android-native/app/src/main/java/.../ui/AuthUi.kt` — 登录/注册/找回。
- `android-native/app/src/main/java/.../ui/ScheduleUi.kt`、`VoiceUi.kt`、`UpdateUi.kt`、`Icons.kt`。
- `android-native/app/src/main/res/values/{colors,themes}.xml`、`mipmap-*`、`mipmap-anydpi-v26/*.xml`、`drawable-nodpi/app_icon.png`、`drawable/ic_launcher_background.xml`。
- `tools/render-app-icon.ps1`。

**Web**
- `backend/app/web/static/style.css` — 唯一 token 权威与 PWA 样式。
- `backend/app/web/static/index.html`、`app.js`、`manifest.webmanifest`、`sw.js`、`icon.png`。
- `backend/app/web/site/site.css`、`index.html`、`changelog.html`、`site.js`、`changelog.json`。

**Tests**
- 新增 `backend/tests/test_native_design_tokens_v0360_contract.py`。
- 新增 `backend/tests/test_design_tokens_parity_v0360.py`。
- 新增 `backend/tests/test_native_ui_performance_shape_v0360.py`。
- 修改品牌/图标/关于/版本行/日程/网站/PWA/CSP/资产版本/五处版本契约测试（见各任务）。

---

## Task 1: Design tokens、主题与窗口底色

**Files:**
- Modify: `android-native/app/src/main/java/xyz/fenever/assistant/nativeapp/theme/Theme.kt`
- Modify: `android-native/app/src/main/java/.../ui/{AuthUi,ChatUi,ScheduleUi,SessionListUi}.kt`（仅 `WebTokens` 引用改名）
- Modify: `android-native/app/src/main/res/values/colors.xml`、`values/themes.xml`
- Test: `backend/tests/test_native_design_tokens_v0360_contract.py`（新建）
- Test: `backend/tests/test_bottom_clamp_jump_arrow_and_ime_flash_v0280_contract.py`

**Interfaces:**
- Produces: `object FenverTokens`，字段 `Bg/BgSoft/Surface/Surface2/Surface3/Line/Text/Text2/Text3/Accent/AccentPressed/AccentSoft/Ai/Danger/CodeBg` 及浅色 `LBg/LSurface/LSurface2/LLine/LText/LText2/LText3/LAccent/LAccentPressed/LAccentSoft/LAi/LDanger/CodeBg`。
- Produces: `@Composable fun isWebLight(): Boolean`（保留原名，避免大面积改调用）。
- Produces: `@Composable fun fenverAccent(): Color`、`@Composable fun fenverSurface(): Color` 等便捷读取。
- 保留：`ThemeMode`、`AiTheme`、`GradientText` 的调用点（本任务先把其内部改为单色 accent，Task 2 再替换为 `BrandText`）。

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_native_design_tokens_v0360_contract.py
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
THEME = REPO / "android-native/app/src/main/java/xyz/fenever/assistant/nativeapp/theme/Theme.kt"
COLORS = REPO / "android-native/app/src/main/res/values/colors.xml"
THEMES = REPO / "android-native/app/src/main/res/values/themes.xml"


def _text(p):
    return p.read_text(encoding="utf-8").replace("\r\n", "\n")


def test_minimal_palette_is_declared_once():
    src = _text(THEME)
    assert "object FenverTokens" in src
    assert "WebTokens" not in src
    for needle in (
        "val Bg = Color(0xFF0A0B0E)",
        "val Surface = Color(0xFF121317)",
        "val Surface2 = Color(0xFF181A20)",
        "val Line = Color(0xFF262A31)",
        "val Text = Color(0xFFF2F3F5)",
        "val Accent = Color(0xFF3DD598)",
        "val Ai = Color(0xFF7C83FF)",
        "val LBg = Color(0xFFF7F8FA)",
        "val LAccent = Color(0xFF0A7D5E)",
    ):
        assert needle in src, "缺少 token：" + needle


def test_no_ui_file_still_says_webtokens():
    ui = REPO / "android-native/app/src/main/java/xyz/fenever/assistant/nativeapp/ui"
    for path in ui.glob("*.kt"):
        assert "WebTokens" not in _text(path), f"{path.name} 还在用旧 token 名"


def test_window_background_matches_token():
    assert "#0A0B0E" in _text(COLORS).upper()
    assert "@color/window_bg" in _text(THEMES)
```

- [ ] **Step 2: 跑测试确认红**

Run:
`..\venv\Scripts\python.exe -m pytest backend/tests/test_native_design_tokens_v0360_contract.py -q`
Expected: FAIL（`object FenverTokens` 不存在 / 仍有 WebTokens）。

- [ ] **Step 3: 重写 Theme.kt 的 token 与 scheme**

把 `WebTokens` 改名为 `FenverTokens`，按 spec 4.1 填值；`DarkScheme` 用 `primary=Accent, background=Bg, surface=Surface, surfaceVariant=Surface2, onSurface=Text, onSurfaceVariant=Text2, outline=Line, error=Danger`；`LightScheme` 同理。`WebTypography` 改为 `FenverTypography`：`headlineSmall 28/36 SemiBold`、`titleLarge 22/30 SemiBold`、`titleMedium 17/24 SemiBold`、`bodyLarge 16/24`、`bodyMedium 14/20`、`bodySmall 12/16`、`labelLarge 13/18 Medium`、`labelMedium 13/18 Medium`、`labelSmall 12/16`。`WebShapes` 改为 `FenverShapes`：4/8/12/16/24dp。删除 `aiPrimaryBrush/aiSoftBrush/aiVerticalBrush/userBubbleBrush` 的渐变实现，改成 `Brush.linearGradient(listOf(accent, accent))` 的兼容占位（Task 2/3 再删调用点），或直接改为 `SolidColor(accent)`。`GradientText` 内部改用 `SolidColor(accent)`，保留函数名到 Task 2。

- [ ] **Step 4: 更新引用与窗口底色**

```bash
rg -l "WebTokens" android-native/app/src/main/java | xargs -r sed -i 's/WebTokens/FenverTokens/g'
```

`colors.xml`：`window_bg` 改 `#0A0B0E`。`themes.xml` 保持 `@color/window_bg`。`test_bottom_clamp...:126` 的 `WebTokens.Bg` 改 `FenverTokens.Bg`。

- [ ] **Step 5: 跑聚焦测试**

Run:
`..\venv\Scripts\python.exe -m pytest backend/tests/test_native_design_tokens_v0360_contract.py backend/tests/test_bottom_clamp_jump_arrow_and_ime_flash_v0280_contract.py -q`
Expected: PASS。

- [ ] **Step 6: 提交**

```bash
git add android-native/app/src/main/java/xyz/fenever/assistant/nativeapp/theme/Theme.kt \
  android-native/app/src/main/java/xyz/fenever/assistant/nativeapp/ui \
  android-native/app/src/main/res/values/colors.xml \
  backend/tests/test_native_design_tokens_v0360_contract.py \
  backend/tests/test_bottom_clamp_jump_arrow_and_ime_flash_v0280_contract.py
git commit -m "feat(ui): 建立极简原生 design tokens 与窗口底色"
```

---

## Task 2: Compose 组件原语 + 聊天主界面

**Files:**
- Create: `android-native/app/src/main/java/xyz/fenever/assistant/nativeapp/ui/FenverComponents.kt`
- Modify: `android-native/app/src/main/java/.../ui/ChatUi.kt`
- Modify: `android-native/app/src/main/java/.../ui/Icons.kt`（尺寸/线宽）
- Test: `backend/tests/test_native_components_v0360_contract.py`（新建）

**Interfaces:**
- Produces: `FenverSurface(modifier, color, border, radius, content)`
- Produces: `FenverRow(leading, title, subtitle, trailing, onClick, divider, minHeight)`
- Produces: `FenverButton(text, onClick, kind, enabled, modifier)`
- Produces: `FenverTextField(value, onValueChange, placeholder, singleLine, modifier)`
- Produces: `FenverTopBar(title, onBack, actions)`
- Produces: `FenverSheet(onDismiss, content)`
- Produces: `FenverChip(text, selected, onClick)`
- Produces: `FenverCard(modifier, content)`
- Produces: `BrandText(text, size)` — 单色品牌字，替代 `GradientText("Fenver")`；调用点保留在两处品牌位。
- Produces: `SettingsCache(providers: List<ModelInfo>, serverDefault: String?, modelsLoaded: Boolean, memoryCount: String, scheduleVal: String)` — 上提到 ChatScreen 的设置数据缓存。

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_native_components_v0360_contract.py
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
UI = REPO / "android-native/app/src/main/java/xyz/fenever/assistant/nativeapp/ui"


def _t(name):
    return (UI / name).read_text(encoding="utf-8")


def test_component_primitives_exist():
    src = _t("FenverComponents.kt")
    for name in ("FenverSurface", "FenverRow", "FenverButton", "FenverTextField",
                 "FenverTopBar", "FenverSheet", "FenverChip", "FenverCard", "BrandText"):
        assert "fun " + name in src, name


def test_chat_uses_flat_surfaces_and_stable_keys():
    src = _t("ChatUi.kt")
    assert "itemsIndexed(messages, key =" in src
    assert "userBubbleBrush(" not in src, "用户气泡还在用渐变"
    assert "FenverSurface(" in src
```

- [ ] **Step 2: 跑测试确认红**

Run:
`..\venv\Scripts\python.exe -m pytest backend/tests/test_native_components_v0360_contract.py -q`
Expected: FAIL。

- [ ] **Step 3: 新增 FenverComponents.kt**

按 spec 5.1 实现 8 个组件。关键约束：padding 用 4/8/12/16/24；`FenverRow` 最小高度 52dp、触控目标 44dp；`FenverButton` 高度 44dp，primary 用 `MaterialTheme.colorScheme.primary`；`FenverTextField` 用 `surface2`、12dp 圆角、focus 1dp accent；`FenverSheet` 顶部 24dp 圆角 + 一层 `shadow(24.dp)`；`FenverCard` 1px `outline` + 16dp 圆角。不要写 `Color(0x...)`，全部从 `MaterialTheme` 取。

- [ ] **Step 4: 迁移聊天主界面**

`ChatUi.kt`：
- 顶栏改 `FenverTopBar`；菜单 44dp；标题 `titleMedium`；模型 chip 用 `FenverChip`，点击仍打开原模型菜单。
- 消息行：助手 `FenverCard` + `surface`；用户 `FenverCard` + `surface2`；删除 `userBubbleBrush()` 调用与彩色阴影。
- 输入区：`FenverSurface` 一张卡；发送键 `Box(Modifier.size(40.dp).background(primary, CircleShape))`；去掉 `shadow` 光晕。
- 回底钮/附件面板：`FenverSurface`。
- `itemsIndexed(messages, key = { _, m -> m.messageId ?: "${m.role}-${m.content.hashCode()}" })`；若本地消息 key 不稳定，在 `UiMsg` 增加 `localId: Long = nextLocalId()` 并用它。
- 保留所有 `LaunchedEffect`/网络/流式逻辑；本任务只改渲染与稳定 key。

- [ ] **Step 5: Icons.kt 统一**

把 17 个自绘图标的默认尺寸统一为 20dp、线宽 1.5dp；不改变路径形状与语义。确保 `IconStepDone/Failed/Busy/ChevronDown` 名称不变（trace 契约锁）。

- [ ] **Step 6: 跑测试与编译**

Run:
`..\venv\Scripts\python.exe -m pytest backend/tests/test_native_components_v0360_contract.py backend/tests/test_native_trace_polish_and_icon_v0300_contract.py -q`
Expected: PASS。
Run:
`C:\Users\34426\gradle\gradle-8.7\bin\gradle.bat -p android-native --no-daemon compileReleaseKotlin`
Expected: BUILD SUCCESSFUL。

- [ ] **Step 7: 提交**

```bash
git add android-native/app/src/main/java/xyz/fenever/assistant/nativeapp/ui/FenverComponents.kt \
  android-native/app/src/main/java/xyz/fenever/assistant/nativeapp/ui/ChatUi.kt \
  android-native/app/src/main/java/xyz/fenever/assistant/nativeapp/ui/Icons.kt \
  backend/tests/test_native_components_v0360_contract.py
git commit -m "feat(ui): 组件原语与聊天主界面极简化"
```

---

## Task 3: 会话抽屉 + 设置一级/二级页

**Files:**
- Modify: `android-native/app/src/main/java/.../ui/SessionListUi.kt`
- Modify: `android-native/app/src/main/java/.../ui/SettingsUi.kt`
- Modify: `android-native/app/src/main/java/.../ui/ChatUi.kt`（设置缓存上提、Dialog 动画）
- Test: `backend/tests/test_brand_surface.py:58-73`、`test_about_page_contract.py`、`test_v0342_single_version_row_contract.py`、`test_schedule_ui_contract.py`（仅锚点）

- [ ] **Step 1: 先改契约测试锚点（红）**

把品牌测试里的 `GradientText("Fenver")` 断言改为新 `BrandText("Fenver")`（或 `FenverText("Fenver")`），并保留「两处品牌位」；关于/版本行测试只改布局锚点，文案不动。运行：
`..\venv\Scripts\python.exe -m pytest backend/tests/test_brand_surface.py backend/tests/test_about_page_contract.py backend/tests/test_v0342_single_version_row_contract.py -q`
Expected: FAIL（新组件未实现）。

- [ ] **Step 2: 抽屉改造**

`SessionListUi.kt`：`FenverSurface` 全高；品牌字改 `BrandText`（单色 text）；行高 48dp；活动行 `surface3` + 左侧 2dp accent 竖条；所有入口与回调不变；打开抽屉不再触发新请求（请求仍在 ChatScreen 启动时预取）。

- [ ] **Step 3: 设置一级页**

`SettingsUi.kt`：`FenverTopBar` + 分组列表；分组「账户/模型/对话/应用/关于」；`FenverRow` 52dp；去掉 `SetCard` 的嵌套卡片背景，保留 `SetGroup/SetRow` 的公开签名或改为 `FenverRow`；版本/检查更新/外观看行的文案与顺序保持契约要求；开关用 accent。

- [ ] **Step 4: 设置二级页与页切换**

所有子页统一 `FenverTopBar` + `FenverRow`/`FenverTextField`；用 `AnimatedContent(targetState = page)` 180ms 包裹；`SettingsSheet` 的模型/记忆/日程缓存上提到 ChatScreen（`SettingsCache` data class，字段 `providers/serverDefault/modelsLoaded/memoryCount/scheduleVal`），打开时先读缓存再后台刷新；子页表单草稿用 page-keyed holder 保留。Dialog 窗口动画通过 `DialogWindowProvider.window.setWindowAnimations(0)` 关闭（放在 `ChatUi` 设置 Dialog 内容顶部）。

- [ ] **Step 5: 跑测试**

Run:
`..\venv\Scripts\python.exe -m pytest backend/tests/test_brand_surface.py backend/tests/test_about_page_contract.py backend/tests/test_v0342_single_version_row_contract.py backend/tests/test_schedule_ui_contract.py backend/tests/test_native_settings_instant_models_contract.py -q`
Expected: PASS。

- [ ] **Step 6: 提交**

```bash
git add android-native/app/src/main/java/xyz/fenever/assistant/nativeapp/ui/SessionListUi.kt \
  android-native/app/src/main/java/xyz/fenever/assistant/nativeapp/ui/SettingsUi.kt \
  android-native/app/src/main/java/xyz/fenever/assistant/nativeapp/ui/ChatUi.kt \
  backend/tests/test_brand_surface.py backend/tests/test_about_page_contract.py \
  backend/tests/test_v0342_single_version_row_contract.py
git commit -m "feat(ui): 抽屉与设置页极简化并保留状态"
```

---

## Task 4: 登录、日程、语音、更新、审批/任务/trace

**Files:**
- Modify: `AuthUi.kt`、`ScheduleUi.kt`、`VoiceUi.kt`、`UpdateUi.kt`、`ChatUi.kt`（审批/任务/trace 卡片）
- Test: `test_voice_naming_and_icons_v0234_contract.py`、`test_schedule_ui_contract.py`、`test_web_trace_panel_parity_v0300_contract.py`、`test_v031_approval_ui_contract.py`

- [ ] **Step 1: 架构/视觉迁移**

登录去掉拱形与光晕，改 `FenverSurface` 居中面板；品牌字 `BrandText`；输入/按钮 48dp；找回/注册流程不动。日程去掉面板底/模糊/阴影，全部用 `MaterialTheme` token（禁止 `Color(0x`）；28 条文案与保存/草稿/返回确认逻辑不动。语音改 `FenverSheet` + accent 波形；更新改 `FenverSheet`；审批/任务/trace 改 `FenverCard`，按钮 primary/secondary，文案/动作不变。

- [ ] **Step 2: 跑测试**

Run:
`..\venv\Scripts\python.exe -m pytest backend/tests/test_voice_naming_and_icons_v0234_contract.py backend/tests/test_schedule_ui_contract.py backend/tests/test_web_trace_panel_parity_v0300_contract.py backend/tests/test_v031_approval_ui_contract.py -q`
Expected: PASS。

- [ ] **Step 3: 编译并提交**

```bash
C:\Users\34426\gradle\gradle-8.7\bin\gradle.bat -p android-native --no-daemon compileReleaseKotlin
git add android-native/app/src/main/java/xyz/fenever/assistant/nativeapp/ui
git commit -m "feat(ui): 登录/日程/语音/更新/状态卡统一极简表面"
```

---

## Task 5: 官网 + PWA 样式与 token

**Files:**
- Modify: `backend/app/web/static/style.css`、`index.html`、`app.js`、`manifest.webmanifest`
- Modify: `backend/app/web/site/site.css`、`index.html`、`changelog.html`、`site.js`
- Test: `backend/tests/test_design_tokens_parity_v0360.py`（新建）
- Test: `test_website.py`、`test_web_pwa.py`、`test_changelog_page.py`、`test_asset_versioning.py`、`test_v0350_csp_hash.py`

**Interfaces:**
- Produces: CSS 变量 `--fv-bg/--fv-surface/--fv-surface-2/--fv-line/--fv-text/--fv-text-2/--fv-text-3/--fv-accent/--fv-accent-soft/--fv-ai/--fv-danger/--fv-code-bg/--fv-radius-*/--fv-space-*/--fv-dur-*/--fv-ease`。
- Produces: `.fv-surface/.fv-row/.fv-btn/.fv-input/.fv-topbar/.fv-sheet/.fv-chip/.fv-card`。

- [ ] **Step 1: 写 parity 测试（红）**

```python
# backend/tests/test_design_tokens_parity_v0360.py
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
THEME = REPO / "android-native/app/src/main/java/xyz/fenever/assistant/nativeapp/theme/Theme.kt"
CSS = REPO / "backend/app/web/static/style.css"

PAIRS = {
    "Bg": ("--fv-bg", "#0a0b0e"),
    "Surface": ("--fv-surface", "#121317"),
    "Surface2": ("--fv-surface-2", "#181a20"),
    "Line": ("--fv-line", "#262a31"),
    "Text": ("--fv-text", "#f2f3f5"),
    "Text2": ("--fv-text-2", "#a1a6b0"),
    "Accent": ("--fv-accent", "#3dd598"),
    "Ai": ("--fv-ai", "#7c83ff"),
    "Danger": ("--fv-danger", "#f87171"),
}


def _hex(value):
    return "#" + value[4:].lower() if value.startswith("0x") else value.lower()


def test_native_and_web_share_the_same_tokens():
    theme = THEME.read_text(encoding="utf-8")
    css = CSS.read_text(encoding="utf-8")
    for name, (var, expected) in PAIRS.items():
        m = re.search(r"val %s = Color\(0x([0-9A-Fa-f]{8})\)" % name, theme)
        assert m, name
        native = "#" + m.group(1)[2:].lower()
        assert native == expected, (name, native, expected)
        assert re.search(re.escape(var) + r"\s*:\s*" + re.escape(expected), css, re.I), var
```

- [ ] **Step 2: 改 static/style.css**

在 `:root` 写 `--fv-*` 全量 token（深色），`[data-theme="light"]` 写浅色；保留旧变量作为过渡别名（`--bg: var(--fv-bg)` 等），本任务结束前删除旧变量。删除 `body` 的两层 radial 背景与 `background-attachment: fixed`；删除 `backdrop-filter`；阴影降到 `--fv-shadow` 一层。新增 `.fv-*` 类，按 spec 5.2 映射。

- [ ] **Step 3: 改 PWA 与官网结构样式**

`static/index.html`/`app.js`：消息气泡改 `surface`/`surface2` + 1px line；输入区单卡；发送键实心 accent；设置行 44dp；保留所有 id/行为/主题切换。`site/site.css` 改为 `--fv-*` 引用；删除 `html::before/body::before` 无限动画与 `body::after` grain；卡片去 blur/重阴影；六节结构、四图、链接、themeBtn、CSP 内联脚本保持。`site.js` 只改样式相关逻辑，不改下载/主题/导航行为。

- [ ] **Step 4: 跑测试**

Run:
`..\venv\Scripts\python.exe -m pytest backend/tests/test_design_tokens_parity_v0360.py backend/tests/test_website.py backend/tests/test_web_pwa.py backend/tests/test_changelog_page.py backend/tests/test_asset_versioning.py backend/tests/test_v0350_csp_hash.py -q`
Expected: PASS（若测到具体锚点，按 spec 同批更新，不改行为）。

- [ ] **Step 5: 提交**

```bash
git add backend/app/web/static/style.css backend/app/web/static/index.html \
  backend/app/web/static/app.js backend/app/web/site/site.css \
  backend/app/web/site/index.html backend/app/web/site/changelog.html \
  backend/app/web/site/site.js backend/tests/test_design_tokens_parity_v0360.py \
  backend/tests/test_website.py backend/tests/test_web_pwa.py \
  backend/tests/test_changelog_page.py backend/tests/test_asset_versioning.py \
  backend/tests/test_v0350_csp_hash.py
git commit -m "feat(web): 官网与 PWA 切换极简原生 token"
```

---

## Task 6: 图标

**Files:**
- Modify: `tools/render-app-icon.ps1`
- Modify: `android-native/app/src/main/res/drawable/ic_launcher_background.xml`
- Modify: `android-native/app/src/main/res/mipmap-anydpi-v26/ic_launcher.xml`、`ic_launcher_round.xml`
- Regenerate: 五桶 `mipmap-*/*.png`、`drawable-nodpi/app_icon.png`、`backend/app/web/static/icon.png`
- Test: `backend/tests/test_native_trace_polish_and_icon_v0300_contract.py:239-290`

- [ ] **Step 1: 改图标契约测试**

把 `test_adaptive_icon_background_is_the_gradient_not_a_stale_color` 改为 `..._is_the_solid_surface`：断言背景 XML 含 `#121317`、不含 `#0E7C86/#7030E0`；两份 anydpi XML 必须含 `<monochrome`；脚本断言改为包含 `#121317`、`#3DD598`，仍保留 `Get-SparkPath`/`Add-RoundedRect`。五桶尺寸/RGBA/PWA 1024/app_icon 存在断言保留，`app_icon` 允许 512。

- [ ] **Step 2: 改生成脚本**

`tools/render-app-icon.ps1`：ASCII-only；保留函数名；背景 `#121317`；气泡/星芒几何按 spec 7.1；输出五桶 + 1024 PWA + 512 app_icon。运行：
`powershell -NoProfile -ExecutionPolicy Bypass -File tools\render-app-icon.ps1`
Expected: 生成全部 PNG，无错误。

- [ ] **Step 3: 改 adaptive XML**

```xml
<adaptive-icon xmlns:android="http://schemas.android.com/apk/res/android">
    <background android:drawable="@drawable/ic_launcher_background" />
    <foreground android:drawable="@mipmap/ic_launcher_foreground" />
    <monochrome android:drawable="@mipmap/ic_launcher_foreground" />
</adaptive-icon>
```

两份 XML 同步；`ic_launcher_background.xml` 改为实心 `#121317`。

- [ ] **Step 4: 跑测试 + 查看产物**

Run:
`..\venv\Scripts\python.exe -m pytest backend/tests/test_native_trace_polish_and_icon_v0300_contract.py -q`
Expected: PASS。用图片查看器打开 48/192/432 三个尺寸，确认气泡+星芒在小尺寸仍可辨。

- [ ] **Step 5: 提交**

```bash
git add tools/render-app-icon.ps1 android-native/app/src/main/res \
  backend/app/web/static/icon.png backend/tests/test_native_trace_polish_and_icon_v0300_contract.py
git commit -m "feat(icon): 气泡星芒几何简化并补 monochrome"
```

---

## Task 7: 性能改造与源码锁

**Files:**
- Modify: `MainActivity.kt`、`ChatUi.kt`、`SessionListUi.kt`、`SettingsUi.kt`、`SecretStore.kt`（如需）、`backend/app/web/static/app.js`
- Test: `backend/tests/test_native_ui_performance_shape_v0360.py`（新建）

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_native_ui_performance_shape_v0360.py
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
NATIVE = REPO / "android-native/app/src/main/java/xyz/fenever/assistant/nativeapp"


def _t(rel):
    return (NATIVE / rel).read_text(encoding="utf-8")


def test_startup_has_no_runblocking_and_chat_has_keys():
    assert "runBlocking" not in _t("MainActivity.kt")
    assert "itemsIndexed(messages, key =" in _t("ui/ChatUi.kt")


def test_ime_inset_is_not_read_by_the_whole_chat_screen():
    src = _t("ui/ChatUi.kt")
    assert "WindowInsets.ime.getBottom" not in src, "IME inset 仍在 ChatScreen 作用域逐帧读"
```

- [ ] **Step 2: 启动与切换**

`MainActivity`：Keystore 初始化、reminders 读取、`sweepStaleApks` 移到 `lifecycleScope.launch(Dispatchers.IO)`；`SecretStore` 改成首次真正需要密钥时才初始化（lazy + 调用方在 IO）；首帧立即 `setContent`。`ChatScreen`：models/tasks/sessions 用 `coroutineScope { async }` 并行；设置缓存上提后的打开路径不再重拉。设置 Dialog 关闭窗口动画。

- [ ] **Step 3: 流式与列表**

`ChatUi`：`LazyColumn` 稳定 key；`RichText` 解析用 `remember(message.messageId, message.content)`/`derivedStateOf`；流式每 50ms 合并；trace 100ms 批量；`imePadding` 只根部一次；附件解码 `Dispatchers.IO` + 目标尺寸采样 + LRU。`app.js`：流式 Markdown 用 `requestAnimationFrame` 批处理，去掉每帧整段 `MD.render`。

- [ ] **Step 4: 跑测试、编译、全量**

Run:
`..\venv\Scripts\python.exe -m pytest backend/tests/test_native_ui_performance_shape_v0360.py -q`
Expected: PASS。
Run:
`C:\Users\34426\gradle\gradle-8.7\bin\gradle.bat -p android-native --no-daemon test assembleRelease`
Expected: BUILD SUCCESSFUL。
Run:
`..\venv\Scripts\python.exe -m pytest backend/tests -q`
Expected: 0 failed / 0 errors。

- [ ] **Step 5: 提交**

```bash
git add android-native/app/src/main/java/xyz/fenever/assistant/nativeapp \
  backend/app/web/static/app.js backend/tests/test_native_ui_performance_shape_v0360.py
git commit -m "perf(ui): 启动并行、切换保状态、流式按帧合并"
```

---

## Task 8: 版本物料、预览与发版

**Files:**
- Modify: `android-native/app/build.gradle`（versionCode 67 / versionName 0.36.0）
- Modify: 五处版本契约测试
- Create: `docs/releases/v0.36.0.md`
- Modify: `backend/app/web/static/app.js`（marker `release-marker-v0.36.0`）
- Regenerate: `backend/app/web/site/changelog.json`
- Modify: `backend/app/web/site/version.json`（APK 落盘后）

- [ ] **Step 1: 版本与发版说明**

`build.gradle` 改 67/0.36.0；五处测试的 66/0.35.5 改 67/0.36.0（注释链加 `v0.36.0 推到 67`）；`docs/releases/v0.36.0.md` 仅 `## 修复了什么` / `## 优化了什么`，不写过程；app.js 加一行 `release-marker-v0.36.0`。

- [ ] **Step 2: 生成 changelog、跑测试**

Run:
`..\venv\Scripts\python.exe tools\gen_changelog.py`
Run:
`..\venv\Scripts\python.exe -m pytest backend/tests/test_release_notes.py backend/tests/test_release_marker_derived_from_version.py backend/tests/test_changelog_page.py -q`
Expected: PASS。

- [ ] **Step 3: 预览构建（不换生产）**

用 Task 7 的签名环境构建 `app-release.apk`；另建 staging 后端到端口 8010（不碰 `dist/run_backend`），截图 App 深/浅各一套 + 官网/PWA；交付用户确认。未确认前停止。

- [ ] **Step 4: 用户确认后发版**

按 `fenver-backend-package-swap` 技能：CI 全绿 → 本地全量 → 本地 tag `v0.36.0` → `pre_tag_gate --pre-build` → `version.txt` → PyInstaller `dist_new` → 产物验收 → DryRun → swap → 线上验收 → 终闸 → 推 tag → Release；APK 先落 `data/apk/0.36.0.apk` 验摘要，再改两份清单。

- [ ] **Step 5: 提交发版物料**

```bash
git add android-native/app/build.gradle backend/tests backend/app/web/static/app.js \
  backend/app/web/site/changelog.json backend/app/web/site/version.json docs/releases/v0.36.0.md
git commit -m "release(v0.36.0): 极简原生 UI、图标与流畅度"
```

---

## Self-Review

- **Spec coverage:** token/主题（Task 1）、组件与聊天（Task 2）、抽屉/设置（Task 3）、登录/日程/语音/更新/状态卡（Task 4）、官网/PWA（Task 5）、图标（Task 6）、性能（Task 7）、版本/预览/发版（Task 8）。spec 的 13 节均有对应任务；非目标未派工。
- **Placeholder scan:** 无 TBD/TODO；每个任务有确切文件、测试命令、提交命令；屏幕迁移用「文件 + 具体替换点 + 保留项」表达，未写伪代码。
- **Type consistency:** `FenverTokens`（不是 WebTokens）、`FenverSurface/Row/Button/TextField/TopBar/Sheet/Chip/Card`、`BrandText`、`SettingsCache` 在后续任务中保持一致；测试锚点引用同一组名字。
- **Known risk:** Task 3/4 的屏幕迁移涉及大文件，执行者必须先跑对应契约测试再改；Task 7 的帧率测量依赖设备，设备不可用按 spec 标记「待确认」。
