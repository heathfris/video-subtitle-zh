# 排查手册

按症状查。每条都注明了原因，方便你判断要不要按这个方向修。

---

## 安装

### `python` 找不到 / 版本太低

需要 **Python 3.9+**。

```bash
# Windows
winget install Python.Python.3.12        # 安装时务必勾选 Add to PATH
# macOS
brew install python@3.12
# Debian/Ubuntu
sudo apt install python3 python3-venv
```

Debian/Ubuntu 上 `python3 -m venv` 报错说缺 `ensurepip`，就是没装 `python3-venv`。

### 依赖装不上（连接超时 / 403）

镜像源的可用性随网络环境变化很大，这也是安装脚本内置多源轮询的原因
（pip 默认源 → 清华 → 阿里 → 官方）。全失败的话手动指定：

```bash
<PY> -m pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple
```

如果挂着代理，注意 `pip` 不一定走系统代理，需要显式告诉它：

```bash
export HTTPS_PROXY=http://127.0.0.1:7890    # 端口换成你自己的
```

### 安装脚本的中文输出变成乱码

Windows 上如果看到 `鎴愬姛`（"成功"）这类乱码，是终端代码页和脚本输出编码不一致。
`install.ps1` 已经主动把输出切到 UTF-8，请配合支持 UTF-8 的终端使用：

- Windows Terminal / VS Code 终端：默认就没问题
- 老式 cmd.exe：先执行 `chcp 65001` 再运行脚本

**这只影响显示，不影响安装结果**——脚本最后的环境自检会照常打印出来，
按那些 ✓ / ✗ 判断即可。

### 提示找不到 ffmpeg

脚本不代装 ffmpeg——各平台包管理器差异太大，代装更容易帮倒忙。

```bash
winget install Gyan.FFmpeg     # Windows
brew install ffmpeg            # macOS
sudo apt install ffmpeg        # Debian/Ubuntu
```

**Windows 上装完必须重开终端**，否则 PATH 没刷新，还是找不到。

验证：`ffmpeg -version` 和 `ffprobe -version` 都要能跑。

---

## 识别

### 卡在下载模型

国内直连 `huggingface.co` 通常不通。脚本会自动探测并切到 `hf-mirror.com`，
探测结果缓存在 `.work/hf_endpoint.txt`。

探测判断失误时可以强制指定：

```bash
export HF_ENDPOINT=https://hf-mirror.com
```

换更小的模型也是办法：`--model small`（480MB）。

**想彻底离线**：在别的机器上下好模型目录整个拷过来，然后直接指路径：

```bash
<PY> scripts/01_transcribe.py video.mp4 --model /path/to/faster-whisper-small
```

### GPU 报错 / 回落到了 CPU

日志里出现 `GPU 初始化失败` 说明 `ctranslate2` 看到了 CUDA 设备，但真正加载
时缺运行库。**GPU 推理需要 CUDA 12 + cuDNN 9，缺任何一个都会失败。**

装 cuDNN：

```bash
pip install nvidia-cudnn-cu12
```

如果不想折腾，加 `--device cpu` 显式走 CPU——慢一些，结果完全一致。

`ctranslate2` 报告的设备数可能会骗人（驱动装了就能看到设备），所以这个回落
是刻意保留的：**探测失败不该让整个任务挂掉**。

### 识别结果里专有名词全是错的

这是 Whisper 的固有短板，不是 bug。两个办法，一起用效果最好：

1. **`--initial-prompt "TypeSafe, Jev, RLCD"`** —— 把专有名词写进去，
   模型会显著倾向于使用这些词。收益最大的一个参数。
2. **识别完通读一遍，手动改**。真实案例里遇到过：
   `Diogo`→`Diego`、`autoregression`→`audio regression`、`LLM`→`LM`
   （模型吞掉一个 L）。这些都得靠人/外部核实才能定。

改完记得同步改 `segments.json`——后续翻译只读这个文件。

### 漏句 / 语音被切碎

默认开着 VAD（静音切分）。音乐、多人抢话、无停顿的素材容易被误切：

```bash
--no-vad
```

### `未识别到任何语音`

先确认音轨真的有声音（用播放器听一下）。如果音频是纯音乐或纯环境声，
Whisper 确实会返回空。批量处理时可以用 `ffprobe` 先筛掉没有音轨的文件。

---

## 字幕

### 中文显示成方块 / 豆腐块

libass 找不到字体。分两种情况：

- **系统没有中文字体**（常见于精简版 Linux 和 Docker）：
  ```bash
  sudo apt install fonts-noto-cjk          # Debian/Ubuntu
  sudo dnf install google-noto-sans-cjk-fonts
  ```
- **有字体但没被找到**：显式指定
  ```bash
  --font /path/to/NotoSansCJK-Regular.ttc
  ```

先跑自检看探测结果：

```bash
<PY> scripts/env.py
```

自检会打印「将写入字幕的字体名」——这个名字必须和字体文件的实际族名一致，
否则 libass 会静默回落到默认字体（然后就变方块了）。

### 字幕位置挡住了画面里的字

这是**动态排版类视频**（宣传片、发布会）的典型问题——画面本身就有大字，
而且经常压得很低。别猜，量一下：

```bash
<PY> scripts/00_probe_safe_area.py video.mp4 --json
```

它按固定间隔抽帧，用水平梯度统计每行的「有内容帧占比」，找出底部真正干净
的区间，反推合适的 `margin_v`。拿到数值后传给生成步骤：

```bash
<PY> scripts/02_make_srt.py <工作目录> --video video.mp4 --margin-v 145
```

输出里 `enough_room: false` 说明干净区塞不下双语字幕块，考虑：只出中文字幕、
缩小字号、或把字幕上移到画面其他空白区。

### 折行难看 / 出现孤字行

折行算法优先在标点处断句，其次求两行长度均衡。想人工干预：

```bash
--limit-zh 30 --limit-en 52      # 收窄每行宽度，逼它多折一行
```

数值是**字符数**：中文按全角宽度计（`--limit-zh 34` 约等于 34 个汉字），
英文按字符数。

### 时间轴整体错位

八成是 `translations.json` 的顺序错位了。跑校验：

```bash
<PY> scripts/check_translations.py <工作目录>
```

这个脚本会抓出条数不一致、空译、译文与原文完全相同（漏翻）等静默错误。
**它在设计上就是要拦这类问题的——条数错位不会让程序崩，只会让字幕整体烂掉，
而人很难在几分钟视频里肉眼发现。**

---

## 压制

### 太慢

- **有 NVIDIA 显卡**：`--encoder h264_nvenc`，通常快 5~10 倍
  （同码率画质略逊，可以先试一版比一比）
- **降 preset**：`--preset ultrafast`，体积会变大
- **只用软字幕**：`--soft`，画面直接流复制不重编码，**秒级完成**。
  缺点是部分平台（比如某些社交 App、电视播放器）不认字幕轨

### 画质明显变糊

重编码丢细节，尤其是 4K 高细节素材。**判据：比较输出码率和源文件。**

```bash
ffprobe -v error -show_entries format=bit_rate -of csv=p=0 源文件.mp4
ffprobe -v error -show_entries format=bit_rate -of csv=p=0 输出.mp4
```

输出码率明显低于源（真实案例：源 6.24Mbps → 输出 3.5Mbps），说明细节在丢。
降 CRF 重压：

```bash
--crf 18 --preset slow
```

CRF 每降 6 个点，体积大约翻倍。18 算「视觉无损」档，代价是慢和文件大。
网点、渐变、细纹理这类内容最吃码率，对它们别心疼参数。

### 输出体积太大

- 提 CRF：`--crf 23`
- 换 H.265：`--encoder hevc_nvenc`（同画质下体积约省 40%，但兼容性略差）

### 输出在部分播放器上打不开

`-pix_fmt yuv420p` 已经在脚本里固定了（最大兼容性）。还打不开的话检查
音频：脚本默认 `-c:a copy` 沿用源音轨，源音轨若是冷门编码，封装进 MP4 后
有些播放器确实不认。这种素材建议先用 ffmpeg 把音频转成 AAC 再压。

---

## 还是不行

先跑自检，把输出贴出来——它覆盖了依赖、ffmpeg、字体、推理设备、
模型缓存五个方面，大部分问题看一眼就知道：

```bash
<PY> scripts/env.py
```
