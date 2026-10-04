# Render the Fenver app icon at every size the shell and the PWA need.
#
# Why a generator instead of one hand-picked PNG: the launcher needs 5 density
# buckets x 3 files (launcher / round / adaptive foreground) plus the in-app
# brand mark and the web manifest icon. Drawing them from one parametric spec
# keeps all 17 files the same artwork, and re-running this after a palette
# tweak is one command. Requires only Windows PowerShell + System.Drawing.
#
# This file stays 100% ASCII on purpose: Windows PowerShell 5.1 mis-parses
# non-ASCII .ps1 without a BOM (same rule the deploy/*.ps1 scripts follow).
#
#   powershell -ExecutionPolicy Bypass -File tools/render-app-icon.ps1

param(
    [string]$OutRoot = (Split-Path -Parent $PSScriptRoot)
)

Add-Type -AssemblyName System.Drawing

# ---- brand palette (single source of truth for the artwork) ----
# teal/indigo are the two ends of the user-bubble gradient in theme/Theme.kt;
# #7030E0 is also res/values/colors.xml:ic_launcher_background, #4F46E5 is the
# PWA theme_color in manifest.webmanifest. The mark is the product: a chat
# bubble whose typing dots grow into a spark = the live thinking trace.
$C_TEAL = '#0E7C86'
$C_INDIGO = '#7030E0'
$C_TRACE = '#4F46E5'
$C_WHITE = '#FFFFFF'

function Get-BrandColor([string]$hex) {
    return [System.Drawing.ColorTranslator]::FromHtml($hex)
}

function Add-RoundedRect($path, [single]$x, [single]$y, [single]$w, [single]$h, [single]$r) {
    $d = $r * 2
    $path.StartFigure()
    $path.AddArc($x, $y, $d, $d, 180, 90)
    $path.AddArc($x + $w - $d, $y, $d, $d, 270, 90)
    $path.AddArc($x + $w - $d, $y + $h - $d, $d, $d, 0, 90)
    $path.AddArc($x, $y + $h - $d, $d, $d, 90, 90)
    $path.CloseFigure()
}

# 4-point star (the "idea" at the end of the trace), built from 8 radii.
function Get-SparkPath([single]$cx, [single]$cy, [single]$outer, [single]$inner, [single]$k) {
    $pts = New-Object 'System.Drawing.PointF[]' 8
    for ($i = 0; $i -lt 8; $i++) {
        $r = if (($i % 2) -eq 0) { $outer } else { $inner }
        $a = [Math]::PI / 4 * $i - [Math]::PI / 2
        $pts[$i] = New-Object System.Drawing.PointF -ArgumentList `
            (($cx + $r * [Math]::Cos($a)) * $k), (($cy + $r * [Math]::Sin($a)) * $k)
    }
    $p = New-Object System.Drawing.Drawing2D.GraphicsPath
    $p.AddPolygon($pts)
    return $p
}

# Draw one icon. Design grid is 1024x1024; every coordinate below is a grid
# value and gets multiplied by k = Size / 1024.
#   Badge      : paint the gradient squircle behind the mark (legacy launcher,
#                in-app brand mark, preview). Without it the caller composites
#                the mark over its own background (adaptive foreground layer).
#   CornerPct  : squircle corner as a share of the canvas (0 = full bleed).
#   Circle     : clip to a disc (ic_launcher_round on pre-26 launchers).
function New-FenverIcon {
    param([int]$Size, [bool]$Badge, [double]$CornerPct = 0.16, [bool]$Circle = $false)

    $S = $Size
    $k = $S / 1024.0
    # Note: a [Type]::Member token written straight into -ArgumentList is parsed
    # in argument mode and arrives as a string. Bind it first, then pass it.
    $fmt = [System.Drawing.Imaging.PixelFormat]::Format32bppArgb
    $bmp = New-Object System.Drawing.Bitmap -ArgumentList $S, $S, $fmt
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
    $g.PixelOffsetMode = [System.Drawing.Drawing2D.PixelOffsetMode]::HighQuality
    $g.CompositingQuality = [System.Drawing.Drawing2D.CompositingQuality]::HighQuality
    $g.Clear([System.Drawing.Color]::Transparent)

    if ($Circle) {
        $clip = New-Object System.Drawing.Drawing2D.GraphicsPath
        $clip.AddEllipse(0, 0, $S, $S)
        $g.SetClip($clip)
    }

    $teal = Get-BrandColor $C_TEAL
    $indigo = Get-BrandColor $C_INDIGO
    $trace = Get-BrandColor $C_TRACE
    $white = Get-BrandColor $C_WHITE
    # Fill* overloads want a Brush; Color only binds to Draw* / Pen / SolidBrush.
    $whiteBrush = New-Object System.Drawing.SolidBrush -ArgumentList $white
    $traceBrush = New-Object System.Drawing.SolidBrush -ArgumentList $trace

    if ($Badge) {
        $r = $S * $CornerPct
        $bg = New-Object System.Drawing.Drawing2D.GraphicsPath
        if ($CornerPct -le 0) {
            $rect = New-Object System.Drawing.Rectangle -ArgumentList 0, 0, $S, $S
            $bg.AddRectangle($rect)
        } else {
            Add-RoundedRect $bg 0 0 $S $S $r
        }
        $rect2 = New-Object System.Drawing.Rectangle -ArgumentList 0, 0, $S, $S
        $brush = New-Object System.Drawing.Drawing2D.LinearGradientBrush -ArgumentList `
            $rect2, $teal, $indigo, 45
        $g.FillPath($brush, $bg)
        $bg.Dispose(); $brush.Dispose()
    }

    # bubble body + tail (both filled white, so the overlap is invisible)
    $body = New-Object System.Drawing.Drawing2D.GraphicsPath
    Add-RoundedRect $body (236 * $k) (258 * $k) (552 * $k) (386 * $k) (124 * $k)
    $tail = New-Object System.Drawing.Drawing2D.GraphicsPath
    $tailPts = @(
        (New-Object System.Drawing.PointF -ArgumentList (330 * $k), (600 * $k)),
        (New-Object System.Drawing.PointF -ArgumentList (292 * $k), (748 * $k)),
        (New-Object System.Drawing.PointF -ArgumentList (470 * $k), (620 * $k))
    )
    $tail.AddPolygon([System.Drawing.PointF[]]$tailPts)
    $g.FillPath($whiteBrush, $body)
    $g.FillPath($whiteBrush, $tail)
    $body.Dispose(); $tail.Dispose()

    # the thinking trace inside the bubble: two growing dots, a line, a spark
    $pen = New-Object System.Drawing.Pen -ArgumentList $trace, (16 * $k)
    $pen.StartCap = [System.Drawing.Drawing2D.LineCap]::Round
    $pen.EndCap = [System.Drawing.Drawing2D.LineCap]::Round
    $g.DrawLine($pen, (380 * $k), (451 * $k), (600 * $k), (451 * $k))
    $pen.Dispose()
    $g.FillEllipse($traceBrush, (380 - 30) * $k, (451 - 30) * $k, 60 * $k, 60 * $k)
    $g.FillEllipse($traceBrush, (492 - 40) * $k, (451 - 40) * $k, 80 * $k, 80 * $k)
    $spark = Get-SparkPath (648) (451) (86) (27) $k
    $g.FillPath($traceBrush, $spark)
    $spark.Dispose()
    $whiteBrush.Dispose(); $traceBrush.Dispose()

    $g.Dispose()
    return $bmp
}

function Save-Icon {
    param($bmp, [string]$Path)
    $dir = Split-Path -Parent $Path
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    $bmp.Save($Path, [System.Drawing.Imaging.ImageFormat]::Png)
    Write-Output ("wrote {0} ({1}x{2}, {3} B)" -f $Path, $bmp.Width, $bmp.Height,
        (Get-Item $Path).Length)
}

$RES = Join-Path $OutRoot 'android-native/app/src/main/res'
# density bucket -> (legacy launcher px, adaptive foreground px)
$DENSITIES = @(
    @{ Name = 'mdpi'; Launcher = 48; Foreground = 108 },
    @{ Name = 'hdpi'; Launcher = 72; Foreground = 162 },
    @{ Name = 'xhdpi'; Launcher = 96; Foreground = 216 },
    @{ Name = 'xxhdpi'; Launcher = 144; Foreground = 324 },
    @{ Name = 'xxxhdpi'; Launcher = 192; Foreground = 432 }
)

$made = @()
foreach ($d in $DENSITIES) {
    $dir = Join-Path $RES ('mipmap-' + $d.Name)
    $px = $d.Launcher
    # Adaptive foreground keeps the mark inside the 66/108 safe zone; the grid
    # mark spans 54% of the canvas, so scaling the whole grid is enough.
    $made += @{ Path = (Join-Path $dir 'ic_launcher.png'); Size = $px; Badge = $true; Corner = 0.16; Circle = $false }
    $made += @{ Path = (Join-Path $dir 'ic_launcher_round.png'); Size = $px; Badge = $true; Corner = 0.0; Circle = $true }
    $made += @{ Path = (Join-Path $dir 'ic_launcher_foreground.png'); Size = $d.Foreground; Badge = $false; Corner = 0.0; Circle = $false }
}
$made += @{ Path = (Join-Path $RES 'drawable-nodpi/app_icon.png'); Size = 192; Badge = $true; Corner = 0.16; Circle = $false }
# PWA: maskable wants a full-bleed square, no baked-in rounding.
$made += @{ Path = (Join-Path $OutRoot 'backend/app/web/static/icon.png'); Size = 1024; Badge = $true; Corner = 0.0; Circle = $false }
# Preview for the human deciding whether this is the icon they want.
$made += @{ Path = (Join-Path $OutRoot 'icon-preview.png'); Size = 1024; Badge = $true; Corner = 0.16; Circle = $false }

foreach ($m in $made) {
    $bmp = New-FenverIcon -Size $m.Size -Badge $m.Badge -CornerPct $m.Corner -Circle $m.Circle
    Save-Icon $bmp $m.Path
    $bmp.Dispose()
    [System.GC]::Collect()
    [System.GC]::WaitForPendingFinalizers()
}
Write-Output 'done'
