package xyz.fenever.assistant.core;

import java.net.MalformedURLException;
import java.net.URL;
import java.util.List;
import java.util.Map;

/**
 * 「检查更新」的全部判断。一行 {@code android.*} 都不 import。
 *
 * <p>为什么单独收一层：这条链路的终点是【让用户装上一个 APK】。判错的代价不是显示错一个数字，
 * 而是装上一个不是我们的东西——所以"取哪个资产、这个地址能不能信、版本号算不算更新"必须能在
 * JVM 台架上被逐条钉住，而不是埋在 Activity 里等真机去撞。
 *
 * <p>三态而不是"有/没有"：读不出来的东西（被网关改了、限流了、GitHub 换了形状）如果和
 * "已经是最新版"回同一句话，用户就永远分不清"没得更"和"检查失败"。这正是本仓反复踩过的
 * "效果没了但不报错"那一类。
 */
public final class ReleasePlan {

    public static final String OWNER = "abonla599";
    public static final String REPO = "fenver";
    /** 改名前的仓名。留着的理由与 {@link #LEGACY_APK_PREFIX} 同一个方向，但这一条更硬：
     *  v0.23 及更早的每一个 Release 都挂在旧仓下，它们的 {@code browser_download_url}
     *  永远长成 {@code /abonla599/ai-assistant/releases/download/…}——把旧仓名从白名单里
     *  摘掉，等于让手里还装着旧版的所有人一夜之间"这一版没有可安装的安卓包"。
     *  两个仓名一起认，与后端 {@code releases.candidate_repos()} 是同一对。 */
    public static final String LEGACY_REPO = "ai-assistant";
    /** 顺序只是可读性：新仓在前。两条路径前缀【都】算官方地址，不存在优先级。 */
    public static final String[] REPOS = {REPO, LEGACY_REPO};

    /**
     * 资产名与 {@code .github/workflows/release-apk.yml} 真实产出的那一个逐字对齐。
     * 2026-09-25 起发的是原生客户端包 {@code ai-assistant-native-<版本>.apk}；
     * v0.24 阶段4 收口起换品牌名 {@code fenver-<版本>.apk}，旧名作为**别名同包**继续发一份。
     *
     * <p>为什么旧名不能直接删：已经装在用户手机上的 0.23.x 壳把 {@link #LEGACY_APK_PREFIX}
     * 写死在它那一版的挑资产、验名、给下载文件起名三处，而这一版改不了它。只发新名的话，
     * 老用户点「检查更新」看到的是"没有可安装资产"——不报错，只是永远升不上来。
     * 所以 {@link #pickAsset} 两趟：新名优先、旧名兜底。
     *
     * <p>这里不再有"去哪问"的地址：2026-09-23 起这条链路的问与取都收在自家服务器
     * （{@code BuildConfig.UPDATE_INFO_URL} / {@code UPDATE_APK_URL}），壳不再直连
     * GitHub 的发布接口——那一跳曾被 ROM 里的下载通道劫持。这一层只负责对
     * 透传回来的 JSON 做判断，所以"全仓（壳源码）不出现任何 GitHub API 地址"
     * 本身成了新的锁。后端拼同一组名字在 {@code releases.ASSET_PREFIXES}，
     * 两边各有一条测试数着工作流写的那一个字面量。
     */
    public static final String APK_PREFIX = "fenver-";
    /** 过渡期别名：老壳唯一认得的前缀，发布流水线与它同包两名一起上传。 */
    public static final String LEGACY_APK_PREFIX = "ai-assistant-native-";
    /** 挑选顺序就是这条数组的顺序：新名在前，只有旧名时旧名兜底。 */
    public static final String[] APK_PREFIXES = {APK_PREFIX, LEGACY_APK_PREFIX};

    /**
     * 透传回来的 JSON 里那个 {@code browser_download_url} 仍然按这一套校验。
     * 壳现在下载走自家服务器钉死的地址、并不用这个 url，留着的理由是纵深防御：
     * 一份连"官方下载地址"都被人改花了的 JSON，本来就不该被当成可信发布信息。
     * GitHub 哪天换了主机的话这里会明确报"地址不在白名单里"，而不是静默放行。
     *
     * <p>官方路径前缀认两条（{@link #REPOS}），因为合法的正式发布确实散在两个仓里：
     * v0.23 及以前在旧仓，v0.24 起在新仓。这里若只留一条，改名那天就是全量断更。
     */
    private static final String ALLOWED_HOST = "github.com";
    private static final String[] ALLOWED_PATH_PREFIXES = allowedPathPrefixes();

    private static String[] allowedPathPrefixes() {
        String[] out = new String[REPOS.length];
        for (int i = 0; i < REPOS.length; i++) {
            out[i] = "/" + OWNER + "/" + REPOS[i] + "/releases/download/";
        }
        return out;
    }

    /**
     * 第二条信任规则（v0.23 T1.5 / R5-AC-3）：**与 APP_URL 同源**且路径【恰好等于】
     * 这一个的那条 URL 也算可信。精确路径不是前缀——这是"同源 + 精确路径"的题面，
     * 也是这条链路唯一的字节出口（{@code web_router.py} 的 {@code GET /site/android.apk}，
     * 与壳 {@code BuildConfig.UPDATE_APK_URL} 钉的是同一条；三处同值由下方测试与
     * backend/tests/test_android_shell.py 各钉各的一段）。
     *
     * <p>信任的是"这条地址与自家服务器同源且只能是那个代取端点"，不是"JSON 里写了
     * 什么 host 都放行"：host 必须逐字符等于传入的 appUrl 的 host（https、不带端口、
     * 不带 userinfo），路径必须整串等于 {@link #SELF_APK_PATH}，query/片段一律不算。
     * 旧的两参 decide 不传 appUrl，行为与只有 GitHub 一条白名单时完全相同——
     * 扩大信任的入口只在新调用方显式交出 APP_URL 的那一刻打开。
     */
    public static final String SELF_APK_PATH = "/site/android.apk";

    private ReleasePlan() {}

    public enum Kind { UP_TO_DATE, AVAILABLE, UNUSABLE }

    public static final class Decision {
        public final Kind kind;
        /** AVAILABLE 时是新版号；UP_TO_DATE 时也是（就是最新那一版）；UNUSABLE 时可能为 null。 */
        public final String version;
        public final String url;
        public final long sizeBytes;
        /** Release 正文，可能为 null。已经按长度截过，够放进一个对话框。 */
        public final String notes;
        /** UNUSABLE 时给人看的那句话；其余为 null。 */
        public final String reason;
        /**
         * 这版安装包内容的 SHA-256（64 位小写十六进制），来自发布正文里由发布流水线
         * 在【签名之后】算好的那一行，由服务端解析后搭车透传。
         *
         * <p>null 的含义是"这份 JSON 没带可信校验值"——缺行、大写、长度不对统统算缺。
         * 调用方（MainActivity）拿着它是两件事：没有它就不许起下载；下完字节算出的
         * 摘要与它不一致就不许起安装页。校验值本身经同一通道回来，防的不是仓库被劫，
         * 防的是下载途中的完整性事故——2026-09-23 那次"第三方下载通道递回来半截残包"
         * 就是没有这道闸时用户自己撞上的。
         */
        public final String sha256;

        private Decision(Kind kind, String version, String url, long sizeBytes,
                         String notes, String reason, String sha256) {
            this.kind = kind;
            this.version = version;
            this.url = url;
            this.sizeBytes = sizeBytes;
            this.notes = notes;
            this.reason = reason;
            this.sha256 = sha256;
        }
    }

    public static String assetName(String version) {
        return APK_PREFIX + version + ".apk";
    }

    /**
     * 给调用方留一个造 UNUSABLE 的口子：连不上、超时这类事实只有 Activity 知道，
     * 但三态的判据必须留在这层，别让它退化成"出错了就当没更新"。
     */
    public static Decision unusable(String reason) {
        return new Decision(Kind.UNUSABLE, null, null, -1L, null, reason, null);
    }

    /**
     * @param currentVersion 本机 {@code BuildConfig.VERSION_NAME}，形如 {@code 0.14}
     * @param releaseJson    {@code BuildConfig.UPDATE_INFO_URL} 透传回来的原样发布 JSON
     *                       （顶层多一个服务端注入的 {@code apk_sha256}，不认识的就当没有）
     */
    public static Decision decide(String currentVersion, String releaseJson) {
        return decide(currentVersion, releaseJson, null);
    }

    /**
     * 带自家来源的判定（T1.5）。{@code appUrl} 传 {@code BuildConfig.APP_URL} 那一族
     * 地址（如 {@code https://ai.fenever.xyz/app/}）：只有此刻起，"同源 + 精确路径"
     * 那条 URL 才升级成可信下载口；传 null 或形状不对等于沿用旧的两条都不认。
     */
    public static Decision decide(String currentVersion, String releaseJson, String appUrl) {
        Object parsed;
        try {
            parsed = MiniJson.decode(releaseJson);
        } catch (RuntimeException e) {
            // 限流页、代理改写的 HTML、空响应都落在这里——不能让它变成"已经是最新版"
            return unusable("发布信息读不出来（不是合法 JSON）");
        }
        if (!(parsed instanceof Map)) return unusable("发布信息不是一个对象");
        Map<?, ?> rel = (Map<?, ?>) parsed;

        if (truthy(rel.get("draft"))) return unusable("最新一版被标成了草稿");
        if (truthy(rel.get("prerelease"))) return unusable("最新一版被标成了预发布");

        String version = normalizeTag(text(rel.get("tag_name")));
        if (version == null) return unusable("发布标签形状不对");
        if (!isNumericVersion(currentVersion)) return unusable("本机版本号形状不对");
        if (compare(version, currentVersion) <= 0) return new Decision(
                Kind.UP_TO_DATE, version, null, -1L, null, null, null);

        Object assets = rel.get("assets");
        if (!(assets instanceof List)) return unusable("发布里没有资产清单");
        Decision picked = pickAsset((List<?>) assets, version, appUrl);
        if (picked == null) {
            // 这句会原样出现在「检查更新失败：…」后面，所以两个找过的名字都得说：
            // 只报新名的话，看到的人（以及以后查这件事的人）会以为旧名那一趟没跑。
            return unusable("这一版没有可安装的安卓包：找过 " + assetName(version)
                    + " 与 " + LEGACY_APK_PREFIX + version + ".apk");
        }
        String body = text(rel.get("body"));
        return new Decision(Kind.AVAILABLE, version, picked.url, picked.sizeBytes,
                truncate(body), null, digestOrNull(text(rel.get("apk_sha256"))));
    }

    /**
     * 只认 64 位小写十六进制，别的一律当"没有"。
     *
     * <p>与后端 {@code releases.APK_SHA256_RE} 认的是同一个形状（发布流水线只写小写），
     * 两边判据的一致性钉在 backend/tests/test_update_channel.py。这里不 trim 不换大小写：
     * 一个需要"再加工一下才像真的"的摘要，恰恰是被动过手脚时最可能出现的形状。
     */
    private static String digestOrNull(String value) {
        if (value == null || value.length() != 64) return null;
        for (int i = 0; i < 64; i++) {
            char c = value.charAt(i);
            if (c < '0' || c > '9') {
                if (c < 'a' || c > 'f') return null;
            }
        }
        return value;
    }

    /**
     * 在资产里找这一版那个包，两趟：先新名 {@code fenver-<version>.apk}，再旧名别名。
     *
     * <p>不按"扩展名是 .apk 就取第一个"办：一次发布可以同时挂着 mapping.txt、别的平台的产物、
     * 或者上一次误传的文件，而这里挑中的东西是要弹给系统去安装的。名字对上版本号顺带钉住了
     * "这个包就是这一版"，链式改错 tag 与资产名时这里会先变红。
     *
     * <p>两趟而不是一把 {@code startsWith}：新壳装新名、老包只挂旧名时兜底装得上，
     * 而"两个都在"的时候必须取新名——那才是这一次改名想让所有人看见的那个名字。
     */
    private static Decision pickAsset(List<?> assets, String version, String appUrl) {
        for (String prefix : APK_PREFIXES) {
            String want = prefix + version + ".apk";
            for (Object item : assets) {
                if (!(item instanceof Map)) continue;
                Map<?, ?> asset = (Map<?, ?>) item;
                if (!want.equals(text(asset.get("name")))) continue;
                String url = text(asset.get("browser_download_url"));
                if (!downloadUrlIsTrusted(url, want, appUrl)) return null;
                return new Decision(Kind.AVAILABLE, version, url, number(asset.get("size")),
                        null, null, null);
            }
        }
        return null;
    }

    /**
     * 校验的是 API 给回来的那个地址，不是我们自己拼的——所以每一项都不省。
     * 两条信任规则并列（T1.5 前只有一条）：GitHub 官方发布路径（精确文件名）
     * 或 与 appUrl 同源 + 路径恰为 {@link #SELF_APK_PATH}。
     */
    static boolean downloadUrlIsTrusted(String raw, String expectedName, String appUrl) {
        if (raw == null || raw.isEmpty()) return false;
        URL url;
        try {
            url = new URL(raw);
        } catch (MalformedURLException e) {
            return false;
        }
        if (!"https".equalsIgnoreCase(url.getProtocol())) return false;
        if (url.getUserInfo() != null) return false;
        if (url.getRef() != null) return false;   // getFile() 不含片段，得单独挡：带 # 的不算那个地址
        String path = url.getPath();
        // 第二条：自家代取端点。host 逐字符对上 appUrl 的 host，路径整串相等；
        // 带 query/fragment/端口的都不在这条规则里（new URL 把 query 从 path 里分出去，
        // 所以还要单独挡一次 getFile 与 getPath 不等的情况）。
        if (sameOriginApk(url, appUrl) && SELF_APK_PATH.equals(path)
                && url.getFile().equals(path)) {
            return true;
        }
        // 第一条（原样保留）：GitHub 官方发布路径 + 资产名精确匹配。
        if (!ALLOWED_HOST.equalsIgnoreCase(url.getHost())) return false;
        if (url.getPort() != -1) return false;                 // 带端口的不是那个下载入口
        if (path == null || !startsWithAny(path)) return false;
        if (path.contains("..")) return false;
        int slash = path.lastIndexOf('/');
        return slash >= 0 && path.length() > slash + 1
                && expectedName.equals(path.substring(slash + 1));
    }

    /** 兼容旧调用与既有测试的两参形状：没有 appUrl 就没有第二条规则。 */
    static boolean downloadUrlIsTrusted(String raw, String expectedName) {
        return downloadUrlIsTrusted(raw, expectedName, null);
    }

    /** 路径以【任意一条】官方前缀开头就算这一项过——两条前缀的来历见 {@link #REPOS}。 */
    private static boolean startsWithAny(String path) {
        for (String prefix : ALLOWED_PATH_PREFIXES) {
            if (path.startsWith(prefix)) return true;
        }
        return false;
    }

    /** url 的主机是否【就是】appUrl 那一族说的主机：https、默认端口、host 精确相等。 */
    private static boolean sameOriginApk(URL url, String appUrl) {
        if (appUrl == null || appUrl.isEmpty()) return false;
        URL self;
        try {
            self = new URL(appUrl);
        } catch (MalformedURLException e) {
            return false;      // 自家来源说不利索，第二条规则整体不启用——宁可窄
        }
        if (!"https".equalsIgnoreCase(self.getProtocol())) return false;
        String selfHost = self.getHost();
        if (selfHost == null || selfHost.isEmpty()) return false;
        if (self.getPort() != -1 || self.getUserInfo() != null) return false;
        String host = url.getHost();
        if (host == null || !host.equalsIgnoreCase(selfHost)) return false;
        return url.getPort() == -1;         // 同源但改了端口就不是那台服务器的那个出口
    }

    /** {@code v0.15} / {@code V0.15} → {@code 0.15}；不是"数字.数字…"的形状就判死。 */
    static String normalizeTag(String tag) {
        if (tag == null) return null;
        String value = tag.trim();
        if (value.startsWith("v") || value.startsWith("V")) value = value.substring(1).trim();
        return isNumericVersion(value) ? value : null;
    }

    private static boolean isNumericVersion(String value) {
        if (value == null || value.isEmpty()) return false;
        boolean digitsSinceDot = false;
        for (int i = 0; i < value.length(); i++) {
            char c = value.charAt(i);
            if (c == '.') {
                if (!digitsSinceDot) return false;         // 开头、连续、结尾的点都不合法
                digitsSinceDot = false;
            } else if (c >= '0' && c <= '9') {
                digitsSinceDot = true;
            } else {
                return false;
            }
        }
        return digitsSinceDot;
    }

    /**
     * 按点分段比数值。
     *
     * <p>不能用字符串比较：{@code "0.9".compareTo("0.10") > 0}，于是 0.9 会被判成比 0.10 新，
     * 而 v0.9 → v0.10 恰好是这种壳真实会走的一步。段数不等时短的那侧补 0。
     */
    public static int compare(String a, String b) {
        String[] left = a.split("\\.");
        String[] right = b.split("\\.");
        int n = Math.max(left.length, right.length);
        for (int i = 0; i < n; i++) {
            long l = i < left.length ? Long.parseLong(left[i]) : 0L;
            long r = i < right.length ? Long.parseLong(right[i]) : 0L;
            if (l != r) return l < r ? -1 : 1;
        }
        return 0;
    }

    private static boolean truthy(Object value) {
        return Boolean.TRUE.equals(value);
    }

    private static String text(Object value) {
        return value instanceof String ? (String) value : null;
    }

    private static long number(Object value) {
        return value instanceof Number ? ((Number) value).longValue() : -1L;
    }

    /** 对话框里放不下整篇发布说明；截断时补一句"完整说明在 Release 页"，不要半句话吊着。 */
    private static String truncate(String body) {
        if (body == null) return null;
        String text = body.trim();
        if (text.isEmpty()) return null;
        if (text.length() <= MAX_NOTES) return text;
        return text.substring(0, MAX_NOTES).trim() + "\n\n…完整说明在 Release 页";
    }

    static final int MAX_NOTES = 900;
}
