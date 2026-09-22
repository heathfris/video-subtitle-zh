#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
可选步骤 —— 探测画面底部的「字幕安全区」

为什么要这一步：很多宣传片、发布会视频本身就在画面里排了大字（动态排版风格），
有些还压得很低。如果不管三七二十一按默认底边距贴上去，中文字幕会和画面原有
文字叠在一起，两条都看不清。

这个脚本按固定间隔抽帧，用水平梯度（文字边缘的特征）统计每一行「有内容的
帧占比」，据此找出画面底部到底哪一段是干净的，然后反推一个合适的字幕底边距。

只依赖 numpy；不装 numpy 也能跳过着一步，用默认边距。

用法:
    python scripts/00_probe_safe_area.py <视频> [--json]
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import env  # noqa: E402

# 抽帧参数：太小会漏掉一闪而过的元素，太大则慢
PROBE_W, PROBE_H = 384, 216
GRAD_THRESHOLD = 30      # 相邻像素灰度差超过这个值，认为踩到了文字/图形边缘
ROW_ACTIVE = 0.02        # 一行里有 2% 的像素是边缘，算这行「有内容」

# 一行要「大多数帧」都有内容，才算被常驻元素（logo、水印、固定排版）占用。
# 这个门槛不能设低：动态排版类素材里，画面大字只在部分时间扫过底部，
# 门槛低会把这种「偶尔经过」也判成常驻，得出「底部全被占满、无处安放字幕」
# 的错误结论。实测阈值 0.15 时几乎整个底部都会被标成占用，而实际大部分
# 帧那里是空的。
FRAME_OCCUPANCY = 0.6

# 只分析画面水平中间的这一段。字幕是居中排版的（左右各留白约 5%），
# 画面两侧的装饰图案、边框、角标根本不会和字幕打架，算进来只会让结论过于保守。
H_MARGIN_RATIO = 0.12

# 字幕块（中文 1~2 行 + 英文 1~2 行）大约占据的画面高度比例。
# 0.11 对应「中文一行 + 英文一行 + 少量折行」这一常见情形。
SUB_BLOCK_RATIO = 0.11


def grab_frames(ffmpeg: str, video: Path, interval: float = 2.0):
    """按固定间隔抽灰度帧，以 rawvideo 从 stdout 读回，避免落盘。"""
    import numpy as np

    vf = f"fps=1/{interval},scale={PROBE_W}:{PROBE_H}:flags=area,format=gray"
    r = subprocess.run(
        [ffmpeg, "-v", "error", "-i", str(video), "-vf", vf,
         "-f", "rawvideo", "-pix_fmt", "gray", "-"],
        capture_output=True)
    if r.returncode != 0 or not r.stdout:
        return None
    buf = np.frombuffer(r.stdout, dtype=np.uint8)
    frame_bytes = PROBE_W * PROBE_H
    n = len(buf) // frame_bytes
    if n == 0:
        return None
    return buf[:n * frame_bytes].reshape(n, PROBE_H, PROBE_W)


def analyze(frames):
    """
    返回一个字典，描述画面各纵向位置的「内容占用情况」。

    做法：算出每帧的水平梯度 → 每行「边缘像素占比」→ 再对帧取统计，
    得到每一行在整段视频里有多常出现内容。偶发的过场元素不会误判。

    统计范围限定在画面水平中间（见 H_MARGIN_RATIO）——因为字幕是居中的，
    两侧的装饰图案不会和它打架。整行判断会让结论过于保守：像半调网点风格
    的素材，左右两侧的图案常常一路铺到底部，但中间是空的。
    """
    import numpy as np

    w0 = int(PROBE_W * H_MARGIN_RATIO)
    w1 = PROBE_W - w0
    band = frames[:, :, w0:w1].astype(np.int16)

    diff = np.abs(np.diff(band, axis=2))                        # (N, H, W')
    row_edge = (diff > GRAD_THRESHOLD).mean(axis=2)             # (N, H)
    occupied = row_edge > ROW_ACTIVE                            # (N, H) bool

    occupancy = occupied.mean(axis=0)                           # (H,) 0~1
    profile = row_edge.mean(axis=0)                             # (H,) 平均强度

    busy = np.where(occupancy > FRAME_OCCUPANCY)[0]
    content_bottom = int(busy.max()) if len(busy) else 0

    return {"occupancy": occupancy, "profile": profile,
            "content_bottom": content_bottom, "frames": frames.shape[0]}


def main():
    ap = argparse.ArgumentParser(description="探测画面底部字幕安全区")
    ap.add_argument("video")
    ap.add_argument("--interval", type=float, default=2.0,
                    help="抽帧间隔秒数，默认 2；长视频可放大到 5")
    ap.add_argument("--json", action="store_true", help="输出 JSON，便于脚本调用")
    args = ap.parse_args()

    video = Path(args.video).expanduser().resolve()
    if not video.exists():
        sys.exit(f"找不到视频：{video}")

    try:
        import numpy  # noqa: F401
    except ImportError:
        sys.exit("需要 numpy：pip install numpy")

    ffmpeg = env.require_tool("ffmpeg")

    # 原视频高度，用于把比例换算成像素
    try:
        r = subprocess.run(
            [env.find_tool("ffprobe") or "ffprobe", "-v", "error",
             "-select_streams", "v:0", "-show_entries", "stream=height",
             "-of", "csv=p=0", str(video)],
            capture_output=True, text=True, timeout=30)
        real_h = int(r.stdout.strip().splitlines()[0])
        real_w = None
        r2 = subprocess.run(
            [env.find_tool("ffprobe") or "ffprobe", "-v", "error",
             "-select_streams", "v:0", "-show_entries", "stream=width",
             "-of", "csv=p=0", str(video)],
            capture_output=True, text=True, timeout=30)
        real_w = int(r2.stdout.strip().splitlines()[0])
    except Exception:
        real_h, real_w = 1080, 1920

    if not args.json:
        print(f"\n抽帧分析中（每 {args.interval}s 一帧）...", flush=True)
    frames = grab_frames(ffmpeg, video, args.interval)
    if frames is None:
        sys.exit("抽帧失败，请确认视频有可解码的画面。")

    res = analyze(frames)
    H = PROBE_H
    content_bottom = res["content_bottom"]

    # 换算到真实分辨率
    clean_top_ratio = content_bottom / H
    clean_height_ratio = 1.0 - clean_top_ratio
    clean_top_px = round(clean_top_ratio * real_h)
    clean_height_px = real_h - clean_top_px

    # 字幕底边距：让整块字幕落在干净区里，四周各留一点缓冲
    bottom_pad = round(real_h * 0.03)
    block_px = round(real_h * SUB_BLOCK_RATIO)
    margin_v = clean_height_px - block_px
    fits = margin_v >= bottom_pad
    margin_v = max(bottom_pad, min(margin_v, round(real_h * 0.055)))
    if not fits:
        margin_v = bottom_pad

    result = {
        "video": str(video),
        "resolution": [real_w, real_h],
        "frames_analyzed": res["frames"],
        "clean_area_top_ratio": round(clean_top_ratio, 4),
        "clean_area_top_px": clean_top_px,
        "clean_area_height_px": clean_height_px,
        "clean_area_height_ratio": round(clean_height_ratio, 4),
        "suggested_margin_v": margin_v,
        "enough_room": bool(fits),
    }

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    print(f"\n分析了 {res['frames']} 帧，画面 {real_w}x{real_h}")
    print(f"\n底部干净区：从 y={clean_top_px}px 到画面底 "
          f"（占画面高度 {clean_height_ratio:.1%}）")

    # 可视化占用曲线
    print("\n底部 40% 区域的内容占用情况（# 越多越可能有画面元素）：")
    start = int(H * 0.6)
    for y in range(start, H, 2):
        occ = res["occupancy"][y]
        bar = "#" * int(occ * 40)
        mark = " ←常被占用" if occ > FRAME_OCCUPANCY else ""
        print(f"  {y / H:6.1%}  {bar}{mark}")

    print(f"\n建议字幕底边距 marginV = {margin_v}px")
    if fits:
        print(f"  干净区高 {clean_height_px}px，足够放下双语字幕块（约 {block_px}px）")
    else:
        print(f"  [提示] 干净区只有 {clean_height_px}px，"
              f"小于字幕块所需（约 {block_px}px）。")
        print(f"  字幕会略微上探到画面元素区。可以考虑：")
        print(f"    · 只用中文字幕（--纯中文），或")
        print(f"    · 缩小字号（改 02_make_srt.py 里的比例），或")
        print(f"    · 把视频整体轻微上移后重新排版")

    print(f"\n用法：把 {margin_v} 传给 02 步")
    print(f"  python scripts/02_make_srt.py <workdir> --margin-v {margin_v}")


if __name__ == "__main__":
    main()
