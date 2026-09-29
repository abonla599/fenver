# Fenver 部署指南（域名 / HTTPS / 隧道 / 更新通道）

> [INSTALL.md](INSTALL.md) 解决"本机跑起来"；这份解决"让它安全地给别人用"。
> 核心原则一句话：**后端永远只听 127.0.0.1，公网暴露交给一层带 TLS 的入口**。

## 1. 选一条对外路径

| 路径 | 适合 | 要点 |
|------|------|------|
| Cloudflare Tunnel（推荐） | 没有公网 IP、不想开路由端口 | 装 cloudflared，注册免费隧道，把 `http://127.0.0.1:8000` 挂到一个域名下。TLS 终结在 Cloudflare，家里机器不需要证书 |
| 自有反代（nginx/Caddy） | 有公网 IP 的 VPS | 反代终结 TLS，回源 127.0.0.1:8000。用 Caddy 的话 HTTPS 是自动的 |
| 直接 `--host 0.0.0.0` | 只在内网用 | 没有 HTTPS 就没有安全登录态可言，公网环境禁止这样跑 |

cloudflared 简版步骤（Windows）：

```powershell
# 官网下载 cloudflared.exe 后
cloudflared tunnel login
cloudflared tunnel create fenver
# 在 cloudflare 后台把域名的 CNAME 指到隧道，再写 config.yml：
# tunnel: <UUID>
# credentials-file: C:\Users\<你>\.cloudflared\<UUID>.json
# ingress:
#   - hostname: ai.example.com
#     service: http://127.0.0.1:8000
#   - service: http_status:404
cloudflared tunnel route dns fenver ai.example.com
cloudflared tunnel run fenver
```

**`config.yml`、`cert.pem`、`<UUID>.json` 是凭据，永远不进仓库、不进备份快照。** 本仓库根 `.gitignore` 不含它们，也不要"顺手"给示例文件填真值。

## 2. HTTPS 之后要检查的三件事

1. 浏览器开 `https://你的域名/app/`，登录、发消息、传附件都过一遍；
2. 手机端 APK 的「设置 → 连接」填同一个 `https://` 地址；
3. 管理员确认 `GET /v1/config` 返回的 `update_repo` 是自己的仓库（见下节）。

多用户注意：注册默认关闭（`registration_open=false`），这是防"公网一开人人可注册"的默认姿态。给人开号两种方式：管理员在 `/admin` 建号发初密；或临时打开注册、发完邀请链接再关掉。禁用某人：`/admin` 账号 Tab 禁用即可，对方下一个请求就 401。

## 3. 更新通道（APK 自动检查更新怎么找到你）

安卓端不直连 GitHub——它问你的后端，后端再去拉 GitHub Release。链路：

```
App「检查更新」→ 你的后端 /v1/update/check → 按 data/config.json 的 update_repo 探测
                                        ↳ 失败自动回退 abonla599/ai-assistant（旧仓兜底）
```

- `update_repo` 默认 `abonla599/fenver`；**你自己 fork 部署的话，把它改成你自己的 `用户/仓库`**，否则用户会收到别人家的更新包。改法：`POST /v1/admin/config` `{"update_repo":"you/fenver"}`，或删掉 `data/config.json` 里这个键用默认。
- 新版本发布 = 在你配置的仓库打 `v*` tag 触发 CI：构建 APK、正式签名、上传 Release、附 SHA256。客户端下载后校验 SHA256 才安装，校验不过会明说。
- 签名：CI 从仓库 Secrets 取 keystore（`APK_KEYSTORE_BASE64` / 口令两项），换仓库要把 Secrets 一起搬。本地生成 keystore 的说明不在源码分发范围（属发布方私有材料）。

## 4. 数据备份与迁移

运行期全部可变数据就三处（路径由 `backend/app/core/paths.py` 一处推导）：

```
data/          会话、用户、配置、反馈、日程、用量账本（密钥单独在 data/provider_keys.json）
chroma_db/     长期记忆向量库
.env           你的凭据（不进版本库，但一定要自己备份）
```

备份 = 停服后拷这三处；迁移 = 新机器装同版本代码，放回去，起服务。`data/users.json` 里密码与找回题答案都是摘要不是明文，但备份它等于备份"能改别人密码"的原始材料，按密钥等级保管。

## 5. 出问题先看哪

- 启动日志第一屏有中文的配置口径与鉴权模式行，先看它;
- `502/连接重置`：隧道或反代层问题，后端本身可能好好的——本机 curl 127.0.0.1:8000 分段排查；
- `503 未配置访问凭据`：见 INSTALL 常见问题，fail-closed 不是坏了；
- 收不到更新检查：多半是 `update_repo` 没改成自己的仓，或 tag 没打。
