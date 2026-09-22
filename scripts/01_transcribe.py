#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Step 1/3 —— 抽取音轨 + 语音识别

产出：
  <outdir>/segments.json   带时间轴的识别结果（后续翻译的输入）
  <outdir>/<lang>.srt      纯源语言字幕，可拿来校对
  <outdir>/audio16k.wav    中间产物，重跑时自动复用

用法:
    python scripts/01_transcribe.py <视频> [--model small] [--lang en]
"""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import env  # noqa: E402


def fmt_ts(seconds: float) -> str:
    """秒 → SRT 时间戳 00:00:00,000"""
    if seconds < 0:
        seconds = 0
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def extract_audio(ffmpeg: str, video: Path, wav: Path):
    """抽 16kHz 单声道 PCM —— Whisper 的标准输入格式。"""
    subprocess.run(
        [ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
         "-i", str(video), "-vn", "-ac", "1", "-ar", "16000",
         "-c:a", "pcm_s16le", str(wav)],
        check=True)


def write_srt(segments, path: Path, lang: str = "en"):
    lines = []
    for i, seg in enumerate(segments, 1):
        lines += [str(i), f"{fmt_ts(seg['start'])} --> {fmt_ts(seg['end'])}",
                  seg["text"].strip(), ""]
    path.write_text("\n".join(lines), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(
        description="抽取音轨并做语音识别，输出 segments.json 与源语言 SRT")
    ap.add_argument("video", help="输入视频或音频文件")
    ap.add_argument("--model", default="small",
                    help="通用：tiny/base/small/medium/large-v3；"
                         "英文专用（更准更快）：distil-small/distil-medium/distil-large-v3。"
                         "也可直接给本地模型目录。默认 small")
    ap.add_argument("--lang", default="en",
                    help="源语言代码（en/ja/ko/...），设为 auto 让模型自行判断。默认 en")
    ap.add_argument("--outdir", default=None,
                    help="工作目录，默认 <视频名>_subs")
    ap.add_argument("--device", default="auto", choices=["auto", "cpu", "cuda"],
                    help="推理设备，默认 auto（有 N 卡就用）")
    ap.add_argument("--initial-prompt", default=None,
                    help="术语提示，例如 \"TypeSafe, Jev, RLCD\" —— "
                         "把专有名词写进去能明显减少误听")
    ap.add_argument("--no-vad", action="store_true",
                    help="关闭静音切分；音乐、多人抢话、无停顿的素材可试")
    args = ap.parse_args()

    video = Path(args.video).expanduser().resolve()
    if not video.exists():
        sys.exit(f"找不到输入文件：{video}")

    outdir = (Path(args.outdir).expanduser().resolve() if args.outdir
              else video.parent / f"{video.stem}_subs")
    outdir.mkdir(parents=True, exist_ok=True)
    wav = outdir / "audio16k.wav"

    print("\n[1/3] 环境检查", flush=True)
    ffmpeg, _ = env.check_ffmpeg()
    model_dir = env.ensure_model(args.model)
    device, compute_type = env.pick_device(args.device)

    # 语种与模型的匹配提醒：蒸馏模型只认英文，用错语种会输出胡话
    base_name = Path(args.model).name if Path(args.model).is_dir() else args.model
    if base_name in env.ENGLISH_ONLY and args.lang not in ("en", "auto"):
        print(f"  [警告] {base_name} 是英文专用模型，对 {args.lang} 效果很差。"
              f"建议改用 small / medium / large-v3。", flush=True)

    print("\n[2/3] 抽取音轨", flush=True)
    if wav.exists() and wav.stat().st_size > 1024:
        print(f"  已存在，跳过（如需重抽请删除 {wav.name}）", flush=True)
    else:
        extract_audio(ffmpeg, video, wav)
        print(f"  → {wav.name}  ({wav.stat().st_size / 1048576:.1f} MB)", flush=True)

    print("\n[3/3] 语音识别", flush=True)
    print(f"  模型：{model_dir}", flush=True)

    from faster_whisper import WhisperModel

    def transcribe_all(m):
        """跑完整识别。真正的计算发生在迭代生成器时，所以必须整个包在 try 里。"""
        it, inf = m.transcribe(
            str(wav),
            language=None if args.lang == "auto" else args.lang,
            beam_size=5,
            vad_filter=not args.no_vad,
            vad_parameters=None if args.no_vad else dict(min_silence_duration_ms=400),
            condition_on_previous_text=False,   # 防止前文错误滚雪球
            initial_prompt=args.initial_prompt,
        )
        out = []
        for s in it:
            text = s.text.strip()
            if text:
                out.append({"start": round(s.start, 3),
                            "end": round(s.end, 3),
                            "text": text})
        return out, inf

    segments, info = [], None
    for attempt in (1, 2):
        print(f"  设备：{device} / {compute_type}", flush=True)
        try:
            t0 = time.time()
            segments, info = transcribe_all(
                WhisperModel(str(model_dir), device=device, compute_type=compute_type))
            break
        except Exception as e:
            # CUDA 的问题常常拖到第一次推理才暴露（缺 cublas / cudnn）。
            # 所以回落逻辑必须罩住「加载 + 推理」，而不能只罩住加载。
            if attempt == 1 and device == "cuda":
                print(f"\n  [警告] GPU 推理失败：{e}", flush=True)
                print("  GPU 推理需要 CUDA 运行库，仅有显卡驱动不够。想用 GPU 请装：",
                      flush=True)
                print(f"    {sys.executable} -m pip install nvidia-cublas-cu12 nvidia-cudnn-cu12",
                      flush=True)
                print("  已自动回落到 CPU 继续（结果完全一致，只是慢一些）。", flush=True)
                print(f"  下次运行将直接用 CPU；装好上面两个包后删掉 "
                      f"{env.CUDA_BROKEN_FLAG} 可重新启用 GPU。\n", flush=True)
                env.mark_cuda_broken(f"{type(e).__name__}: {e}")
                device, compute_type = "cpu", "int8"
                continue
            raise

    print(f"  检测语种：{info.language}（置信度 {info.language_probability:.2f}）"
          f"  音频时长：{info.duration:.1f}s", flush=True)
    print("", flush=True)

    for s in segments:
        print(f"  [{fmt_ts(s['start'])}] {s['text']}", flush=True)

    if not segments:
        sys.exit("未识别到任何语音。若确认有声音，试试 --no-vad。")

    (outdir / "segments.json").write_text(
        json.dumps(segments, ensure_ascii=False, indent=2), encoding="utf-8")
    write_srt(segments, outdir / f"{info.language}.srt")

    elapsed = time.time() - t0
    speed = info.duration / elapsed if elapsed > 0 else 0
    print(f"\n完成，耗时 {elapsed:.0f}s（{speed:.1f}x 实时速度）", flush=True)
    print(f"  共 {len(segments)} 句", flush=True)
    print(f"  识别结果：{outdir / 'segments.json'}", flush=True)
    print(f"  源语言字幕：{outdir / f'{info.language}.srt'}", flush=True)
    print(f"\n下一步：翻译 segments.json 里的 text，写成同长度的 translations.json，"
          f"然后跑 scripts/02_make_srt.py {outdir}", flush=True)


if __name__ == "__main__":
    main()
