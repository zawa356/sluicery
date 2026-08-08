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

umask "${UMASK}"

if [ "$(id -u)" = "0" ]; then
    if ! getent group "${PGID}" >/dev/null 2>&1; then
        groupadd -g "${PGID}" sluicery
    fi
    app_group="$(getent group "${PGID}" | cut -d: -f1)"

    if ! id -u "${PUID}" >/dev/null 2>&1; then
        useradd -u "${PUID}" -g "${app_group}" -M -d /app -s /usr/sbin/nologin sluicery
    fi

    mkdir -p "${DATA_DIR}" "${DATA_DIR}/staging" "${DATA_DIR}/logs"
    chown -R "${PUID}:${PGID}" "${DATA_DIR}"

    exec setpriv --reuid "${PUID}" --regid "${PGID}" --init-groups "$0" "$@"
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
