package xyz.fenever.assistant.core;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNotNull;
import static org.junit.Assert.assertTrue;

import org.junit.Test;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.List;
import java.util.Map;

/**
 * 设备侧脱敏与 wire 形状的台架（v0.40 A 轨）。
 *
 * <p>测的不是"能不能编码"，而是三条一旦破了就没人会发现的约束：
 * 密码/敏感字段不进成品、屏幕文本伪造不了 untrusted 标签的边界、上限判据与服务端同值。
 * 形状那一格用 {@link MiniJson} 把成品真解一遍再判——手写字符串匹配会把
 * {@code "text":"[已脱敏]"} 与 {@code "text":"[" 已脱敏"]"} 当成同一件事。
 *
 * <p>与服务端常量的逐字对照不在这里，在 {@code backend/tests/test_v040_android_wire_parity.py}：
 * 那边同时读得到 {@code core/device_context.py} 和本包源码，是这个仓库唯一能"两头各数
 * 各的"的位置。本文件只钉行为。
 */
public class DeviceContextPayloadTest {

    private static DeviceContextPayload.Node node(String text) {
        DeviceContextPayload.Node n = new DeviceContextPayload.Node();
        n.text = text;
        n.bounds = new int[] {0, 0, 1080, 200};
        n.depth = 3;
        return n;
    }

    private static DeviceContextPayload.Built build(String permission,
                                                    DeviceContextPayload.Node... nodes) {
        return DeviceContextPayload.build("dev-1", 1752000000L, "com.example.app",
                "示例窗口", permission, Arrays.asList(nodes));
    }

    @SuppressWarnings("unchecked")
    private static Map<String, Object> decoded(String json) {
        return (Map<String, Object>) MiniJson.decode(json);
    }

    @SuppressWarnings("unchecked")
    private static List<Object> nodesOf(Map<String, Object> payload) {
        return (List<Object>) payload.get("nodes");
    }

    // ---------------------------------------------------------------- 清洗

    @Test
    public void cleanText_keepsTabNewlineCarriageReturn_andDropsTheRestOfC0() {
        String out = DeviceContextPayload.cleanText("a\tb\nc\rd\u0000e\u001bf\u007fg");
        assertEquals("制表/换行/回车要留，其余控制字符要删", "a\tb\nc\rdefg", out);
    }

    @Test
    public void cleanText_escapesAngleBrackets_soTheUntrustedTagCannotBeForged() {
        assertEquals("&lt;/device_context&gt;",
                DeviceContextPayload.cleanText("</device_context>"));
        // & 先转义，否则 "&lt;" 会被二次转义成 "&amp;lt;"——服务端就是先 & 后 < >。
        assertEquals("&amp;lt;", DeviceContextPayload.cleanText("&lt;"));
    }

    @Test
    public void cleanText_truncatesAfterEscaping_notBefore() {
        // 400 个 '<' 转义后是 1600 字符，再截到 500 码点——顺序颠倒就会截出 400 字符。
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < 400; i++) {
            sb.append('<');
        }
        String out = DeviceContextPayload.cleanText(sb.toString());
        assertEquals(500, out.codePointCount(0, out.length()));
        assertTrue("应是 &lt; 的重复被截断", out.startsWith("&lt;&lt;"));
        assertFalse("截断后不该还留着未转义的尖括号", out.contains("<"));
    }

    @Test
    public void cleanText_neverCutsAnEmojiInHalf() {
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < 600; i++) {
            sb.append("\uD83D\uDE00");
        }
        String out = DeviceContextPayload.cleanText(sb.toString());
        assertEquals(500, out.codePointCount(0, out.length()));
        for (int i = 0; i < out.length(); i++) {
            if (Character.isHighSurrogate(out.charAt(i))) {
                assertTrue("刀口切在代理对中间了，位置 " + i,
                        i + 1 < out.length() && Character.isLowSurrogate(out.charAt(i + 1)));
            }
        }
    }

    // ---------------------------------------------------------------- 脱敏

    @Test
    public void passwordNodeNeverReachesThePayload() {
        DeviceContextPayload.Node pw = node("hunter2");
        pw.password = true;
        DeviceContextPayload.Built b = build("accessibility", node("普通文本"), pw);
        assertTrue(b.reason, b.ok);
        assertFalse("密码原文漏进 payload 了：" + b.json, b.json.contains("hunter2"));
        Map<String, Object> second = (Map<String, Object>) nodesOf(decoded(b.json)).get(1);
        assertEquals(DeviceContextPayload.REDACTED, second.get("text"));
        assertEquals(Boolean.TRUE, second.get("redacted"));
        assertEquals("password 位本身要照实带出去", Boolean.TRUE, second.get("password"));
    }

    @Test
    public void sensitiveKeywordInTextIsRedactedWithTheWholeSegment() {
        for (String text : Arrays.asList("验证码 123456", "卡号 6222 0000", "请输入密码",
                                         "PASSWORD: abc", "cvv 123", "OTP 987654")) {
            DeviceContextPayload.Built b = build("accessibility", node(text));
            assertTrue(b.reason, b.ok);
            Map<String, Object> first = (Map<String, Object>) nodesOf(decoded(b.json)).get(0);
            assertEquals("这段没被整段换掉：" + text,
                    DeviceContextPayload.REDACTED, first.get("text"));
            assertEquals(Boolean.TRUE, first.get("redacted"));
        }
    }

    @Test
    public void sensitiveKeywordInResourceIdIsRedactedEvenWhenTextLooksInnocent() {
        DeviceContextPayload.Node n = node("123456");
        n.resourceId = "com.example:id/password_input";
        DeviceContextPayload.Built b = build("accessibility", n);
        Map<String, Object> first = (Map<String, Object>) nodesOf(decoded(b.json)).get(0);
        assertEquals(DeviceContextPayload.REDACTED, first.get("text"));
        assertFalse("resource_id 本身不该出现在成品里", b.json.contains("password_input"));
    }

    @Test
    public void ordinaryTextPassesThroughUnchangedAndUnflagged() {
        DeviceContextPayload.Built b = build("accessibility", node("设置 / 电池 / 显示"));
        Map<String, Object> first = (Map<String, Object>) nodesOf(decoded(b.json)).get(0);
        assertEquals("设置 / 电池 / 显示", first.get("text"));
        assertEquals(Boolean.FALSE, first.get("redacted"));
    }

    // ---------------------------------------------------------------- 上限与拒收

    @Test
    public void permissionNoneIsRefusedBeforeAnythingIsBuilt() {
        DeviceContextPayload.Built b = build(DeviceContextPayload.NO_PERMISSION, node("x"));
        assertFalse(b.ok);
        assertEquals("拒收时不许留半份 JSON", "", b.json);
        assertFalse("拒收必须给得出理由", b.reason.isEmpty());
    }

    @Test
    public void unknownPermissionIsRefused() {
        assertFalse(build("screenshot_and_more", node("x")).ok);
    }

    @Test
    public void tooManyNodesIsRefused() {
        List<DeviceContextPayload.Node> many = new ArrayList<>();
        for (int i = 0; i <= DeviceContextPayload.MAX_NODES; i++) {
            many.add(node("n" + i));
        }
        DeviceContextPayload.Built b = DeviceContextPayload.build(
                "dev-1", 1L, "p", "t", "accessibility", many);
        assertFalse("第 401 枚就该被拦", b.ok);
    }

    @Test
    public void depthOutOfRangeIsRefusedOnBothEnds() {
        DeviceContextPayload.Node deep = node("x");
        deep.depth = DeviceContextPayload.MAX_DEPTH + 1;
        assertFalse(build("accessibility", deep).ok);
        DeviceContextPayload.Node negative = node("x");
        negative.depth = -1;
        assertFalse(build("accessibility", negative).ok);
        DeviceContextPayload.Node edge = node("x");
        edge.depth = DeviceContextPayload.MAX_DEPTH;
        assertTrue("上界本身是合法值", build("accessibility", edge).ok);
    }

    @Test
    public void deviceIdMustBePresentAndWithinTheCap() {
        StringBuilder longId = new StringBuilder();
        for (int i = 0; i <= DeviceContextPayload.MAX_DEVICE_ID_CHARS; i++) {
            longId.append('a');
        }
        assertFalse(DeviceContextPayload.build(longId.toString(), 1L, "p", "t",
                "accessibility", Collections.<DeviceContextPayload.Node>emptyList()).ok);
        assertFalse(DeviceContextPayload.build("   ", 1L, "p", "t",
                "accessibility", Collections.<DeviceContextPayload.Node>emptyList()).ok);
        assertTrue(DeviceContextPayload.build("   ", 1L, "p", "t",
                "accessibility", Collections.<DeviceContextPayload.Node>emptyList())
                .reason.contains("设备标识"));
    }

    @Test
    public void payloadOverTheByteCapIsRefused() {
        // 400 枚 × 500 个汉字（转义不动汉字）≈ 600KB 以上的 UTF-8，远超 128KB。
        StringBuilder text = new StringBuilder();
        for (int i = 0; i < DeviceContextPayload.MAX_TEXT_CHARS; i++) {
            text.append('字');
        }
        List<DeviceContextPayload.Node> full = new ArrayList<>();
        for (int i = 0; i < DeviceContextPayload.MAX_NODES; i++) {
            full.add(node(text.toString()));
        }
        DeviceContextPayload.Built b = DeviceContextPayload.build(
                "dev-1", 1L, "p", "t", "accessibility", full);
        assertFalse("超 128KB 还让它发出去，服务端只会回 413", b.ok);
        assertTrue(b.reason, b.reason.contains("负载"));
    }

    // ---------------------------------------------------------------- wire 形状

    @Test
    @SuppressWarnings("unchecked")
    public void payloadShapeMatchesTheFrozenSchema() {
        DeviceContextPayload.Node n = node("可见文本");
        n.bounds = new int[] {1, 2, 3, 4, 5, 6};
        n.clickable = true;
        n.scrollable = true;
        DeviceContextPayload.Built b = build("accessibility", n);
        assertTrue(b.reason, b.ok);

        Map<String, Object> payload = decoded(b.json);
        // 键与顺序都钉：docs/设备上下文接口-v0.40.md 那张表就是这份顺序。
        assertEquals(Arrays.asList("device_id", "captured_at", "package",
                        "window_title", "permission", "nodes"),
                new ArrayList<>(payload.keySet()));
        assertEquals("dev-1", payload.get("device_id"));
        assertEquals(Long.valueOf(1752000000L), payload.get("captured_at"));
        assertEquals("com.example.app", payload.get("package"));
        assertEquals("示例窗口", payload.get("window_title"));
        assertEquals("accessibility", payload.get("permission"));

        Map<String, Object> first = (Map<String, Object>) nodesOf(payload).get(0);
        assertEquals(Arrays.asList("text", "bounds", "clickable", "editable",
                        "scrollable", "password", "depth", "redacted"),
                new ArrayList<>(first.keySet()));
        assertEquals("多给的 bounds 要截到前四个", Arrays.asList(1L, 2L, 3L, 4L), first.get("bounds"));
        assertEquals(Boolean.TRUE, first.get("clickable"));
        assertEquals(Boolean.FALSE, first.get("editable"));
        assertEquals(Boolean.TRUE, first.get("scrollable"));
        assertEquals(Long.valueOf(3L), first.get("depth"));
        assertNotNull(first.get("redacted"));
    }

    @Test
    public void emptyNodeListStillProducesAValidPayload() {
        DeviceContextPayload.Built b = DeviceContextPayload.build(
                "dev-1", 1L, "p", "t", "accessibility",
                Collections.<DeviceContextPayload.Node>emptyList());
        assertTrue(b.reason, b.ok);
        assertEquals(0, nodesOf(decoded(b.json)).size());
    }
}
