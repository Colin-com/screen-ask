# Screen Ask · 一键安装（Windows）
# 用法：解压后在本目录右键"使用 PowerShell 运行"，或：
#   powershell -ExecutionPolicy Bypass -File install.ps1
# 可选参数 -NoShortcut 跳过创建桌面快捷方式

param([switch]$NoShortcut)

# 控制台切 UTF-8，否则中文在 CP936 下是乱码
chcp 65001 > $null
$ErrorActionPreference = "Stop"
$here = $PSScriptRoot
Write-Host ""
Write-Host "  SCR-01  SCREEN ASK  INSTALL" -ForegroundColor Red
Write-Host "  ------------------------------" -ForegroundColor DarkGray

# 1) 找 Python
$py = $null
foreach ($c in @("py", "python", "python3")) {
    $cmd = Get-Command $c -ErrorAction SilentlyContinue
    if ($cmd) { $py = $cmd.Source; break }
}
if (-not $py) {
    Write-Host "  [X] 没找到 Python。请先装 Python 3.9+ : https://www.python.org/downloads/" -ForegroundColor Red
    exit 1
}
$ver = & $py -c "import sys;print('%d.%d' % sys.version_info[:2])" 2>$null
Write-Host "  [OK] Python $ver  $py"

# 2) 依赖（只有 Pillow）
Write-Host "  [..] 安装依赖 Pillow ..."
& $py -m pip install --upgrade pip --quiet 2>$null
& $py -m pip install --quiet "Pillow>=9.0"
if ($LASTEXITCODE -ne 0) {
    Write-Host "  [!] 依赖安装失败，试 --user 再装一次" -ForegroundColor Yellow
    & $py -m pip install --user --quiet "Pillow>=9.0"
}
& $py -c "import PIL; print('  [OK] Pillow', PIL.__version__)"

# 3) 图标
Write-Host "  [..] 生成 8-bit 图标 ..."
& $py (Join-Path $here "tools\make_icon.py")

# 4) 桌面快捷方式
if (-not $NoShortcut) {
    Write-Host "  [..] 创建桌面快捷方式 ..."
    & $py (Join-Path $here "tools\make_shortcut.py")
}

# 5) 首次运行提示
$envfile = Join-Path $env:USERPROFILE ".screen-ask\.env"
if (-not (Test-Path $envfile)) {
    Write-Host ""
    Write-Host "  第一次用还需要一把 API KEY（DeepSeek / OpenAI / Claude 任选）：" -ForegroundColor Yellow
    Write-Host "    1. 双击桌面 Screen Ask 启动"
    Write-Host "    2. 点顶栏 API，选 provider，粘贴 key，APPLY"
    Write-Host "  想用本地模型：先装 ollama 并 pull 一个视觉模型，provider 选 OLLAMA。"
}

Write-Host ""
Write-Host "  装好了。快捷键 Ctrl+Alt+Q 随时唤醒/隐藏。" -ForegroundColor Red
Write-Host ""
if (-not $NoShortcut) {
    Read-Host "  按回车关闭"
}
