package xyz.fenever.assistant.nativeapp.ui

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.layout.size
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.CornerRadius
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Rect
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.StrokeJoin
import androidx.compose.ui.graphics.drawscope.DrawScope
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.unit.dp
import kotlin.math.abs
import kotlin.math.cos
import kotlin.math.sin
import kotlin.math.sqrt

/* Fenver 原创图标语言（v0.36.1 图标批）。
 *
 * 这一族不是"再换一套现成图标库"，而是一套有自家记号的线性语言，全部 Canvas 直绘：
 *   · 24×24 格网，内容区 20×20（四周留 2 个设计单位）；
 *   · 描边 1.8 设计单位（绘制时乘 k=画布宽/24 缩放），圆头圆角；
 *   · 签名一：闭合外框的右上角切 2.6 单位 45° 斜角（bevelFrame）——整套"框族"
 *     （相机/键盘/图片/导出…）共用这一刀，一眼认得出是一家人；
 *   · 签名二：开放线的末端做 2 单位折角（人字两翼、对勾收笔、发送/回底箭头翎口），
 *     折角只用在末端自由、且不伤 20dp 可辨性的线上；
 *   · 签名三：多段结构用 3 节点 trace 母题（长期记忆、当前模型、日程行点），
 *     与品牌星芒、过程面板的三点呼吸同宗；
 *   · 除状态点（声纹起点、呼吸三点、忙碌转圈、脏标记那颗点、i 那点、闪光灯、
 *     键帽点、太阳、警示感叹那颗点）外一律不填充。
 * 默认 20dp；16dp（行尾附属）与更大尺寸只在调用方确实需要时用。
 * 旧 IconXxx 函数名全部保留为薄包装：既有契约读的是名字，几何只有下面一份。 */

internal const val GRID = 24f          // 格网边长（设计单位）；内容区 20×20 由逐枚坐标自守四周 2 单位留白
internal const val STROKE = 1.8f       // 描边宽度（设计单位）
internal const val BEVEL = 2.6f        // 闭合外框右上角 45° 切角
internal const val FOLD = 2f           // 开放线末端的折角长度
internal const val NODE = 1.9f         // 3 节点 trace 母题的节点半径
internal const val DOTR = 0.95f        // 状态点半径

/** 图标语义的唯一枚举：同一语义在所有页面用同一枚。 */
enum class FenverIcon {
    Camera, Mic, Keyboard, Plus, ArrowUp, ArrowDown, Stop, Image, File,
    ChevronDown, ChevronRight, ChevronLeft, ChevronDoubleLeft, ChevronDoubleRight,
    VoiceWave, Menu, TraceEye, StepDone, StepFailed, StepBusy, ThinkingDots,
    Close, Check, ThumbUp, ThumbDown, Trash, Edit,
    Settings, ModelService, Model, Memory, Schedule, Reminder, Haptic,
    Notification, ClearCache, Info, Refresh, Moon, Mail, OpenExternal,
    License, ServiceAddress, Logout, SwapAccounts, PasswordKey, Export,
    Bolt, Star, SiteDefault, Context, Warning,
}

/** 所有 UI 图标调用的统一入口。rotation/phase 只服务两枚动画母题（忙碌转圈、呼吸三点）。 */
@Composable
fun FenverIcon(kind: FenverIcon, tint: Color,
               modifier: Modifier = Modifier.size(20.dp),
               rotation: Float = 0f, phase: Float = 0f) {
    Canvas(modifier) {
        val k = size.width / GRID
        drawFenverIcon(kind, tint, k, STROKE * k, rotation, phase)
    }
}

private fun DrawScope.drawFenverIcon(kind: FenverIcon, tint: Color,
                                     k: Float, sw: Float,
                                     rotation: Float, phase: Float) {
    when (kind) {
        FenverIcon.Camera -> drawCamera(tint, k, sw)
        FenverIcon.Mic -> drawMic(tint, k, sw)
        FenverIcon.Keyboard -> drawKeyboard(tint, k, sw)
        FenverIcon.Plus -> drawPlus(tint, k, sw)
        FenverIcon.ArrowUp -> drawArrowUp(tint, k, sw)
        FenverIcon.ArrowDown -> drawArrowDown(tint, k, sw)
        FenverIcon.Stop -> drawStop(tint, k, sw)
        FenverIcon.Image -> drawImage(tint, k, sw)
        FenverIcon.File -> drawFile(tint, k, sw)
        FenverIcon.ChevronDown -> drawChevronDown(tint, k, sw)
        FenverIcon.ChevronRight -> drawChevronRight(tint, k, sw)
        FenverIcon.ChevronLeft -> drawChevronLeft(tint, k, sw)
        FenverIcon.ChevronDoubleLeft -> drawChevronDoubleLeft(tint, k, sw)
        FenverIcon.ChevronDoubleRight -> drawChevronDoubleRight(tint, k, sw)
        FenverIcon.VoiceWave -> drawVoiceWave(tint, k, sw)
        FenverIcon.Menu -> drawMenu(tint, k, sw)
        FenverIcon.TraceEye -> drawTraceEye(tint, k, sw)
        FenverIcon.StepDone -> drawStepDone(tint, k, sw)
        FenverIcon.StepFailed -> drawStepFailed(tint, k, sw)
        FenverIcon.StepBusy -> drawStepBusy(tint, k, rotation)
        FenverIcon.ThinkingDots -> drawThinkingDots(tint, k, phase)
        FenverIcon.Close -> drawClose(tint, k, sw)
        FenverIcon.Check -> drawCheck(tint, k, sw)
        FenverIcon.ThumbUp -> drawThumbUp(tint, k, sw)
        FenverIcon.ThumbDown -> drawThumbDown(tint, k, sw)
        FenverIcon.Trash -> drawTrash(tint, k, sw)
        FenverIcon.Edit -> drawEdit(tint, k, sw)
        FenverIcon.Settings -> drawSettings(tint, k, sw)
        FenverIcon.ModelService -> drawModelService(tint, k, sw)
        FenverIcon.Model -> drawModel(tint, k, sw)
        FenverIcon.Memory -> drawMemory(tint, k, sw)
        FenverIcon.Schedule -> drawSchedule(tint, k, sw)
        FenverIcon.Reminder -> drawReminder(tint, k, sw)
        FenverIcon.Haptic -> drawHaptic(tint, k, sw)
        FenverIcon.Notification -> drawNotification(tint, k, sw)
        FenverIcon.ClearCache -> drawClearCache(tint, k, sw)
        FenverIcon.Info -> drawInfo(tint, k, sw)
        FenverIcon.Refresh -> drawRefresh(tint, k, sw)
        FenverIcon.Moon -> drawMoon(tint, k, sw)
        FenverIcon.Mail -> drawMail(tint, k, sw)
        FenverIcon.OpenExternal -> drawOpenExternal(tint, k, sw)
        FenverIcon.License -> drawLicense(tint, k, sw)
        FenverIcon.ServiceAddress -> drawServiceAddress(tint, k, sw)
        FenverIcon.Logout -> drawLogout(tint, k, sw)
        FenverIcon.SwapAccounts -> drawSwapAccounts(tint, k, sw)
        FenverIcon.PasswordKey -> drawPasswordKey(tint, k, sw)
        FenverIcon.Export -> drawExport(tint, k, sw)
        FenverIcon.Bolt -> drawBolt(tint, k, sw)
        FenverIcon.Star -> drawStar(tint, k, sw)
        FenverIcon.SiteDefault -> drawSiteDefault(tint, k, sw)
        FenverIcon.Context -> drawContext(tint, k, sw)
        FenverIcon.Warning -> drawWarning(tint, k, sw)
    }
}

/* ---------------- 语言积木 ---------------- */

/* 折线（圆头圆角连接）：坐标对是 24 格网设计值，先乘 k 再画。 */
private fun DrawScope.poly(color: Color, sw: Float, k: Float, vararg pts: Pair<Float, Float>) {
    if (pts.isEmpty()) return
    val p = Path().apply {
        moveTo(pts[0].first * k, pts[0].second * k)
        pts.drop(1).forEach { lineTo(it.first * k, it.second * k) }
    }
    drawPath(p, color, style = Stroke(width = sw, cap = StrokeCap.Round, join = StrokeJoin.Round))
}

/* 闭合折线。 */
private fun DrawScope.polyC(color: Color, sw: Float, k: Float, vararg pts: Pair<Float, Float>) {
    if (pts.isEmpty()) return
    val p = Path().apply {
        moveTo(pts[0].first * k, pts[0].second * k)
        pts.drop(1).forEach { lineTo(it.first * k, it.second * k) }
        close()
    }
    drawPath(p, color, style = Stroke(width = sw, cap = StrokeCap.Round, join = StrokeJoin.Round))
}

private fun DrawScope.seg(color: Color, sw: Float, k: Float,
                          a: Pair<Float, Float>, b: Pair<Float, Float>) =
    drawLine(color, Offset(a.first * k, a.second * k), Offset(b.first * k, b.second * k),
        strokeWidth = sw, cap = StrokeCap.Round)

/* 状态点族：语言里唯一允许填充的东西。 */
private fun DrawScope.dot(color: Color, k: Float, x: Float, y: Float, r: Float = DOTR) =
    drawCircle(color, r * k, Offset(x * k, y * k))

/* 签名一：闭合外框，右上角 45° 切 BEVEL 单位斜角，其余角圆角 corner。 */
private fun DrawScope.bevelFrame(color: Color, sw: Float, k: Float,
                                 l: Float, t: Float, r: Float, b: Float,
                                 corner: Float = 2.2f, bevel: Float = BEVEL) {
    val p = Path()
    p.moveTo((l + corner) * k, t * k)
    p.lineTo((r - bevel) * k, t * k)
    p.lineTo(r * k, (t + bevel) * k)
    p.lineTo(r * k, (b - corner) * k)
    p.arcTo(Rect((r - 2 * corner) * k, (b - 2 * corner) * k, r * k, b * k), 0f, 90f, false)
    p.lineTo((l + corner) * k, b * k)
    p.arcTo(Rect(l * k, (b - 2 * corner) * k, (l + 2 * corner) * k, b * k), 90f, 90f, false)
    p.lineTo(l * k, (t + corner) * k)
    p.arcTo(Rect(l * k, t * k, (l + 2 * corner) * k, (t + 2 * corner) * k), 180f, 90f, false)
    p.close()
    drawPath(p, color, style = Stroke(width = sw, cap = StrokeCap.Round, join = StrokeJoin.Round))
}

/* 签名二：开放线末端的折角。foldPoint 给出末点钩位——沿 (from→to) 到达 to 后，
 * 转 90°（side=+1 右手法线 / -1 左手法线）外翻 FOLD 单位。人字、对勾收笔、
 * 发送/回底的箭头翎口都从这一个算式出来，钩长只有 FOLD 一处真相。 */
private fun foldPoint(from: Pair<Float, Float>, to: Pair<Float, Float>,
                      side: Float): Pair<Float, Float> {
    val dx = to.first - from.first
    val dy = to.second - from.second
    val s = FOLD / sqrt(dx * dx + dy * dy)
    return Pair(to.first + side * dy * s, to.second - side * dx * s)
}

/* ---------------- 逐枚几何 ---------------- */

/** 相机：右上切角的机身框 + 取景凸台 + 镜头圈 + 闪光灯点。 */
private fun DrawScope.drawCamera(tint: Color, k: Float, sw: Float) {
    bevelFrame(tint, sw, k, 2.2f, 7.2f, 21.8f, 19.8f, corner = 2.6f)
    poly(tint, sw, k, 8.4f to 7.2f, 9.8f to 4.6f, 14.2f to 4.6f, 15.6f to 7.2f)
    drawCircle(tint, 3.2f * k, Offset(12f * k, 13.5f * k), style = Stroke(width = sw))
    dot(tint, k, 18f, 10.3f)
}

/** 麦克风：胶囊 + 抱弧 + 短杆底座（胶囊是圆形体，不吃方框切角）。 */
private fun DrawScope.drawMic(tint: Color, k: Float, sw: Float) {
    drawRoundRect(tint, Offset(9.3f * k, 2.4f * k), Size(5.4f * k, 10.8f * k),
        CornerRadius(2.7f * k, 2.7f * k), style = Stroke(width = sw))
    drawArc(tint, 0f, 180f, false, Offset(5.8f * k, 6f * k), Size(12.4f * k, 12.4f * k),
        style = Stroke(width = sw, cap = StrokeCap.Round))
    seg(tint, sw, k, 12f to 18.8f, 12f to 21.2f)
    seg(tint, sw, k, 8.8f to 21.4f, 15.2f to 21.4f)
}

/** 键盘：切角外框 + 键帽状态点一排 + 空格条。 */
private fun DrawScope.drawKeyboard(tint: Color, k: Float, sw: Float) {
    bevelFrame(tint, sw, k, 2.2f, 6.2f, 21.8f, 17.4f, corner = 2.6f)
    listOf(5.6f, 8.8f, 12f, 15.2f, 18.4f).forEach { dot(tint, k, it, 10f, 0.85f) }
    seg(tint, sw, k, 8.8f to 14.2f, 15.2f to 14.2f)
}

/** 加号（附件面板开合时整枚旋转 45° 变 ×）：十字本身不加折角，保 20dp 可辨。 */
private fun DrawScope.drawPlus(tint: Color, k: Float, sw: Float) {
    seg(tint, sw, k, 12f to 5.6f, 12f to 18.4f)
    seg(tint, sw, k, 5.6f to 12f, 18.4f to 12f)
}

/** 发送：上箭头，杆 + 带翎口折角的人字头（签名二的箭头用法）。 */
private fun DrawScope.drawArrowUp(tint: Color, k: Float, sw: Float) {
    val apex = 12f to 5.6f
    val l = 6.5f to 11f
    val r = 17.5f to 11f
    seg(tint, sw, k, 12f to 19.4f, apex)
    poly(tint, sw, k, foldPoint(apex, l, 1f), l, apex, r, foldPoint(apex, r, -1f))
}

/** 回到底部：下箭头，与 IconArrowUp 上下镜像同构。 */
private fun DrawScope.drawArrowDown(tint: Color, k: Float, sw: Float) {
    val apex = 12f to 18.4f
    val l = 6.5f to 13f
    val r = 17.5f to 13f
    seg(tint, sw, k, 12f to 4.6f, apex)
    poly(tint, sw, k, foldPoint(apex, l, -1f), l, apex, r, foldPoint(apex, r, 1f))
}

/** 停止：一枚右上切角的小方牌（语言的框族缩到 9.6 格）。 */
private fun DrawScope.drawStop(tint: Color, k: Float, sw: Float) =
    bevelFrame(tint, sw, k, 7.2f, 7.2f, 16.8f, 16.8f, corner = 2.2f)

/** 图片：切角相框 + 空心太阳 + 远山折线。 */
private fun DrawScope.drawImage(tint: Color, k: Float, sw: Float) {
    bevelFrame(tint, sw, k, 2.4f, 5.2f, 21.6f, 18.8f, corner = 2.6f)
    drawCircle(tint, 1.5f * k, Offset(8f * k, 9.6f * k), style = Stroke(width = sw))
    poly(tint, sw, k, 5.6f to 15.8f, 9.6f to 11.6f, 12.6f to 14.2f,
        15.6f to 11f, 18.6f to 13.8f)
}

/** 文件：右上切角即折角页（签名一在这枚就是 dog-ear 本身）+ 文字线，末行末端折 2 单位。 */
private fun DrawScope.drawFile(tint: Color, k: Float, sw: Float) {
    polyC(tint, sw, k, 6.4f to 2.6f, 13.8f to 2.6f, 17.6f to 6.4f, 17.6f to 21.4f, 6.4f to 21.4f)
    poly(tint, sw, k, 13.8f to 2.9f, 13.8f to 6.4f, 17.3f to 6.4f)
    seg(tint, sw, k, 9f to 12.4f, 15f to 12.4f)
    poly(tint, sw, k, 9f to 16.2f, 14f to 16.2f, 15.9f to 14.3f)
}

/** 下拉人字（模型胶囊/过程面板用）：两翼末端各折 2 单位（foldPoint 的法线在这）。 */
private fun DrawScope.drawChevronDown(tint: Color, k: Float, sw: Float) {
    val c = 12f to 15f
    val l = 6.6f to 9.8f
    val r = 17.4f to 9.8f
    poly(tint, sw, k, foldPoint(c, l, 1f), l, c, r, foldPoint(c, r, -1f))
}

/** 行尾进级人字（设置列表默认后缀）：同样两翼折角。 */
private fun DrawScope.drawChevronRight(tint: Color, k: Float, sw: Float) {
    val c = 16.5f to 12f
    val u = 11.3f to 6.6f
    val d = 11.3f to 17.4f
    poly(tint, sw, k, foldPoint(c, u, -1f), u, c, d, foldPoint(c, d, 1f))
}

/** 翻上一月：人字左右镜像（折角版本）。 */
private fun DrawScope.drawChevronLeft(tint: Color, k: Float, sw: Float) {
    val c = 7.5f to 12f
    val u = 12.7f to 6.6f
    val d = 12.7f to 17.4f
    poly(tint, sw, k, foldPoint(c, u, 1f), u, c, d, foldPoint(c, d, -1f))
}

/** 翻上一年：双枚空心人字（34dp 钮里折角伤可辨性，按语言例外条款省去）。 */
private fun DrawScope.drawChevronDoubleLeft(tint: Color, k: Float, sw: Float) {
    poly(tint, sw, k, 14.2f to 6.6f, 9.6f to 12f, 14.2f to 17.4f)
    poly(tint, sw, k, 18.6f to 6.6f, 14f to 12f, 18.6f to 17.4f)
}

/** 下一年：双枚空心人字右向。 */
private fun DrawScope.drawChevronDoubleRight(tint: Color, k: Float, sw: Float) {
    poly(tint, sw, k, 9.8f to 6.6f, 14.4f to 12f, 9.8f to 17.4f)
    poly(tint, sw, k, 5.4f to 6.6f, 10f to 12f, 5.4f to 17.4f)
}

/** 声纹（语音/键盘切换钮）：状态点 + 两道右开弧波纹。 */
private fun DrawScope.drawVoiceWave(tint: Color, k: Float, sw: Float) {
    drawCircle(tint, 1.5f * k, Offset(4.2f * k, 12f * k))
    drawArc(tint, -52f, 104f, false, Offset(4.2f * k, 8f * k), Size(8f * k, 8f * k),
        style = Stroke(width = sw, cap = StrokeCap.Round))
    drawArc(tint, -52f, 104f, false, Offset(1.5f * k, 5.2f * k), Size(13.6f * k, 13.6f * k),
        style = Stroke(width = sw, cap = StrokeCap.Round))
}

/** 抽屉菜单三横。 */
private fun DrawScope.drawMenu(tint: Color, k: Float, sw: Float) {
    seg(tint, sw, k, 4.5f to 6.8f, 19.5f to 6.8f)
    seg(tint, sw, k, 4.5f to 12f, 19.5f to 12f)
    seg(tint, sw, k, 4.5f to 17.2f, 19.5f to 17.2f)
}

/** "看见过程"的眼：杏仁双弧 + 实心瞳（状态点豁免）。 */
private fun DrawScope.drawTraceEye(tint: Color, k: Float, sw: Float) {
    drawArc(tint, 200f, 140f, false, Offset(4.2f * k, 4.2f * k), Size(15.6f * k, 15.6f * k),
        style = Stroke(width = sw, cap = StrokeCap.Round))
    drawArc(tint, 20f, 140f, false, Offset(4.2f * k, 4.2f * k), Size(15.6f * k, 15.6f * k),
        style = Stroke(width = sw, cap = StrokeCap.Round))
    drawCircle(tint, 2.1f * k, Offset(12f * k, 12f * k))
}

/** 这一步做完了：圆圈 + 对勾。 */
private fun DrawScope.drawStepDone(tint: Color, k: Float, sw: Float) {
    drawCircle(tint, 8.6f * k, Offset(12f * k, 12f * k), style = Stroke(width = sw))
    poly(tint, sw, k, 7.9f to 12.3f, 10.7f to 15.1f, 16.2f to 9.3f)
}

/** 这一步失败了：圆圈 + 叉。 */
private fun DrawScope.drawStepFailed(tint: Color, k: Float, sw: Float) {
    drawCircle(tint, 8.6f * k, Offset(12f * k, 12f * k), style = Stroke(width = sw))
    seg(tint, sw, k, 9.4f to 9.4f, 14.6f to 14.6f)
    seg(tint, sw, k, 14.6f to 9.4f, 9.4f to 14.6f)
}

/** 进行中：八颗状态点转圈，rotation 由调用方的无限动画喂进来。 */
private fun DrawScope.drawStepBusy(tint: Color, k: Float, rotation: Float) {
    for (i in 0 until 8) {
        val ang = Math.toRadians((i * 45f + rotation).toDouble())
        val fade = 1f - i / 9f
        drawCircle(tint.copy(alpha = fade), 1.35f * k,
            Offset(12f * k + (7.4f * k * cos(ang)).toFloat(),
                12f * k + (7.4f * k * sin(ang)).toFloat()))
    }
}

/** 三点呼吸（与过程面板同宗的三节点节奏）：phase 0→1 跑一圈。 */
private fun DrawScope.drawThinkingDots(tint: Color, k: Float, phase: Float) {
    listOf(6.5f, 12f, 17.5f).forEachIndexed { i, x ->
        val wave = 1f - abs(((phase + i / 3f) % 1f) * 2f - 1f)
        drawCircle(tint.copy(alpha = 0.3f + 0.7f * wave), 1.55f * k, Offset(x * k, 12f * k))
    }
}

/** 关闭/移除 ×（抽屉、模型页、附件胶囊、缩略图角标同一枚）。 */
private fun DrawScope.drawClose(tint: Color, k: Float, sw: Float) {
    seg(tint, sw, k, 7f to 7f, 17f to 17f)
    seg(tint, sw, k, 17f to 7f, 7f to 17f)
}

/** 当前项/已完成 ✓：开放长线，收笔处折 2 单位的签名收笔（foldPoint 的法线在这）。 */
private fun DrawScope.drawCheck(tint: Color, k: Float, sw: Float) {
    val bend = 10f to 17f
    val tip = 18.6f to 7.6f
    poly(tint, sw, k, 5.2f to 12.4f, bend, tip, foldPoint(bend, tip, 1f))
}

/** 赞 👍：袖口小块 + 握拳轮廓（拇指立起）。 */
private fun DrawScope.drawThumbUp(tint: Color, k: Float, sw: Float) {
    bevelFrame(tint, sw, k, 3.6f, 11.8f, 8.4f, 20.4f, corner = 1.6f)
    polyC(tint, sw, k, 8.8f to 12f, 12.2f to 12f, 14f to 4.8f, 16.8f to 5.6f,
        16.4f to 12.8f, 19.6f to 14f, 18.4f to 20f, 8.8f to 20f)
}

/** 倒赞 👎：ThumbUp 上下镜像（语义"倒"就该同形翻转，不另起一笔）。 */
private fun DrawScope.drawThumbDown(tint: Color, k: Float, sw: Float) {
    bevelFrame(tint, sw, k, 3.6f, 3.6f, 8.4f, 12.2f, corner = 1.6f)
    polyC(tint, sw, k, 8.8f to 12f, 12.2f to 12f, 14f to 19.2f, 16.8f to 18.4f,
        16.4f to 11.2f, 19.6f to 10f, 18.4f to 4f, 8.8f to 4f)
}

/** 删除（垃圾桶）：盖线 + 提手 + 收腰桶身 + 三道竖肋。 */
private fun DrawScope.drawTrash(tint: Color, k: Float, sw: Float) {
    seg(tint, sw, k, 4.6f to 6.8f, 19.4f to 6.8f)
    poly(tint, sw, k, 9.4f to 6.8f, 9.4f to 4.4f, 14.6f to 4.4f, 14.6f to 6.8f)
    poly(tint, sw, k, 6.8f to 6.8f, 7.8f to 20.6f, 16.2f to 20.6f, 17.2f to 6.8f)
    seg(tint, sw, k, 10.2f to 10.4f, 10.5f to 17.4f)
    seg(tint, sw, k, 12f to 10.4f, 12f to 17.4f)
    seg(tint, sw, k, 13.8f to 10.4f, 13.5f to 17.4f)
}

/** 编辑（铅笔）：五边笔身 + 箍线；笔尾那刀就是斜切记号。 */
private fun DrawScope.drawEdit(tint: Color, k: Float, sw: Float) {
    polyC(tint, sw, k, 4.8f to 19.2f, 6.6f to 15f, 15.2f to 6.4f, 17.6f to 8.8f, 9f to 17.4f)
    seg(tint, sw, k, 12.8f to 8.8f, 15.2f to 11.2f)
}

/** 设置（滑块三行）：三横 + 每行一颗空心钮——自家的"设置"不画齿轮。 */
private fun DrawScope.drawSettings(tint: Color, k: Float, sw: Float) {
    seg(tint, sw, k, 4f to 7.6f, 20f to 7.6f)
    seg(tint, sw, k, 4f to 12f, 20f to 12f)
    seg(tint, sw, k, 4f to 16.4f, 20f to 16.4f)
    drawCircle(tint, 1.9f * k, Offset(9f * k, 7.6f * k), style = Stroke(width = sw))
    drawCircle(tint, 1.9f * k, Offset(15f * k, 12f * k), style = Stroke(width = sw))
    drawCircle(tint, 1.9f * k, Offset(7.4f * k, 16.4f * k), style = Stroke(width = sw))
}

/** 模型服务（#）：井字四线，抽屉与设置页同一语义同一枚。 */
private fun DrawScope.drawModelService(tint: Color, k: Float, sw: Float) {
    seg(tint, sw, k, 9f to 4.6f, 9f to 19.4f)
    seg(tint, sw, k, 15f to 4.6f, 15f to 19.4f)
    seg(tint, sw, k, 4.6f to 9f, 19.4f to 9f)
    seg(tint, sw, k, 4.6f to 15f, 19.4f to 15f)
}

/** 当前模型：空心菱，顶角切成 2.8 单位平口——签名一切角在菱形上的同构。 */
private fun DrawScope.drawModel(tint: Color, k: Float, sw: Float) =
    polyC(tint, sw, k, 10.7f to 4.2f, 13.3f to 4.2f, 20.4f to 12f, 12f to 20.4f, 3.6f to 12f)

/** 长期记忆：3 节点 trace 母题（三角拓扑，空心节点），呼应过程面板三点节奏。 */
private fun DrawScope.drawMemory(tint: Color, k: Float, sw: Float) {
    val n1 = 12f to 5.8f; val n2 = 5.6f to 15.6f; val n3 = 18.4f to 15.6f
    seg(tint, sw, k, 10.6f to 7.9f, 7f to 13.5f)
    seg(tint, sw, k, 13.4f to 7.9f, 17f to 13.5f)
    seg(tint, sw, k, 8.2f to 15.6f, 15.8f to 15.6f)
    listOf(n1, n2, n3).forEach { (x, y) ->
        drawCircle(tint, NODE * k, Offset(x * k, y * k), style = Stroke(width = sw))
    }
}

/** 日程（挂历）：切角框 + 顶部两枚吊环 + 三条事项行。 */
private fun DrawScope.drawSchedule(tint: Color, k: Float, sw: Float) {
    bevelFrame(tint, sw, k, 2.8f, 4f, 21.2f, 20.6f, corner = 2.6f)
    seg(tint, sw, k, 8f to 2.2f, 8f to 5.4f)
    seg(tint, sw, k, 16f to 2.2f, 16f to 5.4f)
    seg(tint, sw, k, 6f to 10.4f, 18f to 10.4f)
    seg(tint, sw, k, 6f to 14f, 18f to 14f)
    seg(tint, sw, k, 6f to 17.6f, 14f to 17.6f)
}

/** 提醒（旗）：旗杆 + 三角旗面 + 底座短线。 */
private fun DrawScope.drawReminder(tint: Color, k: Float, sw: Float) {
    seg(tint, sw, k, 7.2f to 4.2f, 7.2f to 21.2f)
    poly(tint, sw, k, 7.2f to 4.6f, 18.6f to 8.6f, 7.2f to 12.6f)
    seg(tint, sw, k, 5.6f to 21.4f, 8.8f to 21.4f)
}

/** 触感反馈：四角星芒空心版——品牌星芒在图标语言里的投影。 */
private fun DrawScope.drawHaptic(tint: Color, k: Float, sw: Float) =
    polyC(tint, sw, k, 12f to 3.8f, 13.9f to 10.1f, 20.2f to 12f, 13.9f to 13.9f,
        12f to 20.2f, 10.1f to 13.9f, 3.8f to 12f, 10.1f to 10.1f)

/** 消息通知（铃）：圆顶弧 + 两撇外撇 + 底梁 + 摆锤弧。 */
private fun DrawScope.drawNotification(tint: Color, k: Float, sw: Float) {
    drawArc(tint, 180f, 180f, false, Offset(5.2f * k, 5.2f * k), Size(13.6f * k, 13.6f * k),
        style = Stroke(width = sw, cap = StrokeCap.Round))
    seg(tint, sw, k, 5.2f to 12f, 4.2f to 16.2f)
    seg(tint, sw, k, 18.8f to 12f, 19.8f to 16.2f)
    seg(tint, sw, k, 3.8f to 16.2f, 20.2f to 16.2f)
    drawArc(tint, 0f, 180f, false, Offset(9.4f * k, 16.2f * k), Size(5.2f * k, 5.2f * k),
        style = Stroke(width = sw, cap = StrokeCap.Round))
}

/** 清理缓存：切角方框 + 一道斜划（"抹掉"）。 */
private fun DrawScope.drawClearCache(tint: Color, k: Float, sw: Float) {
    bevelFrame(tint, sw, k, 4.4f, 4.4f, 19.6f, 19.6f, corner = 2.4f)
    seg(tint, sw, k, 8.4f to 15.6f, 15.6f to 8.4f)
}

/** 版本 ⓘ：圆圈 + 点与竖（点是状态点豁免）。 */
private fun DrawScope.drawInfo(tint: Color, k: Float, sw: Float) {
    drawCircle(tint, 8.4f * k, Offset(12f * k, 12f * k), style = Stroke(width = sw))
    dot(tint, k, 12f, 8f, 0.95f)
    seg(tint, sw, k, 12f to 11.4f, 12f to 16.4f)
}

/** 检查更新 ↻：270° 弧 + 弧端一枚右向箭头翎。 */
private fun DrawScope.drawRefresh(tint: Color, k: Float, sw: Float) {
    drawArc(tint, 60f, 240f, false, Offset(4.4f * k, 4.4f * k), Size(15.2f * k, 15.2f * k),
        style = Stroke(width = sw, cap = StrokeCap.Round))
    poly(tint, sw, k, 13.2f to 5.5f, 15.8f to 5.4f, 14.6f to 3.1f)
}

/** 外观（月牙）：两段弧在 (15,4.6)/(15,19.4) 两尖相交。 */
private fun DrawScope.drawMoon(tint: Color, k: Float, sw: Float) {
    drawArc(tint, 61.7f, 236.6f, false, Offset(2.6f * k, 3.6f * k), Size(16.8f * k, 16.8f * k),
        style = Stroke(width = sw, cap = StrokeCap.Round))
    drawArc(tint, 86.9f, 186.2f, false, Offset(7.2f * k, 4.6f * k), Size(14.8f * k, 14.8f * k),
        style = Stroke(width = sw, cap = StrokeCap.Round))
}

/** 意见反馈（信封）：切角框 + V 形封口。 */
private fun DrawScope.drawMail(tint: Color, k: Float, sw: Float) {
    bevelFrame(tint, sw, k, 2.8f, 5.8f, 21.2f, 18.2f, corner = 2.2f)
    poly(tint, sw, k, 3.6f to 7f, 12f to 12.8f, 20.4f to 7f)
}

/** 开源仓库 ↗：右上缺口的开框 + 斜箭 + 直角翎（折角的箭头用法）。 */
private fun DrawScope.drawOpenExternal(tint: Color, k: Float, sw: Float) {
    poly(tint, sw, k, 12.4f to 9.4f, 4.4f to 9.4f, 4.4f to 19.6f, 19.6f to 19.6f, 19.6f to 11.6f)
    seg(tint, sw, k, 9.6f to 14.4f, 18.2f to 5.8f)
    poly(tint, sw, k, 12.4f to 5.8f, 18.4f to 5.8f, 18.4f to 11.8f)
}

/** 许可证 ©：圆圈 + 空心 C 弧。 */
private fun DrawScope.drawLicense(tint: Color, k: Float, sw: Float) {
    drawCircle(tint, 8.4f * k, Offset(12f * k, 12f * k), style = Stroke(width = sw))
    drawArc(tint, 45f, 270f, false, Offset(8f * k, 7.4f * k), Size(9.2f * k, 9.2f * k),
        style = Stroke(width = sw, cap = StrokeCap.Round))
}

/** 服务地址（◉）：靶心——外环描边 + 实心中心点（状态点豁免）。 */
private fun DrawScope.drawServiceAddress(tint: Color, k: Float, sw: Float) {
    drawCircle(tint, 8.4f * k, Offset(12f * k, 12f * k), style = Stroke(width = sw))
    dot(tint, k, 12f, 12f, 2.6f)
}

/** 退出登录：左门框 + 右向箭（原语义就是那支 →）。 */
private fun DrawScope.drawLogout(tint: Color, k: Float, sw: Float) {
    poly(tint, sw, k, 14.4f to 4.6f, 4.8f to 4.6f, 4.8f to 19.4f, 14.4f to 19.4f)
    seg(tint, sw, k, 9.4f to 12f, 19.8f to 12f)
    poly(tint, sw, k, 15.4f to 7.6f, 19.8f to 12f, 15.4f to 16.4f)
}

/** 切换账户 ⇄：对向双箭。 */
private fun DrawScope.drawSwapAccounts(tint: Color, k: Float, sw: Float) {
    seg(tint, sw, k, 6.6f to 9.2f, 17.4f to 9.2f)
    poly(tint, sw, k, 14.2f to 6f, 17.4f to 9.2f, 14.2f to 12.4f)
    seg(tint, sw, k, 6.6f to 14.8f, 17.4f to 14.8f)
    poly(tint, sw, k, 9.8f to 11.6f, 6.6f to 14.8f, 9.8f to 18f)
}

/** 改密码（钥匙）：环孔圆 + 斜杆 + 两齿。 */
private fun DrawScope.drawPasswordKey(tint: Color, k: Float, sw: Float) {
    drawCircle(tint, 4f * k, Offset(8.6f * k, 9.4f * k), style = Stroke(width = sw))
    seg(tint, sw, k, 11.5f to 12.3f, 18.4f to 19.2f)
    seg(tint, sw, k, 14.6f to 15.4f, 15.9f to 14.1f)
    seg(tint, sw, k, 16.7f to 17.5f, 18f to 16.2f)
}

/** 导出本次对话：入盘箭——竖杆 + V 头 + 下托盘。 */
private fun DrawScope.drawExport(tint: Color, k: Float, sw: Float) {
    seg(tint, sw, k, 12f to 3.8f, 12f to 13.4f)
    poly(tint, sw, k, 7.6f to 8.6f, 12f to 13.4f, 16.4f to 8.6f)
    poly(tint, sw, k, 4.4f to 15.8f, 4.4f to 19.8f, 19.6f to 19.8f, 19.6f to 15.8f)
}

/** 测试连通（闪电）：一笔折线——开放线，收笔按签名二折 2 单位。 */
private fun DrawScope.drawBolt(tint: Color, k: Float, sw: Float) {
    poly(tint, sw, k, 13.8f to 2.6f, 6.2f to 13.4f, 11.4f to 13.4f, 9.6f to 21.4f,
        17.8f to 10.2f, 12.6f to 10.2f, 14.4f to 2.6f)
}

/** 我的默认（星）：五角星空心描边，圆角连接。 */
private fun DrawScope.drawStar(tint: Color, k: Float, sw: Float) =
    polyC(tint, sw, k, 12f to 3.2f, 14.6f to 9.2f, 20.8f to 9.8f, 16f to 14f,
        17.6f to 20.4f, 12f to 16.8f, 6.4f to 20.4f, 8f to 14f, 3.2f to 9.8f, 9.4f to 9.2f)

/** 全站默认（◎）：双环——与 ServiceAddress（靶心）靠"内环空心"区分。 */
private fun DrawScope.drawSiteDefault(tint: Color, k: Float, sw: Float) {
    drawCircle(tint, 8.4f * k, Offset(12f * k, 12f * k), style = Stroke(width = sw))
    drawCircle(tint, 4.2f * k, Offset(12f * k, 12f * k), style = Stroke(width = sw))
}

/** 上下文长度（≡）：三行递减的文本线——与 Menu（三行等长）靠长短区分。 */
private fun DrawScope.drawContext(tint: Color, k: Float, sw: Float) {
    seg(tint, sw, k, 4.6f to 7.8f, 19.4f to 7.8f)
    seg(tint, sw, k, 4.6f to 12f, 19.4f to 12f)
    seg(tint, sw, k, 4.6f to 16.2f, 14f to 16.2f)
}

/** 警示（断网提醒等状态位）：圆角连接三角框（polyC 的 StrokeJoin.Round 就是这枚的软角）
 *  + 感叹竖与状态点（点走状态点豁免）。 */
private fun DrawScope.drawWarning(tint: Color, k: Float, sw: Float) {
    polyC(tint, sw, k, 12f to 3.4f, 21.4f to 19.8f, 2.6f to 19.8f)
    seg(tint, sw, k, 12f to 9.2f, 12f to 13.6f)
    dot(tint, k, 12f, 16.6f)
}

/* ---------------- 旧函数名薄包装 ----------------
 * 这些名字被 v0.23.4 / v0.23.11 / v0.23.12 / v0.28.0 / v0.30.0 的图标契约钉着；
 * 包装只做转发，几何在上面唯一一份里，不再复制。 */

@Composable
fun IconCamera(tint: Color, modifier: Modifier = Modifier.size(20.dp)) =
    FenverIcon(FenverIcon.Camera, tint, modifier)

@Composable
fun IconMic(tint: Color, modifier: Modifier = Modifier.size(20.dp)) =
    FenverIcon(FenverIcon.Mic, tint, modifier)

@Composable
fun IconKeyboard(tint: Color, modifier: Modifier = Modifier.size(20.dp)) =
    FenverIcon(FenverIcon.Keyboard, tint, modifier)

@Composable
fun IconPlus(tint: Color, modifier: Modifier = Modifier.size(20.dp)) =
    FenverIcon(FenverIcon.Plus, tint, modifier)

@Composable
fun IconArrowUp(tint: Color, modifier: Modifier = Modifier.size(20.dp)) =
    FenverIcon(FenverIcon.ArrowUp, tint, modifier)

@Composable
fun IconArrowDown(tint: Color, modifier: Modifier = Modifier.size(20.dp)) =
    FenverIcon(FenverIcon.ArrowDown, tint, modifier)

@Composable
fun IconStop(tint: Color, modifier: Modifier = Modifier.size(20.dp)) =
    FenverIcon(FenverIcon.Stop, tint, modifier)

@Composable
fun IconImage(tint: Color, modifier: Modifier = Modifier.size(20.dp)) =
    FenverIcon(FenverIcon.Image, tint, modifier)

@Composable
fun IconFile(tint: Color, modifier: Modifier = Modifier.size(20.dp)) =
    FenverIcon(FenverIcon.File, tint, modifier)

@Composable
fun IconChevronDown(tint: Color, modifier: Modifier = Modifier.size(20.dp)) =
    FenverIcon(FenverIcon.ChevronDown, tint, modifier)

@Composable
fun IconVoiceWave(tint: Color, modifier: Modifier = Modifier.size(20.dp)) =
    FenverIcon(FenverIcon.VoiceWave, tint, modifier)

@Composable
fun IconMenu(tint: Color, modifier: Modifier = Modifier.size(20.dp)) =
    FenverIcon(FenverIcon.Menu, tint, modifier)

@Composable
fun IconTraceEye(tint: Color, modifier: Modifier = Modifier.size(20.dp)) =
    FenverIcon(FenverIcon.TraceEye, tint, modifier)

@Composable
fun IconStepDone(tint: Color, modifier: Modifier = Modifier.size(20.dp)) =
    FenverIcon(FenverIcon.StepDone, tint, modifier)

@Composable
fun IconStepFailed(tint: Color, modifier: Modifier = Modifier.size(20.dp)) =
    FenverIcon(FenverIcon.StepFailed, tint, modifier)

@Composable
fun IconStepBusy(tint: Color, rotation: Float,
                 modifier: Modifier = Modifier.size(20.dp)) =
    FenverIcon(FenverIcon.StepBusy, tint, modifier, rotation = rotation)

@Composable
fun IconThinkingDots(tint: Color, phase: Float,
                     modifier: Modifier = Modifier.size(20.dp)) =
    FenverIcon(FenverIcon.ThinkingDots, tint, modifier, phase = phase)
