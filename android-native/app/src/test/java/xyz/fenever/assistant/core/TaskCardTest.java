package xyz.fenever.assistant.core;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNotNull;
import static org.junit.Assert.assertNull;
import static org.junit.Assert.assertTrue;

import org.junit.Test;

/**
 * {@link TaskCard} 的台架用例（v0.35 #99）。
 *
 * <p>这一组判据盯的是"卡片把服务端那句话读错了"这一类错：缺游标键时编出"第 1 / 0 步"、
 * 把坏 JSON 画成一张空壳却仍允许提交、把 404 说成断网、拿一个坏时刻开始倒计时。它们都能
 * 在编译通过、界面也打得开的前提下发生，所以在纯 JVM 上钉住，比等真机截图便宜。
 *
 * <p>时间锚点全部用显式 UTC 串（2030-01-01T00:00:00Z = 1893456000000），不依赖跑台架
 * 这台机器的时区；另有一条专门覆盖 +08:00 后缀。
 */
public class TaskCardTest {

    private static final long T0 = 1893456000000L;   // 2030-01-01T00:00:00Z

    private static String waitingDetail() {
        return "{\"task_id\":\"T1\",\"goal\":\"买票\",\"status\":\"waiting_user\","
                + "\"can_resume\":true,\"waiting_expires_at\":null,"
                + "\"resume_cursor\":{\"question\":\"请输入短信验证码\","
                + "\"subtask_index\":1,\"subtask_total\":4}}";
    }

    @Test
    public void waitingDetailCarriesQuestionProgressAndAnswerPermission() {
        TaskCard.View v = TaskCard.view(waitingDetail(), false, T0);
        assertTrue(v.visible);
        assertEquals("T1", v.taskId);
        assertEquals("买票", v.goal);
        assertEquals("需要你输入", TaskCard.TITLE);
        assertEquals("请输入短信验证码", v.question);
        assertEquals("第 2 / 4 步", v.progress);
        assertEquals("等你回复", v.waiting);
        assertEquals("需要你输入", v.state);
        assertTrue(v.canAnswer);
    }

    @Test
    public void busyDisablesSubmitAndSaysExecuting() {
        TaskCard.View v = TaskCard.view(waitingDetail(), true, T0);
        assertTrue("提交在途中卡片不许收起——收起后人就看不见自己在等什么", v.visible);
        assertEquals("执行中", v.state);
        assertFalse(v.canAnswer);
    }

    @Test
    public void canResumeFalseNeverOffersTheButton() {
        String json = "{\"task_id\":\"T1\",\"status\":\"waiting_user\",\"can_resume\":false,"
                + "\"resume_cursor\":{\"question\":\"验证码\"}}";
        TaskCard.View v = TaskCard.view(json, false, T0);
        assertTrue(v.visible);
        assertFalse("能不能按只认 can_resume，不替服务端猜", v.canAnswer);
    }

    @Test
    public void nonWaitingTasksAreAnEmptyShell() {
        TaskCard.View running = TaskCard.view(
                "{\"task_id\":\"T2\",\"status\":\"running\"}", false, T0);
        assertFalse(running.visible);
        assertEquals("执行中", running.state);
        assertFalse(running.canAnswer);

        TaskCard.View pending = TaskCard.view(
                "{\"task_id\":\"T3\",\"status\":\"pending\"}", false, T0);
        assertFalse(pending.visible);
        assertEquals("排队中", pending.state);

        TaskCard.View completed = TaskCard.view(
                "{\"task_id\":\"T4\",\"status\":\"completed\"}", false, T0);
        assertFalse(completed.visible);
        assertEquals("办完了", completed.state);
    }

    @Test
    public void cancelledByTimeoutSaysWhyWithoutASecondWordForCancelled() {
        String json = "{\"task_id\":\"T5\",\"status\":\"cancelled\","
                + "\"end_reason\":\"cancelled_by_timeout\"}";
        assertEquals("已停止 · 等回复等得太久", TaskCard.view(json, false, T0).state);

        String byUser = "{\"task_id\":\"T5\",\"status\":\"cancelled\","
                + "\"end_reason\":\"cancelled_by_user\"}";
        assertEquals("已停止", TaskCard.view(byUser, false, T0).state);
    }

    @Test
    public void brokenJsonOrMissingFieldsDoNotThrowOrInventState() {
        String[] bad = {null, "", "   ", "{", "[]", "{}", "null",
                "{\"status\":5}", "{\"status\":\"waiting_user\"}"};
        for (String json : bad) {
            TaskCard.View v = TaskCard.view(json, false, T0);
            assertNotNull(v);
            assertFalse("坏形状不许长出一张可提交的卡片：" + json, v.canAnswer);
        }
        // 只有 status 的 waiting_user 仍算在等人（can_resume 缺省 false，按钮不亮）。
        TaskCard.View skinny = TaskCard.view(
                "{\"status\":\"waiting_user\"}", false, T0);
        assertTrue(skinny.visible);
        assertEquals("", skinny.question);
        assertFalse(skinny.canAnswer);
    }

    @Test
    public void progressRequiresBothNumbers() {
        String onlyIndex = "{\"status\":\"waiting_user\",\"resume_cursor\":"
                + "{\"question\":\"q\",\"subtask_index\":1}}";
        assertEquals("", TaskCard.view(onlyIndex, false, T0).progress);

        String zeroTotal = "{\"status\":\"waiting_user\",\"resume_cursor\":"
                + "{\"subtask_index\":1,\"subtask_total\":0}}";
        assertEquals("", TaskCard.view(zeroTotal, false, T0).progress);

        String stringNumbers = "{\"status\":\"waiting_user\",\"resume_cursor\":"
                + "{\"subtask_index\":\"0\",\"subtask_total\":\"4\"}}";
        assertEquals("第 1 / 4 步", TaskCard.view(stringNumbers, false, T0).progress);
    }

    @Test
    public void waitingLineCountsDownButNeverInventsADeadline() {
        assertEquals("等你回复", TaskCard.waitingLine(null, T0));
        assertEquals("等你回复", TaskCard.waitingLine("", T0));
        assertEquals("等你回复", TaskCard.waitingLine("不是时刻", T0));
        assertEquals("等你回复", TaskCard.waitingLine("2029-12-31T23:59:59Z", T0));
        assertEquals("20 秒内回复",
                TaskCard.waitingLine("2030-01-01T00:00:20Z", T0));
        assertEquals("1 秒内回复",
                TaskCard.waitingLine("2030-01-01T00:00:00.500Z", T0));
        assertEquals("2 分钟内回复",
                TaskCard.waitingLine("2030-01-01T00:01:30Z", T0));
        // 同一时刻换个写法：+08:00 的 08:00:20 就是 UTC 的 00:00:20。
        assertEquals("20 秒内回复",
                TaskCard.waitingLine("2030-01-01T08:00:20+08:00", T0));
        assertEquals("20 秒内回复",
                TaskCard.waitingLine("2030-01-01T08:00:20+0800", T0));
    }

    @Test
    public void parseRejectsImpossibleCalendarValues() {
        assertNull(TaskCard.parseIsoMillis("2030-13-01T00:00:00Z"));
        assertNull(TaskCard.parseIsoMillis("2030-02-30T00:00:00Z"));
        assertNull(TaskCard.parseIsoMillis("2030-01-01T25:00:00Z"));
        assertNull(TaskCard.parseIsoMillis("2030-01-01T00:00:00+99:00"));
        assertNull(TaskCard.parseIsoMillis("2030-01-01"));
        assertNotNull(TaskCard.parseIsoMillis("2030-01-01T00:00:00Z"));
    }

    @Test
    public void findsFirstWaitingTaskAndCanRestrictToOwner() {
        String list = "{\"total\":3,\"tasks\":["
                + "{\"task_id\":\"T-run\",\"status\":\"running\",\"user_id\":\"u1\"},"
                + "{\"task_id\":\"T-other\",\"status\":\"waiting_user\",\"user_id\":\"u2\"},"
                + "{\"task_id\":\"T-mine\",\"status\":\"waiting_user\",\"user_id\":\"u1\"}]}";
        assertEquals("T-other", TaskCard.waitingTaskId(list));
        assertEquals("T-mine", TaskCard.waitingTaskId(list, "u1"));
        assertEquals("", TaskCard.waitingTaskId(list, "u3"));
        assertEquals("", TaskCard.waitingTaskId("{}", "u1"));
        assertEquals("", TaskCard.waitingTaskId("坏 JSON", "u1"));
    }

    @Test
    public void readErrorMapsHttpStatusToTheRightSentence() {
        assertEquals(TaskCard.GONE_TEXT, TaskCard.readError(404, "任务不存在"));
        assertEquals("", TaskCard.readError(401, "未授权"));
        assertEquals("任务状态没读到。", TaskCard.readError(500, ""));
        assertEquals("任务状态没读到。", TaskCard.readError(500, null));
        assertEquals("服务端忙", TaskCard.readError(503, " 服务端忙 "));
    }

    @Test
    public void suspendPrefixIsTheOnlyThingTheStreamContributes() {
        assertEquals("⏸ ", TaskCard.SUSPEND_PREFIX);
    }
}
