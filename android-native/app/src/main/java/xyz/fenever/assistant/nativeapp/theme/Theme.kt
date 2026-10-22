package xyz.fenever.assistant.nativeapp.theme

import android.app.Activity
import android.content.Context
import android.content.ContextWrapper
import android.graphics.drawable.ColorDrawable
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxScope
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Shapes
import androidx.compose.material3.Typography
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.graphics.toArgb
import androidx.compose.ui.platform.LocalView
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp

/* v0.36.0 极简原生：中性表面 + 单强调色 + 发丝线，数值以 spec 4.1 为准。
 * 深/浅两套共用一组名字；旧调用点用到的过渡 token 保留为同族映射，Task 2/3 再收口。 */
object FenverTokens {
    // ---- dark ----
    val Bg = Color(0xFF0A0B0E)
    val BgSoft = Color(0xFF181A20)
    val BgHover = Color(0xFF1F222A)
    val Surface = Color(0xFF121317)
    val Surface2 = Color(0xFF181A20)
    val Surface3 = Color(0xFF1F222A)
    val Line = Color(0xFF262A31)
    val Text = Color(0xFFF2F3F5)
    val Text2 = Color(0xFFA1A6B0)
    val Text3 = Color(0xFF6F747E)
    val Accent = Color(0xFF3DD598)
    val AccentPressed = Color(0xFF35C98C)
    val AccentSoft = Color(0x1F3DD598)      // rgba(61,213,152,.12)
    val Ai = Color(0xFF7C83FF)
    val Danger = Color(0xFFF87171)
    val CodeBg = Color(0xFF08090C)

    // ---- light ----
    val LBg = Color(0xFFF7F8FA)
    val LBgSoft = Color(0xFFEEF0F4)
    val LBgHover = Color(0xFFE4E7EC)
    val LSurface = Color(0xFFFFFFFF)
    val LSurface2 = Color(0xFFEEF0F4)
    val LSurface3 = Color(0xFFE4E7EC)
    val LLine = Color(0xFFE3E6EB)
    val LText = Color(0xFF16181D)
    val LText2 = Color(0xFF5F6672)
    val LText3 = Color(0xFF8A919C)
    val LAccent = Color(0xFF0A7D5E)
    val LAccentPressed = Color(0xFF086A4F)
    val LAccentSoft = Color(0xFFE6F6F0)
    val LAi = Color(0xFF5566F0)
    val LDanger = Color(0xFFDC2626)
    val LCodeBg = Color(0xFFF3F4F6)

    // 旧调用点的过渡别名：Accent2 即 AI 身份色，Auth*/Glass* 收编为表面色阶。
    val Accent2 = Ai
    val LAccent2 = LAi
    val Accent2Soft = Color(0x297C83FF)
    val LAccent2Soft = Color(0x1F5566F0)
    val BtnPrimaryInk = Bg
    val AuthBand = Bg
    val LAuthBand = LBg
    val AuthPanel = Surface
    val LAuthPanel = LSurface
    val Glass = Bg
    val LGlass = LBg
    val GlassStrong = Surface
    val LGlassStrong = LSurface
}

private val DarkScheme = darkColorScheme(
    primary = FenverTokens.Accent,
    onPrimary = FenverTokens.BtnPrimaryInk,
    primaryContainer = FenverTokens.AccentSoft,
    onPrimaryContainer = FenverTokens.Accent,
    secondary = FenverTokens.Ai,
    onSecondary = Color.White,
    secondaryContainer = FenverTokens.Surface3,
    onSecondaryContainer = FenverTokens.Text,
    tertiary = FenverTokens.Ai,
    onTertiary = Color.White,
    background = FenverTokens.Bg,
    onBackground = FenverTokens.Text,
    surface = FenverTokens.Surface,
    onSurface = FenverTokens.Text,
    surfaceVariant = FenverTokens.Surface2,
    onSurfaceVariant = FenverTokens.Text2,
    error = FenverTokens.Danger,
    onError = FenverTokens.Bg,
    errorContainer = FenverTokens.Surface3,
    onErrorContainer = FenverTokens.Danger,
    outline = FenverTokens.Line,
    outlineVariant = FenverTokens.Line,
)

private val LightScheme = lightColorScheme(
    primary = FenverTokens.LAccent,
    onPrimary = Color.White,
    primaryContainer = FenverTokens.LAccentSoft,
    onPrimaryContainer = FenverTokens.LAccent,
    secondary = FenverTokens.LAi,
    onSecondary = Color.White,
    secondaryContainer = FenverTokens.LSurface2,
    onSecondaryContainer = FenverTokens.LText,
    tertiary = FenverTokens.LAi,
    onTertiary = Color.White,
    background = FenverTokens.LBg,
    onBackground = FenverTokens.LText,
    surface = FenverTokens.LSurface,
    onSurface = FenverTokens.LText,
    surfaceVariant = FenverTokens.LSurface2,
    onSurfaceVariant = FenverTokens.LText2,
    error = FenverTokens.LDanger,
    onError = Color.White,
    errorContainer = FenverTokens.LSurface2,
    onErrorContainer = FenverTokens.LDanger,
    outline = FenverTokens.LLine,
    outlineVariant = FenverTokens.LLine,
)

private val FenverShapes = Shapes(
    extraSmall = RoundedCornerShape(4.dp),
    small = RoundedCornerShape(8.dp),
    medium = RoundedCornerShape(12.dp),
    large = RoundedCornerShape(16.dp),
    extraLarge = RoundedCornerShape(24.dp),
)

// spec 4.2：系统字体，字重/行高按档位收口，局部尺寸才允许在调用点写死。
private val FenverTypography = Typography(
    headlineSmall = TextStyle(fontWeight = FontWeight.SemiBold, fontSize = 28.sp, lineHeight = 36.sp),
    titleLarge = TextStyle(fontWeight = FontWeight.SemiBold, fontSize = 22.sp, lineHeight = 30.sp),
    titleMedium = TextStyle(fontWeight = FontWeight.SemiBold, fontSize = 17.sp, lineHeight = 24.sp),
    bodyLarge = TextStyle(fontWeight = FontWeight.Normal, fontSize = 16.sp, lineHeight = 24.sp),
    bodyMedium = TextStyle(fontWeight = FontWeight.Normal, fontSize = 14.sp, lineHeight = 20.sp),
    bodySmall = TextStyle(fontWeight = FontWeight.Normal, fontSize = 12.sp, lineHeight = 16.sp),
    labelLarge = TextStyle(fontWeight = FontWeight.Medium, fontSize = 13.sp, lineHeight = 18.sp),
    labelMedium = TextStyle(fontWeight = FontWeight.Medium, fontSize = 13.sp, lineHeight = 18.sp),
    labelSmall = TextStyle(fontWeight = FontWeight.Normal, fontSize = 12.sp, lineHeight = 16.sp),
)

/* 外观三态（system/dark/light）：设置页「外观」行换成分段控件后，
 * "system" 在渲染时按 isSystemInDarkTheme() 落成真深浅，跟随系统切换。
 * 默认仍是深色 = 网页 :root。用 Compose state：切一下整棵树立刻换肤。 */
object ThemeMode {
    var value: String by mutableStateOf("dark")
}

@Composable
fun isWebLight(): Boolean = MaterialTheme.colorScheme.background == FenverTokens.LBg

@Composable
fun fenverAccent(): Color = MaterialTheme.colorScheme.primary

@Composable
fun fenverSurface(): Color = MaterialTheme.colorScheme.surface

@Composable
fun fenverSurface2(): Color = MaterialTheme.colorScheme.surfaceVariant

@Composable
fun fenverLine(): Color = MaterialTheme.colorScheme.outline

@Composable
fun fenverText(): Color = MaterialTheme.colorScheme.onSurface

@Composable
fun fenverText2(): Color = MaterialTheme.colorScheme.onSurfaceVariant

@Composable
fun AiTheme(content: @Composable () -> Unit) {
    val dark = when (ThemeMode.value) {
        "light" -> false
        "dark" -> true
        else -> !isSystemInDarkTheme()
    }
    // v0.28（用户真机反馈「点输入框时，键盘出现之前会出现白色闪屏」）：键盘以
    // adjustResize 顶起窗口时，输入卡下方露出来的是**窗口底色**。res/values/
    // themes.xml 把冷启动首帧钉成深色画布（App 默认外观），但外观是运行时可换的
    // 三态——浅色/跟随系统落浅色时，底色必须跟着换回白，否则闪屏只是换了个颜色。
    // 换肤的每一拍在这里重设底色：资源管首帧，这里管之后的一切帧。
    val bg = if (dark) FenverTokens.Bg else FenverTokens.LBg
    val view = LocalView.current
    DisposableEffect(bg) {
        var c: Context = view.context
        while (c is ContextWrapper && c !is Activity) { c = c.baseContext }
        (c as? Activity)?.window?.setBackgroundDrawable(ColorDrawable(bg.toArgb()))
        onDispose { }
    }
    MaterialTheme(
        colorScheme = if (dark) DarkScheme else LightScheme,
        shapes = FenverShapes,
        typography = FenverTypography,
        content = content,
    )
}

/* Task 2/3 会替换全部调用点；本任务先保留函数名，内部收成单色 accent，
 * 避免旧调用点继续画渐变与光晕。 */
@Composable
fun aiPrimaryBrush(): Brush =
    SolidColor(if (isWebLight()) FenverTokens.LAccent else FenverTokens.Accent)

@Composable
fun aiSoftBrush(): Brush =
    SolidColor(if (isWebLight()) FenverTokens.LAccentSoft else FenverTokens.AccentSoft)

@Composable
fun aiVerticalBrush(): Brush =
    SolidColor(if (isWebLight()) FenverTokens.LAccent else FenverTokens.Accent)

@Composable
fun userBubbleBrush(): Brush =
    SolidColor(if (isWebLight()) FenverTokens.LAccentSoft else FenverTokens.AccentSoft)

@Composable
fun glassColor(): Color = if (isWebLight()) FenverTokens.LGlass else FenverTokens.Glass

@Composable
fun glassStrongColor(): Color =
    if (isWebLight()) FenverTokens.LGlassStrong else FenverTokens.GlassStrong

@Composable
fun text3Color(): Color = if (isWebLight()) FenverTokens.LText3 else FenverTokens.Text3

/* 氛围光与网页 body 一致：右上紫罗兰、左下薄荷，很淡，不随内容滚动。
 * Task 2/3 移除整层光晕前，这里仍按 token 取色。 */
@Composable
fun AiGlowBackground(content: @Composable BoxScope.() -> Unit) {
    val light = isWebLight()
    val glow2 = if (light) FenverTokens.LAccent2Soft else FenverTokens.Accent2Soft
    val glowA = if (light) FenverTokens.LAccentSoft else FenverTokens.AccentSoft
    Box(Modifier.fillMaxSize().background(MaterialTheme.colorScheme.background)) {
        Canvas(Modifier.fillMaxSize()) {
            drawCircle(
                Brush.radialGradient(listOf(glow2, Color.Transparent),
                    center = Offset(size.width * 0.88f, -size.height * 0.12f),
                    radius = size.width * 0.72f),
                radius = size.width * 0.72f,
                center = Offset(size.width * 0.88f, -size.height * 0.12f))
            drawCircle(
                Brush.radialGradient(listOf(glowA, Color.Transparent),
                    center = Offset(-size.width * 0.12f, size.height * 1.08f),
                    radius = size.width * 0.66f),
                radius = size.width * 0.66f,
                center = Offset(-size.width * 0.12f, size.height * 1.08f))
        }
        content()
    }
}

/* 品牌记号：直接 use 网页同一枚 icon.png（渐变圆角方块 + 白气泡三点），
 * 不再靠 Canvas 画近似形——用户原话「里面的这个图标也一点都不好看」。
 * 资源在 res/drawable-nodpi/app_icon.png（由 launcher 同一张源图裁出）。 */
@Composable
fun AiBrandMark(sizeDp: Int = 44) {
    Image(
        painter = painterResource(xyz.fenever.assistant.nativeapp.R.drawable.app_icon),
        contentDescription = "Fenver",
        modifier = Modifier.size(sizeDp.dp)
            .clip(RoundedCornerShape((sizeDp / 3.14f).dp)),
    )
}

/* 品牌字：调用点保留到 Task 2；内部先收成单色 accent，不再画渐变。 */
@Composable
fun GradientText(text: String, fontSize: Int, modifier: Modifier = Modifier,
                 weight: FontWeight = FontWeight.SemiBold) {
    androidx.compose.material3.Text(
        text, modifier,
        style = TextStyle(
            brush = SolidColor(if (isWebLight()) FenverTokens.LAccent else FenverTokens.Accent),
            fontWeight = weight,
            fontSize = fontSize.sp,
            lineHeight = (fontSize * 1.4).sp))
}

/* 等宽段（代码块）统一从这取。 */
val AiMono = FontFamily.Monospace
