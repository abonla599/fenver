"""禁句语料的"有意不入库"豁免判据：全仓唯一的一份，网页与原生两把锁共用（工单 #267）。

先例是 `_prod_data_guard.py`（同为 `backend/tests/` 下下划线开头的非收集模块）：
判据抽成一份而不是在两处各抄一遍，抄的那一份会先腐烂——这一句正是本文件存在的理由。

## 这一格在防什么

网页那把锁（`test_v0391_web_cockpit_ui.py` 的 `_web_copy_corpus`）与原生那把
（`test_v0391_native_cockpit.py` 的 `_native_kt_corpus`）都把"磁盘扫到的一份"与
"`git ls-files` 记的一份"钉成**同一个集合**。磁盘这一侧天然会多出一些**被 `.gitignore`
点名不入库**的东西：现网那棵树（仓库目录同时是生产机，AGENTS.md §1）在 `docs/` 下躺着
六份本机私有文档，`git ls-files` 永远不记它们 ⇒ 同集合比对在 CI 与干净克隆绿、偏偏只在
主树红，而 AGENTS.md §5 的"本地全量复跑"只能在主树做，等于换包前给任何人一次假警报。
所以比对前先从这里扣掉那一份。**没被 `.gitignore` 点名的游离文件一律不扣**——那才是
这枚钉的原意（#265 牙二/牙三、#267 牙四分别实跑过正反两面）。

原生侧此前**不问 git**，而是按一份手抄的 `/build/` 段排除构建产物。那是第二份真相：
#265 的审查员往里放一份 `android-native/.gradle/Scratch.kt`（`.gitignore` 第 215 行
`android-native/.gradle/` 确实点名它不入库，但它不在 `/build/` 段里）就 2 failed。
本票把它换成问 git，两把锁自此走同一份判据。

## 三条不许踩的形状（都当场红，不是"降级"）

1. **只问具体文件、绝不带尾斜杠问目录。** 实测口径必须写清，因为旧注释在这里说过假话：
   裸 CR 空行**只存在于 CRLF 检出**——本仓 `.gitignore` 的 blob 是纯 LF、零枚 CR-only
   行，而本机检出（`core.autocrlf=true`）里有 19 枚（行号 8/18/22/29/39/43/47/51/59/
   126/134/140/144/150/159/168/181/189/194，最后一枚 :194 赢）。带尾斜杠问一个不存在
   的目录时 git 就报给它：`git check-ignore -v docs/这一版根本不存在.md/` → rc=0 并回
   `.gitignore:194:`（空模式），而**同一条路径去掉斜杠 → rc=1**。拿那个形状当豁免＝把
   整条判据改成恒真，而且绿得毫无痕迹。这里批量问的是 `p1\0p2\0…` 的具体文件路径。
   （`test_machine_read_pointers.py` 的 `_SENTINEL` 那段记过同一枚假阳性。）
2. **fail-closed：rc 只认 0（有命中）与 1（无命中）。** 128、超时、git 不在 PATH、
   问不到一律当场红。不许"读不到就当没有豁免、继续比集合"——那样"什么都没判"和
   "判过了"长得一模一样，而且红话会反过来宣称"已扣掉，不是漏扫"，比不红更坏（#265
   审查缴械 c 实测的就是这一形：它在 CI 与干净克隆里完全隐形，74 passed）。
3. **反向自查两条：** 报出来的豁免必须是**问过的那一份**（`ignored <= paths`）；且
   不带 `--no-index` 时 git 永远不会把**已跟踪**的路径说成已忽略，真说成了就是这一问答歪了
   （假阳性形状），照它扣会静默吃掉真语料 ⇒ 红。这两条是有牙的：#265 审查员用伪造 git
   输出单点过，分别红在当时的 `:1460` 与 `:1463`。

## 谁在盯这份判据不许被缴械

`backend/tests/test_v0391_corpus_exemption_shape_lock.py`（AST 形状锁，#267 交付 B）：
它从 `git ls-files` 现取"这一份 helper + 两把锁的调用点"三棵文件，钉三件必备——必须
出现 `subprocess.run` 且参数里有 `"check-ignore"`、必须有 `returncode in (0, 1)` 这一形、
豁免集合不许来自字面量（禁 `startswith` 段排除、禁模式 tuple/list 常量）。摘掉下面任何
一条判据，红的是那把形状锁，不是这里。
"""
import subprocess


def deliberately_ignored(paths, tracked, repo):
    """磁盘那一份里，被 `.gitignore` 判定**有意不入库**的那一部分（git 侧本来就不记它们）。

    参数一律是**相对仓库根、posix 分隔符、不带尾斜杠的具体文件路径**：
    `paths` 是磁盘侧扫到的全集，`tracked` 是同一射程里 `git ls-files` 记着的那一份
    （只用于第 3 条反向自查，不参与扣减），`repo` 是问 git 时的 cwd。
    返回的集合就是调用方要从磁盘侧扣掉的那一份；返回空集只有一种合法来源——
    git 回答"这些路径谁也不挨着 `.gitignore`"（rc=1），读不到判据时这里从不返回空集。
    """
    if not paths:
        return set()                      # 磁盘一份都没有：没什么可豁免，交给下面的集合比对红
    payload = b"\0".join(p.encode("utf-8") for p in sorted(paths)) + b"\0"
    try:
        out = subprocess.run(["git", "check-ignore", "--stdin", "-z"],
                             cwd=str(repo), input=payload, capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        raise AssertionError(f"问不到 `git check-ignore`：豁免判据读不到时不许退化成『没有豁免』：{exc!r}")
    assert out.returncode in (0, 1), (
        f"`git check-ignore` 回了 rc={out.returncode}（只认 0=有命中 / 1=无命中）："
        f"读不到判据还继续比集合，等于把『漏扫』洗成『扣掉了』")
    ignored = {p.decode("utf-8") for p in out.stdout.split(b"\0") if p}
    assert ignored <= set(paths), (
        f"`git check-ignore` 报了一份没问过的路径：{sorted(ignored - set(paths))[:3]}——判据的来源不可信")
    wrongly = sorted(ignored & set(tracked))
    assert not wrongly, (
        f"git 把已跟踪的文案说成有意不入库（前几份：{wrongly[:3]}）：这一问答歪了，"
        f"照它扣会静默把真语料移出射程")
    return ignored
