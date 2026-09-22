#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Step 2/3 —— 把识别结果 + 译文合成字幕文件

输入：
  <workdir>/segments.json      01 步的产物
  <workdir>/translations.json  译文数组，顺序与 segments.json 严格一一对应

输出（四份，按需取用）：
  bilingual.srt   中英双语（通用，任何播放器/剪辑软件都能加载）
  zh.srt          纯中文
  bilingual.ass   双语带样式（字号、描边、折行按分辨率自适应，用于压制）
  zh.ass          纯中文带样式

用法:
    python scripts/02_make_srt.py <workdir> [--video 原视频]
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import env  # noqa: E402


# ---------------------------------------------------------------- 时间轴

def fmt_srt(t: float) -> str:
    if t < 0:
        t = 0
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def fmt_ass(t: float) -> str:
    if t < 0:
        t = 0
    cs = int(round(t * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h:d}:{m:02d}:{s:02d}.{cs:02d}"


# ---------------------------------------------------------------- 折行
# 字幕可读性的关键其实在这里：一行太长会来不及读，出现孤字行会很难看。
# 目标都是「尽量折成两行且长度均衡」，断点优先落在标点或词边界上。

def wrap_en(text: str, limit: int = 58):
    """英文折行。先找使两行都不超宽且长度最接近的空格断点。"""
    text = " ".join(text.split())
    if len(text) <= limit:
        return [text]

    best, best_score = None, None
    for i, ch in enumerate(text):
        if ch != " ":
            continue
        left, right = text[:i].strip(), text[i + 1:].strip()
        if not left or not right:
            continue
        if len(left) <= limit and len(right) <= limit:
            score = abs(len(left) - len(right))
            if best_score is None or score < best_score:
                best, best_score = (left, right), score
    if best:
        return list(best)

    # 兜底：贪心填充，最多两行
    words, lines, cur = text.split(), [], ""
    for w in words:
        cand = w if not cur else cur + " " + w
        if len(cand) <= limit:
            cur = cand
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines[:2] if len(lines) > 2 else lines


def _zh_width(text: str) -> int:
    """中日韩字符按 2 个宽度计，西文按 1 个，用于估算实际占位。"""
    return sum(2 if ord(ch) > 0x2E80 else 1 for ch in text)


def wrap_zh(text: str, limit: int = 34):
    """中文折行。优先在标点后断开，其次求两行长度均衡。"""
    text = " ".join(text.split())
    cap = limit * 2
    if _zh_width(text) <= cap:
        return [text]

    puncts = "，。、；：！？）》」』…,.;:!?)]"
    best, best_penalty, best_score = None, None, None
    for i in range(1, len(text)):
        left, right = text[:i].strip(), text[i:].strip()
        if not left or not right:
            continue
        lw, rw = _zh_width(left), _zh_width(right)
        if lw > cap or rw > cap:
            continue
        penalty = 0 if text[i - 1] in puncts else 1     # 标点断句优先
        if best_penalty is None or (penalty, abs(lw - rw)) < (best_penalty, best_score):
            best, best_penalty, best_score = (left, right), penalty, abs(lw - rw)
    if best:
        return list(best)

    # 兜底：按宽度贪心填充
    lines, cur, w = [], "", 0
    for ch in text:
        cw = 2 if ord(ch) > 0x2E80 else 1
        if w + cw > cap and cur:
            lines.append(cur)
            cur, w = "", 0
        cur += ch
        w += cw
    if cur:
        lines.append(cur)
    return lines[:2] if len(lines) > 2 else lines


def probe_size(video: Path):
    """ffprobe 取分辨率，失败回落 1920x1080。"""
    try:
        out = subprocess.run(
            [env.find_tool("ffprobe") or "ffprobe", "-v", "error",
             "-select_streams", "v:0", "-show_entries", "stream=width,height",
             "-of", "csv=p=0:s=x", str(video)],
            capture_output=True, text=True, check=True).stdout.strip()
        parts = out.splitlines()[0].split("x")
        return int(parts[0]), int(parts[1])
    except Exception:
        return 1920, 1080


# ---------------------------------------------------------------- 输出

def write_srt(items, path: Path, bilingual=True):
    blocks = []
    for i, it in enumerate(items, 1):
        body = f"{it['en']}\n{it['zh']}" if bilingual else it["zh"]
        blocks.append(f"{i}\n{fmt_srt(it['start'])} --> {fmt_srt(it['end'])}\n{body}\n")
    path.write_text("\n".join(blocks), encoding="utf-8")


# 两种字幕风格。
#
# outline（描边）：传统做法，画面最干净。但文字本身没有底色，落在白色/浅色
#   画面上，浅色文字配细描边会发灰——素材在明暗场景间切换时对比度不稳定。
# plate（底板）：文字坐在一层半透明黑底上，任何背景都清晰。代价是画面底部
#   多一层色带。（plate_alpha 是 ASS 的透明度：00=不透明，FF=全透明。）
STYLE_PRESETS = {
    "outline": {"border_style": 1, "plate_alpha": None},
    "plate": {"border_style": 3, "plate_alpha": 0x73},
}


def write_ass(items, path: Path, w, h, bilingual=True,
              limit_en=None, limit_zh=None, font_family="sans-serif",
              margin_v=None, style="plate", per_item_margin=None):
    """
    生成带样式的 ASS。字号/描边/边距全部按画面高度按比例推算，
    因此同一个脚本对 720p 和 4K 都能给出合适的观感。

    per_item_margin 若给了，就按句覆盖底边距——配合 00 步的逐句避让，
    让字幕只在会被画面原文字挡住时才抬高（见 00_probe_safe_area.py）。
    """
    zh_size = max(14, round(h * 0.047))
    en_size = max(12, round(h * 0.034))
    if margin_v is None:
        margin_v = max(20, round(h * 0.055))      # 抬高底边距，避免贴边被裁
    mlr = round(w * 0.05)                          # 左右留白
    usable = w - 2 * mlr

    # 折行宽度按实际几何反推
    if limit_zh is None:
        limit_zh = max(12, int(usable / zh_size))
    if limit_en is None:
        limit_en = max(24, int(usable / (en_size * 0.5)))
    # 再压一道可读性上限：单行太长读者来不及扫
    limit_en = min(limit_en, 58)
    limit_zh = min(limit_zh, 34)

    preset = STYLE_PRESETS.get(style) or STYLE_PRESETS["outline"]
    border_style = preset["border_style"]
    if preset["plate_alpha"] is None:
        # 描边模式：Outline 是描边宽度，Shadow 给一点投影
        zh_pad = max(1.6, round(h * 0.0024, 1))
        en_pad = max(1.3, round(h * 0.0019, 1))
        shadow = 1
        back = "&H96000000"
    else:
        # 底板模式：Outline 变成「文字到板边的内边距」，不需要投影
        zh_pad = max(6, round(zh_size * 0.22))
        en_pad = max(5, round(en_size * 0.22))
        shadow = 0
        back = f"&H{preset['plate_alpha']:02X}000000"

    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {w}\n"
        f"PlayResY: {h}\n"
        "WrapStyle: 2\n"
        "ScaledBorderAndShadow: yes\n"
        "YCbCr Matrix: TV.709\n"
        "\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: ZH,{font_family},{zh_size},&H00FFFFFF,&H000000FF,&H00000000,{back},"
        f"0,0,0,0,100,100,0,0,{border_style},{zh_pad},{shadow},2,{mlr},{mlr},{margin_v},1\n"
        f"Style: EN,{font_family},{en_size},&H00EAEAEA,&H000000FF,&H00000000,{back},"
        f"0,0,0,0,100,100,0,0,{border_style},{en_pad},{shadow},2,{mlr},{mlr},{margin_v},1\n"
        "\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )

    rows = []
    for idx, it in enumerate(items):
        en_lines = wrap_en(it["en"], limit_en)
        zh_lines = wrap_zh(it["zh"], limit_zh)
        if bilingual:
            text = ("{\\rEN}" + "\\N".join(en_lines)
                    + "\\N{\\rZH}" + "\\N".join(zh_lines))
        else:
            text = "{\\rZH}" + "\\N".join(zh_lines)
        # 事件自带 MarginV 字段，可逐句覆盖样式里的底边距
        mv = margin_v if per_item_margin is None else per_item_margin[idx]
        rows.append(
            f"Dialogue: 0,{fmt_ass(it['start'])},{fmt_ass(it['end'])},ZH,,0,0,{mv},,{text}")

    path.write_text(header + "\n".join(rows) + "\n", encoding="utf-8-sig")
    return limit_en, limit_zh


def load_translations(path: Path, expect: int):
    """
    读译文。接受两种格式：["...", ...] 或 {"zh": [...]}。
    长度必须与识别结果严格一致——这是整条流水线最容易静默出错的地方，
    所以这里宁可报错也不猜。
    """
    raw = json.loads(path.read_text(encoding="utf-8"))
    zh = raw.get("zh") if isinstance(raw, dict) else raw
    if not isinstance(zh, list):
        sys.exit(f"译文格式不对，应为数组或 {{\"zh\": [...]}}：{path}")
    if len(zh) != expect:
        sys.exit(
            f"译文条数不匹配：\n"
            f"  segments.json      {expect} 条\n"
            f"  translations.json  {len(zh)} 条\n"
            f"请补齐/删减后重试。可以先跑 scripts/check_translations.py 定位问题。")
    empty = [i + 1 for i, z in enumerate(zh) if not str(z).strip()]
    if empty:
        shown = ", ".join(str(i) for i in empty[:10])
        more = f" 等 {len(empty)} 处" if len(empty) > 10 else ""
        print(f"  [警告] 第 {shown} 句译文为空{more}，这些句子的中文字幕会是空白",
              file=sys.stderr)
    return list(zh)


def load_placement(wd: Path, expect: int, play_res_y: int, disabled=False):
    """
    读 00 步产出的逐句避让结果。没有就返回 None，全部句子用统一样式底边距。

    placement.json 里的 margin_v 是按原片高度算出来的，这里按 ASS 的 PlayResY
    等比缩放——`--video` 没给时 PlayRes 默认 1920x1080，与实测分辨率不一致，
    不缩放就会错位。
    """
    if disabled:
        return None
    p = wd / "placement.json"
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        entries = data["entries"]
        src_h = int(data["resolution"][1])
    except Exception as e:
        print(f"  [警告] {p.name} 读不了（{e}），改用统一底边距", file=sys.stderr)
        return None
    if src_h <= 0 or len(entries) != expect:
        print(f"  [警告] {p.name} 有 {len(entries)} 条、字幕有 {expect} 条，"
              f"对不上，改用统一底边距", file=sys.stderr)
        return None
    k = play_res_y / src_h
    return [max(0, round(int(e["margin_v"]) * k)) for e in entries]


def main():
    ap = argparse.ArgumentParser(description="合成中英双语字幕（SRT + ASS）")
    ap.add_argument("workdir", help="01 步产出的工作目录")
    ap.add_argument("--video", default=None, help="用于探测分辨率与安全区")
    ap.add_argument("--limit-en", type=int, default=None, help="覆盖英文折行宽度")
    ap.add_argument("--limit-zh", type=int, default=None, help="覆盖中文折行宽度")
    ap.add_argument("--font", default=None, help="指定中文字体文件路径")
    ap.add_argument("--font-name", default=None, help="指定 ASS 里写的字体族名")
    ap.add_argument("--margin-v", type=int, default=None,
                    help="字幕距画面底部的像素；不填则自适应")
    ap.add_argument("--style", choices=("plate", "outline"), default="plate",
                    help="字幕风格：plate=半透明底板（默认，任何背景都清晰），"
                         "outline=纯描边（画面最干净，但浅色背景上对比度偏弱）")
    ap.add_argument("--flat-margin", action="store_true",
                    help="忽略 placement.json，所有句子用同一个底边距")
    args = ap.parse_args()

    wd = Path(args.workdir).expanduser().resolve()
    seg_path = wd / "segments.json"
    if not seg_path.exists():
        sys.exit(f"找不到 {seg_path}\n请先运行 scripts/01_transcribe.py")

    segs = json.loads(seg_path.read_text(encoding="utf-8"))
    zh = load_translations(wd / "translations.json", len(segs))

    items = [{"start": s["start"], "end": s["end"],
              "en": s["text"].strip(), "zh": str(z).strip()}
             for s, z in zip(segs, zh)]

    font_file, font_family = env.find_cjk_font(args.font, args.font_name)
    if font_family:
        print(f"  字体：{font_family}  ({font_file})")
    else:
        font_family = "sans-serif"
        print("  [警告] 未找到中文字体，字幕可能渲染成方块。"
              "可用 --font 指定字体文件路径。", file=sys.stderr)

    w, h = probe_size(Path(args.video)) if args.video else (1920, 1080)

    per_item = load_placement(wd, len(items), h,
                              disabled=args.flat_margin or args.margin_v is not None)

    write_srt(items, wd / "bilingual.srt", bilingual=True)
    lim_en, lim_zh = write_ass(items, wd / "bilingual.ass", w, h, bilingual=True,
                               limit_en=args.limit_en, limit_zh=args.limit_zh,
                               font_family=font_family, margin_v=args.margin_v,
                               style=args.style, per_item_margin=per_item)
    write_srt(items, wd / "zh.srt", bilingual=False)
    write_ass(items, wd / "zh.ass", w, h, bilingual=False,
              limit_en=args.limit_en, limit_zh=args.limit_zh,
              font_family=font_family, margin_v=args.margin_v,
              style=args.style, per_item_margin=per_item)

    print(f"\n完成，共 {len(items)} 句 | 画面 {w}x{h} | "
          f"折行宽 英 {lim_en} / 中 {lim_zh} | 风格 {args.style}")
    if per_item is None:
        print("  底边距：全文统一"
              + ("（--flat-margin 指定）" if args.flat_margin else
                 "（未找到 placement.json，可先跑 00 步做逐句避让）"))
    else:
        uniq = sorted(set(per_item))
        moved = sum(1 for m in per_item if m > min(uniq))
        print(f"  底边距：逐句避让已启用，{moved}/{len(per_item)} 句上移避让；"
              f"用到的档位 {uniq}")
    for name in ("bilingual.srt", "zh.srt", "bilingual.ass", "zh.ass"):
        p = wd / name
        print(f"  {name:<16} {p.stat().st_size / 1024:6.1f} KB")


if __name__ == "__main__":
    main()
