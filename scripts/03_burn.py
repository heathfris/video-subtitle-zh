#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Step 3/3 —— 压制输出

两种模式：
  硬字幕（默认）   字幕烧进画面，任何一个播放器/平台都能直接看，代价是画面要重编码
  软字幕 --soft    画面直接流复制、不重编码（秒级完成），字幕作为可开关轨道

用法:
    python scripts/03_burn.py <视频> <字幕.ass> [-o 输出.mp4] [--soft]
"""
import argparse
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import env  # noqa: E402


def build_encoder_args(encoder: str, quality: str, preset: str):
    """
    返回视频编码参数。

    libx264 是默认：质量与兼容性最好，跨平台一致。
    NVENC 系是给有 N 卡的人省时间的——同一台机器上通常比软编快 5~10 倍，
    代价是同码率下画质略逊（用 -cq 控制质量，语义接近 CRF）。
    """
    if encoder == "libx264":
        return ["-c:v", "libx264", "-preset", preset, "-crf", str(quality)]
    if encoder == "h264_nvenc":
        return ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr",
                "-cq", str(quality), "-b:v", "0"]
    if encoder == "hevc_nvenc":
        return ["-c:v", "hevc_nvenc", "-preset", "p5", "-rc", "vbr",
                "-cq", str(quality), "-b:v", "0", "-tag:v", "hvc1"]
    raise SystemExit(f"不支持的编码器：{encoder}")


def filter_safe_name(sub: Path, workdir: Path):
    """
    libass 的 filtergraph 参数里，空格、冒号、逗号、方括号都要转义，
    而 Windows 盘符的冒号尤其容易踩坑。与其写转义规则，不如保证传给滤镜的
    永远是「当前目录下的纯 ASCII 文件名」——所以这里必要时复制一份。
    """
    if re.fullmatch(r"[A-Za-z0-9._-]+", sub.name):
        return sub.name
    tmp = workdir / "_subs_input.ass"
    shutil.copy2(sub, tmp)
    return tmp.name


def main():
    ap = argparse.ArgumentParser(description="把字幕压制进视频")
    ap.add_argument("video", help="原始视频")
    ap.add_argument("subtitle", help="字幕文件（.ass，由 02 步生成）")
    ap.add_argument("-o", "--out", default=None,
                    help="输出文件，默认 <视频名>_cn.mp4")
    ap.add_argument("--soft", action="store_true",
                    help="软字幕：封装成可开关轨道，不重编码画面（极快）")
    ap.add_argument("--crf", default="20",
                    help="质量，数值越小越清晰体积越大。x264 默认 20；"
                         "4K 高细节素材建议 18")
    ap.add_argument("--preset", default="medium",
                    help="libx264 预设，越慢压缩率越高：ultrafast…veryslow。默认 medium")
    ap.add_argument("--encoder", default="libx264",
                    choices=["libx264", "h264_nvenc", "hevc_nvenc"],
                    help="视频编码器。有 NVIDIA 显卡可选 h264_nvenc / hevc_nvenc 提速")
    ap.add_argument("--font", default=None, help="指定中文字体文件路径")
    args = ap.parse_args()

    ffmpeg = env.require_tool("ffmpeg")

    video = Path(args.video).expanduser().resolve()
    sub = Path(args.subtitle).expanduser().resolve()
    if not video.exists():
        sys.exit(f"找不到视频：{video}")
    if not sub.exists():
        sys.exit(f"找不到字幕：{sub}")
    if sub.suffix.lower() not in (".ass", ".ssa"):
        sys.exit(f"硬字幕需要 .ass 文件（{sub.suffix} 不支持）。\n"
                 f"先运行 scripts/02_make_srt.py 生成 bilingual.ass，"
                 f"或加 --soft 用 SRT 做软字幕。")

    out = (Path(args.out).expanduser().resolve() if args.out
           else video.with_name(video.stem + "_cn.mp4"))
    out.parent.mkdir(parents=True, exist_ok=True)

    print(f"  输入：{video.name}")
    print(f"  字幕：{sub.name}")
    print(f"  输出：{out}")

    # ---------------------------------------------------------- 软字幕
    if args.soft:
        print("\n  模式：软字幕（画面不重编码）\n", flush=True)
        cmd = [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "warning", "-stats",
            "-i", str(video), "-i", str(sub),
            "-map", "0:v:0", "-map", "0:a?", "-map", "1:0",
            "-c:v", "copy", "-c:a", "copy", "-c:s", "mov_text",
            "-metadata:s:s:0", "language=chi",
            "-metadata:s:s:0", "title=Chinese",
            "-movflags", "+faststart",
            str(out),
        ]
        if subprocess.run(cmd).returncode != 0:
            sys.exit("封装失败。若源音频不是 AAC，试试先转封装再重试。")
        _report(out)
        return

    # ---------------------------------------------------------- 硬字幕
    workdir = sub.parent
    rel = filter_safe_name(sub, workdir)

    # 字体放到独立的 fonts/ 子目录。libass 会把 fontsdir 下的每个文件都当字体
    # 试加载一遍——如果和 .ass/.srt/.json 混在同一目录，日志会被一堆
    # "Error opening memory font" 刷屏。不影响结果，但看起来像出错了。
    font_dir = workdir / "fonts"
    font_dir.mkdir(parents=True, exist_ok=True)
    font_file, font_family = env.copy_font_to(font_dir, args.font)
    if font_family:
        print(f"  字体：{font_family}（已就位 {font_dir.name}/{Path(font_file).name}）")
    else:
        print("  [警告] 未找到中文字体，字幕可能显示为方块。"
              "用 --font 指定字体文件可解决。", file=sys.stderr)

    print(f"\n  模式：硬字幕 | 编码器 {args.encoder} | CRF {args.crf} "
          f"| preset {args.preset}\n", flush=True)

    cmd = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "warning", "-stats",
        "-i", str(video),
        "-vf", f"ass={rel}:fontsdir=fonts",
        *build_encoder_args(args.encoder, args.crf, args.preset),
        "-pix_fmt", "yuv420p",
        "-c:a", "copy",
        "-movflags", "+faststart",
        str(out),
    ]

    t0 = time.time()
    # cwd=workdir 让 ass 滤镜只需文件名，避开所有路径转义问题
    r = subprocess.run(cmd, cwd=workdir)
    if r.returncode != 0:
        sys.exit(f"ffmpeg 失败（退出码 {r.returncode}）。")

    elapsed = time.time() - t0
    print(f"\n  压制耗时：{elapsed:.0f}s")
    _report(out)


def _report(out: Path):
    if not out.exists():
        sys.exit("输出文件未生成。")
    mb = out.stat().st_size / 1048576
    print(f"\n完成 -> {out}")
    print(f"  体积 {mb:.1f} MB")
    try:
        ffprobe = env.find_tool("ffprobe") or "ffprobe"
        r = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries",
             "format=duration,bit_rate", "-of", "default=nw=1:nk=1", str(out)],
            capture_output=True, text=True, timeout=30)
        vals = r.stdout.split()
        if len(vals) >= 2:
            print(f"  时长 {float(vals[0]):.1f}s | "
                  f"码率 {float(vals[1]) / 1e6:.2f} Mbps")
    except Exception:
        pass


if __name__ == "__main__":
    main()
