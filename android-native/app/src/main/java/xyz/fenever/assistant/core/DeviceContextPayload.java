package xyz.fenever.assistant.core;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.regex.Pattern;

/**
 * 设备上下文（只读）的**设备侧**口径：本地脱敏、上限、wire JSON（v0.40 A 轨）。
 *
 * <p>为什么收在 core 而不是直接写在无障碍服务里：设备侧这一层是"第一道脱敏"，
 * 服务端 {@code core/device_context.py} 是第二道。两边要判同一件事——哪些文本算敏感、
 * 截到多长、节点数/深度/字节数的上限是多少——如果各写一份，漂移时没有任何测试会红，
 * 而这类漂移的后果是"服务端以为客户端已经删干净了"。所以：纯 JVM、无 android 依赖，
 * 一份 Java 实现 + 一份 JUnit 台架（CI 的 {@code testDebugUnitTest} 会跑），
 * 再加后端一条两面对照锁（{@code backend/tests/test_v040_android_wire_parity.py}）
 * 逐字比常量、比关键词表、比 JSON 键名。同 {@link ExportName} 的接缝纪律。
 *
 * <p>镜像的行为（逐条对位 {@code _clean_text} 与 {@code _redact_node}）：
 * <ol>
 *   <li>控制字符先删（保留 \t \n \r），再把 {@code & < >} 实体化，
 *       <b>最后</b>才截到 500 个码点——顺序不能颠倒：先截再转义会让一条 500 字的
 *       文本膨胀成一两千字节，而服务端是在转义之后截的；</li>
 *   <li>敏感判定吃的是<b>转义后</b>的文本与 {@code resource_id}，命中就把整段换成
 *       {@code [已脱敏]}——留一半反而更容易把原值拼回来；</li>
 *   <li>节点出口只有 8 个键，<b>不含 resource_id</b>：它只用于敏感判定，不往外送。</li>
 * </ol>
 *
 * <p>射程边界（别读成"设备侧全都验过了"）：本类只判形状与脱敏，不采集。
 * 真正的节点树来自 AccessibilityService，采集侧一行 {@code performAction} 都不许有
 * （锁：{@code backend/tests/test_v040_android_read_only.py} 扫整棵 android 树）。
 *
 * <p>既有跨端不对称（先于本轮存在，只登记不改）：
 * <ul>
 *   <li>device_id 长度：这里 {@code id.length()} 数 UTF-16 单元，服务端 pydantic 数码点，
 *       含增补平面字符时两端长度判定不一致；</li>
 *   <li>package 长度：服务端 pydantic ≤200，这里无 200 闸（只有 {@code cleanText} 的 500 截断）。</li>
 * </ul>
 */
public final class DeviceContextPayload {

    private DeviceContextPayload() {
    }

    // ---------- 与服务端逐字同值的上限（backend/app/core/device_context.py） ----------

    /**
     * 单次负载上限。两端量法不同（登记为可接受残留，本轮不对齐）：服务端在**脱敏前**
     * 的 model_dump 上量，这里量的是**脱敏组装后**的成品。同一条临界输入可能这里放行、
     * 服务端仍回 413。方向 fail-safe：服务端是权威闸且未动，超大负载进不了暂存。
     */
    public static final int MAX_PAYLOAD_BYTES = 128 * 1024;

    /** 节点数上限。 */
    public static final int MAX_NODES = 400;

    /** 节点深度上限（含）。 */
    public static final int MAX_DEPTH = 30;

    /** 单枚节点文本出口长度（码点数，转义之后截）。 */
    public static final int MAX_TEXT_CHARS = 500;

    /** 设备标识长度上限。 */
    public static final int MAX_DEVICE_ID_CHARS = 64;

    /** 服务端暂存 TTL（秒）。设备侧状态条与"再过多久要重报"按这个数显示。 */
    public static final int TTL_SECONDS = 300;

    /** 脱敏后替换进去的整段占位符，与服务端同字。 */
    public static final String REDACTED = "[已脱敏]";

    /** 未授予任何权限：服务端一律拒收，客户端也不该发。 */
    public static final String NO_PERMISSION = "none";

    /** 服务端认的权限取值，其余一律 403。 */
    public static final Set<String> VALID_PERMISSIONS = Collections.unmodifiableSet(
            new LinkedHashSet<>(Arrays.asList("accessibility", "notification",
                                              "clipboard", "screenshot")));

    // ---------- 敏感词与清洗 ----------

    /**
     * 与服务端 {@code _SENSITIVE_RE} 同一份关键词表（锁逐条比）。
     *
     * <p>{@code (?i)} 只管 ASCII 大小写，{@code UNICODE_CASE} 把土耳其无点 i 那一类
     * 也拉齐——Python 的 {@code re.IGNORECASE} 在 str 上是 Unicode 感知的，
     * 只写 {@code (?i)} 会让 {@code PASSWORD} 的某些非常规写法两边判得不一样。
     */
    private static final Pattern SENSITIVE = Pattern.compile(
            "(?i)(密码|口令|验证码|动态码|卡号|身份证|password|passwd|otp|cvv|card.?no|id.?card)",
            Pattern.UNICODE_CASE);

    /** C0 控制字符（保留 \t \n \r）。 */
    private static final Pattern CONTROL =
            Pattern.compile("[\\x00-\\x08\\x0B\\x0C\\x0E-\\x1F\\x7F]");

    /** package 形状闸（与服务端 {@code _PACKAGE_RE} 逐字同值）：至少两段、字母开头。 */
    private static final Pattern PACKAGE_SHAPE = Pattern.compile(
            "^[A-Za-z][A-Za-z0-9_]*(\\.[A-Za-z][A-Za-z0-9_]*)+$");

    /**
     * 控制字符删掉 → {@code & < >} 实体化 → 截到 {@link #MAX_TEXT_CHARS} 码点。
     *
     * <p>实体化是为了让屏幕上的 {@code </device_context>} 变成
     * {@code &lt;/device_context&gt;}，伪造不了服务端那层 untrusted 标签的边界。
     */
    public static String cleanText(String raw) {
        String s = raw == null ? "" : raw;
        s = CONTROL.matcher(s).replaceAll("");
        s = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;");
        return limitCodePoints(s, MAX_TEXT_CHARS);
    }

    /** 敏感判定：密码位、resource_id 命中、或（已清洗的）文本命中，三选一即敏感。 */
    public static boolean isSensitive(String resourceId, String cleanedText, boolean password) {
        if (password) {
            return true;
        }
        if (resourceId != null && SENSITIVE.matcher(resourceId).find()) {
            return true;
        }
        return cleanedText != null && SENSITIVE.matcher(cleanedText).find();
    }

    /**
     * device_id / window_title 的出口谓词：敏感词（与服务端 {@code _redact_field} 同口径）。
     * 判定吃清洗后的文本、与节点树同一份词表，命中整段换 {@link #REDACTED}。
     */
    public static String fieldText(String raw) {
        String cleaned = cleanText(raw);
        return SENSITIVE.matcher(cleaned).find() ? REDACTED : cleaned;
    }

    /**
     * package 的出口谓词：形状闸（与服务端 {@code _package_field} 同口径）。
     * 不走敏感词——密码管理器/银行 app 的合法包名里就有 password/otp，
     * 整段脱敏会打掉头部用例；形状不合法才换 {@link #REDACTED}。
     * 空串没有包名可脱敏，渲染为空而非 {@link #REDACTED}（后者是对模型说假话）。
     * 写成单条三元、不引入分支：本体判据禁分支，死代码掏空会把真脱敏藏进条件块。
     */
    public static String packageText(String raw) {
        String cleaned = cleanText(raw);
        return PACKAGE_SHAPE.matcher(cleaned).matches() || cleaned.isEmpty() ? cleaned : REDACTED;
    }

    private static String limitCodePoints(String s, int limit) {
        if (s.codePointCount(0, s.length()) <= limit) {
            return s;
        }
        return s.substring(0, s.offsetByCodePoints(0, limit));
    }

    // ---------- 采集侧交给本类的形状 ----------

    /** 一枚节点。字段与 AccessibilityNodeInfo 上真要读的那几项一一对应，不多收。 */
    public static final class Node {
        public String text = "";
        /** 屏幕矩形：left, top, right, bottom。多给的会被截到前四个。 */
        public int[] bounds = new int[0];
        public boolean clickable;
        public boolean editable;
        public boolean scrollable;
        public boolean password;
        public int depth;
        public String resourceId = "";
    }

    /** 构造结果：{@code ok=false} 时 {@code reason} 说清为什么不该发，{@code json} 为空。 */
    public static final class Built {
        public final boolean ok;
        public final String json;
        public final String reason;

        private Built(boolean ok, String json, String reason) {
            this.ok = ok;
            this.json = json;
            this.reason = reason;
        }
    }

    private static Built refuse(String reason) {
        return new Built(false, "", reason);
    }

    /**
     * 校验上限 → 逐节点脱敏 → 出 wire JSON。
     *
     * <p>判据顺序与服务端 {@code ingest()} 对齐（权限 → 节点数 → 深度 → 字节数），
     * 这样同一条坏快照在两端被拒的理由是同一个，排障时不用猜是哪一层先拦的。
     * 拒绝时**一个字都不发**：宁可让用户看到"这次没上传"，也不让半份快照过去。
     */
    public static Built build(String deviceId, long capturedAt, String pkg, String windowTitle,
                              String permission, List<Node> nodes) {
        String id = deviceId == null ? "" : deviceId.trim();
        if (id.isEmpty() || id.length() > MAX_DEVICE_ID_CHARS) {
            return refuse("设备标识为空或过长（上限 " + MAX_DEVICE_ID_CHARS + " 字符）");
        }
        String perm = permission == null ? NO_PERMISSION : permission;
        if (!VALID_PERMISSIONS.contains(perm)) {
            return refuse("设备侧未授予有效权限，这次不上传");
        }
        List<Node> list = nodes == null ? Collections.<Node>emptyList() : nodes;
        if (list.size() > MAX_NODES) {
            return refuse("节点数超过上限（" + list.size() + " > " + MAX_NODES + "）");
        }
        for (Node n : list) {
            if (n.depth < 0 || n.depth > MAX_DEPTH) {
                return refuse("节点深度超过上限（" + n.depth + " 不在 0–" + MAX_DEPTH + "）");
            }
        }

        List<Object> outNodes = new ArrayList<>(list.size());
        for (Node n : list) {
            outNodes.add(nodeMap(n));
        }
        Map<String, Object> payload = new LinkedHashMap<>();
        payload.put("device_id", fieldText(id));
        payload.put("captured_at", capturedAt);
        payload.put("package", packageText(pkg));
        payload.put("window_title", fieldText(windowTitle));
        payload.put("permission", perm);
        payload.put("nodes", outNodes);

        String json = MiniJson.encode(payload);
        int bytes = utf8Length(json);
        if (bytes > MAX_PAYLOAD_BYTES) {
            return refuse("单次负载超过上限（" + bytes + " > " + MAX_PAYLOAD_BYTES + " 字节）");
        }
        return new Built(true, json, "");
    }

    /** 一枚节点出口的形状：8 个键，顺序与服务端 {@code _redact_node} 的 dict 同序。 */
    private static Map<String, Object> nodeMap(Node n) {
        String text = cleanText(n.text);
        boolean sensitive = isSensitive(n.resourceId, text, n.password);
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("text", sensitive ? REDACTED : text);
        List<Object> b = new ArrayList<>(4);
        for (int i = 0; i < n.bounds.length && i < 4; i++) {
            b.add(n.bounds[i]);
        }
        m.put("bounds", b);
        m.put("clickable", n.clickable);
        m.put("editable", n.editable);
        m.put("scrollable", n.scrollable);
        m.put("password", n.password);
        m.put("depth", n.depth);
        m.put("redacted", sensitive);
        return m;
    }

    private static int utf8Length(String s) {
        int bytes = 0;
        for (int i = 0; i < s.length(); ) {
            int cp = s.codePointAt(i);
            bytes += cp < 0x80 ? 1 : cp < 0x800 ? 2 : cp < 0x10000 ? 3 : 4;
            i += Character.charCount(cp);
        }
        return bytes;
    }
}
