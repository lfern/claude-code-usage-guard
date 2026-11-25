# usage-guard: tray icon, panel and toasts. Started by usage-guardd.
# Input (stdin):   one JSON line per message, type = state | toast.
# Output (stdout): one JSON line per command, cmd = ready | extend | snooze.
# If stdin closes or 20 s pass without news, it exits: it never shows frozen data.
param(
    [string]$Snapshot = ''   # debugging: saves a PNG of the panel with the first state here
)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()

$SilenceLimitMs = 20000
$AppId = 'UsageGuard.Widget'
$Colors = @{
    green = [System.Drawing.Color]::FromArgb(46, 160, 67)
    amber = [System.Drawing.Color]::FromArgb(227, 148, 0)
    red   = [System.Drawing.Color]::FromArgb(218, 54, 51)
    gray  = [System.Drawing.Color]::FromArgb(128, 128, 128)
}
$Background = [System.Drawing.Color]::FromArgb(32, 32, 36)
$Track = [System.Drawing.Color]::FromArgb(62, 62, 68)
$Foreground = [System.Drawing.Color]::FromArgb(236, 236, 236)
$Muted = [System.Drawing.Color]::FromArgb(172, 172, 178)

# ---------------------------------------------------------------- communication
$Inbox = New-Object 'System.Collections.Concurrent.ConcurrentQueue[string]'
$Link = [hashtable]::Synchronized(@{ Closed = $false })
$Output = New-Object System.IO.StreamWriter([Console]::OpenStandardOutput(), (New-Object System.Text.UTF8Encoding($false)))
$Output.AutoFlush = $true

function Send-Command([hashtable]$Message) {
    # Always ASCII: anything else is escaped as \uXXXX.
    $json = $Message | ConvertTo-Json -Compress
    $ascii = [regex]::Replace($json, '[^\x00-\x7F]', { param($m) '\u{0:x4}' -f [int][char]$m.Value })
    try { $Output.WriteLine($ascii) } catch { $Link.Closed = $true }
}

# stdin is read in another thread so that the UI does not block.
$Runspace = [runspacefactory]::CreateRunspace()
$Runspace.Open()
$Reader = [powershell]::Create()
$Reader.Runspace = $Runspace
[void]$Reader.AddScript({
    param($Inbox, $Link)
    try {
        $in = New-Object System.IO.StreamReader([Console]::OpenStandardInput(), [System.Text.Encoding]::UTF8)
        while ($null -ne ($line = $in.ReadLine())) { $Inbox.Enqueue($line) }
    } catch { }
    $Link.Closed = $true
}).AddArgument($Inbox).AddArgument($Link)
[void]$Reader.BeginInvoke()

# ----------------------------------------------------------------------- icon
$script:State = $null
$script:BlinkOff = $false
$IconCache = @{}   # each HICON is created only once

function Get-StatusIcon([string]$Text, [string]$Color) {
    $key = "$Color|$Text"
    if (-not $IconCache.ContainsKey($key)) {
        $bitmap = New-Object System.Drawing.Bitmap(32, 32)
        $g = [System.Drawing.Graphics]::FromImage($bitmap)
        $g.SmoothingMode = 'AntiAlias'
        $g.TextRenderingHint = 'AntiAliasGridFit'
        $brush = New-Object System.Drawing.SolidBrush($Colors[$Color])
        $g.FillEllipse($brush, 0, 0, 31, 31)
        $size = if ($Text.Length -ge 3) { 13 } else { 17 }
        $font = New-Object System.Drawing.Font('Segoe UI', $size, [System.Drawing.FontStyle]::Bold, [System.Drawing.GraphicsUnit]::Pixel)
        $format = New-Object System.Drawing.StringFormat
        $format.Alignment = 'Center'; $format.LineAlignment = 'Center'
        $g.DrawString($Text, $font, [System.Drawing.Brushes]::White, (New-Object System.Drawing.RectangleF(-4, 0, 40, 32)), $format)
        $font.Dispose(); $brush.Dispose(); $g.Dispose()
        $IconCache[$key] = [System.Drawing.Icon]::FromHandle($bitmap.GetHicon())
        $bitmap.Dispose()
    }
    $IconCache[$key]
}

$BlankBitmap = New-Object System.Drawing.Bitmap(32, 32)
$BlankIcon = [System.Drawing.Icon]::FromHandle($BlankBitmap.GetHicon())
$Tray = New-Object System.Windows.Forms.NotifyIcon
$Tray.Icon = Get-StatusIcon '-' 'gray'
$Tray.Text = 'usage-guard: waiting for data'
$Tray.Visible = $true

function Update-Icon {
    if ($null -eq $State) { return }
    $text = '-'
    if ($null -ne $State.ratio) { $text = [string][math]::Round($State.ratio * 100) }
    if ($State.blink -and $script:BlinkOff) { $Tray.Icon = $BlankIcon }
    else { $Tray.Icon = Get-StatusIcon $text $State.color }
    $tip = 'usage-guard: no data'
    if ($null -ne $State.account) { $tip = "$($State.account): $text% of today's budget" }
    if ($tip.Length -gt 63) { $tip = $tip.Substring(0, 63) }   # NotifyIcon limit
    $Tray.Text = $tip
}

# ----------------------------------------------------------------------- panel
$RowHeight = 132
$PanelWidth = 360
$Form = New-Object System.Windows.Forms.Form
$Form.Text = 'usage-guard'
$Form.FormBorderStyle = 'FixedToolWindow'
$Form.ShowInTaskbar = $false
$Form.TopMost = $true
$Form.StartPosition = 'Manual'
$Form.BackColor = $Background
$Form.ForeColor = $Foreground
$Form.Font = New-Object System.Drawing.Font('Segoe UI', 9)
$BoldFont = New-Object System.Drawing.Font('Segoe UI', 10, [System.Drawing.FontStyle]::Bold)
$Form.ClientSize = New-Object System.Drawing.Size($PanelWidth, 60)
$Form.Add_FormClosing({ if ($_.CloseReason -eq 'UserClosing') { $_.Cancel = $true; $Form.Hide() } })

function Format-Number($Value) { if ($null -eq $Value) { '--' } else { ([double]$Value).ToString('0.#') } }

function New-Label([int]$X, [int]$Y, [int]$W, $Color) {
    $label = New-Object System.Windows.Forms.Label
    $label.SetBounds($X, $Y, $W, 18)
    $label.ForeColor = $Color
    $label.AutoEllipsis = $true
    $label.UseMnemonic = $false
    $label
}

function New-Bar([int]$Y) {
    $back = New-Object System.Windows.Forms.Panel
    $back.SetBounds(70, $Y, 150, 10)
    $back.BackColor = $Track
    $fill = New-Object System.Windows.Forms.Panel
    $fill.SetBounds(0, 0, 0, 10)
    $back.Controls.Add($fill)
    @{ Back = $back; Fill = $fill }
}

function Set-Bar($Bar, $Share, $Color) {
    $share = [math]::Max([double]0, [math]::Min([double]1, [double]$Share))
    $Bar.Fill.Width = [int]($share * $Bar.Back.Width)
    $Bar.Fill.BackColor = $Color
}

function New-Button([string]$Text, [int]$X, [int]$Y, [string]$Account, [string]$Cmd) {
    $button = New-Object System.Windows.Forms.Button
    $button.Text = $Text
    $button.SetBounds($X, $Y, 110, 26)
    $button.FlatStyle = 'Flat'
    $button.FlatAppearance.BorderColor = $Track
    $button.Tag = @{ cmd = $Cmd; account = $Account }
    $button.Add_Click({ Send-Command $this.Tag })
    $button
}

$script:Rows = @{}
$script:Layout = ''

function Build-Rows {
    foreach ($old in @($Form.Controls)) { $Form.Controls.Remove($old); $old.Dispose() }
    $script:Rows = @{}
    $y = 8
    foreach ($account in $State.accounts) {
        $row = @{}
        $row.Name = New-Label 12 $y 200 $Foreground
        $row.Name.Font = $BoldFont; $row.Name.Height = 20
        $row.Age = New-Label 212 ($y + 2) 136 $Muted
        $row.Age.TextAlign = 'TopRight'
        $row.Week = New-Bar ($y + 30); $row.WeekText = New-Label 228 ($y + 26) 122 $Foreground
        $row.Day = New-Bar ($y + 50); $row.DayText = New-Label 228 ($y + 46) 122 $Foreground
        $row.Pace = New-Label 12 ($y + 68) 336 $Muted
        $weekTitle = New-Label 12 ($y + 26) 56 $Muted; $weekTitle.Text = 'Week'
        $dayTitle = New-Label 12 ($y + 46) 56 $Muted; $dayTitle.Text = 'Today'
        $extend = New-Button '+5% today' 12 ($y + 92) $account.account 'extend'
        $row.Snooze = New-Button 'Snooze 1 h' 130 ($y + 92) $account.account 'snooze'
        $Form.Controls.AddRange(@($row.Name, $row.Age, $weekTitle, $row.Week.Back, $row.WeekText,
            $dayTitle, $row.Day.Back, $row.DayText, $row.Pace, $extend, $row.Snooze))
        $script:Rows[$account.account] = $row
        $y += $RowHeight
    }
    $Form.ClientSize = New-Object System.Drawing.Size($PanelWidth, [math]::Max(60, $y))
    Set-PanelPosition
}

function Set-PanelPosition {
    $area = [System.Windows.Forms.Screen]::PrimaryScreen.WorkingArea
    $Form.Location = New-Object System.Drawing.Point(($area.Right - $Form.Width - 12), ($area.Bottom - $Form.Height - 12))
}

function Update-Panel {
    $layout = ($State.accounts | ForEach-Object { $_.account }) -join "`n"
    if ($layout -ne $script:Layout) { $script:Layout = $layout; Build-Rows }
    foreach ($account in $State.accounts) {
        $row = $Rows[$account.account]
        $color = $Colors[$account.color]
        $row.Name.Text = $account.account
        $row.Age.Text = $account.age
        $row.Pace.Text = $account.pace
        $row.WeekText.Text = "$(Format-Number $account.week)%"
        Set-Bar $row.Week ($(if ($null -eq $account.week) { 0 } else { $account.week / 100 })) $(if ($account.fresh) { $Muted } else { $Colors.gray })
        if ($account.fresh) {
            $row.DayText.Text = "$(Format-Number $account.spent) / $(Format-Number $account.budget)%"
            Set-Bar $row.Day $account.ratio $color
        } else {
            $row.DayText.Text = 'no data'
            Set-Bar $row.Day 0 $color
        }
        $row.Snooze.Text = if ($account.snoozed) { 'Snoozed' } else { 'Snooze 1 h' }
    }
}

$Tray.Add_MouseClick({
    if ($Form.Visible) { $Form.Hide() }
    elseif ($null -ne $State) { Set-PanelPosition; $Form.Show(); $Form.Activate() }
})

# ---------------------------------------------------------------------- toasts
$script:ToastReady = $false
try {
    # Our own identifier in HKCU (no admin needed) so that the toast says "usage-guard".
    $key = "HKCU:\Software\Classes\AppUserModelId\$AppId"
    if (-not (Test-Path $key)) { [void](New-Item -Path $key -Force) }
    Set-ItemProperty -Path $key -Name DisplayName -Value 'usage-guard'
    [void][Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]
    [void][Windows.UI.Notifications.ToastNotification, Windows.UI.Notifications, ContentType = WindowsRuntime]
    [void][Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom, ContentType = WindowsRuntime]
    $script:ToastReady = $true
} catch { }

function Show-Toast($Message) {
    if ($script:ToastReady) {
        try {
            $title = [System.Security.SecurityElement]::Escape($Message.title)
            $body = [System.Security.SecurityElement]::Escape($Message.body)
            $xml = New-Object Windows.Data.Xml.Dom.XmlDocument
            $xml.LoadXml("<toast><visual><binding template=`"ToastGeneric`"><text>$title</text><text>$body</text></binding></visual></toast>")
            $toast = New-Object Windows.UI.Notifications.ToastNotification($xml)
            [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($AppId).Show($toast)
            return
        } catch { }
    }
    $Tray.ShowBalloonTip(8000, $Message.title, $Message.body, 'Warning')   # fallback
}

# ---------------------------------------------------------------------- loop
$Silence = [System.Diagnostics.Stopwatch]::StartNew()
$LastTick = [System.Diagnostics.Stopwatch]::StartNew()
$Timer = New-Object System.Windows.Forms.Timer
$Timer.Interval = 500
$Timer.Add_Tick({
    try {
        # After the computer sleeps, the silence does not count: give the daemon some time.
        if ($LastTick.ElapsedMilliseconds -gt 5000) { $Silence.Restart() }
        $LastTick.Restart()
        $line = $null
        while ($Inbox.TryDequeue([ref]$line)) {
            $Silence.Restart()
            try { $message = $line | ConvertFrom-Json } catch { continue }
            if ($message.type -eq 'state') { $script:State = $message; Update-Panel }
            elseif ($message.type -eq 'toast') { Show-Toast $message }
        }
        if ($Link.Closed -or $Silence.ElapsedMilliseconds -gt $SilenceLimitMs) {
            $Timer.Stop()
            [System.Windows.Forms.Application]::Exit()
            return
        }
        $script:BlinkOff = -not $script:BlinkOff
        Update-Icon
        if ($Snapshot -and $null -ne $State) {
            $target = $Snapshot
            $script:Snapshot = ''   # DoEvents may re-enter this handler
            $Form.Show()
            [System.Windows.Forms.Application]::DoEvents()
            $image = New-Object System.Drawing.Bitmap($Form.Width, $Form.Height)
            $Form.DrawToBitmap($image, (New-Object System.Drawing.Rectangle(0, 0, $Form.Width, $Form.Height)))
            $image.Save($target)
            $image.Dispose()
        }
    } catch { [Console]::Error.WriteLine("widget: $_") }
})
$Timer.Start()
Send-Command @{ cmd = 'ready' }
try { [System.Windows.Forms.Application]::Run() }
finally {
    $Tray.Visible = $false
    $Tray.Dispose()
    [Environment]::Exit(0)   # the reader thread may still be blocked on stdin
}
