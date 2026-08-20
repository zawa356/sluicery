# syntax=docker/dockerfile:1
#
# app / worker-network / worker-compute はすべてこのイメージを使い、
# 起動コマンド（scripts/entrypoint.sh の第一引数）で役割を分ける（要件定義 §3.1）。
#
# ベースイメージは digest でピン留めする（要件定義 §4.2）。
# python:3.12-slim（3.12.13-slim-trixie, linux/amd64, 2026-08-08 時点の最新タグ）
FROM python:3.12-slim@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36 AS runtime

# rclone / ffmpeg / Deno はバージョンを明示的に固定し、取得後に checksum で検証する
# （要件定義 §4.2, §3、docs/phase9-12_指示書.md §2.3）。
ARG RCLONE_VERSION=1.75.0
ARG RCLONE_SHA256=aa2804e08f48250e71009c727124b6341cd0288465804a9a09d14663cabafbaa

# yt-dlp の YouTube EJS challenge solver が使う推奨ランタイム。Deno は yt-dlp で
# 既定有効なので PATH 上へ配置すればよく、--js-runtimes の明示指定は不要。
# 上流更新で checksum 不一致になった場合も、GitHub Releases の versioned asset は
# 残るため DENO_URL と DENO_SHA256 を同じリリースの組へ差し替えて復旧する。
ARG DENO_VERSION=v2.9.5
ARG DENO_URL=https://github.com/denoland/deno/releases/download/${DENO_VERSION}/deno-x86_64-unknown-linux-gnu.zip
ARG DENO_SHA256=8b010a3b1a4a0188a67cdb8a7a27348b2a501af78aec7fc74f2ace167368d530

# ffmpeg 静的ビルドの配布元（D-002）。johnvansickle は最新版を固定名で配布し、
# 旧版はバージョン番号付きファイル名で old-releases/ 配下に保持し続ける。
# 上流がローテーションして checksum 不一致でビルドが失敗した場合は、
# https://johnvansickle.com/ffmpeg/old-releases/ から該当 checksum に合う
# versioned な tar.xz の URL を調べ、FFMPEG_URL をそれに差し替えれば
# 同一バージョンを再取得できる（例: ffmpeg-6.1.1-amd64-static.tar.xz）。
ARG FFMPEG_URL=https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz
ARG FFMPEG_SHA256=abda8d77ce8309141f83ab8edf0596834087c52467f6badf376a6a2a4c87cf67

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        unzip \
        xz-utils \
        util-linux \
    && rm -rf /var/lib/apt/lists/*

# ---- rclone ----
RUN curl -fsSL "https://downloads.rclone.org/v${RCLONE_VERSION}/rclone-v${RCLONE_VERSION}-linux-amd64.zip" -o /tmp/rclone.zip \
    && echo "${RCLONE_SHA256}  /tmp/rclone.zip" | sha256sum -c - \
    && unzip -q /tmp/rclone.zip -d /tmp/rclone \
    && install -m 755 "/tmp/rclone/rclone-v${RCLONE_VERSION}-linux-amd64/rclone" /usr/local/bin/rclone \
    && rm -rf /tmp/rclone /tmp/rclone.zip

# ---- Deno（yt-dlp EJS runtime） ----
RUN curl -fsSL "${DENO_URL}" -o /tmp/deno.zip \
    && echo "${DENO_SHA256}  /tmp/deno.zip" | sha256sum -c - \
    && unzip -q /tmp/deno.zip -d /tmp/deno \
    && install -m 755 /tmp/deno/deno /usr/local/bin/deno \
    && rm -rf /tmp/deno /tmp/deno.zip

# ---- ffmpeg / ffprobe（静的ビルド） ----
RUN curl -fsSL "${FFMPEG_URL}" -o /tmp/ffmpeg.tar.xz \
    && echo "${FFMPEG_SHA256}  /tmp/ffmpeg.tar.xz" | sha256sum -c - \
    && mkdir -p /tmp/ffmpeg \
    && tar -xJf /tmp/ffmpeg.tar.xz -C /tmp/ffmpeg --strip-components=1 \
    && install -m 755 /tmp/ffmpeg/ffmpeg /usr/local/bin/ffmpeg \
    && install -m 755 /tmp/ffmpeg/ffprobe /usr/local/bin/ffprobe \
    && rm -rf /tmp/ffmpeg /tmp/ffmpeg.tar.xz

WORKDIR /app

# Python 依存は requirements.lock（pip-compile --generate-hashes 生成）から
# --require-hashes でインストールする。生成済みでない場合は `make lock` を先に実行すること。
COPY requirements.lock ./
RUN pip install --require-hashes -r requirements.lock

COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-deps -e .

COPY alembic.ini ./
COPY migrations ./migrations
COPY config ./config

COPY scripts/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# yt-dlp は永続 volume 上の venv にインストールする（イメージには焼き込まない、要件定義 §5.2）。
VOLUME ["/data"]

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["web"]

# ---- test ステージ ----
# `make test` 専用。本番イメージ（runtime）には dev 依存を焼き込まない（Phase 3 指示書 §0.1）。
FROM runtime AS test

COPY requirements-dev.lock ./
RUN pip install --require-hashes -r requirements-dev.lock

COPY tests ./tests
