package xyz.fenever.assistant.core;

import java.text.ParsePosition;
import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Calendar;
import java.util.List;
import java.util.Locale;
import java.util.TimeZone;

/**
 * 日程看板的日期算式与文案（v0.23 T2.6/T2.7，PRD R3）。
 *
 * <p>一天一份清单，界面上跟日期有关的东西一共就这么几件：往前往后挪一天、这一天的星期名、
 * 顶栏的「2026年9月26日 · 今天」、月历那一格里的日子数字、整月摊进周一起头的格子、
 * 翻月翻年、以及"这一天是不是已经过去了"；再加组头「今日安排 / 某天安排」与计数
 * 「3 项 · 已完成 1」。每件都短，但每件都有一次算错的机会（尤其"按 UTC 解析本地日"和
 * "月序号跨年进位"这两处——差一天，整张月历就错位，而且只有跨零点或跨时区的人才看得见）。
 * 所以算式收在这一份纯 JVM 的类里，台架（tools/shell_jvm_tests.py）直接跑，不靠 CI 猜。
 *
 * <p><b>刻意的取舍</b>：
 * <ul>
 *   <li>解析按<b>本地时区</b>。这里没有 UTC 也不该有：`YYYY-MM-DD` 是"哪一天"，
 *       不是某个时刻；{@link #parse} 用默认时区，格式化也用默认时区，两边同一个钟。</li>
 *   <li>「今天」由调用方传进来，值来自服务端 GET 响应的 {@code day} 字段（R3-AC-3）。
 *       这一份不知道"今天是几号"，也不去问设备时钟——手机改了系统日期不该让它问出
 *       别人的一天。</li>
 *   <li>星期表与后端 {@code app/core/schedule.py} 的 {@code _WEEKDAYS} 同序同字
 *       （0=周一），两处各有钉死测试；换算入口只有 {@link #weekdayIndex} 一处。</li>
 * </ul>
 *
 * <p>字符串文案（「 · 今天」「今日安排」「 项 · 已完成 」）与网页
 * {@code backend/app/web/static/app.js} 的同名算式逐字同值，由
 * {@code backend/tests/test_schedule_ui_contract.py} 两头钉住。
 */
public final class ScheduleBoard {

    private ScheduleBoard() {
    }

    /** 与后端 _WEEKDAYS 同序：下标 0 = 周一。 */
    public static final String[] WEEKDAYS = {"周一", "周二", "周三", "周四", "周五", "周六", "周日"};

    /** 月历表头那七个字：与网页同一理由，从 {@link #WEEKDAYS} 派生，不另立第二张表。 */
    public static final String[] WEEK_HEAD = headOf(WEEKDAYS);

    private static String[] headOf(String[] weekdays) {
        String[] out = new String[weekdays.length];
        for (int i = 0; i < weekdays.length; i++) {
            out[i] = weekdays[i].substring(1);          // 「周一」→「一」
        }
        return out;
    }

    /** 顶栏日期后面的那截锚点：全角间隔号 + 前后各一个空格，与网页同一条串。 */
    public static final String TODAY_SUFFIX = " · 今天";

    /** 组头：看的就是今天时用它，否则用 {@link #groupTitle} 的另一支。 */
    public static final String GROUP_TODAY = "今日安排";

    /** 组头另一支的后缀：「2026年9月26日」+ 这个 = 「2026年9月26日安排」。 */
    public static final String GROUP_SUFFIX = "安排";

    /** 计数串的分隔：{@code 3 + ITEM + 3 + DONE + 1}。 */
    public static final String COUNT_ITEM = " 项 · 已完成 ";

    /** 月历那一格读屏文案的后缀：有安排的日子才带。 */
    public static final String HAS_PLAN_SUFFIX = "，有安排";

    /** 时间角标留空时占位的破折号：靠 CSS/布局隐掉，但仍在那一格上。 */
    public static final String NO_TIME = "—";

    /** 时间格空白时的提示字样：只提示形状，不做校验（校验的口径在服务端 {@code clean_at}）。 */
    public static final String TIME_PLACEHOLDER = "HH:MM";

    private static final String DAY_PATTERN = "yyyy-MM-dd";

    /**
     * `YYYY-MM-DD` → 本地零点的 Calendar。形状不对返回 null（不抛）：
     * 界面上"拿不到那一天"和"那天没有安排"是两件事，调用方要能各自表态。
     */
    public static Calendar parse(String iso) {
        if (iso == null) {
            return null;
        }
        String text = iso.trim();
        if (text.length() != DAY_PATTERN.length()) {
            return null;
        }
        SimpleDateFormat fmt = new SimpleDateFormat(DAY_PATTERN, Locale.US);
        fmt.setTimeZone(TimeZone.getDefault());
        fmt.setLenient(false);
        ParsePosition pos = new ParsePosition(0);
        java.util.Date date = fmt.parse(text, pos);
        if (date == null || pos.getIndex() != text.length()) {
            return null;
        }
        Calendar cal = Calendar.getInstance();
        cal.setTime(date);
        cal.set(Calendar.HOUR_OF_DAY, 0);
        cal.set(Calendar.MINUTE, 0);
        cal.set(Calendar.SECOND, 0);
        cal.set(Calendar.MILLISECOND, 0);
        return cal;
    }

    /** Calendar → `YYYY-MM-DD`（与 {@link #parse} 同一个时区、同一份格式）。 */
    public static String iso(Calendar cal) {
        SimpleDateFormat fmt = new SimpleDateFormat(DAY_PATTERN, Locale.US);
        fmt.setTimeZone(TimeZone.getDefault());
        return fmt.format(cal.getTime());
    }

    /** 挪 ±N 天（跨月跨年都走 Calendar 的字段加法，不做秒数除法）。 */
    public static String shift(String isoDay, int delta) {
        Calendar cal = parse(isoDay);
        if (cal == null) {
            return "";
        }
        cal.add(Calendar.DAY_OF_MONTH, delta);
        return iso(cal);
    }

    /** `YYYY-MM-DD` → 星期表下标，0 = 周一。日历按本地时区解析，与 UTC 无关。 */
    public static int weekdayIndex(String isoDay) {
        Calendar cal = parse(isoDay);
        if (cal == null) {
            return -1;
        }
        // Calendar.DAY_OF_WEEK: 周日=1…周六=7；周一开头的表要把周日挪到最后。
        return (cal.get(Calendar.DAY_OF_WEEK) + 5) % 7;
    }

    public static String weekday(String isoDay) {
        int at = weekdayIndex(isoDay);
        return at < 0 ? "" : WEEKDAYS[at];
    }

    /** 「2026年9月26日」——月与日都不补零，与网页 {@code schedFullDate} 同形。 */
    public static String fullDate(String isoDay) {
        Calendar cal = parse(isoDay);
        if (cal == null) {
            return "";
        }
        return cal.get(Calendar.YEAR) + "年" + (cal.get(Calendar.MONTH) + 1) + "月"
                + cal.get(Calendar.DAY_OF_MONTH) + "日";
    }

    /** 顶栏那一截：今天多带「 · 今天」，其余只报日期。 */
    public static String dayLabel(String isoDay, String today) {
        String base = fullDate(isoDay);
        if (base.isEmpty()) {
            return "";
        }
        return base + (isToday(isoDay, today) ? TODAY_SUFFIX : "");
    }

    /** 月历那一格里的日子数字（不补零）。 */
    public static String dayNumber(String isoDay) {
        Calendar cal = parse(isoDay);
        return cal == null ? "" : String.valueOf(cal.get(Calendar.DAY_OF_MONTH));
    }

    public static boolean isToday(String isoDay, String today) {
        return isoDay != null && today != null && isoDay.length() > 0 && isoDay.equals(today);
    }

    /** 那一格说给读屏器听的一整句：光秃秃一个数字在月历里谁也不知道是哪天。 */
    public static String cellLabel(String isoDay, boolean has) {
        String base = fullDate(isoDay);
        if (base.isEmpty()) {
            return "";
        }
        return base + " " + weekday(isoDay) + (has ? HAS_PLAN_SUFFIX : "");
    }

    /**
     * 早于某一天？{@code YYYY-MM-DD} 定宽零补齐，字典序即日期序，与网页
     * {@code iso < today} 同一条算式。今天以前的日子在月历里不可点（评审第 1 条）。
     */
    public static boolean isBefore(String isoDay, String other) {
        return isoDay != null && other != null
                && isoDay.length() > 0 && other.length() > 0
                && isoDay.compareTo(other) < 0;
    }

    /** 「2026年9月」：月不补零，与 {@link #fullDate} 同一个口径。{@code month} 从 0 起。 */
    public static String monthTitle(int year, int month) {
        return year + "年" + (month + 1) + "月";
    }

    /** 年月折成一个"月序号"，翻月/夹边界都只比这一个数。 */
    public static int monthIndex(int year, int month) {
        return year * 12 + month;
    }

    /**
     * 某一天落在哪个月：{@code {年, 月(0 起)}}；坏日期返回 null（不抛）。
     * 月历跟着看的那天走，靠的就是这一个换算。
     */
    public static int[] monthOf(String isoDay) {
        Calendar cal = parse(isoDay);
        if (cal == null) {
            return null;
        }
        return new int[]{cal.get(Calendar.YEAR), cal.get(Calendar.MONTH)};
    }

    /** 翻月：{@code delta} 以月为单位（±12 就是翻年）。跨年进位不特判。 */
    public static int[] shiftMonth(int year, int month, int delta) {
        int at = monthIndex(year, month) + delta;
        return new int[]{Math.floorDiv(at, 12), Math.floorMod(at, 12)};
    }

    /**
     * 那个月摊在"周一起头"的格子里长什么样：本月之外的格子是空串（占位，不画格子）。
     *
     * <p>行数按整月实际占几周给（4~6 周），不硬凑 42 格：空着第六行比挂七个空格子好看，
     * 也少一处「这一格是上月的 30 号还是本月的 30 号」的歧义。月份越界返回空数组，
     * 让界面自己决定"没有月历"长什么样（原先窗口那份同一条口径）。
     */
    public static String[] monthCells(int year, int month) {
        if (month < 0 || month > 11) {
            return new String[0];
        }
        Calendar first = Calendar.getInstance();
        first.clear();
        first.set(year, month, 1);
        List<String> out = new ArrayList<>();
        for (int i = (first.get(Calendar.DAY_OF_WEEK) + 5) % 7; i > 0; i--) {
            out.add("");
        }
        int days = first.getActualMaximum(Calendar.DAY_OF_MONTH);
        String cur = iso(first);
        for (int d = 0; d < days; d++) {
            out.add(cur);
            cur = shift(cur, 1);          // 逐日只走 shift 这一条路：与网页同一份算式
        }
        while (out.size() % 7 != 0) {
            out.add("");
        }
        return out.toArray(new String[0]);
    }

    /** 组头：今天 = 「今日安排」；别的日子 = 「2026年10月1日安排」。 */
    public static String groupTitle(String isoDay, String today) {
        if (isoDay == null || isoDay.isEmpty()) {
            return GROUP_TODAY;
        }
        return isToday(isoDay, today) ? GROUP_TODAY : fullDate(isoDay) + GROUP_SUFFIX;
    }

    /** 「3 项 · 已完成 1」。空清单也给一句（「0 项 · 已完成 0」），别让计数位空着晃。 */
    public static String countText(int total, int done) {
        return total + COUNT_ITEM + done;
    }
}
