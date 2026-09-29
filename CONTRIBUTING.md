# 怎么参与

先说规矩再谈代码——这仓库对"话怎么说"有硬要求，对"改动怎么证明"也是。

## 环境

```bash
python -m venv venv
venv\Scripts\pip install -r backend\requirements.txt   # Windows；Linux/macOS 自行换路径
venv\Scripts\python run_backend.py                      # 后端起在 :8000
```

跑测试请用**装过 requirements 的那个解释器**（venv 里那份），CI 按同一份清单从零装包，
解释器不一致时结论会两边飘（仓库里有条锁测试专门盯这件事）。

## 测试与契约文化

- 全量：`venv\Scripts\python -m pytest backend/tests -q`，约 1150 条，基线零红。
- 这个仓库大量使用**契约测试**：双端（网页/安卓）共用的一批中文文案、日期算式、
  接口形状各钉一份期望值，两端各自和它对——漂一个字当场红。改这类表面时先改契约，
  红的那条会告诉你另一端的哪个文件要跟着动。
- 新端点先写鉴权契约测试再写实现（`test_route_auth_contract.py` 会保证每个 /v1 路由
  的依赖树里挂着身份或管理员守卫，漏挂即红）。
- 可变数据（JSON 存储）一律走 `backend/app/core/atomic_write.py` 的公共落盘函数，
  自己手写 tmp+replace 会被静态检查红出来。

## 日志与提交的说话方式（硬性规范）

三类文字统一"人话三原则"：**说清改了什么、为什么、真机或线上什么现象没了**。

1. **commit message**：conventional 前缀 + 中文人话标题，根因写进正文一句话。
   例：`fix(android): 更新下载 0KB 卡死——服务端转发 GitHub 首字节 7.6s 超过读取超时，改为 60s 且不盲跟跳转`
2. **Release 更新日志**：修复/优化/新增三段短条目，每条落到具体现象。
   ❌"优化了语音交互体验" ✅"长按说话松手后不再等 1.6 秒才发出去"。
   禁用句式：旨在、赋能、进一步提升了用户体验、总而言之、让我们……
3. **运行日志**：只留"发生了什么 + 去哪查"；错误行必须带人话主语和下一步
   （例："DeepSeek 连接超时，用户会看到重试提示；详情见 data/logs/upstream_20260929.log"），
   删掉无信息量的逐过程打印。

发版检查单里有"日志抽查 3 条"，不合格改写后才允许打 tag。

## 提 PR 前

- 本地全量测试零红；涉及双端表面的，跑 `python tools/shell_jvm_tests.py`（安卓纯逻辑台架，
  不需要装 Android SDK）。
- 不要提交任何 `data/`、`.env`、密钥形态的字符串；CI 有 gitleaks 门禁（v0.24 起）。
- 一个 PR 说一件事。描述里写清楚：现象、根因、改法、怎么验的。
