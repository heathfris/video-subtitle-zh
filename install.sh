#!/usr/bin/env bash
#
# video-subtitle-zh 一键安装（macOS / Linux）
#
#   bash install.sh                        装依赖 + 环境自检
#   bash install.sh --with-model           顺带下好默认模型 small（约 480MB）
#   bash install.sh --with-model --model distil-large-v3
#
# 装完不会污染系统 Python：所有东西都在仓库目录下的 .venv 里，
# 直接删掉这个目录就等于卸载干净。
#
set -euo pipefail

# 让 Python 以 UTF-8 输出。Windows 上的 Python 默认走系统 ANSI 代码页（GBK），
# 在 Git Bash 里会把中文日志变成乱码。
export PYTHONIOENCODING=utf-8

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$ROOT/.venv"

WITH_MODEL=0
MODEL="small"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --with-model) WITH_MODEL=1 ;;
    --model)      MODEL="${2:?--model 后面要跟模型名}"; shift ;;
    -h|--help)    sed -n '3,11p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)            echo "未知参数：$1（用 --help 查看用法）" >&2; exit 1 ;;
  esac
  shift
done

echo
echo "=========================================================="
echo " video-subtitle-zh 安装"
echo "=========================================================="
echo "  仓库目录：$ROOT"
echo

# ---------------------------------------------------------- 1. Python

find_python() {
  local cand v
  for cand in python3 python python3.13 python3.12 python3.11 python3.10 python3.9; do
    command -v "$cand" >/dev/null 2>&1 || continue
    v="$("$cand" -c 'import sys; print(sys.version_info[0]*100+sys.version_info[1])' 2>/dev/null || echo 0)"
    if [[ "$v" -ge 309 ]]; then echo "$cand"; return 0; fi
  done
  return 1
}

echo "[1/4] 查找 Python"
if ! PYBIN="$(find_python)"; then
  echo "  ✗ 需要 Python 3.9 或更高版本，但没有找到。" >&2
  case "$(uname -s)" in
    Darwin) echo "    brew install python@3.12" >&2 ;;
    Linux)  echo "    sudo apt install python3 python3-venv   # 或对应发行版的命令" >&2 ;;
  esac
  exit 1
fi
# shellcheck disable=SC2005
echo "  ✓ $PYBIN $("$PYBIN" -c 'import sys; print(".".join(map(str, sys.version_info[:3])))')"

# ---------------------------------------------------------- 2. 虚拟环境

echo
echo "[2/4] 创建虚拟环境"
if [[ -x "$VENV/bin/python" || -x "$VENV/Scripts/python.exe" ]]; then
  echo "  已存在，复用：$VENV"
else
  "$PYBIN" -m venv "$VENV" || {
    echo "  ✗ 创建虚拟环境失败。" >&2
    echo "    Debian/Ubuntu 上通常是因为缺 python3-venv：sudo apt install python3-venv" >&2
    exit 1
  }
  echo "  ✓ 已创建：$VENV"
fi

# venv 的解释器位置因平台而异：POSIX 在 bin/，Windows 在 Scripts/。
# 本脚本在 Git Bash / MSYS2 下也可能被用来操作 Windows 版 Python，所以两种都认。
if [[ -x "$VENV/bin/python" ]]; then
  PY="$VENV/bin/python"
elif [[ -x "$VENV/Scripts/python.exe" ]]; then
  PY="$VENV/Scripts/python.exe"
else
  echo "  ✗ 虚拟环境里找不到 python 解释器" >&2
  exit 1
fi
echo "  ✓ 解释器 $PY"

# ---------------------------------------------------------- 3. 依赖

echo
echo "[3/4] 安装 Python 依赖"
"$PY" -m pip install -q --upgrade pip >/dev/null 2>&1 || true

# 各源依次尝试：先用 pip 自身配置，再国内镜像，最后官方源。
# 镜像的可用性随网络环境变化很大，写死任何一个都会让一部分人失败。
INDEXES=(
  ""
  "https://pypi.tuna.tsinghua.edu.cn/simple"
  "https://mirrors.aliyun.com/pypi/simple"
  "https://pypi.org/simple"
)

installed=0
for idx in "${INDEXES[@]}"; do
  label="${idx:-pip 默认源}"
  printf '  尝试 %s ... ' "$label"
  if [[ -z "$idx" ]]; then
    if "$PY" -m pip install -q -r "$ROOT/requirements.txt" 2>/dev/null; then
      echo "成功"; installed=1; break
    fi
  else
    if "$PY" -m pip install -q -i "$idx" -r "$ROOT/requirements.txt" 2>/dev/null; then
      echo "成功"; installed=1; break
    fi
  fi
  echo "失败"
done

if [[ "$installed" -ne 1 ]]; then
  echo "  ✗ 所有源都装不上。请检查网络或代理设置，然后手动重试：" >&2
  echo "    $PY -m pip install -r $ROOT/requirements.txt" >&2
  exit 1
fi

# ---------------------------------------------------------- ffmpeg

echo
echo "  ffmpeg 检查"
if command -v ffmpeg >/dev/null 2>&1 && command -v ffprobe >/dev/null 2>&1; then
  echo "    ✓ $(ffmpeg -version 2>/dev/null | head -1 | awk '{print $3}')"
else
  echo "    ✗ 未找到 ffmpeg，抽音轨和压制都会失败。" >&2
  case "$(uname -s)" in
    Darwin) echo "      brew install ffmpeg" >&2 ;;
    Linux)  echo "      Debian/Ubuntu: sudo apt install ffmpeg" >&2
            echo "      Fedora/RHEL:   sudo dnf install ffmpeg" >&2
            echo "      Arch:          sudo pacman -S ffmpeg" >&2 ;;
  esac
  echo "    装完后重跑本脚本。" >&2
fi

# ---------------------------------------------------------- 4. 模型

echo
echo "[4/4] 识别模型"
if [[ "$WITH_MODEL" -eq 1 ]]; then
  "$PY" -c "
import sys
sys.path.insert(0, r'$ROOT/scripts')
import env
env.ensure_model('$MODEL')
"
else
  echo "  跳过（首次使用时会自动下载）"
  echo "  想现在就下好可以重跑：bash install.sh --with-model"
fi

# ---------------------------------------------------------- 自检

echo
"$PY" "$ROOT/scripts/env.py"

echo
echo "安装完成。把视频丢给 AI 助手，或者直接跑："
echo "  $PY $ROOT/scripts/run_all.py <你的视频>"
echo
