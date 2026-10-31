package xyz.fenever.assistant.nativeapp.export

import android.app.DownloadManager
import android.content.Context
import android.net.Uri
import android.os.Environment

/**
 * 一次性票据 → 系统 Downloads（v0.23 T2.4，PRD 7.2-2 / R2）。
 *
 * 为什么选 DownloadManager 而不是自己流式落盘：PRD 要"完成后系统『已下载』
 * 通知"，这条通知是系统下载队列自带的——自管下载就得自己养通知渠道、点击
 * 行为、进度样式，那是把系统已经做好的东西重做一遍还做不像。断点续传和
 * Wi-Fi 大文件调度也顺手白拿。
 *
 * 纪律（调用方必须已满足，这里不复核但注释钉死）：
 * - URL 必须是 baseUrl + 过完 ExportName.isTicketPath 门的相对票据路径：
 *   DownloadManager 会把这条 URL 存进系统下载队列（跨进程、可被设置里的
 *   "下载管理"看到），所以入队前的形状门就是"不产生第二个可访问 URL"的
 *   客户端半边——另一半是后端的一次性兑换。
 * - 文件名来自 ExportName.exportFileName（服务端清洗规则的同源镜像），
 *   不是用户输入直拼——禁用字符在那边就已经换成空格了。
 * - 落点是 App 私有外部目录（M-8 第 5 条）：公共 Downloads 里的对话明码可被
 *   其他应用读到，导出的是整段对话，不能走公共目录。私有目录不需要存储权限。
 *
 * 残余：minSdk 24，Android 10 及更早持有 READ_EXTERNAL_STORAGE 的应用仍可能读到
 * `/Android/data/<pkg>/files/Download`。彻底修需 API<30 写内部 filesDir + FileProvider
 * 分享；当前接受这条残余，发版说明必须写明。
 */
object SessionDownloads {

    /** 入队并返回系统下载 id（>0）。异常一律上抛，由 UI 层兜文案。 */
    fun enqueue(context: Context, absoluteUrl: String, fileName: String): Long {
        val dm = context.getSystemService(Context.DOWNLOAD_SERVICE) as DownloadManager
        val request = DownloadManager.Request(Uri.parse(absoluteUrl)).apply {
            setTitle(fileName)
            setMimeType("text/markdown")
            // 完成时保留通知——这就是 PRD 里那颗系统「已下载」通知的来源
            setNotificationVisibility(
                DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED)
            setDestinationInExternalFilesDir(context, Environment.DIRECTORY_DOWNLOADS, fileName)
        }
        return dm.enqueue(request)
    }
}
