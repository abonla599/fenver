"""出网 HTTPS 的信任锚。

默认走 certifi 那份自带根证书包，本机不适用：卡巴斯基的「加密连接扫描」会在路上
换一张它自己签发的证书，而 certifi 里不可能有它的自签根，于是每一次模型调用都是
CERTIFICATE_VERIFY_FAILED。ssl.create_default_context() 读的是操作系统那套根证书库
——用户既然把这张根装进了系统可信根，就是明确接受过这个中间人存在。

这里只是不再自作主张地换一套更窄的信任集，verify_mode 与 check_hostname 一律保持
默认（必须验证书、必须对主机名）。

不许给下面那次构造加 cafile= / capath= / cadata=：任何一枚都等于把信任集换成另一份
名单。本机实测钉 cafile=certifi 之后，卡巴斯基那枚根从信任集里消失（它在 Windows
根库里、不在 certifi 里），而 verify_mode 与 check_hostname 全都保持默认——"关校验"
那一族判据一个都不会红。后果也不止报错：模型调用/云端嵌入/搜索/取包一律
CERTIFICATE_VERIFY_FAILED，记忆面则静默降级成伪嵌入，界面上毫无痕迹。反向锁在
backend/tests/test_providers.py（AST 判 keyword.arg，不是子串）。

信任集还能从**环境**被扩大：OpenSSL 自己读 SSL_CERT_FILE / SSL_CERT_DIR
（ssl.get_default_verify_paths() 的 openssl_cafile_env / openssl_capath_env）。
已实测：把 SSL_CERT_DIR 指向 <subject_hash>.0 形式的哈希目录，验链从
CERTIFICATE_VERIFY_FAILED 变成成功，而 len(ctx.get_ca_certs()) 不变——hash-dir
查找是惰性的，只在链构建时按 hash 取文件，证书不进 store 的对象列表，所以"根数"
这个量具对 capath 天生是盲的，判它不许用根数。这条路只在**子进程启动时的环境继承**
上关：deploy/ 与 tools/ 的启动名单负责删掉这两枚名字（锁在
backend/tests/test_launch_env_hygiene.py，射程与仍漏清单写在那份文件头）；
进程内部读 .env、以及操作者手工启动，都不在那道名单的射程里。
"""
import ssl


def system_ssl_context() -> ssl.SSLContext:
    return ssl.create_default_context()
