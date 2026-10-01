# 3.12，不是 3.11：CI（tests.yml）、本机跑测试的 venv、以及打 EXE 用的全局解释器
# 今天全是 3.12，而 requirements 里钉死的 chromadb 对上限敏感。容器这条路径此前
# 悄悄用另一个解释器装另一套依赖，等于"备用部署"和"被验过的部署"不是同一个东西。
FROM python:3.12-slim

WORKDIR /app

# 安装 curl 用于健康检查
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

# 依赖只有一份事实来源：backend/requirements.txt（CI 装的就是它，逐条带"为什么需要"的注释）。
# 仓库根原先还躺着一份 118 行的 pip freeze 转储（UTF-16、零注释、numpy/onnxruntime 等
# 版本号与这份对不上），而这里 COPY 的正是那一份——于是 docker build 出来的容器
# 从来不是 CI 验过的那套依赖。那份转储已删；要重新生成随时 pip freeze 可得，留着它
# 只会让人以为自己是清单。
COPY backend/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# 复制源码
COPY backend/ ./backend/

# 不以任何人对镜像的默认期待为理由 root：这个进程手里有 .env 里的模型密钥、
# data/ 下所有人的会话与口令摘要、chroma_db/ 下的长期记忆。容器逃逸或任何一处
# 任意写漏洞，在 root 下够到的是宿主机，在下面的 uid 下够到的只是自己那棵树。
# uid 固定成 10001 而不用现成用户：python:3.12-slim 的基础用户集合随 Debian
# 版本变过，镜像里"恰好有个 uid 1000 的 ubuntu"不是可以依赖的事实。
#
# 代价说清楚：compose 把宿主机的 ./data 与 ./chroma_db 挂进来，容器里的写权限
# 由**宿主机目录的属主**决定，镜像里的 chown 管不到挂载点。所以第一次切到
# 非 root 之前要在宿主机上执行一次
#     sudo chown -R 10001:10001 data chroma_db
# 漏掉这一步的症状是启动即 PermissionError，而不是静默丢数据。
# 详见 docs/安装部署指南.md。
RUN useradd --system --uid 10001 --user-group --no-create-home --shell /usr/sbin/nologin fenver \
    && mkdir -p /app/data /app/chroma_db \
    && chown -R fenver:fenver /app

USER fenver

EXPOSE 8000

# 从 backend/ 里以 app.main 起，和 run_backend.py、CI 同一个导入根：
# 代码里全是 `from app.core...`，写成 backend.app.main 会在收集期就 ImportError。
WORKDIR /app/backend
# HOST 与 uvicorn --host 保持同一份事实：authz 的 disabled 远程护栏读它。
# 容器天然绑 0.0.0.0，因此镜像里配 AUTH_MODE=disabled 会被 503 拒绝，除非显式 ALLOW_DISABLED_REMOTE=1。
ENV HOST=0.0.0.0
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]