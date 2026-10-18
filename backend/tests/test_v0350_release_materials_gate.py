"""#22 发版物料顺序闸：**包先落盘并验过摘要，清单才许宣告这一版**。

## 这条锁守的是哪次事故

v0.34.2 收尾时，自托管更新通道被指向一个当时在盘上还验不出摘要的版本，宣告只能回滚。
手机侧的表现不是"温柔地留在旧版"，而是「检查更新失败」：清单摘要与盘上字节对不上时
`/v1/update/info` 整条 502（`releases._fetch_manifest` → `apk_cache.cached_file_state`），
壳那侧 `UP_TO_DATE` 只在 `ReleasePlan.decide` 的版本比较里产生，`apk_sha256` 是它之后
才读的一格——所以这次事故里**运行时每一环都正确地 fail-closed，发版那一刻却没有任何
东西变红**，红的是手机。缺的是发版时的顺序闸，本文件锁的就是那一格。

## 每条锁都真跑闸门

沿用本仓纪律（**能用机器问的地方不许写文本锁**）：判据是拿 `tmp_path` 手搭的假清单
与假 APK 真跑 `main()` / `check_announcement_materials()`，断言的是**真实退出码与
真实输出**。唯一保留的文本断言是"闸门拒绝时必须把显式开关的名字说出来"——那是在
测给人的可操作提示，不是测判据。

反例一律用**手搭的假路径**：`RELEASE_MANIFEST_PATH` 与 `APK_CACHE_DIR` 两个口子在
每个用例里都被钉在 `tmp_path` 下（`data/version.json` 是**线上那句公开宣告**，写它
等于告诉所有手机有新版本；`data/apk/` 是线上正在代管的字节）。钉住这件事本身也有一条
锁，见 `test_pinned_paths_never_point_at_the_real_repo_data`。
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
# 默认测仓库里这把闸门本身；`PRE_TAG_GATE_PATH` 只服务于"先见过红"那两次实验
# （把闸门的历史那份拷到仓库外来跑），生产代码里没有读这个变量的地方。
GATE = Path(os.environ.get("PRE_TAG_GATE_PATH") or (REPO_ROOT / "tools" / "pre_tag_gate.py"))

# 仓库数据路径里那两个**真**落点：任何用例都不许指到它们。
REAL_MANIFEST = (REPO_ROOT / "data" / "version.json").resolve()
REAL_APK_DIR = (REPO_ROOT / "data" / "apk").resolve()
REAL_SEED = (REPO_ROOT / "backend" / "app" / "web" / "site" / "version.json").resolve()

TAG = "v0.35.0"                      # 假想的下一次发版
VERSION = "0.35.0"
OLD_VERSION = "0.34.2"


def _load_gate():
    spec = importlib.util.spec_from_file_location("pre_tag_gate_under_test", str(GATE))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _abspath(*parts) -> str:
    return os.path.join(*[str(p) for p in parts])


@pytest.fixture
def materials(tmp_path, monkeypatch):
    """把宣告侧与包侧的两个环境变量口子钉在 tmp_path 下，返回三把假路径。

    这里同时是**卫生锁的执行处**：真仓库的 `data/version.json` / `data/apk` 绝不出现在
    这两个变量里（`RELEASE_MANIFEST_PATH` 正是清单的覆写口子，也就是最顺手打向真文件的
    那条路，所以每条用例都钉死）。
    """
    fake_root = tmp_path / "fake-repo"
    manifest = tmp_path / "fake-announce" / "version.json"
    apk_dir = tmp_path / "fake-apk-dir"
    seed = tmp_path / "fake-seed" / "version.json"
    monkeypatch.setenv("RELEASE_MANIFEST_PATH", str(manifest))
    monkeypatch.setenv("APK_CACHE_DIR", str(apk_dir))
    # 钉不住就是事故现场：断言的是"绝不等价于真路径"，不是"看起来不像"。
    for value, real in ((manifest, REAL_MANIFEST), (apk_dir, REAL_APK_DIR), (seed, REAL_SEED)):
        resolved = Path(os.path.abspath(str(value)))
        assert str(resolved) != str(real), "测试把环境变量指向了仓库真实数据路径：%s" % resolved
        assert tmp_path in resolved.parents or resolved == tmp_path, "假路径必须落在 tmp 下"
    return {"root": fake_root, "manifest": manifest, "apk_dir": apk_dir, "seed": seed}


def write_manifest(path, version=VERSION, sha=None, apk_name=None, omit_sha=False):
    """手搭一份**假**清单（形状照 data/version.json 的那几格）。"""
    body = {
        "version": version,
        "apk_name": apk_name if apk_name is not None else "fenver-%s.apk" % version,
        "sha256": sha if sha is not None else "a" * 64,
        "size": 7805351,
        "url": "https://ai.fenever.xyz/site/android.apk",
        "changelog": "v%s" % version,
    }
    if omit_sha:
        body.pop("sha256")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body), encoding="utf-8")
    return body


def write_apk(apk_dir, version=VERSION, payload=b"these are the bytes of the real apk"):
    """手搭一个**假**包，返回它这串字节的真摘要。"""
    d = Path(apk_dir)
    d.mkdir(parents=True, exist_ok=True)
    p = d / ("%s.apk" % version)
    p.write_bytes(payload)
    return hashlib.sha256(payload).hexdigest()


def _pin_seed(gate, monkeypatch, seed_path):
    """把"打包内种子"的落点也挪进 tmp：否则运行时的 `root` 会指向真仓库去读真种子。"""
    monkeypatch.setattr(gate, "seed_manifest_path", lambda repo_root: str(seed_path))


def run_gate(gate, monkeypatch, materials, extra_argv=()):
    """真跑 `main()`：只接掉与顺序闸无关的旁证，顺序闸自己一律走真判据。

    - `fetch_build` 必须接掉：不接就是在测试里打线上 `https://ai.fenever.xyz/health`；
    - 泄露隔离闸（第 6 格）也必须接掉：本文件的 `root` 是真仓库，那一格判的是真仓库
      此刻的 ref 状态，留着它，"物料齐了就放行"这条正控测到的就不是物料顺序，而是
      本机有没有脏 tag——那种断言一删 tag 就变，方向还是反的。它自己的牙在
      test_v0350_leak_quarantine_gate.py（一次性 `tmp_path` 仓库，红／绿／判不出三档）；
    - versionName 那条用闸门自己的 `--no-versionname-check`（现成的、既有的口子），
      不靠 monkeypatch，免得把"这条判据还在"这件事测虚了。
    """
    monkeypatch.setattr(gate, "fetch_build", lambda url, timeout: (TAG, "ok"))
    monkeypatch.setattr(gate, "release_note_exists", lambda root, tag: True)
    monkeypatch.setattr(gate, "check_leak_quarantine",
                        lambda root: (True, "ok", "stubbed：本用例只测物料顺序那一格"))
    _pin_seed(gate, monkeypatch, materials["seed"])
    return gate.main(["--expect", TAG, "--no-versionname-check"] + list(extra_argv))


# ---------------------------------------------------------------- 三条主判据


def test_refuses_when_manifest_announces_a_version_with_no_package_on_disk(materials, monkeypatch, capsys):
    """(a) 清单宣告 V、盘上没有 V 那串字节 ⇒ 闸门必须拒绝（就是那次事故的形状）。"""
    gate = _load_gate()
    write_manifest(materials["manifest"], version=VERSION, sha="b" * 64)
    # 盘上什么都没有：故意不建 apk_dir，连目录都不存在。
    code = run_gate(gate, monkeypatch, materials)
    out = capsys.readouterr().out
    assert code == 1, "清单跑在了包前面，闸门却放行了（退出码 %s）：%s" % (code, out)
    assert "apk-materials=REFUSED token=package-missing" in out
    assert "verdict=PASS" not in out
    assert str(Path(materials["apk_dir"]) / (VERSION + ".apk")) in out, "拒绝要说出找的是哪个路径"


def test_refuses_when_package_is_on_disk_but_digest_does_not_match(materials, monkeypatch, capsys):
    """(b) 包在盘上但摘要与清单不符 ⇒ 拒绝；而且整串摘要不许外泄（只报前 8 位）。"""
    gate = _load_gate()
    real_sha = write_apk(materials["apk_dir"], version=VERSION, payload=b"different bytes entirely")
    body = write_manifest(materials["manifest"], version=VERSION, sha="c" * 64)
    assert real_sha != body["sha256"], "反例没造出来：盘上摘要与清单摘要相同，这条测不到不符"
    code = run_gate(gate, monkeypatch, materials)
    out = capsys.readouterr().out
    assert code == 1, "盘上那串字节与清单对不上号，闸门却放行：%s" % out
    assert "apk-materials=REFUSED token=package-digest-mismatch" in out
    assert body["sha256"] not in out and real_sha not in out, "拒绝文案里不该出现整串摘要"


def test_passes_when_package_present_digest_and_version_match(materials, monkeypatch, capsys):
    """(c) 包在盘上、摘要对得上、版本对得上 ⇒ 放行（正控：只测红路会把闸门写成恒红）。"""
    gate = _load_gate()
    sha = write_apk(materials["apk_dir"], version=VERSION, payload=b"the shipped bytes")
    write_manifest(materials["manifest"], version=VERSION, sha=sha)
    code = run_gate(gate, monkeypatch, materials)
    out = capsys.readouterr().out
    assert code == 0, "物料齐、摘要对、版本对，闸门却不放行：%s" % out
    assert "apk-materials=ok" in out
    assert "verdict=PASS" in out


# ---------------------------------------------------------------- 显式退出闸门


def test_backend_only_optout_passes_and_prints_its_own_token(materials, monkeypatch, capsys):
    """(d) 纯后端版：显式带 `--backend-only` ⇒ 放行，并且必须自己喊出来（留痕）。"""
    gate = _load_gate()
    # 这一版的宣告**确实**没落地（清单还停在 0.34.2），盘上也什么都没有：
    # 只有显式选择能过，靠推断过不了（下一条用例锁的就是这一点）。
    write_manifest(materials["manifest"], version=OLD_VERSION, sha="d" * 64)
    code = run_gate(gate, monkeypatch, materials, extra_argv=["--backend-only"])
    out = capsys.readouterr().out
    assert code == 0, "合法的纯后端版被顺序闸卡死了：那这把闸会教人绕过它：%s" % out
    assert "apk-materials=SKIPPED" in out, "走了退出闸门必须打印留痕，不许安静放行"
    assert "--backend-only" in out, "留痕要说清是哪个开关被选了"
    assert "verdict=PASS" in out


def test_skip_is_never_inferred_from_a_stale_manifest(materials, monkeypatch, capsys):
    """**反证**：同一棵树、同一个物料状态，不显式带开关就必须拒绝。

    这条和上一条合起来才是"退出闸门是操作者的选择，不是闸门的猜测"：清单没宣告这一版
    就是清单没宣告这一版，它可能是忘了改，也可能真的是纯后端版——闸门没资格替人判断，
    只能问人。
    """
    gate = _load_gate()
    write_manifest(materials["manifest"], version=OLD_VERSION, sha="d" * 64)
    code = run_gate(gate, monkeypatch, materials)
    out = capsys.readouterr().out
    assert code == 1, "闸门自己推断出了『大概是不发 APK 吧』并放行——事故重演入口：%s" % out
    assert "apk-materials=REFUSED token=manifest-version-mismatch" in out
    assert "--backend-only" in out, "拒绝时要告诉人显式开关叫什么"


def test_allow_unreachable_does_not_swallow_the_materials_gate(materials, monkeypatch, capsys):
    """`--allow-unreachable` 只管可达性，**不许顺手放过物料顺序**（沿用既有那条边界的锁法）。"""
    gate = _load_gate()
    write_manifest(materials["manifest"], version=VERSION, sha="e" * 64)   # 宣告了，但盘上没包
    monkeypatch.setattr(gate, "fetch_build", lambda url, timeout: (None, "unreachable:URLError"))
    monkeypatch.setattr(gate, "release_note_exists", lambda root, tag: True)
    _pin_seed(gate, monkeypatch, materials["seed"])
    code = gate.main(["--expect", TAG, "--no-versionname-check", "--allow-unreachable"])
    out = capsys.readouterr().out
    assert code != 0, "离线逃生门把物料顺序也一起放过了：它只能管可达性"
    assert "apk-materials=REFUSED token=package-missing" in out


# ---------------------------------------------------------------- 拿不到 = 不放行


def test_unreadable_announcement_is_fail_closed_with_code_two(materials, monkeypatch, capsys):
    """宣告侧两份都不存在 ⇒ 拒绝，退出码 2（"判据拿不到"那一档，与线上取不到同档）。"""
    gate = _load_gate()
    # 假清单与假种子都不建；环境变量已钉在 tmp 下，所以这里读不到的是"什么都没有"。
    code = run_gate(gate, monkeypatch, materials)
    out = capsys.readouterr().out
    assert code == 2, "证明不了清单宣告的是哪一版，闸门却放行了：%s" % out
    assert "apk-materials=REFUSED token=manifest-unreadable" in out


@pytest.mark.parametrize("sha,omit,label", [
    ("A" * 64, False, "大写"),
    ("c" * 63, False, "长度不对"),
    ("", False, "空串"),
    (None, True, "整格缺失"),
])
def test_malformed_digest_shape_refuses(materials, monkeypatch, capsys, sha, omit, label):
    """形状关：清单里那串摘要"需要再加工一下才像真的"就不算数（照 `releases._manifest_ok`）。

    四种坏形状都配一个**盘上真的存在、字节也没问题**的包当反证：如果闸门只是在看
    "包在不在"，这四条会全部假绿。
    """
    gate = _load_gate()
    write_apk(materials["apk_dir"], version=VERSION)
    write_manifest(materials["manifest"], version=VERSION, sha=sha, omit_sha=omit)
    code = run_gate(gate, monkeypatch, materials)
    out = capsys.readouterr().out
    assert code == 2, "%s的摘要形状本该直接拒绝（这是'证明不了'那一档）：%s" % (label, out)
    assert "形状不对" in out, "%s：拒绝的理由没落在形状上，可能只是在报'包不在'：%s" % (label, out)


def test_broken_runtime_manifest_does_not_fall_back_to_a_perfect_seed(materials, monkeypatch, capsys):
    """运行时那份读不动时**不许**悄悄回落种子——那等于用另一版冒充"最新"。

    与 `releases.load_manifest` 的优先级逐字对齐：种子那份是完全健康、物料齐备的，
    照样不该被采纳。反例就在这条里：种子好、包在盘上、摘要对得上。
    """
    gate = _load_gate()
    sha = write_apk(materials["apk_dir"], version=VERSION)
    write_manifest(materials["seed"], version=VERSION, sha=sha)        # 好种子
    Path(materials["manifest"]).parent.mkdir(parents=True, exist_ok=True)
    Path(materials["manifest"]).write_text("{ this is not json", encoding="utf-8")   # 坏运行时
    code = run_gate(gate, monkeypatch, materials)
    out = capsys.readouterr().out
    assert code == 2, "运行时清单坏了却回落到种子放行：优先级被改了：%s" % out
    assert "运行时清单读不出来" in out
    assert "apk-materials=REFUSED token=manifest-unreadable" in out


def test_seed_is_used_when_runtime_manifest_absent(materials, monkeypatch, capsys):
    """正控（与上一条成对）：运行时那份不存在时，闸门与运行时一样采纳种子。"""
    gate = _load_gate()
    sha = write_apk(materials["apk_dir"], version=VERSION)
    write_manifest(materials["seed"], version=VERSION, sha=sha)
    code = run_gate(gate, monkeypatch, materials)
    out = capsys.readouterr().out
    assert code == 0, "种子物料齐备时闸门不放行，纯后端版之外的每次发版都会被白卡：%s" % out
    assert "apk-materials=ok" in out
    assert str(Path(materials["seed"])) in out, "要说出用的是哪一份，人才能复核"


# ---------------------------------------------------------------- 与运行时同判据


@pytest.mark.parametrize("disk_payload,manifest_sha_of_disk", [
    (b"the shipped bytes", True),
    (b"tampered bytes", False),
])
def test_gate_verdict_matches_the_runtime_verdict(materials, monkeypatch, disk_payload,
                                                 manifest_sha_of_disk):
    """闸门说的"能不能宣告"必须与**运行时**那句 `cached_file_state` 同一个答案。

    两边喂同一份假盘（同一个 `APK_CACHE_DIR`）、同一个版本号、同一串摘要：闸门放行
    的时刻恰好就是运行时愿意端出字节的时刻。少这一条，闸门就是在自报一套判据。
    """
    from app.core import apk_cache

    gate = _load_gate()
    actual = write_apk(materials["apk_dir"], version=VERSION, payload=disk_payload)
    declared = actual if manifest_sha_of_disk else ("f" * 64)
    write_manifest(materials["manifest"], version=VERSION, sha=declared)

    ok, token, detail = gate.check_announcement_materials(str(materials["root"]), VERSION)
    runtime_ok, runtime_why = apk_cache.cached_file_state(VERSION, declared)
    assert bool(runtime_ok) == manifest_sha_of_disk, "反例没造出来：运行时那侧没按预期分开"
    assert ok is manifest_sha_of_disk, "闸门与运行时答案不一致：%s / %s" % (token, runtime_why)
    assert (ok and token == "ok") or (not ok and token == "package-digest-mismatch")
    assert detail, "判定必须带一句可复核的说明"


def test_gate_reads_the_same_two_env_hooks_the_runtime_does(materials, monkeypatch):
    """落点一致性：`RELEASE_MANIFEST_PATH` / `APK_CACHE_DIR` 的语义与运行时同名同义。"""
    gate = _load_gate()
    assert gate.MANIFEST_ENV_VAR == "RELEASE_MANIFEST_PATH"
    assert gate.APK_CACHE_ENV_VAR == "APK_CACHE_DIR"
    assert gate.runtime_manifest_path(str(REPO_ROOT)) == os.path.abspath(str(materials["manifest"]))
    want = _abspath(materials["apk_dir"], VERSION + ".apk")
    assert gate.package_path_for(str(REPO_ROOT), VERSION) == want
    # 版本形状不认识 ⇒ 拼不出路径 ⇒ 调用方按"没有可用的包"处理（照 apk_cache.path_for）。
    assert gate.package_path_for(str(REPO_ROOT), "0.35.0-beta") == ""


def test_pinned_paths_never_point_at_the_real_repo_data(materials, monkeypatch, capsys):
    """卫生锁：整个文件跑的时候，两个口子都不许指到仓库里那两个真落点。

    这条不是形式：`RELEASE_MANIFEST_PATH` 就是清单的覆写口子，测试里最容易出的错就是
    忘了钉它，然后某条用例把假的 `data/version.json` 写到了**线上那句公开宣告**上。
    """
    gate = _load_gate()
    assert os.environ["RELEASE_MANIFEST_PATH"] == str(materials["manifest"])
    assert os.environ["APK_CACHE_DIR"] == str(materials["apk_dir"])
    sha = write_apk(materials["apk_dir"], version=VERSION)
    write_manifest(materials["manifest"], version=VERSION, sha=sha)
    code = run_gate(gate, monkeypatch, materials)
    out = capsys.readouterr().out
    assert code == 0
    for real in (str(REAL_MANIFEST), str(REAL_APK_DIR), str(REAL_SEED)):
        assert real not in out, "闸门的路径判据落到了真仓库数据上：%s" % real


def test_the_materials_gate_is_on_by_default(materials, monkeypatch, capsys):
    """默认值不许被新判据顺手改掉：**不带任何新开关时顺序闸就在工作**。

    机器问法两问：① 物料不齐 + 不带新开关 ⇒ 真退出码非零（默认是开着的）；
    ② 带一个闸门不认识的名字 ⇒ argparse 自己退码 2（证明 `--backend-only` 是**具名**
    开关、拼错不会被当成"没带"而静默跳过）。
    """
    gate = _load_gate()
    write_manifest(materials["manifest"], version=VERSION, sha="2" * 64)   # 物料不齐
    code = run_gate(gate, monkeypatch, materials)                          # 不带任何新开关
    assert code == 1, "顺序闸变成默认关闭了：那它拦不住任何东西"
    assert "apk-materials=REFUSED token=package-missing" in capsys.readouterr().out

    with pytest.raises(SystemExit) as boom:
        gate.main(["--expect", TAG, "--no-versionname-check", "--backend-onlyy"])
    assert boom.value.code == 2, "拼错的开关被静默忽略了：显式选择就变成了可以靠猜"


def test_backend_only_does_not_authorize_an_announcement_that_ran_ahead(materials, monkeypatch, capsys):
    """显式退出闸门**不是事故的重演入口**：清单已经把这一版宣告出去、盘上没那串字节时，
    带了 `--backend-only` 也必须红——它授权的是"不发新 APK"，不是"宣告可以跑在包前面"。
    """
    gate = _load_gate()
    write_manifest(materials["manifest"], version=VERSION, sha="3" * 64)   # 宣告了 TAG
    code = run_gate(gate, monkeypatch, materials, extra_argv=["--backend-only"])
    out = capsys.readouterr().out
    assert code == 1, "--backend-only 能授权『清单已宣告但包不在盘上』：那把闸等于没有：%s" % out
    assert "apk-materials=REFUSED token=package-missing" in out
    assert "apk-materials=SKIPPED" not in out
    assert "--backend-only" in out, "要说清是这个开关没能覆盖这一格"



# ------------------------------------------- 既有四条判据的顺序与语义没被动过


def test_existing_checks_still_fire_before_the_materials_gate(materials, monkeypatch, capsys):
    """(e) 回归锁：前面四条判据一条没少、顺序没变——物料坏的时候它们各自先红。

    判据是"真跑 main() 看先撞哪一条"，不是数源码里的注释序号。改的正是这把守发版的
    闸门，所以它原有的格子必须一格一格原地还在：清单/包是**最后**一条。
    """
    gate = _load_gate()
    write_manifest(materials["manifest"], version=VERSION, sha="1" * 64)   # 盘上没包：第 5 条必红

    # 第 2 条（发版说明）先红 ⇒ 输出里根本不该出现顺序闸的字。
    monkeypatch.setattr(gate, "fetch_build", lambda url, timeout: (TAG, "ok"))
    monkeypatch.setattr(gate, "release_note_exists", lambda root, tag: False)
    _pin_seed(gate, monkeypatch, materials["seed"])
    code = gate.main(["--expect", TAG, "--no-versionname-check"])
    out = capsys.readouterr().out
    assert code != 0 and "release-note-missing" in out, "第 2 条不先红了：%s" % out
    assert "apk-materials" not in out, "顺序闸抢到了第 2 条前面"

    # 第 3 条（线上 build 错位）先红。
    monkeypatch.setattr(gate, "release_note_exists", lambda root, tag: True)
    monkeypatch.setattr(gate, "fetch_build", lambda url, timeout: ("v0.34.2", "ok"))
    code = gate.main(["--expect", TAG, "--no-versionname-check"])
    out = capsys.readouterr().out
    assert code != 0 and "live-build-mismatch" in out, "第 3 条不先红了：%s" % out
    assert "apk-materials" not in out

    # 第 1 条（versionName）先红：这条要**不**带 --no-versionname-check 才走得到。
    monkeypatch.setattr(gate, "fetch_build", lambda url, timeout: (TAG, "ok"))
    monkeypatch.setattr(gate, "declared_version_name", lambda root: ("0.34.2", "ok"))
    code = gate.main(["--expect", TAG])
    out = capsys.readouterr().out
    assert code != 0 and "versionname-mismatch" in out, "第 1 条不先红了：%s" % out
    assert "apk-materials" not in out

    # 第 4 条（本机 build）先红：顺序闸在它之后，所以带 --check-local 时也轮不到第 5 条。
    monkeypatch.setattr(gate, "declared_version_name", lambda root: (VERSION, "ok"))

    def _fetch(url, timeout):
        return ("v0.34.2" if "127.0.0.1" in url else TAG), "ok"

    monkeypatch.setattr(gate, "fetch_build", _fetch)
    code = gate.main(["--expect", TAG, "--check-local"])
    out = capsys.readouterr().out
    assert code != 0 and "local-build-mismatch" in out, "第 4 条不先红了：%s" % out
    assert "apk-materials" not in out

    # 四条全过时，第 5 条才第一次开口——证明它是**追加**而不是替换。
    monkeypatch.setattr(gate, "fetch_build", lambda url, timeout: (TAG, "ok"))
    code = gate.main(["--expect", TAG])
    out = capsys.readouterr().out
    assert code == 1 and "apk-materials=REFUSED token=package-missing" in out, (
        "四条旁证都过了、物料却没齐，闸门仍然放行：%s" % out)
    for line in ("PRETAG versionname=ok", "PRETAG release-note=ok", "PRETAG live-build=ok"):
        assert line in out, "既有判据的产出被改写了：%s" % line
