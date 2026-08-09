# ストレージ方式の解説

sluicery は最終保存先を `Storage` として抽象化する（要件定義 §6）。UI から複数の Storage を登録でき、
Playlist × Profile の組ごとにどの Storage のどの subpath に書き込むかを指定する。

現バージョンで選択できる kind は `local` と `remote` の2種類。

## local

コンテナから直接見えるパス（`${MEDIA_ROOT}` の bind mount 配下）への書き込み。
特権は不要。単一ホストで完結する運用に向く。

## remote（既定・推奨）

rclone のリモート定義（SMB / NFS / WebDAV / SFTP / S3 など）経由の転送。特権は不要。
ユーザースペース転送のため、コンテナの隔離を弱めない。**このため既定かつ推奨の方式としている。**

いずれの kind でも、Staging（常にローカル）を経由してから最終保存先へ Publish する（要件定義 §6.2）。
ネットワークストレージ上で直接 yt-dlp / ffmpeg を動かすことは行わない。

## mount（オプトイン、非推奨）

`compose.privileged.yaml` で `privileged` 系の cap を明示的に有効化した場合にのみ選択できる。
`mount -t cifs` / `mount -t nfs` をコンテナ内で実行し、カーネルマウントされたパスへ直接書き込む。

**未実装（実装順序 #19 で追加予定）。** 実装後も既定では無効。

有効化に必要な compose 設定：

```yaml
cap_add:
  - SYS_ADMIN
  - DAC_READ_SEARCH
security_opt:
  - apparmor:unconfined
```

トレードオフ：

- `SYS_ADMIN` は実質 root 相当の権限であり、コンテナの隔離を大幅に弱める
- コンテナが強制終了されると、ホスト側にマウント残骸が残ることがある（ホスト側で `umount` が必要）
- ホストのディストリビューションや AppArmor 設定によっては追加の調整が必要になる場合がある

有効化しない限り、この kind は UI の選択肢に一切表示されない。

**LXC 環境について（未検証）**：Proxmox LXC 等のコンテナ内でさらに Docker を動かす構成では、
`mount` kind が要求する特権が LXC 側の制約（`nesting=1,keyctl=1` の要否、非特権コンテナでの UID
オフセット等）と衝突し、そもそも利用不可になる見込みが高い。現時点でこの見込みを裏付ける実機検証は
行っていない（[docs/deployment.md](deployment.md) §8 参照）。
