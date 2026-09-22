#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
校验译文完整性 —— 翻译环节的守门人

整条流水线的其他地方出错都会立刻炸掉，唯独翻译不会：条数少一句、
顺序错位一格、某几句忘了翻，程序全都照常跑完，只是字幕整体对不上，
而且很难靠肉眼在几分钟的视频里发现。

所以在生成字幕前先过一遍这里。

用法:
    python scripts/check_translations.py <workdir>            # 校验
    python scripts/check_translations.py <workdir> --init     # 生成空白模板
"""
import argparse
import json
import sys
from pathlib import Path


def fmt_ts(t: float) -> str:
    ms = int(round(max(t, 0) * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}"


def main():
    ap = argparse.ArgumentParser(description="校验译文与识别结果是否对齐")
    ap.add_argument("workdir")
    ap.add_argument("--init", action="store_true",
                    help="生成空白译文模板（不覆盖已有文件）")
    args = ap.parse_args()

    wd = Path(args.workdir).expanduser().resolve()
    seg_path = wd / "segments.json"
    tr_path = wd / "translations.json"

    if not seg_path.exists():
        sys.exit(f"找不到 {seg_path}\n请先运行 scripts/01_transcribe.py")

    segs = json.loads(seg_path.read_text(encoding="utf-8"))
    n = len(segs)

    if args.init:
        if tr_path.exists():
            existing = json.loads(tr_path.read_text(encoding="utf-8"))
            existing = existing.get("zh") if isinstance(existing, dict) else existing
            if isinstance(existing, list) and len(existing) == n:
                print(f"已存在且条数正确，未改动：{tr_path}")
                return
            print(f"[提示] 已有文件但条数不符（{len(existing) if isinstance(existing, list) else '?'} vs {n}），"
                  f"将被覆盖")
        tr_path.write_text(json.dumps([""] * n, ensure_ascii=False, indent=2),
                           encoding="utf-8")
        print(f"已生成空白模板（{n} 条）：{tr_path}")
        print("把每句中文译文按顺序填进数组即可。")
        return

    # ---------------------------------------------------------- 校验
    problems = []
    if not tr_path.exists():
        print(f"译文文件不存在：{tr_path}")
        print(f"运行以下命令生成模板："
              f"\n  python scripts/check_translations.py {wd} --init")
        sys.exit(1)

    raw = json.loads(tr_path.read_text(encoding="utf-8"))
    zh = raw.get("zh") if isinstance(raw, dict) else raw
    if not isinstance(zh, list):
        sys.exit(f"译文格式不对（应为数组或 {{\"zh\": [...]}}）：{tr_path}")

    print(f"识别结果 {n} 句 | 译文 {len(zh)} 句")
    if len(zh) != n:
        problems.append(f"条数不一致：差 {abs(n - len(zh))} 句")
        for i in range(min(len(zh), n), max(len(zh), n)):
            side = "缺少译文" if i >= len(zh) else "多出译文"
            en = segs[i]["text"][:60] if i < n else ""
            print(f"  第 {i + 1} 句 {side}"
                  + (f"  [原文] {en}" if en else ""))
    else:
        print("  条数一致 ✓")

    # 逐句体检（只在条数一致时才有意义）
    empty, overlong = [], []
    if len(zh) == n:
        lens = [len(str(z).strip()) for z in zh]
        body = [x for x in lens if x > 0]
        median = sorted(body)[len(body) // 2] if body else 0

        for i, (s, z) in enumerate(zip(segs, zh), 1):
            zs = str(z).strip()
            if not zs:
                empty.append(i)
            elif median and len(zs) > median * 3.5:
                overlong.append((i, len(zs)))
            # 把英文原文原样抄进译文，多半是漏翻
            elif zs.lower() == s["text"].strip().lower():
                problems.append(f"第 {i} 句译文与原文完全相同，疑似漏翻")

    if empty:
        shown = ", ".join(str(i) for i in empty[:12])
        suffix = f" 等共 {len(empty)} 句" if len(empty) > 12 else ""
        problems.append(f"空译文：第 {shown}{suffix}")
    if overlong:
        shown = ", ".join(f"第 {i} 句({ln}字)" for i, ln in overlong[:6])
        problems.append(f"译文异常长，可能要断句：{shown}")

    print()
    if problems:
        print("发现问题：")
        for p in problems:
            print(f"  ✗ {p}")
        print(f"\n修好 {tr_path} 后重新运行本脚本确认。")
        sys.exit(1)

    print("校验通过 ✓ 可以生成字幕了：")
    print(f"  python scripts/02_make_srt.py {wd}")


if __name__ == "__main__":
    main()
