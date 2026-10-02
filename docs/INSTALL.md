# Fenver 安装指南

> 目标：从 `git clone` 到浏览器打开自己的助理界面。两条路任选：**A. Windows 裸机**（一台家用电脑/NAS 就够）、**B. Docker**（机器上有 Docker 就一条命令）。
> 全程只需要：Python 3.12（路线 A）、你自己的模型 API Key、约 10 分钟。

## 路线 A：Windows 裸机（venv）

### 1. 拿代码、建环境

```powershell
git clone https://github.com/abonla599/fenver.git
cd fenver
python -m venv venv
venv\Scripts\python.exe -m pip install -r backend\requirements.txt
```

Python 版本钉 3.12：CI、测试、打包全在 3.12 上验过，别的版本不承诺能装齐依赖（chromadb 对版本上限敏感）。

### 2. 配一份 .env

```powershell
copy .env.example .env
notepad .env
```

要填的就三样，其余保持注释状态即可：

| 变量 | 填什么 |
|------|--------|
| `DEEPSEEK_API_KEY`（或 `OPENAI_API_KEY`） | 你自己买的模型 Key，至少一家 |
| `api_key` + `EMBEDDINGS_BASE_URL` + `EMBEDDINGS_MODEL` | 向量嵌入服务三项，记忆模块用语义检索；不填会降级为本地模型，能跑但检索质量差一档 |
| `ACCESS_TOKEN` | 管理员口令，自己定一个长随机串。只有拿这把 token 的请求是管理员；普通用户走「注册/登录」，令牌与密码都是摘要存储 |

三条安全底线（代码里都有测试钉着，别绕）：
- `.env` 不进版本库（`.gitignore` 已挡）；历史上有人把它提交上去过，Key 公开了五个月。
- `ACCESS_TOKEN` 只留给自己。发给任何人，对方就是能改模型配置、删用户的管理员。
- 不要把 ACCESS_TOKEN 打进 APK 或前端页面；用户侧一律用用户名密码。

### 3. 起服务

```powershell
cd backend
..\venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

看到启动日志里一行中文的配置口径（注册开关、更新仓库）就算起来了。浏览器打开 **http://127.0.0.1:8000/app/** ——这就是助理界面；`/admin` 是管理页（v0.24 起三 Tab 控制台）。

只监听 127.0.0.1 意味着只有本机可访问，这是默认也是建议。要给别人用，先读完 [DEPLOY.md](DEPLOY.md) 的 HTTPS 一节再开口 `--host 0.0.0.0`。

流式回答有个计费防护默认值：关掉页面后最多再跑完当前这一轮就停，不会因为你不在就接着起新轮子花钱；默认 120 秒内重开可以接着看（改法：`data/config.json` 的 `stream_no_reader_grace_seconds`，或 `.env` 里 `STREAM_NO_READER_GRACE_SECONDS` 覆盖，0 表示不留宽限）。

### 3b. 模型单价与积分口径（v0.25 R4b，部署者必读）

本版没有钱包、不扣积分，但**影子积分**从第一次调用就在算：每次按「真实 token × 你配的单价」算出"本应消耗多少"。这是你垫了多少钱的唯一记录，也是 1.0 定价的唯一依据——所以**每个在用的模型都该把 `pricing` 配上**。没配价的模型会被如实标成「未定价」（显示 `?x`），算不出就绝不当成 0（那等于白烧你的钱还报"没花钱"）。

给模型配单价：管理员 `PUT /v1/providers/{id}`、用户 `PUT /v1/me/providers/{id}`，请求体加一格：

```json
{"pricing": {"mode": "token", "currency": "CNY",
             "input_per_m": 1.0, "output_per_m": 4.0,
             "price_checked_on": "2026-10-01", "free_until": null}}
```

- `input_per_m` / `output_per_m`：**每百万 token** 的现价（去各家官网核对后填，`price_checked_on` 记核价日期）。两档必须一起给，半套价格会被拒绝。
- `free_until`：显式免费期的截止日期（如 `"2099-12-31"`）。免费期内显示 `0.00x`、对用户分文不扣，但影子照常按真实单价算——"0.00x"和"?x"是两种东西，别混。
- `mode: "per_call"`（按次/按张计价）：结构已预留，**本版结算未实现**，碰到它会明确拒绝并说明原因，不会偷偷按 token 公式折算。
- 单价任何改动都会落 `audit.jsonl`（谁、哪格、从多少到多少）；请求里不带 `pricing` 键 = 不改价，显式 `pricing: {}` 才是取消定价。

下面是一张**示例价表**（截至 2026-10 各家公开牌价的形状，仅供照抄格式，下单前务必自己核对当日价格——代码里没有任何内置默认价）：

| 模型（示例） | mode | currency | input_per_m | output_per_m | 备注 |
|---|---|---|---|---|---|
| DeepSeek flash | token | CNY | 1.0 | 4.0 | 官方长期牌价形状；若厂商限免，`free_until` 填限免截止日 |
| DeepSeek pro | token | CNY | 3.0 | 6.0 | 缓存命中便宜，但本口径 `cached_tokens` **不打折**（打折就是把账算成猜的） |
| 通义千问 plus | token | CNY | 0.8 | 2.0 | `reasoning_tokens` 含在输出里按输出档计，不另加一遍 |
| 未核价的模型 | —— | —— | —— | —— | 先别配 `pricing`，让它显示 `?x`，比猜一个价诚实 |

基准价（¥→积分的锚，"1 积分 = 基准价跑 1000 个混合 token"）住在 `data/config.json` 的 `credit_benchmark`，默认 输入 ¥1/百万 + 输出 ¥4/百万 = 1.00x、折算比例 4:1，部署者可改；**倍率只是展示**（选模型时的直觉），结算与影子一律真实单价，倍率不是合同价。

每日影子护栏默认**关**（`credit_shadow_daily_limit = 0`）：只算不拦。想让它拦人，`POST /v1/admin/config` 设一个正数（或 `.env` 里 `CREDIT_SHADOW_DAILY_LIMIT`）——某用户当日影子到线后，新的付费轮在轮次边界停在「等待你确认是否继续」，且不再向模型出网。管理端 `GET /v1/admin/usage` 的 `credits` 字段给按天、按 provider 的影子合计与「免费期已垫付 N 积分」。

### 4. 注册没开，怎么进第一个账号

新装默认**注册关闭**（防公网裸奔）。管理员在 `.env` 场景下用 ACCESS_TOKEN 直接以「本机管理员」身份用 `/admin` 建号；或者临时打开注册：`.env` 里加一行 `REGISTRATION_OPEN=1` 重启，注册完删掉这行再重启。注册入口的开关在 `data/config.json`，管理员也可以登录后用接口改（`POST /v1/admin/config`）。

## 路线 B：Docker

```bash
git clone https://github.com/abonla599/fenver.git
cd fenver
cp .env.example .env    # 同样只填上表那三样
docker compose up -d --build
```

打开 `http://<机器IP>:8000/app/`。数据落在挂载的 `./data/` 与 `./chroma_db/`，删容器不丢记忆。

镜像里的服务进程不是 root（uid 10001）。容器对**挂载进来的目录**有没有写权限，由宿主机上那两个目录的属主决定，所以第一次用这份 compose 前先执行一次：

```bash
sudo chown -R 10001:10001 data chroma_db
```

漏掉这一步的症状是容器启动即 `PermissionError`，不会静默丢数据。

注意：**不要**把 docker compose 和裸机 exe/uvicorn 同时起——两个进程同写一份 `data/` 会静默互相覆盖，没有任何报错。一个数据目录只允许一个服务进程。

## 装完自检

```bash
curl http://127.0.0.1:8000/          # 官网页
curl -H "Authorization: Bearer <你的ACCESS_TOKEN>" http://127.0.0.1:8000/v1/config
```

第二条应返回注册开关与更新仓库的当前值。全量测试约 1150 条（`venv\Scripts\python.exe -m pytest backend/tests -q`），CI 每次推送都跑。

## 常见问题

- **装依赖报版本冲突**：确认用的是 3.12（`venv\Scripts\python.exe --version`），不要混用系统 Python。
- **503「服务端未配置访问凭据」**：`ACCESS_TOKEN` 为空且 `data\users.json` 里没有管理员，这是刻意的 fail-closed——填上 token 重启即可，不算 bug。
- **手机端**：装 GitHub Releases 上的 APK，它是指向你部署地址的壳；在应用内「设置 → 连接」填你的 `https://` 地址即可。网页端浏览器直接开 `/app/` 也支持 PWA 装到桌面。
