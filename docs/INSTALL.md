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
