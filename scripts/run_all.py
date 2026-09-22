#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
一键串联全部步骤

    抽音轨 → 语音识别 → 翻译（需要人工/AI 介入）→ 生成字幕 → 压制

翻译是唯一没法全自动、也最值得认真做的一步，所以流程在这里设计成「半自动」：
第一次运行会自动识别并把空白译文模板准备好，然后停下来；译文补齐后
再跑一次，它会自动接着往下做完。

用法:
    python scripts/run_all.py <视频>
    python scripts/run_all.py <视频> --model distil-large-v3 --crf 18 --probe
    python scripts/run_all.py <视频> --workdir ./mysubs --soft

`--probe` 会逐句分析画面占用，把字幕只在该避让的句子上抬（见 00_probe_safe_area.py）。
"""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PY = sys.executable


def step(title):
    print(f"\n{'=' * 62}\n {title}\n{'=' * 62}", flush=True)


def run(cmd, **kw):
    print("  $", " ".join(str(c) for c in cmd), flush=True)
    r = subprocess.run([str(c) for c in cmd], **kw)
    if r.returncode != 0:
        sys.exit(f"\n步骤失败（退出码 {r.returncode}）。")


def main():
    ap = argparse.ArgumentParser(
        description="一键跑完整条英文字幕→中文字幕流水线",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("video", help="输入视频")
    ap.add_argument("--workdir", default=None,
                    help="工作目录，默认 <视频名>_subs")
    ap.add_argument("--model", default="small",
                    help="识别模型，默认 small。英文建议 distil-large-v3（更准）")
    ap.add_argument("--lang", default="en", help="源语言，默认 en")
    ap.add_argument("--initial-prompt", default=None,
                    help="术语提示，如 \"TypeSafe, Jev, RLCD\"，能明显改善专有名词识别")
    ap.add_argument("--device", default="auto", choices=["auto", "cpu", "cuda"])
    ap.add_argument("--crf", default="20", help="压制质量，越小越清晰。4K 建议 18")
    ap.add_argument("--preset", default="medium", help="libx264 预设")
    ap.add_argument("--encoder", default="libx264",
                    choices=["libx264", "h264_nvenc", "hevc_nvenc"],
                    help="有 N 卡可用 h264_nvenc 大幅提速")
    ap.add_argument("--soft", action="store_true", help="输出软字幕，不重编码画面")
    ap.add_argument("--probe", action="store_true",
                    help="先分析画面占用、规划逐句避让（动态排版类视频强烈建议开）")
    ap.add_argument("--style", choices=("plate", "outline"), default="plate",
                    help="字幕风格：plate=半透明底板（默认，任何背景都清晰），"
                         "outline=纯描边（画面最干净，浅色背景对比度偏弱）")
    ap.add_argument("--font", default=None, help="指定中文字体文件")
    ap.add_argument("--no-vad", action="store_true", help="关闭静音切分")
    args = ap.parse_args()

    video = Path(args.video).expanduser().resolve()
    if not video.exists():
        sys.exit(f"找不到视频：{video}")

    wd = (Path(args.workdir).expanduser().resolve() if args.workdir
          else video.parent / f"{video.stem}_subs")
    wd.mkdir(parents=True, exist_ok=True)

    t_start = time.time()

    # ---------------------------------------------- 1. 识别
    seg_path = wd / "segments.json"
    if seg_path.exists():
        n = len(json.loads(seg_path.read_text(encoding="utf-8")))
        print(f"\n[跳过] 已有识别结果（{n} 句）：{seg_path}")
        print("       要重新识别请删除该文件。")
    else:
        step("步骤 1/3  语音识别")
        cmd = [PY, HERE / "01_transcribe.py", str(video),
               "--model", args.model, "--lang", args.lang,
               "--outdir", str(wd), "--device", args.device]
        if args.initial_prompt:
            cmd += ["--initial-prompt", args.initial_prompt]
        if args.no_vad:
            cmd.append("--no-vad")
        run(cmd)

    # ---------------------------------------------- 2. 翻译（人工环节）
    tr_path = wd / "translations.json"
    need_translation = True
    if tr_path.exists():
        try:
            raw = json.loads(tr_path.read_text(encoding="utf-8"))
            zh = raw.get("zh") if isinstance(raw, dict) else raw
            n_seg = len(json.loads(seg_path.read_text(encoding="utf-8")))
            if isinstance(zh, list) and len(zh) == n_seg and all(str(z).strip() for z in zh):
                need_translation = False
            else:
                print(f"\n[提示] 译文文件存在但还不完整，需要补齐。")
        except Exception as e:
            print(f"\n[提示] 译文文件读不出来（{e}），重新生成模板。")

    if need_translation:
        step("步骤 2/3  翻译（需要你介入）")
        run([PY, HERE / "check_translations.py", str(wd), "--init"])
        print(f"""
{'─' * 62}
识别已经完成，接下来需要把每句话翻成中文。

  1. 打开 {tr_path}
  2. 里面是一个字符串数组，顺序与 segments.json 一一对应
  3. 逐句填入中文译文（数组长度必须保持一致）
  4. 存盘后重新运行本命令，会自动接着跑完

如果你在用 AI 助手，可以直接让它读 segments.json 并写出 translations.json。
{'─' * 62}""")
        sys.exit(0)

    # ---------------------------------------------- 3. 生成字幕
    step("步骤 2/3  生成字幕")
    cmd = [PY, HERE / "02_make_srt.py", str(wd), "--video", str(video),
           "--style", args.style]
    if args.font:
        cmd += ["--font", args.font]
    if args.probe:
        print("  先分析画面占用，规划逐句避让...", flush=True)
        r = subprocess.run(
            [PY, str(HERE / "00_probe_safe_area.py"), str(video),
             "--segments", str(seg_path), "--plan-out", str(wd / "placement.json")],
            capture_output=True, text=True)
        if r.returncode == 0:
            for line in r.stdout.strip().splitlines()[-6:]:
                print("  " + line)
        else:
            print(f"  [警告] 画面分析失败，所有句子用统一底边距。"
                  f"{r.stderr.strip()[:200]}")
    run(cmd)

    # ---------------------------------------------- 4. 压制
    step("步骤 3/3  压制输出")
    sub_file = wd / ("bilingual.ass" if not args.soft else "bilingual.srt")
    cmd = [PY, HERE / "03_burn.py", str(video), str(sub_file),
           "-o", str(wd / f"{video.stem}_cn.mp4"),
           "--crf", args.crf, "--encoder", args.encoder]
    if args.soft:
        cmd.append("--soft")
    else:
        cmd += ["--preset", args.preset]
        if args.font:
            cmd += ["--font", args.font]
    run(cmd)

    total = time.time() - t_start
    print(f"\n{'=' * 62}")
    print(f" 全部完成，总耗时 {total / 60:.1f} 分钟")
    print(f" 产物目录：{wd}")
    print(f"{'=' * 62}")
    for name in ("bilingual.srt", "zh.srt", "bilingual.ass", "zh.ass",
                 f"{video.stem}_cn.mp4"):
        p = wd / name
        if p.exists():
            print(f"  {name:<28} {p.stat().st_size / 1048576:7.2f} MB")


if __name__ == "__main__":
    main()
