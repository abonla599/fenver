"""单次请求的体积上限：唯一来源。

三个量不是同一个东西，不要互相替代：

- `MAX_CHAT_MESSAGES`：一次 `/v1/chat` 请求里带几条消息（50）。
- `MAX_MESSAGE_CHARS`：任何一条消息/目标文本最多多少字（32000）。
- `MAX_SESSION_MESSAGES`：整份会话回写（`PUT /v1/sessions/{id}/messages`）允许
  几条（500）。前端每次聊天后都把**整份历史**回写一次（app.js 的 syncSession），
  一个聊过 25 轮的会话自然就有 50 条以上；拿单次聊天的 50 去卡它，等于长会话
  从某一轮起再也存不进后端。

这个模块刻意不 import 任何东西：`app.main` 与 `app.session.session_store` 都要
拿这几个常量，而两者本就互相引用（main 构造 SessionStore），常量放在任何一侧
都会把 import 关系拧成环。
"""

MAX_CHAT_MESSAGES = 50
MAX_MESSAGE_CHARS = 32_000
MAX_SESSION_MESSAGES = 500
