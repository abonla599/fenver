package xyz.fenever.assistant.nativeapp.theme

import android.app.Activity
import android.content.Context
import android.content.ContextWrapper
import android.graphics.drawable.ColorDrawable
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxScope
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Shapes
import androidx.compose.material3.Typography
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
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

/* 单主题墨绿：色板唯一真源是官网 site/site.css 的 :root。
 * 网页 /app/style.css 的 --fv-* 与下面 FenverTokens 逐字同源，三方对照由
 * backend/tests/test_design_tokens_parity_v0360.py 从官网动态读值核对。
 * 应用图标那三色不跟页面令牌走，见 tools/render-app-icon.ps1。 */
object FenverTokens {
    // ---- dark ----
    val Bg = Color(0xFF2B3D34)
    val BgSoft = Color(0xFF3B5247)
    val BgHover = Color(0xFF435C50)
    val Surface = Color(0xFF344940)
    val Surface2 = Color(0xFF3B5247)
    val Surface3 = Color(0xFF435C50)
    val Line = Color(0xFF4D6859)
    val Text = Color(0xFFF0F5F2)
    val Text2 = Color(0xFFB3C7BE)
    val Text3 = Color(0xFF9AAFA6)
    val Accent = Color(0xFF5CC79B)
    val AccentPressed = Color(0xFF74D9B2)
    val AccentSoft = Color(0xFF32493E)      // 官网 --brand-soft，实色表面
    val Ai = Color(0xFFA3A7FF)
    val AiSoft = Color(0xFF33365A)     // 官网 --ai-soft
    val Danger = Color(0xFFFF8F8F)
    val BrandInk = Color(0xFF06150F)   // 官网 --brand-ink，主色按钮上的深字
    val CodeBg = Color(0xFF2B3D34)


    // 旧调用点的过渡别名：单主题后只剩一套，全部指向上面的官网色板。
    val Accent2 = Ai
    val Accent2Soft = AiSoft
    val BtnPrimaryInk = BrandInk
    val AuthBand = Bg
    val AuthPanel = Surface2
    val Glass = Bg
    val GlassStrong = Surface
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

private val FenverShapes = Shapes(
    extraSmall = RoundedCornerShape(10.dp),
    small = RoundedCornerShape(10.dp),
    medium = RoundedCornerShape(14.dp),
    large = RoundedCornerShape(24.dp),
    extraLarge = RoundedCornerShape(24.dp),
)

// spec 4.2：系统字体，字重/行高按档位收口，局部尺寸才允许在调用点写死。
private val FenverTypography = Typography(
    headlineSmall = TextStyle(fontWeight = FontWeight.Bold, fontSize = 28.sp, lineHeight = 36.sp),
    titleLarge = TextStyle(fontWeight = FontWeight.Bold, fontSize = 22.sp, lineHeight = 30.sp),
    titleMedium = TextStyle(fontWeight = FontWeight.Bold, fontSize = 17.sp, lineHeight = 24.sp),
    bodyLarge = TextStyle(fontWeight = FontWeight.Normal, fontSize = 16.sp, lineHeight = 24.sp),
    bodyMedium = TextStyle(fontWeight = FontWeight.Normal, fontSize = 14.sp, lineHeight = 20.sp),
    bodySmall = TextStyle(fontWeight = FontWeight.Normal, fontSize = 12.sp, lineHeight = 16.sp),
    labelLarge = TextStyle(fontWeight = FontWeight.Medium, fontSize = 13.sp, lineHeight = 18.sp),
    labelMedium = TextStyle(fontWeight = FontWeight.Medium, fontSize = 13.sp, lineHeight = 18.sp),
    labelSmall = TextStyle(fontWeight = FontWeight.Normal, fontSize = 12.sp, lineHeight = 16.sp),
)

@Composable
fun AiTheme(content: @Composable () -> Unit) {
    // 单主题：窗口底色固定取官网 --bg。资源 colors.xml 管冷启动首帧，
    // 这里管之后的一切帧（键盘 adjustResize 露出窗口底时不能闪白）。
    val bg = FenverTokens.Bg
    val view = LocalView.current
    DisposableEffect(bg) {
        var c: Context = view.context
        while (c is ContextWrapper && c !is Activity) { c = c.baseContext }
        (c as? Activity)?.window?.setBackgroundDrawable(ColorDrawable(bg.toArgb()))
        onDispose { }
    }
    MaterialTheme(
        colorScheme = DarkScheme,
        shapes = FenverShapes,
        typography = FenverTypography,
        content = content,
    )
}

@Composable
fun text3Color(): Color = FenverTokens.Text3

/* 品牌记号：直接使用与桌面图标同源的 app_icon.png。 */
@Composable
fun AiBrandMark(sizeDp: Int = 44) {
    Image(
        painter = painterResource(xyz.fenever.assistant.nativeapp.R.drawable.app_icon),
        contentDescription = "Fenver",
        modifier = Modifier.size(sizeDp.dp)
            .clip(RoundedCornerShape((sizeDp / 3.14f).dp)),
    )
}

/* 等宽段（代码块）统一从这取。 */
val AiMono = FontFamily.Monospace
