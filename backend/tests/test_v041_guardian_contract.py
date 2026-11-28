"""守护的守护（watchdog-guardian）契约锁（2026-11-28，#P0.1）。

为什么要有这个文件：崩溃守护是一个计划任务。`swap-package.ps1` 在停服务之前把它
禁掉（第 3 步），在健康与标记验收**之后**才重新启用（第 8 步）——这个顺序是对的，
一个没验证过的包不该被「保护」。第 8 步也确实会恢复它。

**洞在那两步之间**：换包卡住或被杀掉，就永远走不到第 8 步，守护停在 Disabled——
而一个被禁用的任务没法发现自己被禁用了。

  · 2026-11-13：等价路径挂住，守护停了 16 小时，没有任何东西在看着。
  · 2026-11-27：v0.40.1 换包卡在 Start-Process（没输出重定向），守护停在 Disabled，
    靠人手重新启用。

所以恢复必须住在守护**外面**。`deploy/watchdog-guardian.ps1` 就是那个第二个任务。

这条锁钉三件事：
① 脚本在、纯 ASCII/CRLF（PS 5.1 会把非 ASCII 字节错解码成语法错误——我先写的中文
   注释就是这么被自己的锁抓出来的）；
② 判据形状对：它必须按 **action** 认守护（不是按名字）、必须先读回 Settings.Enabled
   再动手、读不到时**不许动**（fail-closed 方向是「判不出就不碰」，不是「判不出就当它
   没开」）；
③ swap 脚本真的在禁用后写 marker、恢复后删 marker——没有 marker，监护人分不清
   「换包正在进行」和「换包死了、守护没人管」。
"""
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
GUARDIAN = REPO_ROOT / "deploy" / "watchdog-guardian.ps1"
REGISTER = REPO_ROOT / "deploy" / "register-guardian.ps1"
SWAP = REPO_ROOT / "deploy" / "swap-package.ps1"
WRAPPER = REPO_ROOT / "deploy" / "guardian-hidden.vbs"


def _read(p: Path) -> str:
    return p.read_text(encoding="ascii")


def test_the_guardian_and_its_register_and_wrapper_exist():
    for p in (GUARDIAN, REGISTER, WRAPPER):
        assert p.is_file(), f"{p.name} 不在：守护的守护没有载体"


def test_they_are_pure_ascii_and_crlf():
    """PS 5.1 会把非 ASCII 字节错解码成语法错误，这条差点被自己的锁抓两次。"""
    for p in (GUARDIAN, REGISTER):
        raw = p.read_bytes()
        bad = [b for b in raw if b > 127]
        assert not bad, f"{p.name} 出现非 ASCII 字节（前 3 处 {bad[:3]}）：PS 5.1 会整脚本错解码"
        assert raw.count(b"\r\n") == raw.count(b"\n"), f"{p.name} 必须是 CRLF"


def test_the_guard_is_identified_by_action_not_name():
    """按名字认会在改名/换语言时静默失效；action 才是身份（三份脚本同一条规矩）。"""
    text = _read(GUARDIAN)
    assert "wscript.exe" in text and "watchdog-hidden.vbs" in text, \
        "监护人没有按 action 认守护：换掉名字它就找不到要看的东西了"
    assert "$_.Actions.Execute" in text and "$_.Actions.Arguments" in text


def test_enabled_is_read_from_settings_and_unreadable_means_hands_off():
    """Enabled 在 Settings 关联上；读不到必须**不动**，方向不许反。"""
    text = _read(GUARDIAN)
    assert "Settings.Enabled" in text, \
        "读的不是 Settings.Enabled：裸 task.Enabled 在这台机器上静默答 $null"
    assert "settings-enabled-absent" in text, "没有登记「这一格读不到」的那种形状"
    # 读不到时必须是拒绝动手，不是「当成没开去开它」
    assert "readable=no" in text and "not touched" in text, \
        "读不到时的口径不是「不碰」——把「我不知道」当「它没开」= fail-open"


def test_it_reads_back_after_enabling():
    """Enable 调用返回不是证据，必须回读（swap 脚本对同一个 cmdlet 也这么要求）。"""
    text = _read(GUARDIAN)
    assert "enable-did-not-take" in text, "没有回读证明：Enable 返回了就当成功是假的绿"


def test_swap_writes_and_clears_the_in_progress_marker():
    """没有 marker，监护人分不清「换包在跑」与「换包死了」——会把活着的换包打断。"""
    text = _read(SWAP)
    assert "swap-in-progress.marker" in text, "swap 没写进行中标记"
    # 时间线：禁用 → 写 marker → ... → step 8 重新启用 → 删 marker。
    # 写晚了（在启用之后）等于危险窗口没有标记；删早了（在启用之前）等于把还在跑的
    # 换包暴露给监护人。所以两头都判。
    disable_at = text.index("Disable-ScheduledTask")
    marker_write = text.index("swap-in-progress.marker")
    assert marker_write > disable_at, \
        "marker 写在禁用之前：那时换包还没进危险窗口，监护人无从判断"
    # 最后一项 marker 引用是那个 Remove-Item（删）；它必须晚于 step 8 的 Enable。
    marker_clear = text.rindex("swap-in-progress.marker")
    enable_at = text.index("Enable-ScheduledTask -TaskPath $task.TaskPath")
    assert marker_clear > enable_at, \
        "marker 在重新启用之前就被删了：还在跑的换包会被监护人当成死掉的"


def test_a_stale_marker_is_what_licenses_re_enabling():
    """新鲜 marker = 换包在跑，必须让路；陈旧 marker = 换包死了，才许动手。"""
    text = _read(GUARDIAN)
    assert "StaleMinutes" in text, "没有陈旧判据：一次挂掉的换包会让守护永久不被恢复"
    assert "action=wait" in text, "没有「让路」这条分支：新鲜 marker 时也会去抢"
    assert "action=would-enable" in text or "action=enabled" in text


def test_the_register_refuses_without_a_real_guard():
    """找不到守护时它必须拒绝注册，而不是装一个永远报错的空转任务。"""
    text = _read(REGISTER)
    assert "REFUSED" in text, "找不到守护时不拒绝：会注册一个永远空转的任务"
