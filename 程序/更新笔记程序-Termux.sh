#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="${DICTTANGO_APP_DIR:-$SCRIPT_DIR}"
UPDATER="$APP_DIR/update_termux.py"

if [[ ! -f "$UPDATER" ]]; then
    printf '找不到 Termux 更新程序：%s\n' "$UPDATER" >&2
    exit 1
fi
if ! command -v python >/dev/null 2>&1; then
    printf '请先在 Termux 执行：pkg install python\n' >&2
    exit 1
fi

python "$UPDATER" --app-directory "$APP_DIR"
