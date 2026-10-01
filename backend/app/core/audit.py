"""管理端写操作审计：一份只能往里贴、不能从外面改的账。

为什么留这个（D14）：建号、停用、换发、重置、改配置这些动作都是"一个人替所有人
动了身份/上游"。线上排障、事后追责、乃至开源用户自证清白，靠的都是"谁在什么时候
干了哪一下、改前改后是什么"。这份账一旦能从界面删掉，它就不再是证据。

三条硬规矩：
1. **append-only**：只 open("a") 追加，一行一条 JSON，没有提供任何删除/改写入口，
   也没有 DELETE 路由。文件损坏的那一行读到时标成 _corrupt_line，不假装解析成功。
2. **落的是摘要，不是秘密**：before/after 里凡是键名撞上口令/令牌/密钥的，一律换成
   "***"——审计要证明"动过密码"，绝不需要把那枚密码记下来。这条是防手滑：调用方
   就算把整个用户记录原样递进来，也不会把 pw_hash / initial_password 写进盘。
3. **审计写失败绝不连累被审计的那次操作**：真存储已经落盘成功、事务意义上的动作
   已经完成，这时因为磁盘满/权限炸而不许改配置，是把可审计性做成了新的单点故障。
   所以 log() 吞掉写异常、往 stderr 打一行"审计在漏"，让运维看得见，而不是让管理员
   的"保存"按钮变红。

落点与其余每份可变数据同一个口径：$AUDIT_LOG_PATH > <项目根>/data/audit.jsonl。
"""
import datetime
import json
import os
import sys
import threading

from app.core.paths import data_file, ensure_parent

_LOCK = threading.Lock()

# 出现在 before/after 摘要里就一律不落明文的键名（小写比对）。这些不是"我们判断
# 它们敏感"，而是它们本就不该被记下来：审计要的是"这一格被改过"，不是那格的内容。
_SECRET_KEYS = {
    "password", "old_password", "new_password", "initial_password",
    "token", "tokens", "api_key", "apikey", "pw_hash", "secret",
    "authorization", "access_token", "refresh_token", "cookie",
}


def _default_path() -> str:
    return data_file("AUDIT_LOG_PATH", "audit.jsonl")


path = _default_path


def _redact(obj):
    """递归擦掉敏感键与超长串。字典的键按小写比对命中即置 ***。"""
    if isinstance(obj, dict):
        return {k: ("***" if str(k).lower() in _SECRET_KEYS else _redact(v))
                for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_redact(x) for x in obj]
    if isinstance(obj, str):
        return obj if len(obj) <= 400 else obj[:400] + "…"
    if obj is None or isinstance(obj, (bool, int, float)):
        return obj
    return str(obj)[:400]


def _actor_bits(actor):
    """Principal 或带同名键的 dict 都能取，取不到就留空——审计宁可少一栏也不炸。"""
    def pick(name):
        if isinstance(actor, dict):
            return actor.get(name)
        return getattr(actor, name, None)
    return {
        "actor_id": pick("user_id"),
        "actor_name": pick("username"),
        "actor_role": pick("role"),
    }


def log(actor, action, target=None, before=None, after=None, detail=""):
    """贴一条审计进去。返回写出的记录（便于测试断言），写盘失败也只影响落盘那一步。"""
    rec = {
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        **_actor_bits(actor),
        "action": action,
        "target": _redact(target),
        "before": _redact(before),
        "after": _redact(after),
        "detail": (detail or "")[:500],
    }
    p = _default_path()
    try:
        ensure_parent(p)
        line = json.dumps(rec, ensure_ascii=False)
        with _LOCK, open(p, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())
    except Exception as e:                     # noqa: BLE001 - 审计不许把被审计的动作带崩
        try:
            print(f"[audit] 一条审计没能落盘（{action}）：{e}", file=sys.stderr)
        except Exception:                       # noqa: BLE001 - stderr 都没了就只能沉默
            pass
    return rec


def read(limit: int = 200) -> list:
    """倒序读最近 limit 条再翻回正序（最旧在前）。admin-only 的路由去套这个。

    文件还不存在＝一条都还没发生，返回空表而不是抛错：新装的机器第一次进管理页
    本就没有历史，把 404 甩给一个合法的空状态是给自己找事。逐行独立解析，坏行标
    记出来，好行照给——一份被截断的尾巴不该让前面所有的证据一起看不见。
    """
    p = _default_path()
    if not os.path.exists(p):
        return []
    try:
        with open(p, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception:                           # noqa: BLE001 - 读不出就当下没有
        return []
    out = []
    for ln in lines[-max(1, int(limit)):][::-1]:
        ln = ln.strip()
        if not ln:
            continue
        try:
            out.append(json.loads(ln))
        except Exception:                       # noqa: BLE001 - 坏行不假装是好行
            out.append({"action": "_corrupt_line", "detail": ln[:200]})
    out.reverse()
    return out
