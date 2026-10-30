"""离线建一个真实管理员账号（H-1）。

**必须在服务停止时跑**：AuthStore 在进程启动时把 users.json 读进内存，运行中
直接改盘会被下一次写回覆盖。工具要求显式 `--i-stopped-the-service` 才动盘。

口令默认随机生成，只写进仓库外的凭据文件（终端只回显路径，不打印口令）。
账号首登仍会被要求改密（与管理员建号同一条纪律）。

用法：
    venv\\Scripts\\python.exe tools\\create_admin_account.py --username fenver-admin --i-stopped-the-service
    venv\\Scripts\\python.exe tools\\create_admin_account.py --username fenver-admin --password-file C:\\path\\pw.txt --i-stopped-the-service
"""
import argparse
import os
import secrets
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from app.core.auth import AuthStore, AuthError  # noqa: E402


def _write_credentials(path: Path, username: str, password: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"username: {username}\npassword: {password}\n"
        "注意：首登会要求改密；这份文件看完请删除或移入密码管理器。\n",
        encoding="utf-8")
    try:
        import subprocess
        subprocess.run(["icacls", str(path), "/inheritance:r",
                        "/grant:r", f"{os.getlogin()}:F",
                        "/grant:r", "SYSTEM:F", "/grant:r", "Administrators:F"],
                       check=False, capture_output=True)
    except Exception:
        pass


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--username", required=True)
    parser.add_argument("--password-file", default="")
    parser.add_argument("--users-path", default="")
    parser.add_argument("--credentials-dir",
                        default=r"C:\Users\34426\dl\admin-credentials")
    parser.add_argument("--i-stopped-the-service", action="store_true")
    args = parser.parse_args(argv)
    if not args.i_stopped_the_service:
        print("拒绝执行：先停服务，再带 --i-stopped-the-service 重跑。", file=sys.stderr)
        return 2

    if args.password_file:
        password = Path(args.password_file).read_text(encoding="utf-8").strip()
    else:
        password = secrets.token_urlsafe(18)
    if len(password) < 8:
        print("口令太短", file=sys.stderr)
        return 2

    store = AuthStore(path=args.users_path) if args.users_path else AuthStore()
    try:
        record = store.admin_create_user(args.username, password, role="admin")
    except AuthError as e:
        print(f"建号失败：{e.reason}", file=sys.stderr)
        return 1

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    cred_path = Path(args.credentials_dir) / f"{args.username}-{stamp}.txt"
    _write_credentials(cred_path, args.username, password)
    print(f"已建管理员 {args.username}（{record['user_id']}），凭据写入 {cred_path}")
    print("下一步：启动服务，用该账号登录并立即改密；确认可用后再从 .env 移除 ACCESS_TOKEN。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
