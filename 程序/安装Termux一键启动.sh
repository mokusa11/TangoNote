#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="${DICTTANGO_APP_DIR:-$SCRIPT_DIR}"
LAUNCHER="$APP_DIR/启动笔记服务-Termux.sh"
UPDATER="$APP_DIR/更新笔记程序-Termux.sh"
STATIC_DIR="$HOME/.shortcuts"
TASKS_DIR="$STATIC_DIR/tasks"
STATIC_SHORTCUT="$STATIC_DIR/启动笔记服务"
TASK_SHORTCUT="$TASKS_DIR/启动笔记服务"
UPDATE_TASK_SHORTCUT="$TASKS_DIR/更新笔记程序"
TEMP_STATIC="$STATIC_SHORTCUT.tmp.$$"
TEMP_TASK="$TASK_SHORTCUT.tmp.$$"
TEMP_UPDATE_TASK="$UPDATE_TASK_SHORTCUT.tmp.$$"

if [[ ! -f "$APP_DIR/app.py" || ! -f "$LAUNCHER" || ! -f "$UPDATER" ]]; then
    printf '找不到笔记程序，请确认项目目录完整：%s\n' "$APP_DIR" >&2
    exit 1
fi

if ! command -v python >/dev/null 2>&1; then
    printf '请先在 Termux 执行：pkg install python\n' >&2
    exit 1
fi

mkdir -p "$STATIC_DIR" "$TASKS_DIR"
trap 'rm -f "$TEMP_STATIC" "$TEMP_TASK" "$TEMP_UPDATE_TASK"' EXIT
printf -v quoted_app_dir '%q' "$APP_DIR"
write_shortcut() {
    printf '#!/data/data/com.termux/files/usr/bin/bash\n'
    printf 'export DICTTANGO_APP_DIR=%s\n' "$quoted_app_dir"
    printf 'exec bash "$DICTTANGO_APP_DIR/启动笔记服务-Termux.sh" "$@"\n'
}
write_update_shortcut() {
    printf '#!/data/data/com.termux/files/usr/bin/bash\n'
    printf 'export DICTTANGO_APP_DIR=%s\n' "$quoted_app_dir"
    printf 'exec bash "$DICTTANGO_APP_DIR/更新笔记程序-Termux.sh" "$@"\n'
}

write_shortcut > "$TEMP_STATIC"
write_shortcut > "$TEMP_TASK"
write_update_shortcut > "$TEMP_UPDATE_TASK"

for shortcut in "$STATIC_SHORTCUT" "$TASK_SHORTCUT" "$UPDATE_TASK_SHORTCUT"; do
    temp_shortcut="$shortcut.tmp.$$"
    if [[ -e "$shortcut" ]] && ! cmp -s "$temp_shortcut" "$shortcut"; then
        printf '快捷方式已存在且内容不同，请先检查或移走：%s\n' "$shortcut" >&2
        exit 1
    fi
done

if [[ ! -e "$STATIC_SHORTCUT" ]]; then
    mv "$TEMP_STATIC" "$STATIC_SHORTCUT"
fi
if [[ ! -e "$TASK_SHORTCUT" ]]; then
    mv "$TEMP_TASK" "$TASK_SHORTCUT"
fi
if [[ ! -e "$UPDATE_TASK_SHORTCUT" ]]; then
    mv "$TEMP_UPDATE_TASK" "$UPDATE_TASK_SHORTCUT"
fi
chmod 700 "$STATIC_SHORTCUT" "$TASK_SHORTCUT" "$UPDATE_TASK_SHORTCUT"
printf '安装完成。请在 Termux:Widget 中刷新小组件；桌面启动入口、后台任务和一键更新任务均已配置。\n'
