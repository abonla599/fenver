package xyz.fenever.assistant.nativeapp.ui

import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxScope
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ColumnScope
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.RowScope
import androidx.compose.foundation.layout.defaultMinSize
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.BasicTextField
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.minimumInteractiveComponentSize
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.rotate
import androidx.compose.ui.draw.shadow
import androidx.compose.ui.focus.onFocusChanged
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Shape
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.VisualTransformation
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.ui.window.Dialog
import androidx.compose.ui.window.DialogProperties

/* 极简原生组件原语：所有颜色只从 MaterialTheme 取，默认无阴影，
 * 层级靠 1dp 发丝线与表面色阶，不靠渐变或光晕。 */

/** 中性表面：surface/surfaceVariant + 1dp outline，可选点击。 */
@Composable
fun FenverSurface(
    modifier: Modifier = Modifier,
    color: Color = MaterialTheme.colorScheme.surface,
    border: BorderStroke? = BorderStroke(1.dp, MaterialTheme.colorScheme.outline),
    shape: Shape = RoundedCornerShape(16.dp),
    contentPadding: PaddingValues = PaddingValues(0.dp),
    contentAlignment: Alignment = Alignment.TopStart,
    onClick: (() -> Unit)? = null,
    content: @Composable BoxScope.() -> Unit,
) {
    Surface(
        modifier = modifier.then(
            if (onClick != null) Modifier.clip(shape).clickable(onClick = onClick)
            else Modifier),
        shape = shape,
        color = color,
        border = border,
        tonalElevation = 0.dp,
        shadowElevation = 0.dp,
    ) {
        Box(Modifier.padding(contentPadding),
            contentAlignment = contentAlignment, content = content)
    }
}

/** 列表行：leading 20dp 图标位、标题/副标题、trailing，最小高度 52dp。 */
@Composable
fun FenverRow(
    title: String,
    modifier: Modifier = Modifier,
    leading: (@Composable () -> Unit)? = null,
    subtitle: String? = null,
    trailing: (@Composable () -> Unit)? = null,
    onClick: (() -> Unit)? = null,
    divider: Boolean = false,
    minHeight: Dp = 52.dp,
) {
    Column(modifier.fillMaxWidth()) {
        Row(
            Modifier.fillMaxWidth()
                .then(if (onClick != null) Modifier.clickable(onClick = onClick) else Modifier)
                .defaultMinSize(minHeight = minHeight)
                .padding(horizontal = 16.dp, vertical = 8.dp),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            if (leading != null) {
                Box(Modifier.size(20.dp), contentAlignment = Alignment.Center) { leading() }
            }
            Column(Modifier.weight(1f)) {
                Text(title, style = MaterialTheme.typography.bodyLarge,
                    color = MaterialTheme.colorScheme.onSurface,
                    maxLines = 1, overflow = TextOverflow.Ellipsis)
                if (subtitle != null) {
                    Text(subtitle, style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                        maxLines = 2, overflow = TextOverflow.Ellipsis)
                }
            }
            trailing?.invoke()
        }
        if (divider) {
            Box(Modifier.fillMaxWidth().height(1.dp)
                .background(MaterialTheme.colorScheme.outline))
        }
    }
}

enum class FenverButtonKind { Primary, Secondary, Ghost, Danger }

/** 实心/次级/幽灵/危险四档，统一 44dp 高，无渐变无光晕。 */
@Composable
fun FenverButton(
    text: String,
    onClick: () -> Unit,
    kind: FenverButtonKind = FenverButtonKind.Primary,
    enabled: Boolean = true,
    modifier: Modifier = Modifier,
) {
    val scheme = MaterialTheme.colorScheme
    val background = when (kind) {
        FenverButtonKind.Primary -> scheme.primary
        FenverButtonKind.Secondary -> scheme.surfaceVariant
        FenverButtonKind.Ghost -> Color.Transparent
        FenverButtonKind.Danger -> scheme.error
    }
    val ink = when (kind) {
        FenverButtonKind.Primary -> scheme.onPrimary
        FenverButtonKind.Secondary -> scheme.onSurface
        FenverButtonKind.Ghost -> scheme.primary
        FenverButtonKind.Danger -> scheme.onError
    }
    val border = when (kind) {
        FenverButtonKind.Secondary -> BorderStroke(1.dp, scheme.outline)
        else -> null
    }
    Surface(
        modifier = modifier.heightIn(min = 44.dp)
            .then(if (enabled) Modifier.clickable(onClick = onClick) else Modifier),
        shape = RoundedCornerShape(12.dp),
        color = background,
        border = border,
        tonalElevation = 0.dp,
        shadowElevation = 0.dp,
    ) {
        Box(
            Modifier.fillMaxWidth().heightIn(min = 44.dp).padding(horizontal = 16.dp),
            contentAlignment = Alignment.Center,
        ) {
            Text(text, style = MaterialTheme.typography.labelLarge,
                color = ink.copy(alpha = if (enabled) 1f else 0.5f),
                maxLines = 1, overflow = TextOverflow.Ellipsis)
        }
    }
}

/** 输入框：surfaceVariant 底、12dp 圆角、获得焦点时 1dp primary 描边。
 *  密码遮蔽 / 行尾控件 / 就地报错走可选参数，登录页不必另写一套输入框。 */
@Composable
fun FenverTextField(
    value: String,
    onValueChange: (String) -> Unit,
    placeholder: String = "",
    modifier: Modifier = Modifier,
    singleLine: Boolean = true,
    enabled: Boolean = true,
    minHeight: Dp = 44.dp,
    isError: Boolean = false,
    keyboardType: KeyboardType = KeyboardType.Text,
    imeAction: ImeAction = ImeAction.Done,
    visualTransformation: VisualTransformation = VisualTransformation.None,
    trailing: (@Composable () -> Unit)? = null,
) {
    val scheme = MaterialTheme.colorScheme
    var focused by remember { mutableStateOf(false) }
    val shape = RoundedCornerShape(12.dp)
    val stroke = when {
        isError -> scheme.error
        focused -> scheme.primary
        else -> scheme.outline
    }
    BasicTextField(
        value = value,
        onValueChange = onValueChange,
        enabled = enabled,
        singleLine = singleLine,
        visualTransformation = visualTransformation,
        textStyle = MaterialTheme.typography.bodyLarge.copy(color = scheme.onSurface),
        cursorBrush = SolidColor(scheme.primary),
        keyboardOptions = KeyboardOptions(keyboardType = keyboardType, imeAction = imeAction),
        modifier = modifier
            .fillMaxWidth()
            .defaultMinSize(minHeight = minHeight)
            .background(scheme.surfaceVariant, shape)
            .border(BorderStroke(1.dp, stroke), shape)
            .padding(horizontal = 12.dp, vertical = 11.dp)
            .onFocusChanged { focused = it.isFocused },
        decorationBox = { inner ->
            Row(verticalAlignment = Alignment.CenterVertically) {
                Box(Modifier.weight(1f), contentAlignment = Alignment.CenterStart) {
                    if (value.isEmpty() && placeholder.isNotEmpty()) {
                        Text(placeholder, style = MaterialTheme.typography.bodyLarge,
                            color = scheme.onSurfaceVariant)
                    }
                    inner()
                }
                trailing?.invoke()
            }
        },
    )
}

/** 顶栏：56dp；返回/自定义前导二选一，标题单行省略，动作位由调用方给。 */
@Composable
fun FenverTopBar(
    title: String,
    modifier: Modifier = Modifier,
    onBack: (() -> Unit)? = null,
    leading: (@Composable () -> Unit)? = null,
    actions: @Composable RowScope.() -> Unit = {},
) {
    Column(modifier.fillMaxWidth().background(MaterialTheme.colorScheme.surface)) {
        Row(
            Modifier.fillMaxWidth().heightIn(min = 56.dp).padding(horizontal = 4.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            when {
                onBack != null -> FenverTopBarAction(onBack) {
                    IconChevronDown(MaterialTheme.colorScheme.onSurface,
                        Modifier.size(20.dp).rotate(90f))
                }
                leading != null -> leading()
            }
            Text(
                title,
                style = MaterialTheme.typography.titleMedium,
                color = MaterialTheme.colorScheme.onSurface,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
                modifier = Modifier.weight(1f).padding(horizontal = 4.dp),
            )
            actions()
        }
        Box(Modifier.fillMaxWidth().height(1.dp)
            .background(MaterialTheme.colorScheme.outline))
    }
}

/** 顶栏动作位：44dp 可点区，图标由调用方给。 */
@Composable
fun FenverTopBarAction(onClick: () -> Unit, content: @Composable () -> Unit) {
    Box(
        Modifier.size(44.dp).clip(CircleShape).clickable(onClick = onClick),
        contentAlignment = Alignment.Center,
    ) { content() }
}

/** 底部弹层：顶部 24dp 圆角、surface 底、一层轻阴影，不提供拖拽关闭。
 *  inline=true 时只画壳、不新开窗口——按住说话的手势锚点在 Activity 窗口里，
 *  Dialog 会把按下中的触摸序列抢走，录音卡只能在原位渲染同一套壳。 */
@Composable
fun FenverSheet(
    onDismiss: () -> Unit,
    modifier: Modifier = Modifier,
    inline: Boolean = false,
    content: @Composable ColumnScope.() -> Unit,
) {
    if (inline) {
        FenverSheetShell(modifier, content)
        return
    }
    Dialog(
        onDismissRequest = onDismiss,
        properties = DialogProperties(usePlatformDefaultWidth = false),
    ) {
        Box(Modifier.fillMaxSize(), contentAlignment = Alignment.BottomCenter) {
            FenverSheetShell(modifier, content)
        }
    }
}

@Composable
private fun FenverSheetShell(
    modifier: Modifier,
    content: @Composable ColumnScope.() -> Unit,
) {
    val shape = RoundedCornerShape(topStart = 24.dp, topEnd = 24.dp)
    Column(
        modifier
            .fillMaxWidth()
            .shadow(24.dp, shape)
            .clip(shape)
            .background(MaterialTheme.colorScheme.surface)
            .padding(horizontal = 16.dp, vertical = 16.dp),
        content = content,
    )
}

/** 模型/状态胶囊：999 圆角、label 字号，视觉小、触控区仍按 48dp 兜底。 */
@Composable
fun FenverChip(
    text: String,
    selected: Boolean = false,
    modifier: Modifier = Modifier,
    onClick: (() -> Unit)? = null,
    trailing: (@Composable () -> Unit)? = null,
) {
    val scheme = MaterialTheme.colorScheme
    Box(
        modifier.minimumInteractiveComponentSize()
            .then(if (onClick != null) Modifier.clickable(onClick = onClick) else Modifier),
        contentAlignment = Alignment.Center,
    ) {
        Row(
            Modifier
                .clip(CircleShape)
                .background(
                    if (selected) scheme.primaryContainer else scheme.surfaceVariant,
                    CircleShape)
                .border(
                    BorderStroke(1.dp, if (selected) scheme.primary else scheme.outline),
                    CircleShape)
                .padding(horizontal = 12.dp, vertical = 6.dp),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(4.dp),
        ) {
            Text(text, style = MaterialTheme.typography.labelLarge,
                color = if (selected) scheme.primary else scheme.onSurface,
                maxLines = 1, overflow = TextOverflow.Ellipsis,
                modifier = Modifier.widthIn(max = 140.dp))
            trailing?.invoke()
        }
    }
}

/** 通用卡片：1dp outline、16dp 圆角、无阴影；内容由调用方按列排。 */
@Composable
fun FenverCard(
    modifier: Modifier = Modifier,
    color: Color = MaterialTheme.colorScheme.surface,
    shape: Shape = RoundedCornerShape(16.dp),
    contentPadding: PaddingValues = PaddingValues(16.dp),
    content: @Composable ColumnScope.() -> Unit,
) {
    FenverSurface(
        modifier = modifier,
        color = color,
        border = BorderStroke(1.dp, MaterialTheme.colorScheme.outline),
        shape = shape,
        contentPadding = contentPadding,
    ) {
        Column(content = content)
    }
}

/** 品牌字：单色，默认 onSurface；强调位可传 primary。 */
@Composable
fun BrandText(
    text: String,
    size: Int = 22,
    modifier: Modifier = Modifier,
    color: Color = MaterialTheme.colorScheme.onSurface,
) {
    Text(
        text,
        modifier,
        style = MaterialTheme.typography.titleLarge.copy(
            fontSize = size.sp, fontWeight = FontWeight.SemiBold),
        color = color,
    )
}
