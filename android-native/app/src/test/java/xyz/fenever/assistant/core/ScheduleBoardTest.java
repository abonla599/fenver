package xyz.fenever.assistant.core;

import static org.junit.Assert.assertArrayEquals;
import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNotNull;
import static org.junit.Assert.assertNull;
import static org.junit.Assert.assertTrue;

import java.util.Calendar;

import org.junit.Test;

/**
 * {@link ScheduleBoard} 的台架用例（v0.23 T2.6/T2.7）。
 *
 * <p>为什么要单独一份而不是只靠 CI：这个类里全是"错一天没人发现"的算式——按 UTC 解析、
 * 周一开头的表按下标 0 排、月不补零、月序号跨年进位。这四条各自都能在编译通过、页面能打开
 * 的前提下把界面变成错的。台架（tools/shell_jvm_tests.py）三秒给判据。
 *
 * <p>星期锚点取自设计稿 {@code docs/v0.23-阶段0/T0.1-日程设计稿-双端同构.html}：
 * 那一排的 2026-09-21 是周一、<b>2026-09-26 是周六</b>。改任何一条星期算式，先被这条钉住。
 * 月历那一摊格子（表头顺序、月初前空几格、跨月翻年）是 2026-09-27 评审第 1 条换上的，
 * 判据在 {@link #monthGridSpansMondayToSunday} 与 {@link #monthCellsPadToWholeWeeks}。
 */
public class ScheduleBoardTest {

    private static final String SAT = "2026-09-26";

    @Test
    public void parsesLocalMidnightAndRejectsBadShapes() {
        Calendar cal = ScheduleBoard.parse(SAT);
        assertNotNull(cal);
        assertEquals(2026, cal.get(Calendar.YEAR));
        assertEquals(Calendar.SEPTEMBER, cal.get(Calendar.MONTH));
        assertEquals(26, cal.get(Calendar.DAY_OF_MONTH));
        assertEquals(0, cal.get(Calendar.HOUR_OF_DAY));
        assertEquals(0, cal.get(Calendar.MINUTE));

        assertNull(ScheduleBoard.parse(null));
        assertNull("空串不是某一天", ScheduleBoard.parse(""));
        assertNull("月日必须补零", ScheduleBoard.parse("2026-9-6"));
        assertNull("多出来的字符不收", ScheduleBoard.parse("2026-09-26T00:00"));
        assertNull("13 月不存在", ScheduleBoard.parse("2026-13-01"));
        assertNull("2026 不是闰年", ScheduleBoard.parse("2026-02-30"));
        assertNotNull("闰日合法", ScheduleBoard.parse("2028-02-29"));
    }

    @Test
    public void weekdayTableStartsOnMonday() {
        // 设计稿那一排：21 周一 … 26 周六。
        assertEquals("周一", ScheduleBoard.weekday("2026-09-21"));
        assertEquals("周五", ScheduleBoard.weekday("2026-09-25"));
        assertEquals("周六", ScheduleBoard.weekday(SAT));
        assertEquals("周日", ScheduleBoard.weekday("2026-09-27"));
        assertEquals("周一", ScheduleBoard.weekday("2026-09-28"));
        assertEquals(0, ScheduleBoard.weekdayIndex("2026-09-21"));
        assertEquals(6, ScheduleBoard.weekdayIndex("2026-09-27"));
        assertEquals(-1, ScheduleBoard.weekdayIndex("not-a-day"));
        assertEquals(7, ScheduleBoard.WEEKDAYS.length);
    }

    @Test
    public void monthGridSpansMondayToSunday() {
        // 表头那七个字是从星期表派生的，不是另抄一遍：这里同时钉住"派生"与"顺序"。
        assertArrayEquals(new String[]{"一", "二", "三", "四", "五", "六", "日"},
                ScheduleBoard.WEEK_HEAD);
        for (int i = 0; i < ScheduleBoard.WEEKDAYS.length; i++) {
            assertEquals(ScheduleBoard.WEEKDAYS[i].substring(1), ScheduleBoard.WEEK_HEAD[i]);
        }
    }

    @Test
    public void shiftsAcrossMonthAndYearBoundaries() {
        assertEquals("2026-09-25", ScheduleBoard.shift(SAT, -1));
        assertEquals("2026-09-27", ScheduleBoard.shift(SAT, 1));
        assertEquals("2026-09-30", ScheduleBoard.shift(SAT, 4));
        assertEquals("2026-10-01", ScheduleBoard.shift(SAT, 5));
        assertEquals("2026-12-31", ScheduleBoard.shift("2026-12-30", 1));
        assertEquals("2027-01-01", ScheduleBoard.shift("2026-12-31", 1));
        assertEquals("2026-02-28", ScheduleBoard.shift("2026-03-01", -1));
        assertEquals("2028-02-29", ScheduleBoard.shift("2028-02-28", 1));
        assertEquals("", ScheduleBoard.shift("", 3));
    }

    @Test
    public void monthTitlesDoNotZeroPadTheMonth() {
        assertEquals("2026年9月", ScheduleBoard.monthTitle(2026, 8));
        assertEquals("2026年1月", ScheduleBoard.monthTitle(2026, 0));
        assertEquals("2026年12月", ScheduleBoard.monthTitle(2026, 11));
    }

    @Test
    public void dayAnchorsWhichMonthTheGridShows() {
        assertArrayEquals(new int[]{2026, 8}, ScheduleBoard.monthOf(SAT));
        assertArrayEquals(new int[]{2026, 0}, ScheduleBoard.monthOf("2026-01-05"));
        assertNull("坏日期不给月份，界面自己决定'还没锚上'长什么样",
                ScheduleBoard.monthOf("2026-9-6"));
        assertEquals(2026 * 12 + 8, ScheduleBoard.monthIndex(2026, 8));
    }

    @Test
    public void monthNavigationCarriesAcrossTheYear() {
        assertArrayEquals(new int[]{2026, 9}, ScheduleBoard.shiftMonth(2026, 8, 1));
        assertArrayEquals(new int[]{2026, 7}, ScheduleBoard.shiftMonth(2026, 8, -1));
        assertArrayEquals(new int[]{2026, 11}, ScheduleBoard.shiftMonth(2026, 8, 3));
        assertArrayEquals(new int[]{2027, 0}, ScheduleBoard.shiftMonth(2026, 11, 1));
        assertArrayEquals(new int[]{2026, 11}, ScheduleBoard.shiftMonth(2027, 0, -1));
        assertArrayEquals(new int[]{2025, 11}, ScheduleBoard.shiftMonth(2026, 8, -9));
        assertArrayEquals(new int[]{2027, 8}, ScheduleBoard.shiftMonth(2026, 8, 12));
        assertArrayEquals(new int[]{2025, 8}, ScheduleBoard.shiftMonth(2026, 8, -12));
    }

    @Test
    public void monthCellsPadToWholeWeeks() {
        // 2026-09-01 是周二：月历第一行前面空一格；30 天 → 共 5 周。
        String[] sept = ScheduleBoard.monthCells(2026, 8);
        assertEquals(35, sept.length);
        assertEquals("", sept[0]);
        assertEquals("2026-09-01", sept[1]);
        assertEquals("2026-09-30", sept[30]);
        assertEquals("", sept[31]);
        // 2026-02-01 是周日：前面空六格，28 天 → 35 格。闰日那一年多一格，仍然整周。
        String[] feb = ScheduleBoard.monthCells(2026, 1);
        assertEquals(35, feb.length);
        assertEquals("", feb[5]);
        assertEquals("2026-02-01", feb[6]);
        assertEquals("2026-02-28", feb[33]);
        String[] leap = ScheduleBoard.monthCells(2028, 1);
        assertEquals(0, leap.length % 7);
        assertTrue("闰日必须在格子里", java.util.Arrays.asList(leap).contains("2028-02-29"));
        // 每一格要么空、要么落在本月之内：不许把上月的 31 号画进这月的格子。
        for (String cell : sept) {
            assertTrue("月历格子里混进了别的日子：" + cell,
                    cell.isEmpty() || cell.startsWith("2026-09-"));
        }
        assertEquals(0, ScheduleBoard.monthCells(2026, 12).length);
        assertEquals(0, ScheduleBoard.monthCells(2026, -1).length);
    }

    @Test
    public void pastDaysAreTheOnesBeforeToday() {
        assertTrue(ScheduleBoard.isBefore("2026-09-25", SAT));
        assertTrue("跨年也要比得出来", ScheduleBoard.isBefore("2025-12-31", SAT));
        assertFalse("今天不是过去", ScheduleBoard.isBefore(SAT, SAT));
        assertFalse(ScheduleBoard.isBefore("2026-09-27", SAT));
        assertFalse("锚点没回来时谁都不许被判成过去", ScheduleBoard.isBefore("2026-09-25", ""));
        assertFalse(ScheduleBoard.isBefore("", SAT));
        assertFalse(ScheduleBoard.isBefore(null, SAT));
    }

    @Test
    public void formatsWithoutZeroPaddingOnMonthAndDay() {
        assertEquals("2026年9月26日", ScheduleBoard.fullDate(SAT));
        assertEquals("2026年1月5日", ScheduleBoard.fullDate("2026-01-05"));
        assertEquals("", ScheduleBoard.fullDate("2026-1-5"));
    }

    @Test
    public void todayAnchorsComeFromTheServerDay() {
        assertEquals("2026年9月26日 · 今天", ScheduleBoard.dayLabel(SAT, SAT));
        assertEquals("2026年9月25日", ScheduleBoard.dayLabel("2026-09-25", SAT));
        assertEquals("26", ScheduleBoard.dayNumber(SAT));
        assertEquals("9", ScheduleBoard.dayNumber("2026-10-09"));
        assertEquals("", ScheduleBoard.dayNumber("2026-1-9"));
        assertTrue(ScheduleBoard.isToday(SAT, SAT));
        // 今天为空 = 还没拿到服务端那份 day，此时谁都不该被标成今天。
        assertFalse(ScheduleBoard.isToday("", ""));
    }

    @Test
    public void cellLabelsSayWhichDayItIs() {
        assertEquals("2026年9月26日 周六", ScheduleBoard.cellLabel(SAT, false));
        assertEquals("2026年9月26日 周六，有安排", ScheduleBoard.cellLabel(SAT, true));
        assertEquals("坏日期不配一句标签", "", ScheduleBoard.cellLabel("2026-9-6", true));
    }

    @Test
    public void groupTitlesAndCountReadLikeTheWeb() {
        assertEquals("今日安排", ScheduleBoard.groupTitle(SAT, SAT));
        assertEquals("2026年9月25日安排", ScheduleBoard.groupTitle("2026-09-25", SAT));
        // 跨月那天单独钉一次：月不补零这条算式只在跨月时才和月历对上。
        assertEquals("2026年10月1日安排", ScheduleBoard.groupTitle("2026-10-01", SAT));
        assertEquals("今日安排", ScheduleBoard.groupTitle("", SAT));
        assertEquals("3 项 · 已完成 1", ScheduleBoard.countText(3, 1));
        assertEquals("0 项 · 已完成 0", ScheduleBoard.countText(0, 0));
    }

    @Test
    public void sharedCopyConstantsMatchTheWeb() {
        // 时间格的提示字样：网页 app.js 的 at.placeholder 与原生 ScheduleUi 用的是同一串。
        assertEquals("HH:MM", ScheduleBoard.TIME_PLACEHOLDER);
        // 留空那格的占位破折号（网页 .at.none 靠 visibility 隐掉，两端同一个字符）。
        assertEquals("—", ScheduleBoard.NO_TIME);
        assertEquals(" · 今天", ScheduleBoard.TODAY_SUFFIX);
        assertEquals("今日安排", ScheduleBoard.GROUP_TODAY);
        assertEquals("安排", ScheduleBoard.GROUP_SUFFIX);
        assertEquals(" 项 · 已完成 ", ScheduleBoard.COUNT_ITEM);
        assertEquals("，有安排", ScheduleBoard.HAS_PLAN_SUFFIX);
    }
}
