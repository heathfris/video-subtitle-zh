#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
可选步骤 —— 探测画面底部的「字幕安全区」

为什么要这一步：很多宣传片、发布会视频本身就在画面里排了大字（动态排版风格），
有些还压得很低。如果不管三七二十一按默认底边距贴上去，中文字幕会和画面原有
文字叠在一起，两条都看不清。

这个脚本有两种用法：

1. **全局探测**（默认）：找出画面底部哪一段干净，反推一个合适的字幕底边距。
   适合口播、采访这类画面稳定的素材。

2. **逐句避让**（`--segments`）：宣传片、发布会这类动态排版素材，画面大字会满屏
   漂浮，**固定位置无论选哪儿都会有一部分句子撞上**。这个模式逐句判断该句时段内
   底部是否被占，只在被占时才把这一句抬高到空位，输出 `placement.json` 供
   02 步按句设置底边距。

只依赖 numpy；不装 numpy 也能跳过这一步，用默认边距。

用法:
    # 全局探测
    python scripts/00_probe_safe_area.py <视频> [--json]

    # 逐句避让（推荐给动态排版素材）
    python scripts/00_probe_safe_area.py <视频> \\
        --segments <workdir>/segments.json --plan-out <workdir>/placement.json
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

# ---------------------------------------------------------------- 逐句避让
#
# 全局探测给出的只是「整片最空闲的那个高度」，是**固定位置**。
# 但动态排版素材的大字会满画面漂浮：实测某宣传片里，即便选了全片最优位置，
# 仍有约六成句子的字幕会和画面原有文字局部重叠，密集排版段落几乎必叠。
#
# 所以再做一层逐句判断：默认仍贴底，只有当**这一句**的时间区间里底部确实
# 被原片内容占住时，才把这一句抬到更高的空位去。这样字幕大部分时间不动，
# 只在真正需要时避让，观感比「每句都重新找位置」（会频繁上下跳）稳得多。

PROBE_FPS_SEG = 4.0      # 逐句分析的采样率，0.25s 一帧；比全局探测密，避免漏掉扫过的元素

# 候选槽位 = 字幕底边距占画面高度的比例。第 0 个是默认位置（贴底），
# 其余依次向上抬。相邻槽位间距小于字幕块高度是刻意的——目标是「采样几个
# 不同的高度去找空位」，而不是「把字幕块码成互不重叠的一摞」。
SLOT_RATIOS = (0.055, 0.145, 0.245, 0.34)

INTRUDE_TRIGGER = 0.30   # 默认位置侵入率超过它就尝试上移
INTRUDE_ACCEPT = 0.15    # 上移后的槽位要低于它才算「躲开了」

# 找不到空位时的兜底：只有另一个槽位**明显**比原位干净才值得动。
# 否则会出现「所有槽位都被占满、随便挑一个」的情况——动了等于白动，
# 白白让字幕跳一下，反而更难看。实测阈值放到 0.25 时，某句从 0.86 挪到 0.57
# （还是大半时间被挡）也会触发，跳完几乎没改善；0.35 只留下真正躲得开的。
INTRUDE_IMPROVE = 0.35

# 太短的句子不为避让而移动。1 秒出头的字幕突然从底部弹到画面中部、
# 下一句又弹回来，观感上像故障；这点收益不值得。
MIN_MOVE_DURATION = 1.5

# 规划时按这个块高来判定是否碰撞。比 SUB_BLOCK_RATIO 留得多一些，
# 因为双语块最坏情况是「英文 2 行 + 中文 2 行」，比常见情形高。
PLAN_BLOCK_RATIO = 0.14


def grab_frames_fps(ffmpeg: str, video: Path, fps: float):
    """按指定采样率抽灰度帧，以 rawvideo 从 stdout 读回，避免落盘。"""
    import numpy as np

    vf = f"fps={fps},scale={PROBE_W}:{PROBE_H}:flags=area,format=gray"
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


def grab_frames(ffmpeg: str, video: Path, interval: float = 2.0):
    """按固定间隔抽帧，等价于 fps = 1/interval。"""
    return grab_frames_fps(ffmpeg, video, 1.0 / interval)


def row_edge_ratio(frames):
    """
    每行「边缘像素占比」，(N, H)。

    只统计画面水平中间一段（见 H_MARGIN_RATIO）——字幕居中排版，
    两侧的装饰图案不会和它打架，算进来只会让结论过于保守。
    """
    import numpy as np

    w0 = int(frames.shape[2] * H_MARGIN_RATIO)
    w1 = frames.shape[2] - w0
    band = frames[:, :, w0:w1].astype(np.int16)
    return (np.abs(np.diff(band, axis=2)) > GRAD_THRESHOLD).mean(axis=2)


def occupancy_mask(frames):
    """(N, H) bool：某帧某行是否「有内容」。"""
    return row_edge_ratio(frames) > ROW_ACTIVE


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

    row_edge = row_edge_ratio(frames)
    occupied = row_edge > ROW_ACTIVE
    occupancy = occupied.mean(axis=0)                           # (H,) 0~1
    profile = row_edge.mean(axis=0)                             # (H,) 平均强度

    busy = np.where(occupancy > FRAME_OCCUPANCY)[0]
    content_bottom = int(busy.max()) if len(busy) else 0

    return {"occupancy": occupancy, "profile": profile,
            "content_bottom": content_bottom, "frames": frames.shape[0]}


# ---------------------------------------------------------------- 逐句避让

def slot_intrusion(occupied, fps: float, t0: float, t1: float,
                   slot: int, block_ratio: float = PLAN_BLOCK_RATIO):
    """
    某个候选槽位在 [t0, t1] 区间内被原片内容侵入的帧比例。

    槽位按「底边距」定义：slot 越大，字幕块越靠上。
    """
    H = occupied.shape[1]
    bottom = 1.0 - SLOT_RATIOS[slot]
    top = bottom - block_ratio
    y0 = max(0, int(round(top * H)))
    y1 = min(H, int(round(bottom * H)))
    if y1 <= y0:
        return 0.0

    i0 = max(0, int(t0 * fps))
    i1 = min(occupied.shape[0], max(int(t1 * fps), i0 + 1))
    if i1 <= i0:
        return 0.0
    return float(occupied[i0:i1, y0:y1].any(axis=1).mean())


def plan_placement(occupied, fps: float, segments):
    """
    为每一句字幕选一个槽位。

    默认贴底（slot 0）；只有该句区间内底部确实被占住时才向上找空位。
    找不到空位时，只有别的槽位**明显**更干净才动——否则保持原位，
    避免「所有位置都脏、随便挪一下」这种无意义的跳动。
    """
    plan = []
    for seg in segments:
        t0, t1 = float(seg["start"]), float(seg["end"])
        rates = [slot_intrusion(occupied, fps, t0, t1, k)
                 for k in range(len(SLOT_RATIOS))]

        slot = 0
        if rates[0] > INTRUDE_TRIGGER and (t1 - t0) >= MIN_MOVE_DURATION:
            clear = [k for k in range(1, len(SLOT_RATIOS))
                     if rates[k] <= INTRUDE_ACCEPT]
            if clear:
                slot = clear[0]                       # 最低的那个空位
            else:
                best = min(range(1, len(SLOT_RATIOS)), key=lambda k: rates[k])
                if rates[0] - rates[best] >= INTRUDE_IMPROVE:
                    slot = best                       # 确实躲得开才动

        plan.append({"slot": slot, "rates": [round(r, 3) for r in rates]})
    return plan


def probe_size(video: Path):
    """ffprobe 取真实分辨率，失败回落 1920x1080。"""
    try:
        out = subprocess.run(
            [env.find_tool("ffprobe") or "ffprobe", "-v", "error",
             "-select_streams", "v:0", "-show_entries", "stream=width,height",
             "-of", "csv=p=0:s=x", str(video)],
            capture_output=True, text=True, timeout=30).stdout.strip()
        parts = out.splitlines()[0].split("x")
        return int(parts[0]), int(parts[1])
    except Exception:
        return 1920, 1080


def run_plan(ffmpeg, video: Path, real_w, real_h, seg_path: Path,
             fps: float, out_path):
    """逐句避让模式：为每句字幕算一个槽位，写出 placement.json。"""
    segments = json.loads(seg_path.read_text(encoding="utf-8"))
    if not isinstance(segments, list) or not segments:
        sys.exit(f"{seg_path} 的内容不是非空数组")

    print(f"\n逐句分析中（{fps}fps 采样，共 {len(segments)} 句）...", flush=True)
    frames = grab_frames_fps(ffmpeg, video, fps)
    if frames is None:
        sys.exit("抽帧失败，请确认视频有可解码的画面。")

    plan = plan_placement(occupancy_mask(frames), fps, segments)

    entries = []
    for seg, p in zip(segments, plan):
        entries.append({
            "start": round(float(seg["start"]), 3),
            "end": round(float(seg["end"]), 3),
            "slot": p["slot"],
            "margin_v": round(SLOT_RATIOS[p["slot"]] * real_h),
            "intrusion": p["rates"],
        })

    moved = [e for e in entries if e["slot"] > 0]
    result = {
        "video": str(video),
        "resolution": [real_w, real_h],
        "fps": fps,
        "block_ratio": PLAN_BLOCK_RATIO,
        "slot_ratios": list(SLOT_RATIOS),
        "slot_margin_v": [round(r * real_h) for r in SLOT_RATIOS],
        "segments": len(entries),
        "moved": len(moved),
        "entries": entries,
    }

    if out_path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2),
                            encoding="utf-8")
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))

    if moved:
        print(f"\n{len(entries)} 句里 {len(moved)} 句需要上移避让"
              f"（{len(moved) / len(entries):.0%}）")
        for k in range(len(SLOT_RATIOS)):
            c = sum(1 for e in entries if e["slot"] == k)
            if c:
                print(f"  槽位 {k} 底边距 {round(SLOT_RATIOS[k] * real_h):>5}px"
                      f"  {c:>3} 句  {'#' * max(1, int(c / len(entries) * 40))}")
    else:
        print(f"\n{len(entries)} 句全部可以贴底，无需避让。")
    if out_path:
        print(f"\n已写出 {out_path}")


def main():
    ap = argparse.ArgumentParser(description="探测画面字幕安全区 / 规划逐句避让")
    ap.add_argument("video")
    ap.add_argument("--interval", type=float, default=2.0,
                    help="全局探测的抽帧间隔秒数，默认 2；长视频可放大到 5")
    ap.add_argument("--json", action="store_true", help="输出 JSON，便于脚本调用")
    ap.add_argument("--segments", default=None,
                    help="segments.json 路径；给了就进入逐句避让模式")
    ap.add_argument("--fps", type=float, default=PROBE_FPS_SEG,
                    help=f"逐句避让模式的采样率，默认 {PROBE_FPS_SEG}")
    ap.add_argument("--plan-out", default=None,
                    help="逐句避让结果写到哪个文件；不填则打到 stdout")
    args = ap.parse_args()

    video = Path(args.video).expanduser().resolve()
    if not video.exists():
        sys.exit(f"找不到视频：{video}")

    try:
        import numpy  # noqa: F401
    except ImportError:
        sys.exit("需要 numpy：pip install numpy")

    ffmpeg = env.require_tool("ffmpeg")
    real_w, real_h = probe_size(video)

    if args.segments:
        seg_path = Path(args.segments).expanduser().resolve()
        if not seg_path.exists():
            sys.exit(f"找不到 {seg_path}")
        run_plan(ffmpeg, video, real_w, real_h, seg_path, args.fps,
                 Path(args.plan_out).expanduser().resolve() if args.plan_out else None)
        return

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
