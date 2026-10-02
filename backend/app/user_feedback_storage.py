"""用户意见反馈（意见反馈页提交的那份）的落盘存储。

与 feedback.json 分家：那份记的是聊天回答的 👍/👎，被偏好分析器消费、按条追加、
从不删除；这一份是「用户写给管理员的话」——管理员要读、要标已读、读完要能删。
两种生命周期塞一个文件，删除反馈就会连带打穿 preference_analyzer 的读口径，
所以各存各的：data/user_feedback.json + data/user_feedback_images/。

编号是这条链路的合同（"每条反馈有独立编号，直达管理员"）：FB- 前缀 + 递增序号，
计数器与记录存在同一份 JSON 里、同一把锁下递增——重启后接着数，删除不回补、
编号永不复用。

图片在提交那一刻从附件目录**复制**进反馈目录：附件属用户，他随时能删
（/v1/uploads/{id} 的 DELETE 就摆在那里），而反馈是管理员的处理凭据，不能因为
用户手滑删了附件就变成"有反馈没图"。复制之后反馈与附件再无瓜葛，管理员读图
也不需要一条"越属主读别人附件"的新口子——那道口子的形状我们清楚有多危险。

与兄弟存储同一套纪律：模块级 threading.Lock 串行化读—改—写（一个数据根目录
只允许一个服务进程），落盘走 write_json_atomic，读不懂的文件原样抛出去而不是
当成空表继续写。
"""
import json
import os
import re
import shutil
import threading
from datetime import datetime

from app.core.atomic_write import write_json_atomic
from app.core.paths import data_file, ensure_parent

USER_FEEDBACK_FILE = data_file("USER_FEEDBACK_FILE", "user_feedback.json")

_LOCK = threading.Lock()

TEXT_MAX = 300            # 反馈正文上限（字数），与意见反馈页的 x/300 同一合同
IMAGE_MAX = 4             # 单条反馈最多附几张截图
ID_RE = re.compile(r"^FB-\d{6,}$")


def _image_dir() -> str:
    """截图目录钉在数据文件旁边：USER_FEEDBACK_FILE 指到哪，图就跟到哪。

    不另开一个环境变量：测试隔离的判据是"指走数据文件就不碰真实数据"，
    目录若各指各的，漏指那半边的形状恰好就是当年 CHROMA_DB_PATH 漏指的翻版。
    """
    parent = os.path.dirname(USER_FEEDBACK_FILE) or "."
    return os.path.join(parent, "user_feedback_images")


def _empty_doc() -> dict:
    return {"next_no": 1, "items": []}


def _read_doc(path: str) -> dict:
    """读出 {计数器, 列表} 整份文档；文件还不存在时是空文档，形状不对则抛。

    与 feedback_storage._read_all 同一条底线：读不懂绝不能当成"还没有反馈"然后
    继续写——那等于一次磁盘抖动把攒下的反馈连同编号历史一起清零。
    """
    if not os.path.exists(path):
        return _empty_doc()
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if (not isinstance(data, dict) or not isinstance(data.get("items"), list)
            or not isinstance(data.get("next_no"), int) or data["next_no"] < 1):
        raise ValueError(
            f"意见反馈文件 {path} 形状不对（应为 {{next_no, items}} 文档），"
            "拒绝在修好之前写入，以免整表被覆盖。")
    return data


def _write_doc(path: str, doc: dict) -> None:
    tmp = path + ".tmp"
    try:
        write_json_atomic(path, doc)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def submit(user_id: str, text: str, email: str, image_sources: list) -> dict:
    """记下一条用户反馈并返回记录。

    image_sources 是路由层过了属主与类型校验的 [(源文件路径, 扩展名), ...]；
    这里把每张复制成 <编号>-<序号><扩展名>。任何一步失败（磁盘满、源文件在
    校验后被删）都把已复制的半成品扫掉再抛——宁可这条反馈没记上，也不留一条
    "编号占了他没图"的残账；编号本身同样不消耗：写回失败时计数器原样。
    """
    with _LOCK:
        doc = _read_doc(USER_FEEDBACK_FILE)
        fb_id = f"FB-{doc['next_no']:06d}"
        ensure_parent(USER_FEEDBACK_FILE)
        os.makedirs(_image_dir(), exist_ok=True)
        names = []
        try:
            for i, (src, ext) in enumerate(image_sources):
                name = f"{fb_id}-{i}{ext}"
                shutil.copyfile(src, os.path.join(_image_dir(), name))
                names.append(name)
            record = {
                "id": fb_id,
                "user_id": user_id,
                "text": text,
                "email": email or "",
                "images": names,
                "created_at": datetime.now().isoformat(),
                "read": False,
                "read_at": None,
            }
            doc["items"].append(record)
            doc["next_no"] += 1
            _write_doc(USER_FEEDBACK_FILE, doc)
        except BaseException:
            for name in names:
                try:
                    os.remove(os.path.join(_image_dir(), name))
                except OSError:
                    pass
            raise
        return record


def get(fb_id: str):
    with _LOCK:
        for row in _read_doc(USER_FEEDBACK_FILE)["items"]:
            if row.get("id") == fb_id:
                return row
    return None


def list_all() -> list:
    """全部反馈，按提交先后原序返回；倒序与未读计数是路由层的事。"""
    with _LOCK:
        return list(_read_doc(USER_FEEDBACK_FILE)["items"])


def mark_read(fb_id: str):
    """标已读。幂等：已读过的再点一次原样返回，read_at 记第一次。"""
    with _LOCK:
        doc = _read_doc(USER_FEEDBACK_FILE)
        for row in doc["items"]:
            if row.get("id") == fb_id:
                if not row.get("read"):
                    row["read"] = True
                    row["read_at"] = datetime.now().isoformat()
                    _write_doc(USER_FEEDBACK_FILE, doc)
                return row
    return None


def delete(fb_id: str):
    """删除一条反馈连同它的截图。返回 (状态, 被删记录或 None)。

    状态：deleted / not_found / unread。**未读不许删**是这条接口的合同——
    用户说得很明白，管理员"已读之后可以选择删除"；把闸门做在服务层而不是界面
    按钮上，是因为界面从来不是边界（手搓 DELETE 请求的人不该绕过这个语义）。
    编号不回收：删掉的 FB-000007 之后不会再出现第二条 7。
    """
    with _LOCK:
        doc = _read_doc(USER_FEEDBACK_FILE)
        for i, row in enumerate(doc["items"]):
            if row.get("id") == fb_id:
                if not row.get("read"):
                    return "unread", None
                record = doc["items"].pop(i)
                _write_doc(USER_FEEDBACK_FILE, doc)
                for name in record.get("images", []):
                    try:
                        os.remove(os.path.join(_image_dir(), name))
                    except OSError:
                        pass
                return "deleted", record
    return "not_found", None


def image_path(record: dict, index: int):
    """第 index 张截图的磁盘路径；文件不在（被手工清过）返回 None。"""
    names = record.get("images", [])
    if not isinstance(index, int) or index < 0 or index >= len(names):
        return None
    path = os.path.join(_image_dir(), names[index])
    return path if os.path.isfile(path) else None
