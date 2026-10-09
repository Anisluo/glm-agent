---
name: datasheet-compare
description: 对目录内多份芯片/器件 datasheet（PDF）做参数对比，先写 LaTeX 源文件再用 XeLaTeX 编译成 PDF 报告。触发词：datasheet 对比、规格书对比、竞品对比、参数对比报告、比较这几个芯片。先检查并安装 LaTeX 工具链。
---

# Datasheet 对比报告（LaTeX → PDF）

## 交付物（固定）
1. `<主题>对比报告.tex` —— LaTeX 源文件，放在 datasheet 所在目录
2. `<主题>对比报告.pdf` —— 用 XeLaTeX 编译两遍得到，放在同一目录
3. 编译完成后删除 `.aux .log .out`，并把 PDF 发给用户

**不要**只给 HTML / Markdown / artifact。用户要的是 .tex + .pdf。
版本迭代时用户可能把文件改名为 `-V2.0` 之类，.tex 与 .pdf 保持同名。

## 流程

### 第 0 步：环境检查（每次任务开始都做）
运行 `scripts/check_env.ps1`（PowerShell）或按其中逻辑手动检查：
- `xelatex --version` 能跑 → 有 MiKTeX 或 TeX Live
- Python 有 `pypdf`、`pymupdf`；OCR 不需要，扫描件用页面图片直接读
- 系统有中文字体（Windows 自带 微软雅黑 / SimHei / SimSun，ctex 会自动用）

缺什么装什么，见「安装」一节。装完再回到第 1 步。

### 第 1 步：清点文件并提取文本
```
python scripts/extract_pdfs.py <目录>
```
- 输出每份 PDF 的页数、字符数，文本存到 `<临时目录>/txt/`
- **字符数 < 100 的是扫描件**：脚本会自动把每页渲染成 PNG（110 dpi），用 Read 工具直接看图读数
- 同一型号多个版本：用 `difflib` 逐行 diff（脚本 `--diff a.pdf b.pdf`），只把差异写进报告，不要重复整列

### 第 2 步：读 datasheet，整理数据
统一到同一测试条件（如 Vcc=3.3 V、25 °C 典型值）。每个器件记录：
- 基本：厂商、型号、版本/日期、封装、订货信息
- 极限：绝对最大电压、温度范围、ESD、热阻
- 电气：每个关键参数的 **典型值 + min/max**，算出公差百分比
- 规格书未给的填 “--”，不要猜
- 顺手记录 **规格书本身的错误**（单位错、标签互换、正文与表格不一致、型号笔误、空白条目），单独成章

对特定器件类别可推算派生指标（例如线性霍尔：静态点占 Vcc 百分比、单边可用磁场行程 = (钳位 − Vq)/灵敏度），推算值要在报告里注明“本报告推算，非厂商标称”。

### 第 3 步：写 .tex
从 `templates/report_template.tex` 起步，结构固定：
1. 概述与结论（bullet 列出各维度最优，再一段面向目标应用的选型判断）
2. 样本说明表（文件名、型号、厂商、版本/日期、备注）
3. 参数总表（器件多于 5 列时放 `landscape` 环境 + `\small` + 缩小 `\tabcolsep`）
4. 派生分析表 / 图（TikZ 条形图，见模板）
5. 逐款点评（`description` 环境，每款一段：优点、短板、规格书特殊事项）
6. 规格书疑点与勘误（`enumerate`）
7. 结尾 `\small` 数据来源说明

### 第 4 步：编译并检查
```
xelatex -interaction=nonstopmode <文件>.tex   # 跑两遍（longtable、引用）
```
检查 log 中：
- `^!` 开头的错误 → 必须清零
- `Overfull \hbox (NNpt too wide)` 且 NN ≥ 10 → 表格列宽或 TikZ 坐标溢出，必须修
- 用 PyMuPDF 把 PDF 页面渲染成 PNG 看一遍，重点看：宽表是否裁切、图例是否重叠、长文件名是否溢出到相邻列
修完再编两遍，删 `.aux .log .out`，发送 PDF。

## 风格规范（与既有报告保持一致）

### 文档类与包
```latex
\documentclass[11pt,a4paper]{ctexart}
\usepackage[margin=2.0cm]{geometry}
\usepackage{booktabs,longtable,array,multirow}
\usepackage[table]{xcolor}      % 必须带 [table]，否则 \rowcolor 未定义
\usepackage{pdflscape}          % 宽表横放
\usepackage{tikz}               % 条形图
\usepackage{hyperref}
\hypersetup{colorlinks=true,linkcolor=blue!60!black,urlcolor=blue!60!black}
```

### 颜色与宏
```latex
\definecolor{hdr}{RGB}{31,78,121}     % 表头深蓝，字白色加粗
\definecolor{rowa}{RGB}{240,245,250}  % 隔行浅蓝
\definecolor{barb}{RGB}{47,111,179}   % 条形图主色（蓝）
\definecolor{barr}{RGB}{184,58,58}    % 条形图对比色（红），仅用于双向图
\definecolor{trk}{RGB}{236,239,243}   % 条形图底槽
\newcommand{\best}[1]{\textcolor{green!45!black}{\textbf{#1}}}   % 该行最优
\newcommand{\worst}[1]{\textcolor{red!70!black}{\textbf{#1}}}    % 需留意的短板
\newcommand{\nd}{--}                                             % 未给出
\renewcommand{\arraystretch}{1.3}
```

### 表格
- 一律 `longtable` + `booktabs`（`\toprule \midrule \bottomrule`），表头行 `\rowcolor{hdr}` + `\textcolor{white}{\textbf{…}}`，`\endhead` 使跨页重复表头
- 数据行交替 `\rowcolor{rowa}`
- 列宽用 `p{Ncm}`，文字列 `>{\raggedright\arraybackslash}`，数值列 `>{\centering\arraybackslash}`
- **列宽总和 + 列间距必须 ≤ 版心宽**：A4 竖版 2 cm 边距版心 17 cm；横版 25.7 cm。每列两侧各 `\tabcolsep`（默认 6 pt），8 列约占 2.5 cm
- 长文件名/型号在 `_` 后加 `\allowbreak` 让其可换行
- 参数名带单位写在第一列，如 `工作电流 典型(最大) mA`

### 标题区
```latex
\title{\textbf{<主题> N 款竞品对比报告}\\[4pt] \large 型号A / 型号B / …}
\author{基于目录内 N 份原厂规格书整理（<特殊说明>）}   % 只能一行，\author 不是 \long，里面不能用 \par；太长会溢出
\date{YYYY年M月D日}
```

### 图
用 TikZ 手绘水平条形图，不依赖 pgfplots：
```latex
\begin{tikzpicture}[x=2.2cm,y=1cm]   % x 单位 = 1 个数据单位，满刻度 × x 不超过 ~8cm
\foreach \name/\v [count=\i] in {型号A/3.15, 型号B/2.7, {CZ / XYX}/2.3}{   % 名字含 "/" 必须整体加大括号
  \node[anchor=east,font=\small] at (-0.1,-0.42*\i) {\name};
  \fill[trk]  (0,-0.42*\i-0.13) rectangle (3.5,-0.42*\i+0.13);   % 底槽 = 满刻度
  \fill[barb] (0,-0.42*\i-0.13) rectangle (\v,-0.42*\i+0.13);
  \node[anchor=west,font=\scriptsize] at (\v+0.03,-0.42*\i) {\v};
}
\end{tikzpicture}
```
- 一张图一个量，按数值排序，条尾直接标数值，图下一行 `\small 图 N：…` 说明
- 双向图（如正负行程）左红右蓝，中线黑，图例放图顶部左右分开，别重叠
- 图的 x 单位要先算好：数据最大值 × x 单位 ≈ 6–8 cm，否则整页溢出

### 文字
- 中文全角标点；数值与单位之间 `\,`（`2.5\,mA`）；范围用 `--`（`2.5--5.5\,V`）
- 符号用数学模式：`$V_{CC}$ $V_Q$ $\pm5\%$ $\theta_{JA}$ $^\circ$C`；微用 `\textmu`
- 结论先行：每节第一句就是判断，后面才是依据
- 规格书错误如实写，标出是哪一版，新版修正了就写“已在 Vx.x 修正”

## 安装（缺什么装什么）

### XeLaTeX（Windows，推荐 MiKTeX）
```powershell
winget install --id MiKTeX.MiKTeX -e --accept-package-agreements --accept-source-agreements
```
装完新开终端；MiKTeX 默认路径 `C:\Users\<user>\AppData\Local\Programs\MiKTeX\miktex\bin\x64\`，若 `xelatex` 找不到就把它加进 PATH。
首次编译会按需自动下载 ctex、pgf、xcolor、pdflscape 等包，需要联网；若弹窗被拦，先跑：
```powershell
miktex packages install ctex pgf xcolor colortbl pdflscape booktabs longtable hyperref
```
Linux/macOS 用 TeX Live：`sudo apt install texlive-xetex texlive-lang-chinese texlive-pictures` 或 `brew install --cask mactex-no-gui`。

### Python 依赖
```
pip install pypdf pymupdf
```

### 中文字体
Windows 自带 SimSun/SimHei/微软雅黑，ctex 自动识别。Linux 需 `fonts-noto-cjk`，并在 `\documentclass` 加 `[fontset=fandol]` 或 `[fontset=ubuntu]`。

## 踩过的坑（照做可避免）
1. **在 bash heredoc 里写含中文的 Python 脚本会被按 GBK 解码**（Windows）。含中文的编辑脚本先写成 .py 文件，再 `python -X utf8 script.py` 运行；控制台设 `PYTHONIOENCODING=utf-8`
2. `\usepackage{xcolor}` 不带 `[table]` → `\rowcolor` 未定义
3. TikZ `\foreach` 列表里的项含 `/` 会被当分隔符，含 `,` 同理 → 用 `{…}` 包住
4. TikZ 坐标 `x=` 单位设错会产生几千 pt 的 Overfull，页面整体错位；先算 满刻度 × x
5. `\author{}` 里不能用 `\par`、`\parbox` 换行会报 “Paragraph ended before \author was complete”；一行放不下就缩短，把说明挪到正文第一段
6. longtable 在 `landscape` 内的页眉会沿用上一节名，属正常
7. 编译要跑两遍，否则 `\ref` 显示 `??`、longtable 列宽不稳定
8. 扫描 PDF 不要装 OCR，直接渲染 PNG 用视觉读表，准确率够
9. 同型号多版本先 diff，参数没变就只在勘误章节写变化，不新增列
10. 用户重命名了 PDF（如加 -V2.0）时，把 .tex 也改成同名并删除旧 .tex，避免两套源文件
11. **含中文的 .ps1 必须保存为 UTF-8 带 BOM**，否则 Windows PowerShell 5.1 按 GBK 解析，字符串引号被吃掉报 “missing terminator”。`scripts/check_env.ps1` 已带 BOM；用 Write 工具改写后要重新加 BOM（`python -X utf8 -c "p='x.ps1';b=open(p,'rb').read();open(p,'wb').write(b'\xef\xbb\xbf'+b if not b.startswith(b'\xef\xbb\xbf') else b)"`）
12. .tex 文件用 UTF-8 无 BOM 保存；xelatex 对 BOM 也能容忍，但保持一致

## 目录结构
```
datasheet-compare-skill/
├── SKILL.md                      # 本文件
├── scripts/
│   ├── check_env.ps1             # 环境检查；-Install 自动安装 MiKTeX/包/Python 依赖
│   ├── extract_pdfs.py           # 提取文本 / 扫描件渲染 PNG / --diff 两版本
│   └── build_pdf.py              # 编译两遍 + 报错误与溢出 + 渲染预览 + 清理
└── templates/
    └── report_template.tex       # 报告骨架（样式、宏、表格、TikZ 图）
```
快速命令：
```
powershell -ExecutionPolicy Bypass -File scripts/check_env.ps1 -Install
python -X utf8 scripts/extract_pdfs.py <datasheet目录>
python -X utf8 scripts/build_pdf.py <目录>/<报告>.tex
```
