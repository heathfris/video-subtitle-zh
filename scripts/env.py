#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
env.py —— 跨平台环境探测与资源定位

video-subtitle-zh 的公共模块，被 00~03 及 run_all 共用。把所有「因机器而异」
的东西收在一处，让主流程脚本保持干净：

  · ffmpeg / ffprobe 定位（PATH 优先，再探常见安装目录）
  · 中日韩字体探测 + ASS 字体名映射（Windows / macOS / Linux 三平台）
  · 识别模型：别名解析 → 本地缓存 → 按需下载（HuggingFace 官方/镜像自动选路）
  · 推理设备：CUDA 可用则 GPU 加速，否则 CPU int8，探测失败绝不中断主流程

自检（打印当前机器上的全部探测结果）：
    python scripts/env.py
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

__all__ = [
    "SKILL_ROOT", "WORK_DIR", "MODEL_DIR",
    "find_tool", "require_tool", "check_ffmpeg",
    "find_cjk_font", "copy_font_to",
    "MODEL_ALIASES", "ensure_model", "resolve_model",
    "pick_device", "pick_hf_endpoint",
    "mark_cuda_broken", "clear_cuda_broken",
    "describe",
]

# ---------------------------------------------------------------- 目录

SKILL_ROOT = Path(__file__).resolve().parent.parent
WORK_DIR = Path(os.environ.get("VSZ_WORK_DIR") or (SKILL_ROOT / ".work"))
MODEL_DIR = WORK_DIR / "models"

# GPU 推理失败过的标记。
# 关键：ctranslate2 报告「有 CUDA 设备」并不等于真能跑——缺 cublas/cudnn 时，
# 它会等到第一次推理才抛错。把失败记下来，后续运行就能直接走 CPU，
# 省掉每次必然失败的等待。
CUDA_BROKEN_FLAG = WORK_DIR / "cuda_broken.txt"


# ---------------------------------------------------------------- 工具查找

# 系统 PATH 之外，各平台常见的安装位置
_EXTRA_BIN_DIRS = {
    "Windows": [
        r"C:\ffmpeg\bin",
        r"C:\Program Files\ffmpeg\bin",
        r"C:\ProgramData\chocolatey\bin",
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Links"),
        os.path.expandvars(r"%USERPROFILE%\scoop\shims"),
    ],
    "Darwin": [
        "/opt/homebrew/bin",       # Apple Silicon Homebrew
        "/usr/local/bin",          # Intel Homebrew
        "/opt/local/bin",          # MacPorts
    ],
    "Linux": [
        "/usr/local/bin",
        "/usr/bin",
        "/snap/bin",
        os.path.expanduser("~/.local/bin"),
    ],
}

_INSTALL_HINT = {
    "Windows": (
        "  winget install Gyan.FFmpeg\n"
        "  或 choco install ffmpeg\n"
        "  或从 https://www.gyan.dev/ffmpeg/builds/ 下载后把 bin 目录加进 PATH"
    ),
    "Darwin": "  brew install ffmpeg",
    "Linux": (
        "  Debian/Ubuntu:  sudo apt install ffmpeg\n"
        "  Fedora/RHEL:    sudo dnf install ffmpeg\n"
        "  Arch:           sudo pacman -S ffmpeg"
    ),
}


def find_tool(name: str):
    """定位可执行文件，返回绝对路径字符串；找不到返回 None。"""
    exe = name + (".exe" if os.name == "nt" else "")
    hit = shutil.which(name) or shutil.which(exe)
    if hit:
        return hit
    system = platform.system()
    for d in _EXTRA_BIN_DIRS.get(system, []):
        if not d:
            continue
        cand = Path(d) / exe
        if cand.exists():
            return str(cand)
    return None


def require_tool(name: str) -> str:
    """定位可执行文件，找不到就带着安装指引退出。"""
    p = find_tool(name)
    if p:
        return p
    system = platform.system()
    hint = _INSTALL_HINT.get(system, "  请查阅 ffmpeg 官网安装说明")
    sys.exit(
        f"\n[环境缺失] 找不到 {name}。\n\n"
        f"安装方法（{system}）：\n{hint}\n\n"
        f"装好后重开终端、确认 `{name} -version` 能跑，再重试。\n"
    )


def check_ffmpeg(verbose: bool = True):
    """检查 ffmpeg + ffprobe，返回 (ffmpeg, ffprobe)。"""
    ff, fp = find_tool("ffmpeg"), find_tool("ffprobe")
    if verbose:
        if ff:
            ver = ""
            try:
                out = subprocess.run([ff, "-version"], capture_output=True,
                                     text=True, timeout=10).stdout
                ver = out.splitlines()[0].replace("ffmpeg version ", "").split(" ")[0]
            except Exception:
                pass
            print(f"  ffmpeg  : {ff}  {ver}")
        if fp:
            print(f"  ffprobe : {fp}")
    if not ff or not fp:
        missing = "ffmpeg" if not ff else "ffprobe"
        require_tool(missing)          # 直接退出并给指引
    return ff, fp


# ---------------------------------------------------------------- 字体

# 候选表：平台 → [(字体文件, ASS 中应写的字体名), ...]
# 顺序即优先级。ASS 里的 Fontname 必须与实际字体族的内部名称一致，
# 否则 libass 会静默回落到默认字体（中文就变成方块）。
_FONT_CANDIDATES: dict[str, list[tuple[str, str]]] = {
    "Windows": [
        (r"C:\Windows\Fonts\msyh.ttc",   "Microsoft YaHei"),
        (r"C:\Windows\Fonts\msyh.ttf",   "Microsoft YaHei"),
        (r"C:\Windows\Fonts\msyhl.ttc",  "Microsoft YaHei Light"),
        (r"C:\Windows\Fonts\simhei.ttf", "SimHei"),
        (r"C:\Windows\Fonts\simsun.ttc", "SimSun"),
    ],
    "Darwin": [
        ("/System/Library/Fonts/PingFang.ttc",                    "PingFang SC"),
        ("/System/Library/Fonts/Hiragino Sans GB.ttc",            "Hiragino Sans GB"),
        ("/System/Library/Fonts/STHeiti Medium.ttc",              "Heiti SC"),
        ("/Library/Fonts/Arial Unicode.ttf",                      "Arial Unicode MS"),
    ],
    "Linux": [
        ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",  "Noto Sans CJK SC"),
        ("/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf", "Noto Sans CJK SC"),
        ("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",  "Noto Sans CJK SC"),
        ("/usr/share/fonts/google-noto-cjk/NotoSansCJK-Regular.ttc", "Noto Sans CJK SC"),
        ("/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",       "Noto Sans CJK SC"),
        ("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",            "WenQuanYi Zen Hei"),
        ("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",          "WenQuanYi Micro Hei"),
        ("/usr/share/fonts/truetype/arphic/uming.ttc",              "AR PL UMing CN"),
    ],
}

_FONT_HINT = {
    "Windows": "系统自带微软雅黑，若缺失请检查 C:\\Windows\\Fonts\\msyh.ttc",
    "Darwin": "系统自带苹方（PingFang SC），若缺失可在「字体册」中恢复",
    "Linux": (
        "  Debian/Ubuntu:  sudo apt install fonts-noto-cjk\n"
        "  Fedora:         sudo dnf install google-noto-sans-cjk-fonts\n"
        "  Arch:           sudo pacman -S noto-fonts-cjk"
    ),
}


def _name_from_path(path: Path) -> str:
    """用户自定义字体文件时，按文件名猜一个可用作 ASS Fontname 的族名。"""
    stem = path.stem.lower()
    mapping = [
        (("msyh", "microsoftyahei", "microsoft_yahei"), "Microsoft YaHei"),
        (("simhei",), "SimHei"),
        (("simsun",), "SimSun"),
        (("pingfang",), "PingFang SC"),
        (("hiragino",), "Hiragino Sans GB"),
        (("notosanscjk", "notosanssc", "notosans"), "Noto Sans CJK SC"),
        (("wqyzenhei", "wqy-zenhei"), "WenQuanYi Zen Hei"),
        (("wqymicrohei", "wqy-microhei"), "WenQuanYi Micro Hei"),
        (("sourcehansans", "sourcehans"), "Source Han Sans SC"),
    ]
    for keys, name in mapping:
        if any(k in stem for k in keys):
            return name
    return path.stem


def find_cjk_font(custom_file: str | None = None,
                  custom_name: str | None = None):
    """
    探测一个可用的中文字体。

    返回 (字体文件路径:Path|None, ASS 字体名:str|None)。

    优先级：显式参数 > 环境变量 SUBTITLE_FONT_FILE/NAME > 平台候选表 > fc-match
    """
    custom_file = custom_file or os.environ.get("SUBTITLE_FONT_FILE")
    custom_name = custom_name or os.environ.get("SUBTITLE_FONT_NAME")

    if custom_file:
        p = Path(custom_file).expanduser()
        if p.exists():
            return p, (custom_name or _name_from_path(p))
        print(f"  [警告] 指定的字体文件不存在：{p}", file=sys.stderr)

    for path_str, family in _FONT_CANDIDATES.get(platform.system(), []):
        p = Path(path_str)
        if p.exists():
            return p, family

    # Linux 兜底：系统 fontconfig 自己知道装了哪些中文字体
    if platform.system() == "Linux" and shutil.which("fc-match"):
        try:
            out = subprocess.run(
                ["fc-match", "-f", "%{file}\n%{family}\n", ":lang=zh"],
                capture_output=True, text=True, timeout=10).stdout.splitlines()
            if out and out[0].strip() and Path(out[0].strip()).exists():
                fam = out[1].split(",")[0].strip() if len(out) > 1 else None
                p = Path(out[0].strip())
                return p, (fam or _name_from_path(p))
        except Exception:
            pass

    return None, None


def copy_font_to(workdir: Path, custom_file: str | None = None,
                 custom_name: str | None = None):
    """
    把探测到的中文字体复制进 workdir，供 ffmpeg 的 `ass=...:fontsdir=.` 加载。

    返回 (字体路径|None, ASS 字体名|None)。

    为什么要复制：libass 在部分环境下不读系统 fontconfig，只认 fontsdir；
    同时路径写成本地相对名可以彻底绕开 Windows 盘符冒号在 filtergraph 里的转义问题。
    """
    src, family = find_cjk_font(custom_file, custom_name)
    if not src:
        return None, None
    dst = workdir / src.name
    if not dst.exists():
        try:
            shutil.copy2(src, dst)
        except Exception as e:
            print(f"  [警告] 字体复制失败（{e}），将依赖系统 fontconfig",
                  file=sys.stderr)
            return src, family
    return dst, family


# ---------------------------------------------------------------- 模型

MODEL_ALIASES = {
    # 通用多语言
    "tiny":            "Systran/faster-whisper-tiny",
    "base":            "Systran/faster-whisper-base",
    "small":           "Systran/faster-whisper-small",
    "medium":          "Systran/faster-whisper-medium",
    "large-v3":        "Systran/faster-whisper-large-v3",
    # 英文专用（蒸馏版，同精度下快数倍；非英文内容不要用）
    "distil-small":    "Systran/faster-distil-whisper-small.en",
    "distil-medium":   "Systran/faster-distil-whisper-medium.en",
    "distil-large-v3": "Systran/faster-distil-whisper-large-v3",
}

_MODEL_SIZE_HINT = {
    "tiny": "~75 MB", "base": "~145 MB", "small": "~480 MB",
    "medium": "~1.5 GB", "large-v3": "~3.1 GB",
    "distil-small": "~330 MB", "distil-medium": "~790 MB",
    "distil-large-v3": "~1.5 GB",
}

ENGLISH_ONLY = {"distil-small", "distil-medium", "distil-large-v3"}


def resolve_model(model: str) -> tuple[str, Path]:
    """模型别名 → (HF repo id, 本地目标目录)。也接受现成的本地目录。"""
    p = Path(model).expanduser()
    if p.is_dir():
        return "", p.resolve()

    repo = MODEL_ALIASES.get(model)
    if repo is None:
        if "/" in model:
            repo = model                     # 直接给 HF repo id
        else:
            opts = "、".join(MODEL_ALIASES)
            sys.exit(f"未知模型名「{model}」。\n可选：{opts}\n"
                     f"也可以直接传 HuggingFace 仓库名（如 Systran/faster-whisper-small）"
                     f"或本地模型目录的路径。")
    return repo, MODEL_DIR / repo.split("/")[-1]


def _model_ready(local: Path) -> bool:
    """权重文件存在且大小合理，才算下完（避免半截文件被当成完整模型）。"""
    if not local.is_dir():
        return False
    for name in ("model.bin", "model.safetensors"):
        f = local / name
        if f.exists() and f.stat().st_size > 10 * 1024 * 1024:
            return True
    return False


def pick_hf_endpoint(timeout: int = 6, use_cache: bool = True) -> str:
    """
    选一个能连上的 HuggingFace 端点。

    官方 huggingface.co 在部分网络环境下不可达，国内镜像 hf-mirror.com 可作替代，
    两者接口兼容，只要设置 HF_ENDPOINT 即可。探测结果会缓存，避免每次跑都等。
    """
    forced = os.environ.get("HF_ENDPOINT")
    if forced:
        return forced.rstrip("/")

    cache = WORK_DIR / "hf_endpoint.txt"
    if use_cache and cache.exists():
        try:
            cached = cache.read_text(encoding="utf-8").strip()
            if cached:
                return cached
        except Exception:
            pass

    import urllib.request

    probe = "/api/models/Systran/faster-whisper-small"
    for ep in ("https://huggingface.co", "https://hf-mirror.com"):
        try:
            with urllib.request.urlopen(ep + probe, timeout=timeout) as r:
                if r.status < 500:
                    WORK_DIR.mkdir(parents=True, exist_ok=True)
                    cache.write_text(ep, encoding="utf-8")
                    return ep
        except Exception:
            continue

    return "https://hf-mirror.com"


def ensure_model(model: str = "small", log=print) -> Path:
    """把模型准备好，返回本地目录路径。已缓存则直接返回。"""
    repo, local = resolve_model(model)

    if not repo:                              # 用户直接给了本地目录
        log(f"  使用本地模型目录：{local}")
        return local

    if _model_ready(local):
        log(f"  模型已缓存：{local}")
        return local

    endpoint = pick_hf_endpoint()
    size = _MODEL_SIZE_HINT.get(model, "")
    log(f"  首次使用，下载模型 {model} {size}".rstrip())
    log(f"  数据源：{endpoint}")

    os.environ["HF_ENDPOINT"] = endpoint
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")   # xet 传输在国内易失败

    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        sys.exit("缺少 huggingface_hub，请先运行安装脚本（install.sh / install.ps1）")

    local.mkdir(parents=True, exist_ok=True)
    try:
        snapshot_download(
            repo,
            local_dir=str(local),
            allow_patterns=["*.json", "*.bin", "*.txt", "*.safetensors"],
            ignore_patterns=["*.md", "*.h5", "*.msgpack", "*.onnx"],
        )
    except Exception as e:
        sys.exit(
            f"\n[下载失败] {e}\n\n"
            f"排查建议：\n"
            f"  1. 确认网络能访问 {endpoint}\n"
            f"  2. 手动指定镜像：export HF_ENDPOINT=https://hf-mirror.com\n"
            f"  3. 换更小的模型：--model small\n"
            f"  4. 或先手动下载整个模型目录，再用 --model /路径/到/模型 指定\n"
        )

    if not _model_ready(local):
        sys.exit(f"模型下载不完整：{local}\n请删除该目录后重试。")

    log(f"  下载完成：{local}")
    return local


# ---------------------------------------------------------------- 推理设备


def pick_device(prefer: str = "auto", log=print,
                use_cache: bool = True) -> tuple[str, str]:
    """
    选推理设备，返回 (device, compute_type)。

    CUDA 可用 → ("cuda", "float16")，比 CPU 快一个数量级；
    否则 → ("cpu", "int8")，兼容性最好。

    注意这里只能确认「设备存在」，不能确认「真能推理」——缺 CUDA 运行库时
    要到第一次推理才暴露。所以调用方仍须准备好回落，见 mark_cuda_broken()。
    """
    if prefer == "cpu":
        return "cpu", "int8"

    if prefer == "auto" and use_cache and CUDA_BROKEN_FLAG.exists():
        log(f"  上次 GPU 推理失败过，直接用 CPU"
            f"（想重试 GPU 请删除 {CUDA_BROKEN_FLAG}）")
        return "cpu", "int8"

    try:
        import ctranslate2
        n = ctranslate2.get_cuda_device_count()
        if n > 0:
            log(f"  检测到 {n} 块 CUDA 设备，尝试 GPU 加速")
            return "cuda", "float16"
        if prefer == "cuda":
            log("  [警告] 指定了 cuda 但未检测到可用设备，回落到 CPU")
    except Exception:
        if prefer == "cuda":
            log("  [警告] CUDA 探测失败，回落到 CPU")
    return "cpu", "int8"


def mark_cuda_broken(reason: str):
    """记下 GPU 推理不可用，让后续运行跳过这次注定失败的尝试。"""
    try:
        WORK_DIR.mkdir(parents=True, exist_ok=True)
        CUDA_BROKEN_FLAG.write_text(str(reason).strip()[:500], encoding="utf-8")
    except Exception:
        pass


def clear_cuda_broken():
    """清掉 GPU 失败标记，下次运行会重新尝试 GPU。"""
    try:
        CUDA_BROKEN_FLAG.unlink(missing_ok=True)
    except Exception:
        pass


# ---------------------------------------------------------------- 自检


def describe() -> str:
    """打印本机探测结果，用于安装后自检和问题排查。"""
    lines = ["", "=" * 58, " video-subtitle-zh 环境自检", "=" * 58, ""]
    lines.append(f"  操作系统 : {platform.system()} {platform.release()}")
    lines.append(f"  Python   : {sys.version.split()[0]}  ({sys.executable})")
    lines.append(f"  仓库根   : {SKILL_ROOT}")
    lines.append(f"  工作目录 : {WORK_DIR}")
    lines.append("")

    lines.append("  [依赖]")
    for mod in ("faster_whisper", "ctranslate2", "huggingface_hub", "numpy"):
        try:
            m = __import__(mod)
            ver = getattr(m, "__version__", "")
            lines.append(f"    ✓ {mod:<18} {ver}")
        except ImportError:
            lines.append(f"    ✗ {mod:<18} 未安装 —— 请运行 install.sh / install.ps1")

    lines.append("")
    lines.append("  [外部工具]")
    for tool in ("ffmpeg", "ffprobe"):
        p = find_tool(tool)
        lines.append(f"    {'✓' if p else '✗'} {tool:<8} {p or '未找到'}")
    if not find_tool("ffmpeg"):
        lines.append(f"      安装：{_INSTALL_HINT.get(platform.system(), '')}")

    lines.append("")
    lines.append("  [中文字体]")
    f, fam = find_cjk_font()
    if f:
        lines.append(f"    ✓ {f}")
        lines.append(f"      将写入字幕的字体名：{fam}")
    else:
        lines.append("    ✗ 未找到中文字幕字体")
        lines.append(f"      {_FONT_HINT.get(platform.system(), '')}")

    lines.append("")
    lines.append("  [推理设备]")
    dev, ct = pick_device(prefer="auto", log=lambda *_: None)
    lines.append(f"    将使用：{dev} / {ct}")

    lines.append("")
    lines.append("  [已缓存模型]")
    if MODEL_DIR.exists():
        found = [d.name for d in MODEL_DIR.iterdir() if d.is_dir()]
        for d in sorted(found):
            ok = "✓" if _model_ready(MODEL_DIR / d) else "…"
            lines.append(f"    {ok} {d}")
        if not found:
            lines.append("    (无，首次运行时自动下载)")
    else:
        lines.append("    (无，首次运行时自动下载)")

    lines.append("")
    lines.append("=" * 58)
    return "\n".join(lines)


if __name__ == "__main__":
    print(describe())
