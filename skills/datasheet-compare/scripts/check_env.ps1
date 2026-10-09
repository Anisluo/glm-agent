# 检查 / 安装 datasheet 对比报告所需工具链（Windows PowerShell 5.1 兼容）
# 用法: powershell -ExecutionPolicy Bypass -File check_env.ps1 [-Install]
param([switch]$Install)

$script:ok = $true
function Report($name, $found, $hint) {
    if ($found) {
        Write-Host "[OK]      $name" -ForegroundColor Green
    } else {
        Write-Host "[MISSING] $name  -> $hint" -ForegroundColor Yellow
        $script:ok = $false
    }
}

# 1. xelatex
$miktexBin = Join-Path $env:LOCALAPPDATA "Programs\MiKTeX\miktex\bin\x64"
$xe = Get-Command xelatex -ErrorAction SilentlyContinue
if ($null -eq $xe) {
    if (Test-Path (Join-Path $miktexBin "xelatex.exe")) {
        $env:PATH = "$miktexBin;$env:PATH"
        Write-Host "已把 MiKTeX 加入本次会话 PATH: $miktexBin"
        $xe = Get-Command xelatex -ErrorAction SilentlyContinue
    }
}
Report "xelatex" ($null -ne $xe) "winget install --id MiKTeX.MiKTeX -e --accept-package-agreements --accept-source-agreements"
if ($null -eq $xe -and $Install) {
    winget install --id MiKTeX.MiKTeX -e --accept-package-agreements --accept-source-agreements
    $env:PATH = "$miktexBin;$env:PATH"
    $xe = Get-Command xelatex -ErrorAction SilentlyContinue
    Report "xelatex (安装后)" ($null -ne $xe) "重新打开终端再试"
}

# 2. 必需的 LaTeX 包（MiKTeX 可按需自动装，预装可避免编译中途弹窗）
$pkgs = @("ctex","pgf","xcolor","colortbl","pdflscape","booktabs","longtable","hyperref","geometry","multirow","array")
if ($null -ne $xe) {
    $mk = Get-Command miktex -ErrorAction SilentlyContinue
    if ($null -ne $mk -and $Install) {
        & miktex packages install $pkgs
        Write-Host "[INFO]    已尝试安装 MiKTeX 包: $($pkgs -join ', ')"
    } else {
        Write-Host "[INFO]    MiKTeX 包 ($($pkgs -join ', ')) 会在首次编译时自动安装；加 -Install 可预装"
    }
}

# 3. Python 与依赖
$py = Get-Command python -ErrorAction SilentlyContinue
Report "python" ($null -ne $py) "winget install Python.Python.3.12"
if ($null -ne $py) {
    foreach ($m in @("pypdf","pymupdf")) {
        & python -c "import $m" 2>$null
        $has = ($LASTEXITCODE -eq 0)
        Report "python module $m" $has "pip install $m"
        if (-not $has -and $Install) { & pip install $m }
    }
}

# 4. 中文字体
$fontDir = Join-Path $env:WINDIR "Fonts"
$cjk = (Test-Path (Join-Path $fontDir "simsun.ttc")) -or (Test-Path (Join-Path $fontDir "msyh.ttc")) -or (Test-Path (Join-Path $fontDir "simhei.ttf"))
Report "中文字体 (SimSun/微软雅黑/SimHei)" $cjk "Windows 通常自带；缺失时安装 Noto Sans CJK 并在 ctexart 用 [fontset=...]"

# 5. 最小编译测试
if ($null -ne $xe) {
    $tmp = Join-Path $env:TEMP "ds_skill_test"
    New-Item -ItemType Directory -Force $tmp | Out-Null
    $tex = @'
\documentclass{ctexart}
\usepackage[table]{xcolor}\usepackage{booktabs,longtable,pdflscape,tikz}
\begin{document}测试 \rowcolor{blue!10} OK \begin{tikzpicture}\fill (0,0) rectangle (1,0.2);\end{tikzpicture}\end{document}
'@
    $utf8 = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText((Join-Path $tmp "t.tex"), $tex, $utf8)
    Push-Location $tmp
    $null = & xelatex -interaction=nonstopmode t.tex 2>&1
    Pop-Location
    $built = Test-Path (Join-Path $tmp "t.pdf")
    Report "xelatex 最小编译 (ctex+xcolor[table]+tikz)" $built "查看 $tmp\t.log；多为缺包，联网后重试或 miktex packages install ctex pgf"
}

Write-Host ""
if ($script:ok) {
    Write-Host "环境就绪。" -ForegroundColor Green
    exit 0
} else {
    Write-Host "有缺项。加 -Install 参数自动安装，或按提示手动安装。" -ForegroundColor Yellow
    exit 1
}
