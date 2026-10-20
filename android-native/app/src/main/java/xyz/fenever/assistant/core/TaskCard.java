package xyz.fenever.assistant.core;

import java.util.Calendar;
import java.util.GregorianCalendar;
import java.util.List;
import java.util.Map;
import java.util.TimeZone;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * 「需要你输入」卡片（v0.35 #99）的纯 JVM 判断核。
 *
 * <p>为什么判断放在 core/：卡片上的每一句话都来自 {@code GET /v1/tasks/{id}} 这一份
 * JSON——问题原文在 {@code resume_cursor.question}、进度在游标里的两个数、能不能按
 * 在 {@code can_resume}。把这些取值写在 Compose 组合里，台架（tools/shell_jvm_tests.py）
 * 一行都跑不到；而"缺一个键就画成第 1/0 步"这类错在真机上很难一眼看出。所以这里只做
 * 三件事：解析、判断、给文案，全部纯函数，不碰网络、不碰 android.*。
 *
 * <p>文本纪律：服务端给的字一律当字面量输出，不拼 HTML、不二次解释。挂起前缀
 * {@link #SUSPEND_PREFIX} 只用来回答"流上这一枪值不值得去读一次任务"——帧上没有任务
 * 身份（见 backend/app/core/streaming.py 那一格），卡片状态从来不由它推断。
 *
 * <p>与网页版（backend/app/web/static/app.js 的 task* 一族）保持同一套文案与同一条
 * 判据：{@code waiting_user} 才显示卡片、{@code can_resume} 才允许提交、404 说"打不开"
 * 而不是"断网"、倒计时只认 {@code waiting_expires_at}（默认窗口下服务端答 None，
 * 于是只说「等你回复」，既不说数字也不说"无限"）。
 */
public final class TaskCard {
    private TaskCard() {}

    public static final String TITLE = "需要你输入";
    public static final String WAITING_TEXT = "等你回复";
    public static final String EXECUTING_TEXT = "执行中";
    /** 非属主与不存在的 id 在服务端是同一句 404；这句话要说"读不到"，不是"断网"。 */
    public static final String GONE_TEXT = "这条任务打不开：它可能已经不在了，也可能本来就不是你的。";
    public static final String NETWORK_ERROR_TEXT = "连不上服务端，任务状态还没读到。";
    public static final String STATUS_WAITING_USER = "waiting_user";
    /** 与 backend/app/tools/response.py 的挂起前缀逐字一致；这里只当"去读一次"的触发器。 */
    public static final String SUSPEND_PREFIX = "⏸ ";

    /** 一张卡片要画的东西，一次算完。detail 为空 / 不是 waiting_user 时 visible=false。 */
    public static final class View {
        public final boolean visible;
        public final String taskId;
        public final String goal;
        public final String question;
        public final String progress;
        public final String waiting;
        public final String state;
        public final boolean canAnswer;

        private View(boolean visible, String taskId, String goal, String question,
                     String progress, String waiting, String state, boolean canAnswer) {
            this.visible = visible;
            this.taskId = taskId;
            this.goal = goal;
            this.question = question;
            this.progress = progress;
            this.waiting = waiting;
            this.state = state;
            this.canAnswer = canAnswer;
        }

        private static View empty() {
            return new View(false, "", "", "", "", "", "", false);
        }
    }

    /** 组合里的默认入口：倒计时按本机墙钟算一次，不挂定时器（与网页同一条：这一页没有定时器）。 */
    public static View view(String detailJson, boolean busy) {
        return view(detailJson, busy, System.currentTimeMillis());
    }

    public static View view(String detailJson, boolean busy, long nowMs) {
        Map<?, ?> detail = obj(detailJson);
        if (detail == null) return View.empty();
        String taskId = str(detail.get("task_id"));
        String goal = str(detail.get("goal"));
        String status = str(detail.get("status"));
        String state = stateLine(detail, busy);
        if (!STATUS_WAITING_USER.equals(status)) {
            return new View(false, taskId, goal, "", "", "", state, false);
        }
        Map<?, ?> cursor = asMap(detail.get("resume_cursor"));
        String question = cursor == null ? "" : str(cursor.get("question"));
        return new View(true, taskId, goal, question, progressLine(cursor),
                waitingLine(str(detail.get("waiting_expires_at")), nowMs),
                state, bool(detail.get("can_resume")) && !busy);
    }

    /**
     * 列表里"当前该回答的那一条"：第一条 waiting_user。userId 非空时还要求归属匹配——
     * 管理员看得到全量，聊天页上的卡片不该把别人家的任务端到这个人面前。
     */
    public static String waitingTaskId(String listJson) {
        return waitingTaskId(listJson, "");
    }

    public static String waitingTaskId(String listJson, String userId) {
        Map<?, ?> root = obj(listJson);
        if (root == null) return "";
        Object rows = root.get("tasks");
        if (!(rows instanceof List)) return "";
        String want = userId == null ? "" : userId.trim();
        for (Object row : (List<?>) rows) {
            Map<?, ?> task = asMap(row);
            if (task == null) continue;
            if (!STATUS_WAITING_USER.equals(str(task.get("status")))) continue;
            if (!want.isEmpty() && !want.equals(str(task.get("user_id")))) continue;
            String id = str(task.get("task_id"));
            if (!id.isEmpty()) return id;
        }
        return "";
    }

    /** 状态字面量 → 一句人话；认不出的原样留着，不猜。 */
    public static String statusText(String status) {
        String key = status == null ? "" : status;
        switch (key) {
            case "pending": return "排队中";
            case "running": return EXECUTING_TEXT;
            case "waiting_user": return TITLE;
            case "completed": return "办完了";
            case "failed": return "没办成";
            case "cancelled": return "已停止";
            default: return key;
        }
    }

    /**
     * 错误怎么说法：404 有自己那句话（归属的事），401 交给登录页（这里不重复说），
     * 其余照服务端给的原因说；连不上（非 HTTP 错误）由调用方用 NETWORK_ERROR_TEXT。
     */
    public static String readError(int status, String detail) {
        if (status == 404) return GONE_TEXT;
        if (status == 401) return "";
        String msg = detail == null ? "" : detail.trim();
        return msg.isEmpty() ? "任务状态没读到。" : msg;
    }

    /** 卡片上那句"这一条在干什么"；busy 是提交在途的界面表达，不是任务自己的状态。 */
    static String stateLine(Map<?, ?> detail, boolean busy) {
        if (detail == null) return "";
        if (busy) return EXECUTING_TEXT;
        String status = str(detail.get("status"));
        String label = statusText(status);
        // 超时作废与用户自己取消在 status 上是同一个词，服务端用 end_reason 分开这两件事。
        if ("cancelled".equals(status)
                && "cancelled_by_timeout".equals(str(detail.get("end_reason")))) {
            return label + " · 等回复等得太久";
        }
        return label;
    }

    /** 进度只在游标真的给了两个数时才说；缺一个就当没说（猜出来的"第 1 / 0 步"比空白更坏）。 */
    static String progressLine(Map<?, ?> cursor) {
        if (cursor == null) return "";
        Long index = asLong(cursor.get("subtask_index"));
        Long total = asLong(cursor.get("subtask_total"));
        if (index == null || total == null || total <= 0) return "";
        return "第 " + (index + 1) + " / " + total + " 步";
    }

    /**
     * 时限那一句的唯一出处：只看 waiting_expires_at。正数剩余向上取整到秒；不足 1 秒说
     * 「1 秒内回复」，只有真到点（或根本没有时限）才说「等你回复」。
     */
    public static String waitingLine(String expiresAt, long nowMs) {
        if (expiresAt == null || expiresAt.trim().isEmpty()) return WAITING_TEXT;
        Long at = parseIsoMillis(expiresAt.trim());
        if (at == null) return WAITING_TEXT;
        long left = (long) Math.ceil((at - nowMs) / 1000.0);
        if (left <= 0) return WAITING_TEXT;
        if (left < 60) return left + " 秒内回复";
        return ((left + 59) / 60) + " 分钟内回复";
    }

    private static final Pattern ISO = Pattern.compile(
            "^(\\d{4})-(\\d{2})-(\\d{2})[T ](\\d{2}):(\\d{2}):(\\d{2})"
            + "(?:\\.(\\d{1,9}))?(Z|[+-]\\d{2}:?\\d{2})?$");

    /**
     * ISO-8601 → epoch 毫秒。服务端 datetime.isoformat() 默认给的是**本地墙上时间**
     * （没有后缀），浏览器 Date.parse 也按本地时间读，所以这里同样：没后缀用默认时区。
     * 不用 java.time：minSdk 24 上它是 API 26 才有的类，core/ 里出现等于给老机器埋崩溃。
     * 解析不出来回 null，调用方落到「等你回复」——绝不拿一个猜的时刻倒计时。
     */
    static Long parseIsoMillis(String raw) {
        Matcher m = ISO.matcher(raw);
        if (!m.matches()) return null;
        int year = Integer.parseInt(m.group(1));
        int month = Integer.parseInt(m.group(2));
        int day = Integer.parseInt(m.group(3));
        int hour = Integer.parseInt(m.group(4));
        int minute = Integer.parseInt(m.group(5));
        int second = Integer.parseInt(m.group(6));
        String frac = m.group(7);
        String zone = m.group(8);

        TimeZone tz;
        if (zone == null || zone.isEmpty()) {
            tz = TimeZone.getDefault();
        } else if ("Z".equals(zone)) {
            tz = TimeZone.getTimeZone("UTC");
        } else {
            int sign = zone.charAt(0) == '-' ? -1 : 1;
            String hm = zone.substring(1).replace(":", "");
            int hh = Integer.parseInt(hm.substring(0, 2));
            int mm = Integer.parseInt(hm.substring(2, 4));
            if (hh > 23 || mm > 59) return null;
            tz = TimeZone.getTimeZone("GMT" + (sign < 0 ? "-" : "+")
                    + pad2(hh) + ":" + pad2(mm));
        }
        Calendar cal = new GregorianCalendar(tz);
        cal.setLenient(false);
        cal.clear();
        cal.set(Calendar.YEAR, year);
        cal.set(Calendar.MONTH, month - 1);
        cal.set(Calendar.DAY_OF_MONTH, day);
        cal.set(Calendar.HOUR_OF_DAY, hour);
        cal.set(Calendar.MINUTE, minute);
        cal.set(Calendar.SECOND, second);
        cal.set(Calendar.MILLISECOND, 0);
        long millis;
        try {
            millis = cal.getTimeInMillis();
        } catch (RuntimeException e) {
            return null;
        }
        if (frac != null && !frac.isEmpty()) {
            millis += Long.parseLong((frac + "000").substring(0, 3));
        }
        return millis;
    }

    private static String pad2(int v) {
        return v < 10 ? "0" + v : String.valueOf(v);
    }

    private static Map<?, ?> obj(String json) {
        if (json == null || json.isEmpty()) return null;
        try {
            return asMap(MiniJson.decode(json));
        } catch (RuntimeException e) {
            return null;   // 坏 JSON 只让这张卡片不出现，绝不让一次渲染崩掉
        }
    }

    private static Map<?, ?> asMap(Object v) {
        return v instanceof Map ? (Map<?, ?>) v : null;
    }

    private static String str(Object v) {
        return v instanceof String ? (String) v : "";
    }

    private static boolean bool(Object v) {
        return v instanceof Boolean && (Boolean) v;
    }

    private static Long asLong(Object v) {
        if (v instanceof Number) {
            double d = ((Number) v).doubleValue();
            if (Double.isNaN(d) || Double.isInfinite(d)) return null;
            return (long) d;
        }
        if (v instanceof String) {
            try {
                return (long) Double.parseDouble((String) v);
            } catch (NumberFormatException e) {
                return null;
            }
        }
        return null;
    }
}
