# ストレージ方式の解説

sluicery は最終保存先を `Storage` として抽象化する（要件定義 §6）。UI から複数の Storage を登録でき、
Playlist × Profile の組ごとにどの Storage のどの subpath に書き込むかを指定する。

現バージョンで選択できる kind は `local` と `remote` の2種類。両方とも `StorageAdapter` の
接続テスト、publish、存在確認、再帰一覧、移動、空き容量取得を実装済み。

## local

コンテナから直接見えるパス（`${MEDIA_ROOT}` の bind mount 配下）への書き込み。
特権は不要。単一ホストで完結する運用に向く。コンテナ内の境界は常に `/mnt/media` で、
その外を指す設定は拒否する。Staging と bind mount が異なる mount の場合は copy へ
フォールバックし、保存先と同じディレクトリの一時名から最終化する。

## remote（既定・推奨）

rclone 経由のユーザースペース転送。特権を要求せず、コンテナの隔離を弱めない。
**Phase 5 で実装・実機検証済みの protocol は SMB だけであり、NFS / WebDAV / SFTP / S3 等は
対応済みではない。** 未検証 protocol を指定すると明示的に拒否する。

SMB の host / user / password 等は rclone 子プロセス限定の `RCLONE_CONFIG_*` 環境変数で渡す。
設定ファイルは生成せず、password の obscure 変換も stdin だけを使う。CLI の詳細表示は
資格情報を「設定済み」とだけ表示し、平文を返さない。

いずれの kind でも、Staging（常にローカル）を経由してから最終保存先へ Publish する（要件定義 §6.2）。
ネットワークストレージ上で直接 yt-dlp / ffmpeg を動かすことは行わない。

publish は最終名へ直接書かず、`<dest>.sluicery-tmp-<uuid>` へ転送してサイズ（local は加えて
SHA-256）を検証し、同一ディレクトリ内で rename する。既存の最終名は既定で上書きしない。
失敗した一時ファイルは原因調査とデータ保護のため自動削除せず、呼び出し元へ相対パスを報告する。

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
