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
# v0.36 minimal mark: dark neutral surface, one light bubble outline, mint
# trace. These are the same tokens the native theme draws with
# (theme/Theme.kt: Surface #121317, Text #F2F3F5, Accent #3DD598). The mark is
# still the product: a chat bubble whose typing dots grow into a spark = the
# live thinking trace. No gradient, no shadow, no text.
$C_SURFACE = '#121317'
$C_TEXT = '#F2F3F5'
$C_MINT = '#3DD598'

# Stroke width in grid units (canvas is 1024). 44 units renders 2.06 px on the
# 48 px launcher, so the outline never drops below a 2 px hairline in the
# smallest bucket; every larger size scales proportionally from there.
$STROKE = 44

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

# The bubble is one closed outline: a rounded rectangle with a tail cut into
# its bottom edge. Stroking the combined path keeps the tail hollow (surface
# shows through) instead of gluing a solid wedge onto a stroked box, which is
# what reads as "outline icon" at every size.
function Get-BubblePath([single]$k) {
    $x0 = 236 * $k
    $y0 = 258 * $k
    $x1 = 788 * $k
    $y1 = 644 * $k
    $r = 124 * $k
    $tailStart = 400 * $k
    $tailEnd = 610 * $k
    $tipX = 262 * $k
    $tipY = 752 * $k
    $p = New-Object System.Drawing.Drawing2D.GraphicsPath
    $p.StartFigure()
    $p.AddLine($x0 + $r, $y0, $x1 - $r, $y0)
    $p.AddArc($x1 - 2 * $r, $y0, 2 * $r, 2 * $r, 270, 90)
    $p.AddLine($x1, $y0 + $r, $x1, $y1 - $r)
    $p.AddArc($x1 - 2 * $r, $y1 - 2 * $r, 2 * $r, 2 * $r, 0, 90)
    $p.AddLine($x1 - $r, $y1, $tailEnd, $y1)
    $p.AddLine($tailEnd, $y1, $tipX, $tipY)
    $p.AddLine($tipX, $tipY, $tailStart, $y1)
    $p.AddLine($tailStart, $y1, $x0 + $r, $y1)
    $p.AddArc($x0, $y1 - 2 * $r, 2 * $r, 2 * $r, 90, 90)
    $p.AddLine($x0, $y1 - $r, $x0, $y0 + $r)
    $p.AddArc($x0, $y0, 2 * $r, 2 * $r, 180, 90)
    $p.CloseFigure()
    return $p
}

# Draw one icon. Design grid is 1024x1024; every coordinate below is a grid
# value and gets multiplied by k = Size / 1024.
#   Badge      : paint the solid squircle behind the mark (legacy launcher,
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

    $surface = Get-BrandColor $C_SURFACE
    $text = Get-BrandColor $C_TEXT
    $mint = Get-BrandColor $C_MINT

    if ($Badge) {
        $bg = New-Object System.Drawing.Drawing2D.GraphicsPath
        if ($CornerPct -le 0) {
            $rect = New-Object System.Drawing.Rectangle -ArgumentList 0, 0, $S, $S
            $bg.AddRectangle($rect)
        } else {
            Add-RoundedRect $bg 0 0 $S $S ($S * $CornerPct)
        }
        $bgBrush = New-Object System.Drawing.SolidBrush -ArgumentList $surface
        $g.FillPath($bgBrush, $bg)
        $bg.Dispose(); $bgBrush.Dispose()
    }

    # bubble outline (light stroke on the dark surface, tail included)
    $stroke = $STROKE * $k
    $bubblePen = New-Object System.Drawing.Pen -ArgumentList $text, $stroke
    $bubblePen.LineJoin = [System.Drawing.Drawing2D.LineJoin]::Round
    $bubblePen.StartCap = [System.Drawing.Drawing2D.LineCap]::Round
    $bubblePen.EndCap = [System.Drawing.Drawing2D.LineCap]::Round
    $bubble = Get-BubblePath $k
    $g.DrawPath($bubblePen, $bubble)
    $bubble.Dispose(); $bubblePen.Dispose()

    # the thinking trace inside the bubble: two solid dots, one thin line,
    # one 4-point spark. Same mint, no gradient.
    $mintBrush = New-Object System.Drawing.SolidBrush -ArgumentList $mint
    $g.FillEllipse($mintBrush, (350 - 30) * $k, (451 - 30) * $k, 60 * $k, 60 * $k)
    $g.FillEllipse($mintBrush, (470 - 42) * $k, (451 - 42) * $k, 84 * $k, 84 * $k)
    $tracePen = New-Object System.Drawing.Pen -ArgumentList $mint, $stroke
    $tracePen.StartCap = [System.Drawing.Drawing2D.LineCap]::Round
    $tracePen.EndCap = [System.Drawing.Drawing2D.LineCap]::Round
    $g.DrawLine($tracePen, 520 * $k, 451 * $k, 596 * $k, 451 * $k)
    $tracePen.Dispose()
    $spark = Get-SparkPath (662) (451) (84) (26) $k
    $g.FillPath($mintBrush, $spark)
    $spark.Dispose()
    $mintBrush.Dispose()

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
    # Adaptive foreground keeps the mark inside the central 66/108 box (the
    # bubble + tail span ~60% of the canvas), so system masks never crop the
    # bubble or the spark.
    $made += @{ Path = (Join-Path $dir 'ic_launcher.png'); Size = $px; Badge = $true; Corner = 0.16; Circle = $false }
    $made += @{ Path = (Join-Path $dir 'ic_launcher_round.png'); Size = $px; Badge = $true; Corner = 0.0; Circle = $true }
    $made += @{ Path = (Join-Path $dir 'ic_launcher_foreground.png'); Size = $d.Foreground; Badge = $false; Corner = 0.0; Circle = $false }
}
$made += @{ Path = (Join-Path $RES 'drawable-nodpi/app_icon.png'); Size = 512; Badge = $true; Corner = 0.16; Circle = $false }
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
