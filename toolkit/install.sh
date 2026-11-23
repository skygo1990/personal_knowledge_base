#!/bin/sh
set -eu
umask 077

VERSION=0.2.0
UV_VERSION=0.7.13
REPOSITORY=https://github.com/skygo1990/personal_knowledge_base
ROOT=${PKB_HOME:-"$HOME/.local/share/ai-context-hub"}
SYSTEM=$(uname -s)
case "$SYSTEM" in Darwin|Linux) ;; *) echo '仅支持 macOS/Linux；原生 Windows 尚未适配。' >&2; exit 2 ;; esac
case "$ROOT" in /*) ;; *) echo 'PKB_HOME 必须是绝对路径。' >&2; exit 2 ;; esac
for dependency in git curl; do
    command -v "$dependency" >/dev/null 2>&1 || { echo "请先安装 $dependency；不会修改系统软件。" >&2; exit 2; }
done
git --version >/dev/null
ACCEPTED=false
for argument in "$@"; do
    if [ "$argument" = --accept-install ]; then ACCEPTED=true; fi
done
printf '安装个人知识库 %s 至 %s\n' "$VERSION" "$ROOT"
printf '%s\n' '将安装隔离的 Python 环境并配置 Codex，不修改 shell PATH，不使用 sudo。' \
    '同账户不防绕过。默认不启用对话批准；可用 --conversation-approval 显式启用，但每份内容仍须本人确认。' \
    '默认知识仓库为公开仓库；本次安装只读取，不提交或推送任何知识。'
if [ "$ACCEPTED" != true ]; then
    printf '确认以上安装范围？输入 yes：' >/dev/tty 2>/dev/null || { echo '非交互运行须传 --accept-install。' >&2; exit 2; }
    read -r answer </dev/tty
    [ "$answer" = yes ] || exit 2
fi
parent=$ROOT
while [ "$parent" != / ]; do
    [ ! -L "$parent" ] || { echo '安装路径不能包含符号链接。' >&2; exit 2; }
    if [ -d "$parent" ]; then
        if [ "$SYSTEM" = Darwin ]; then info=$(stat -f '%u %Lp' "$parent"); else info=$(stat -c '%u %a' "$parent"); fi
        owner=${info%% *}; mode=${info#* }
        [ "$owner" = 0 ] || [ "$owner" = "$(id -u)" ] || { echo '安装路径存在不可信属主。' >&2; exit 2; }
        if [ "$((0$mode & 0022))" != 0 ] && [ "$((0$mode & 01000))" = 0 ]; then
            echo '安装路径存在可被其他用户修改的父目录。' >&2; exit 2
        fi
    fi
    parent=$(dirname "$parent")
done
mkdir -p "$ROOT"
if [ "$SYSTEM" = Darwin ]; then info=$(stat -f '%u %Lp' "$ROOT"); else info=$(stat -c '%u %a' "$ROOT"); fi
[ "$info" = "$(id -u) 700" ] || { echo '安装目录须由当前用户独占（0700）；不会覆盖原权限。' >&2; exit 2; }
for directory in "$ROOT/downloads" "$ROOT/apps"; do
    [ ! -L "$directory" ] || { echo '安装子目录不能是符号链接。' >&2; exit 2; }
    mkdir -p "$directory"
done
BUNDLE=${PKB_BUNDLE_DIR:-"$ROOT/downloads/v$VERSION"}
WHEEL=ai_context_hub-$VERSION-py3-none-any.whl
if [ -z "${PKB_BUNDLE_DIR:-}" ]; then
    [ ! -L "$BUNDLE" ] || exit 2
    mkdir -p "$BUNDLE"
    for artifact in SHA256SUMS "$WHEEL" requirements.lock; do
        [ ! -L "$BUNDLE/$artifact" ] || exit 2
        curl --proto '=https' --tlsv1.2 -fsSL --retry 2 --connect-timeout 15 --max-time 180 \
            "$REPOSITORY/releases/download/v$VERSION/$artifact" -o "$BUNDLE/$artifact"
    done
fi
for artifact in "$WHEEL" requirements.lock; do
    expected=$(awk -v name="$artifact" '$2 == name {print $1}' "$BUNDLE/SHA256SUMS")
    printf '%s\n' "$expected" | grep -Eq '^[0-9a-f]{64}$' || { echo '校验清单无效。' >&2; exit 2; }
    if command -v sha256sum >/dev/null 2>&1; then actual=$(sha256sum "$BUNDLE/$artifact"); else actual=$(shasum -a 256 "$BUNDLE/$artifact"); fi
    [ "${actual%% *}" = "$expected" ] || { echo "下载校验失败：$artifact" >&2; exit 2; }
done
if command -v uv >/dev/null 2>&1; then
    UV=$(command -v uv)
else
    [ ! -L "$ROOT/uv" ] || exit 2
    mkdir -p "$ROOT/uv"
    [ ! -L "$ROOT/downloads/uv-installer.sh" ] || exit 2
    curl --proto '=https' --tlsv1.2 -fsSL --retry 2 --max-time 120 "https://astral.sh/uv/$UV_VERSION/install.sh" -o "$ROOT/downloads/uv-installer.sh"
    UV_UNMANAGED_INSTALL="$ROOT/uv" sh "$ROOT/downloads/uv-installer.sh"
    UV="$ROOT/uv/uv"
fi
ENVIRONMENT="$ROOT/apps/v$VERSION"
[ ! -L "$ENVIRONMENT" ] || exit 2
if [ ! -x "$ENVIRONMENT/bin/python" ]; then "$UV" venv --python 3.12 "$ENVIRONMENT"; fi
"$UV" pip install --python "$ENVIRONMENT/bin/python" --require-hashes -r "$BUNDLE/requirements.lock"
"$UV" pip install --python "$ENVIRONMENT/bin/python" --no-deps "$BUNDLE/$WHEEL"
"$ENVIRONMENT/bin/python" -m ai_context_hub init --home "$ROOT" --remote "$REPOSITORY.git" --accept-install "$@"
"$ENVIRONMENT/bin/python" - "$ROOT" <<'PY'
from pathlib import Path
import shlex
import sys
from mcp_server.storage import atomic_text
root = Path(sys.argv[1])
launcher = '#!/bin/sh\nexport PKB_HOME=' + shlex.quote(str(root)) + '\nexec ' + shlex.quote(sys.executable) + ' -m ai_context_hub "$@"\n'
atomic_text(root / 'pkb', launcher, mode=0o700)
PY
if "$ROOT/pkb" doctor --refresh; then
    printf '\n安装与读取自检成功。重开 Codex 会话后，要求读取个人知识库。\n'
else
    printf '\n程序已安装，但读取自检未通过；请检查 Git 连接或登录，然后运行：\n%s\n' "\"$ROOT/pkb\" doctor --refresh" >&2
    exit 2
fi
