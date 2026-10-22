from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
UI = REPO / "android-native/app/src/main/java/xyz/fenever/assistant/nativeapp/ui"


def _t(name):
    return (UI / name).read_text(encoding="utf-8")


def test_component_primitives_exist():
    src = _t("FenverComponents.kt")
    for name in ("FenverSurface", "FenverRow", "FenverButton", "FenverTextField",
                 "FenverTopBar", "FenverSheet", "FenverChip", "FenverCard", "BrandText"):
        assert "fun " + name in src, name


def test_chat_uses_flat_surfaces_and_stable_keys():
    src = _t("ChatUi.kt")
    assert "itemsIndexed(card.msgs, key =" in src  # v0.36.1 动效批：列表画的是转场快照
    assert "userBubbleBrush(" not in src, "用户气泡还在用渐变"
    assert "FenverSurface(" in src
