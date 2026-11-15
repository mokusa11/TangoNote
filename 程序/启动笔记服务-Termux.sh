#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="${DICTTANGO_APP_DIR:-$SCRIPT_DIR}"
STATE_DIR="$HOME/.local/state/dicttango"
PID_FILE="$STATE_DIR/service.pid"
LOG_FILE="$STATE_DIR/service.log"
URL="http://127.0.0.1:8766/"

fail() {
    printf '启动失败：%s\n' "$1" >&2
    exit 1
}

is_service_process() {
    local pid="$1"
    local command_line

    [[ "$pid" =~ ^[0-9]+$ ]] || return 1
    kill -0 "$pid" 2>/dev/null || return 1
    command_line="$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null)" || return 1
    [[ "$command_line" == *"$APP_DIR/app.py"* ]]
}

service_is_ready() {
    python -c 'from urllib.request import urlopen; response = urlopen("http://127.0.0.1:8766/", timeout=1); response.close()' >/dev/null 2>&1
}

wait_for_service() {
    local pid="$1"

    for ((attempt = 0; attempt < 60; attempt++)); do
        is_service_process "$pid" || return 1
        service_is_ready && return 0
        sleep 1
    done
    return 2
}

ACTION="${1:-start}"
if [[ $# -gt 1 || ( "$ACTION" != "start" && "$ACTION" != "stop" ) ]]; then
    fail "用法：bash $0 [start|stop]"
fi

if [[ "$ACTION" == "stop" ]]; then
    if [[ -f "$PID_FILE" ]]; then
        read -r existing_pid < "$PID_FILE" || existing_pid=""
        if is_service_process "$existing_pid"; then
            kill "$existing_pid" || fail "无法停止笔记服务进程 $existing_pid"
            for ((attempt = 0; attempt < 10; attempt++)); do
                is_service_process "$existing_pid" || break
                sleep 1
            done
            is_service_process "$existing_pid" &&
                fail "笔记服务进程仍在运行：$existing_pid"
        fi
        rm -f "$PID_FILE"
    fi
    if command -v termux-wake-unlock >/dev/null 2>&1; then
        termux-wake-unlock || fail "无法释放 Termux 唤醒锁"
    fi
    printf '笔记服务已停止，Termux 唤醒锁已释放。\n'
    exit 0
fi

[[ -f "$APP_DIR/app.py" ]] || fail "找不到程序文件：$APP_DIR/app.py"
command -v python >/dev/null 2>&1 || fail "请先在 Termux 执行：pkg install python"
mkdir -p "$STATE_DIR"

if command -v termux-wake-lock >/dev/null 2>&1; then
    termux-wake-lock || fail "无法获取 Termux 唤醒锁"
else
    printf '警告：未找到 termux-wake-lock，锁屏后服务可能被系统挂起。\n' >&2
fi

if [[ -f "$PID_FILE" ]]; then
    read -r existing_pid < "$PID_FILE" || existing_pid=""
    if is_service_process "$existing_pid"; then
        if wait_for_service "$existing_pid"; then
            printf '笔记服务已经运行：%s\n' "$URL"
            printf 'Termux 唤醒锁已启用；停止时运行：bash %s stop\n' "$0"
            exit 0
        fi
        if is_service_process "$existing_pid"; then
            fail "笔记服务进程存在，但网页未就绪。请检查日志：$LOG_FILE"
        fi
    fi
    rm -f "$PID_FILE"
fi

: > "$LOG_FILE"
nohup python "$APP_DIR/app.py" --host 127.0.0.1 --port 8766 >>"$LOG_FILE" 2>&1 </dev/null &
pid=$!
printf '%s\n' "$pid" > "$PID_FILE"

if wait_for_service "$pid"; then
    printf '笔记服务已启动：%s\n' "$URL"
    printf '日志文件：%s\n' "$LOG_FILE"
    printf 'Termux 唤醒锁已启用；停止时运行：bash %s stop\n' "$0"
    exit 0
fi

if ! is_service_process "$pid"; then
    rm -f "$PID_FILE"
    if command -v termux-wake-unlock >/dev/null 2>&1; then
        termux-wake-unlock || printf '警告：无法释放 Termux 唤醒锁。\n' >&2
    fi
    printf '笔记服务进程已退出。最近日志：\n' >&2
    tail -n 40 "$LOG_FILE" >&2 || true
    fail "请根据上方日志排查启动问题"
fi

printf '笔记服务启动超时。最近日志：\n' >&2
tail -n 40 "$LOG_FILE" >&2 || true
fail "服务进程仍在运行，但网页未就绪；日志：$LOG_FILE"
