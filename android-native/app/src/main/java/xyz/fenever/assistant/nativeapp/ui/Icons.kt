package xyz.fenever.assistant.nativeapp.ui

import androidx.compose.foundation.Canvas
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.CornerRadius
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.StrokeJoin
import androidx.compose.ui.graphics.drawscope.DrawScope
import androidx.compose.ui.graphics.drawscope.Stroke

/* 自绘线性图标族（第 5 轮真机反馈：emoji 按钮「看着太简陋」）。
 * 统一语言：24 格网、1.7 描边、圆头圆角，全部 Canvas 直绘——零新依赖，
 * 深浅主题只换一个 tint。几何按 24x24 设计稿写，绘制时乘 k=画布宽/24 缩放。 */

private const val GRID = 24f

@Composable
private fun LineIcon(stroke: Float = 1.7f, modifier: Modifier = Modifier,
                     draw: DrawScope.(k: Float, sw: Float) -> Unit) {
    Canvas(modifier) {
        val k = size.width / GRID
        draw(k, stroke * k)
    }
}

/* 折线（圆头圆角连接）：坐标对是 24 格网设计值，先乘 k 再画。 */
private fun DrawScope.poly(color: Color, sw: Float, k: Float, vararg pts: Pair<Float, Float>) {
    if (pts.isEmpty()) return
    val p = Path().apply {
        moveTo(pts[0].first * k, pts[0].second * k)
        pts.drop(1).forEach { lineTo(it.first * k, it.second * k) }
    }
    drawPath(p, color, style = Stroke(width = sw, cap = StrokeCap.Round, join = StrokeJoin.Round))
}

private fun DrawScope.seg(color: Color, sw: Float, k: Float,
                          a: Pair<Float, Float>, b: Pair<Float, Float>) =
    drawLine(color, Offset(a.first * k, a.second * k), Offset(b.first * k, b.second * k),
        strokeWidth = sw, cap = StrokeCap.Round)

/** 相机：机身圆角框 + 取景凸台 + 镜头圈 + 闪光灯点。 */
@Composable
fun IconCamera(tint: Color, modifier: Modifier = Modifier) =
    LineIcon(modifier = modifier) { k, sw ->
        drawRoundRect(tint, Offset(2.6f * k, 7.6f * k), Size(18.8f * k, 12.4f * k),
            CornerRadius(3.2f * k, 3.2f * k), style = Stroke(width = sw))
        poly(tint, sw, k, 8.2f to 7.6f, 9.7f to 4.9f, 14.3f to 4.9f, 15.8f to 7.6f)
        drawCircle(tint, 3.4f * k, Offset(12f * k, 13.8f * k), style = Stroke(width = sw))
        drawCircle(tint, 0.95f * k, Offset(18.4f * k, 10.7f * k))
    }

/** 麦克风：胶囊 + 底部半圆抱弧 + 短杆底座。 */
@Composable
fun IconMic(tint: Color, modifier: Modifier = Modifier) =
    LineIcon(modifier = modifier) { k, sw ->
        drawRoundRect(tint, Offset(9f * k, 2.8f * k), Size(6f * k, 11f * k),
            CornerRadius(3f * k, 3f * k), style = Stroke(width = sw))
        drawArc(tint, 0f, 180f, false, Offset(5.5f * k, 6f * k), Size(13f * k, 13f * k),
            style = Stroke(width = sw, cap = StrokeCap.Round))
        seg(tint, sw, k, 12f to 19f, 12f to 21.4f)
        seg(tint, sw, k, 8.6f to 21.4f, 15.4f to 21.4f)
    }

/** 键盘：外框 + 两排键点 + 空格条。 */
@Composable
fun IconKeyboard(tint: Color, modifier: Modifier = Modifier) =
    LineIcon(modifier = modifier) { k, sw ->
        drawRoundRect(tint, Offset(2.6f * k, 6.4f * k), Size(18.8f * k, 11.2f * k),
            CornerRadius(2.6f * k, 2.6f * k), style = Stroke(width = sw))
        listOf(5.8f, 9.0f, 12.1f, 15.2f, 18.3f).forEach {
            drawCircle(tint, 0.8f * k, Offset(it * k, 10.1f * k))
        }
        seg(tint, sw, k, 8.6f to 13.9f, 15.4f to 13.9f)
    }

/** 加号（附件面板开合时整枚旋转 45° 变 ×）。 */
@Composable
fun IconPlus(tint: Color, modifier: Modifier = Modifier) =
    LineIcon(modifier = modifier) { k, sw ->
        seg(tint, sw, k, 12f to 5.2f, 12f to 18.8f)
        seg(tint, sw, k, 5.2f to 12f, 18.8f to 12f)
    }

/** 发送：上箭头（杆 + 人字头）。 */
@Composable
fun IconArrowUp(tint: Color, modifier: Modifier = Modifier) =
    LineIcon(modifier = modifier) { k, sw ->
        seg(tint, sw, k, 12f to 19.2f, 12f to 6.2f)
        poly(tint, sw, k, 6.6f to 11.4f, 12f to 6f, 17.4f to 11.4f)
    }

/** 停止：实心圆角小方块。 */
@Composable
fun IconStop(tint: Color, modifier: Modifier = Modifier) =
    LineIcon(modifier = modifier) { k, _ ->
        drawRoundRect(tint, Offset(8f * k, 8f * k), Size(8f * k, 8f * k),
            CornerRadius(2f * k, 2f * k))
    }

/** 图片：相框 + 太阳点 + 远山折线。 */
@Composable
fun IconImage(tint: Color, modifier: Modifier = Modifier) =
    LineIcon(modifier = modifier) { k, sw ->
        drawRoundRect(tint, Offset(3f * k, 5.2f * k), Size(18f * k, 13.6f * k),
            CornerRadius(2.6f * k, 2.6f * k), style = Stroke(width = sw))
        drawCircle(tint, 1.7f * k, Offset(8.2f * k, 9.8f * k))
        poly(tint, sw, k, 5.6f to 16.4f, 10f to 11.8f, 13.2f to 14.8f, 15.8f to 12.6f, 18.6f to 15.4f)
    }

/** 文件：折角页 + 两道文字线。 */
@Composable
fun IconFile(tint: Color, modifier: Modifier = Modifier) =
    LineIcon(modifier = modifier) { k, sw ->
        poly(tint, sw, k, 6.2f to 3.2f, 14f to 3.2f, 17.8f to 7f, 17.8f to 20.8f,
            6.2f to 20.8f, 6.2f to 3.2f)
        poly(tint, sw, k, 14f to 3.2f, 14f to 7f, 17.8f to 7f)
        seg(tint, sw, k, 9f to 12.6f, 15f to 12.6f)
        seg(tint, sw, k, 9f to 16.2f, 15f to 16.2f)
    }

/** 下拉人字（模型胶囊用）。 */
@Composable
fun IconChevronDown(tint: Color, modifier: Modifier = Modifier) =
    LineIcon(modifier = modifier) { k, sw ->
        poly(tint, sw, k, 7.5f to 10f, 12f to 14.6f, 16.5f to 10f)
    }

/** 声纹（第 12 轮豆包三态输入区）：实心点 + 两道右开弧波纹，语音/键盘切换钮用。 */
@Composable
fun IconVoiceWave(tint: Color, modifier: Modifier = Modifier) =
    LineIcon(modifier = modifier) { k, sw ->
        drawCircle(tint, 1.5f * k, Offset(4.2f * k, 12f * k))
        drawArc(tint, -52f, 104f, false, Offset(4.2f * k, 8f * k), Size(8f * k, 8f * k),
            style = Stroke(width = sw, cap = StrokeCap.Round))
        drawArc(tint, -52f, 104f, false, Offset(1.5f * k, 5.2f * k), Size(13.6f * k, 13.6f * k),
            style = Stroke(width = sw, cap = StrokeCap.Round))
    }

/** 抽屉菜单三横。 */
@Composable
fun IconMenu(tint: Color, modifier: Modifier = Modifier) =
    LineIcon(modifier = modifier) { k, sw ->
        seg(tint, sw, k, 4.5f to 6.8f, 19.5f to 6.8f)
        seg(tint, sw, k, 4.5f to 12f, 19.5f to 12f)
        seg(tint, sw, k, 4.5f to 17.2f, 19.5f to 17.2f)
    }
