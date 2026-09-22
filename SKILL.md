---
name: video-subtitle-zh
description: 给没有字幕的外语视频配上中文字幕——本地语音识别、逐句翻译、生成 SRT/ASS、压制硬字幕成品。Use when the user asks to 给视频加中文字幕、生成字幕、英文视频转中文、视频翻译、烧字幕、压制字幕、把英文视频做成中文版, or wants Chinese subtitles for a video that has none (transcribe / subtitle / translate a video into Chinese).
---

# 英文视频 → 中文字幕

一条本地流水线：**抽音轨 → 语音识别 → 逐句翻译 → 生成字幕 → 压制成品**。

识别在本地跑（不联网、不上传），翻译由读得懂上下文的 AI 来做。产出可直接分发的
中英双语硬字幕视频，外加 SRT / ASS 各版本。

---

## 开始之前

### 环境（只需做一次）

```bash
bash install.sh                                        # macOS / Linux
powershell -ExecutionPolicy Bypass -File install.ps1   # Windows
```

脚本会建好 `.venv`、装依赖、检查 ffmpeg。装完可以用自检确认：

```bash
.venv/bin/python scripts/env.py            # macOS / Linux
.venv\Scripts\python.exe scripts\env.py    # Windows
```

下面所有命令里的 `<PY>` 都指这个解释器。

### 需要知道的限制

- **要有 ffmpeg**，脚本不代装（各平台包管理器差异太大，代装容易帮倒忙）。
- **首次使用会下模型**：默认 `small` 约 480MB。国内网络会自动切到镜像源。
- **翻译这一步需要 AI 介入**，无法全自动。这是设计选择，不是缺陷——见下文。

---

## 工作流

### 第 1 步：识别

```bash
<PY> scripts/01_transcribe.py "<视频路径>" --model small --outdir <工作目录>
```

产出 `<工作目录>/segments.json`（带时间轴的识别结果）和 `<语言>.srt`。

**模型怎么选**

| 模型 | 体积 | 适用 |
|---|---|---|
| `small` | 480MB | 默认。跑得快，清晰人声够用 |
| `medium` | 1.5GB | 口音重、有背景音乐 |
| `large-v3` | 3.1GB | 多语言，最准 |
| `distil-large-v3` | 1.5GB | **英文内容首选**：准确率接近 large-v3，速度快数倍 |

蒸馏版（`distil-*`）只认英文，非英文内容用了会输出胡话，脚本会拦一下。

**如果视频里专有名词多**（产品名、人名、缩写），务必加术语提示——这一招的
收益最大：

```bash
--initial-prompt "TypeSafe, Jev, RLCD, Diogo Almeida"
```

识别完后**通读一遍 segments.json**。产品名、人名、技术术语经常被听成近音词
（真实案例：`Diogo`→`Diego`、`autoregression`→`audio regression`、
`LLM`→`LM`）。先用搜索引擎核实正确写法，改掉之后再翻译——错误的专有名词会
一路污染到译文里。

### 第 2 步：翻译（你来做，也是成品质量的分水岭）

先通读全文再动笔。长视频分批处理，但每批都要带着整体语境。

在 `<工作目录>/translations.json` 写一个**与 segments.json 等长的字符串数组**：

```json
["第一句的中文", "第二句的中文", "…"]
```

然后**必须**校验：

```bash
<PY> scripts/check_translations.py <工作目录>
```

这一步不是可选的。条数错位、漏翻、空译都不会让程序崩溃，只会让字幕整体
对不上，而人很难在几分钟视频里肉眼发现。

翻译的具体要求见 `references/translation-guide.md`，核心就四条：
**术语统一、口语化、控制长度、不逐字直译**。

### 第 3 步：生成字幕

```bash
<PY> scripts/02_make_srt.py <工作目录> --video "<原视频>"
```

得到四份：`bilingual.srt`（双语）、`zh.srt`（纯中文）、
`bilingual.ass`、`zh.ass`（带样式，压制用）。字号、描边、折行宽度
全部按视频分辨率自动推算，720p 和 4K 都是合适的观感。

**如果画面里本身就有大字排版**（宣传片、发布会常见，且经常压得很低），
先量一下底部安全区，别让字幕和画面原有文字叠在一起：

```bash
<PY> scripts/00_probe_safe_area.py "<原视频>" --json
```

拿到 `suggested_margin_v` 后传给第 3 步的 `--margin-v`。粗略判断：动态排版风格
的片子值得量一下，纯口播/采访类可以直接用默认值。

### 第 4 步：压制

```bash
<PY> scripts/03_burn.py "<原视频>" <工作目录>/bilingual.ass -o "<输出>" --crf 20
```

**CRF 怎么定**——这条容易踩坑：

| 素材 | 建议 | 原因 |
|---|---|---|
| 普通口播/演示 | `--crf 20 --preset medium` | 默认即可 |
| 4K 高细节、有网点/渐变/细纹理 | `--crf 18 --preset slow` | 默认参数下这些纹理会被压糊 |

判据是**比较输出码率和源文件**：重编码后码率明显低于源（比如源 6.2Mbps 压成
3.5Mbps），说明细节在丢，降 CRF 重压。有 NVIDIA 显卡可以加
`--encoder h264_nvenc` 提速数倍。

**只要字幕不要视频**的话，到第 3 步就够了，`.srt` / `.ass` 直接给用户。

---

## 一条命令跑完

```bash
<PY> scripts/run_all.py "<视频>" --model distil-large-v3 --probe --crf 18
```

识别完成后会停下来提示补译文，补好再跑一次即可接着做完。适合已经清楚流程、
不想分步操作的时候。

---

## 输出物

| 文件 | 用途 |
|---|---|
| `bilingual.srt` | 中英双语，丢进剪映/B站/任意播放器都能加载 |
| `zh.srt` | 纯中文 |
| `bilingual.ass` / `zh.ass` | 带样式，可自己调字号位置后重新压制 |
| `*_cn.mp4` | 字幕已烧进画面，可直接分发 |

---

## 排查

装不上、识别跑不动、字幕显示成方块、压制参数怎么调——见
`references/troubleshooting.md`。

---

## 这个 skill 的边界

**能做**：本地识别（离线、不上传视频）、中英双语字幕、多语言源、SRT/ASS 输出、
硬字幕压制、画面安全区探测。

**不做**：不代装 ffmpeg；翻译不接机器翻译 API（质量不可控）；不做语音克隆、
不做配音、不做字幕时间轴的手工微调工具。
