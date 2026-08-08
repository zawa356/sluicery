#!/usr/bin/env bash
# app / worker-network / worker-compute の共通エントリポイント。
# 要件定義 N-5（PUID/PGID/UMASK 制御）と §19-2（SECRET_KEY 未設定時の起動拒否）を実装する。
set -euo pipefail

if [ -z "${SECRET_KEY:-}" ]; then
    echo "FATAL: SECRET_KEY が設定されていません。.env に SECRET_KEY を設定してください。" >&2
    exit 1
fi

: "${PUID:=1000}"
: "${PGID:=1000}"
: "${UMASK:=022}"
: "${DATA_DIR:=/data}"
: "${STAGING_DIR:=${DATA_DIR}/staging}"
: "${MEDIA_MOUNT:=/mnt/media}"
: "${RUN_DIR:=/run/sluicery}"

umask "${UMASK}"

# 事前チェック：対象ディレクトリが存在し、PUID/PGID で書き込めることを確認する。
# 失敗時は理由を明示して起動を止める（要件 N-5 の権限問題をここで検知する）。
check_writable() {
    local dir="$1"
    local label="$2"

    if [ ! -d "${dir}" ]; then
        echo "FATAL: ${label}（${dir}）が存在しません。マウント設定を確認してください。" >&2
        exit 1
    fi

    local testfile="${dir}/.sluicery_write_test.$$"
    if ! setpriv --reuid "${PUID}" --regid "${PGID}" --init-groups --inh-caps=-all \
            touch "${testfile}" 2>/dev/null; then
        echo "FATAL: ${label}（${dir}）に PUID=${PUID}/PGID=${PGID} で書き込めません。ホスト側の所有者・権限を確認してください。" >&2
        exit 1
    fi
    rm -f "${testfile}"
}

if [ "$(id -u)" = "0" ]; then
    if ! getent group "${PGID}" >/dev/null 2>&1; then
        groupadd -g "${PGID}" sluicery
    fi
    app_group="$(getent group "${PGID}" | cut -d: -f1)"

    if ! id -u "${PUID}" >/dev/null 2>&1; then
        useradd -u "${PUID}" -g "${app_group}" -M -d /app -s /usr/sbin/nologin sluicery
    fi

    mkdir -p "${DATA_DIR}" "${STAGING_DIR}" "${DATA_DIR}/logs"
    chown -R "${PUID}:${PGID}" "${DATA_DIR}"

    # tmpfs は compose 側で uid/gid 付きマウントを指定しているが、
    # マウントオプションが効かない環境向けに root 権限で明示的にも所有者を合わせる。
    if [ -d "${RUN_DIR}" ]; then
        chown "${PUID}:${PGID}" "${RUN_DIR}"
        chmod 700 "${RUN_DIR}"
    fi

    check_writable "${MEDIA_MOUNT}" "MEDIA_ROOT"
    check_writable "${STAGING_DIR}" "STAGING_DIR"

    exec setpriv --reuid "${PUID}" --regid "${PGID}" --init-groups --inh-caps=-all "$0" "$@"
fi

case "${1:-}" in
    web)
        exec python3 -m sluicery.cli web
        ;;
    worker-network)
        exec python3 -m sluicery.cli worker --class network
        ;;
    worker-compute)
        exec python3 -m sluicery.cli worker --class compute
        ;;
    *)
        exec "$@"
        ;;
esac
