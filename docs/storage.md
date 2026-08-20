# ストレージ方式の解説

sluicery は最終保存先を `Storage` として抽象化する（要件定義 §6）。UI から複数の Storage を登録でき、
Playlist × Profile の組ごとにどの Storage のどの subpath に書き込むかを指定する。

現バージョンの kind は `local`、`remote`、オプトインの`mount`。3種とも`StorageAdapter`の
接続テスト、publish、存在確認、再帰一覧、移動、空き容量取得を実装済み。ただし`mount`は
通常構成では利用不可で、実CIFS / NFS環境では未検証。

## local

コンテナから直接見えるパス（`${MEDIA_ROOT}` の bind mount 配下）への書き込み。
特権は不要。単一ホストで完結する運用に向く。コンテナ内の境界は常に `/mnt/media` で、
その外を指す設定は拒否する。Staging 元は最終化まで保持し、同一 filesystem では hardlink、
mount 境界や hardlink 非対応時は copy で一時名を作る。

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
SHA-256）を検証し、同一ディレクトリ内で rename する。local は no-replace rename、remote は
`--ignore-existing` と一時名消滅確認を使い、競合時も既存の最終名を上書きしない。
失敗した一時ファイルは原因調査とデータ保護のため自動削除せず、呼び出し元へ相対パスを報告する。

Playlistの明示フォルダ移動も同じ上書き拒否境界を使う。preview時にArtifactの実体識別情報と移動先不在を
確認し、期限付き署名tokenへDB snapshotを束縛する。実行時はlocalの原子的no-replace rename、remoteの
`--ignore-existing`と移動元消滅・移動先存在確認を使い、成功したArtifact pathを1件ずつDBへ反映する。
Playlist名の通常編集、整合性チェック、差分レポートからこの移動経路を呼ぶことはない。

## mount（オプトイン、非推奨）

`compose.privileged.yaml` で `privileged` 系の cap を明示的に有効化した場合にのみ選択できる。
`mount -t cifs` / `mount -t nfs` をコンテナ内で実行し、カーネルマウントされたパスへ直接書き込む。

Phase 19で実装済み。ただし、**外部検証VMへ接続できないため、実CIFS / NFSサーバーを使った
mount・読書き・再接続は未検証**。ローカルDockerでは、通常Composeで利用不可になること、
補助コマンドの存在、非root UIDへの必要capability継承、設定検証、認証失敗を含むエラー処理までを
確認した。検証していない実mountを検証済みとは扱わない。

有効化は通常の`compose.yaml`へ権限を追加せず、次のように別overlayを明示する。

```bash
MOUNT_ROOT=/host/path/to/shared-mount-root \
  docker compose -f compose.yaml -f compose.privileged.yaml up -d
```

`MOUNT_ROOT`は事前に作成し、`PUID` / `PGID`から書込み可能にする。さらにbind propagationのため、
ホスト側の親mountが`shared`でなければDockerがコンテナ作成を拒否する。この条件を満たせない環境では
`mount`を使わず、既定・推奨の`remote`を使う。

有効化に必要な compose 設定：

```yaml
cap_add:
  - SYS_ADMIN
  - DAC_READ_SEARCH
security_opt:
  - apparmor:unconfined
```

overlayが権限を付けるのは、管理・接続試験・retentionを担う`app`と、publishを担う
`worker-network`だけ。`worker-compute`には付与しない。アプリは固定sentinelと実効capabilityの
両方を確認し、片方でも無ければWeb UIの選択肢を隠し、CLI / factoryも明示エラーにする。

CIFS資格情報は暗号化DBから実行時だけ`/run/sluicery`のmode 600一時ファイルへ展開し、
コマンドラインへ値を載せず、mount試行直後に削除する。NFSは資格情報を保存しない。既存mountpointが
別source / filesystemを指す場合、symlinkの場合、未mountで非空の場合は安全側に停止する。
adapterは自動`umount`を行わず、同じ接続先の既存mountを再利用する。

トレードオフ：

- `SYS_ADMIN` は実質 root 相当の権限であり、コンテナの隔離を大幅に弱める
- コンテナが強制終了されると、ホスト側にマウント残骸が残ることがある（ホスト側で `umount` が必要）
- bind propagationにより、コンテナ内mountがホスト側にも見える。終了前後のmount状態を運用者が管理する
- ホストのディストリビューションや AppArmor 設定によっては追加の調整が必要になる場合がある

有効化しない限り、この kind は UI の選択肢に一切表示されない。

**LXC 環境について（未検証）**：Proxmox LXC 等のコンテナ内でさらに Docker を動かす構成では、
`mount` kind が要求する特権が LXC 側の制約（`nesting=1,keyctl=1` の要否、非特権コンテナでの UID
オフセット等）と衝突し、そもそも利用不可になる見込みが高い。現時点でこの見込みを裏付ける実機検証は
行っていない（[docs/deployment.md](deployment.md) §8 参照）。
