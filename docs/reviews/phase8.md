# Phase 8 独立レビュー

## 総評

discover / downloadの二相同期、空振り保護、delistedの非削除、投入上限、Run統計、
所有権付き状態遷移は要件と整合している。レビューまでに検出した指摘はすべて回帰試験付きで
対応済みで、現時点で重大な残存指摘はない。実運用規模の全download完走だけは、HTTP 403多発時の
停止指示を優先したため制限付きとした。

## 指摘

### [中・対応済み] `sync run --all`が後続PlaylistのdiscoverをFIFO待ちにする

- 該当箇所: `src/sluicery/cli_sync.py` の`dispatch()`
- 内容: Playlistごとにdiscover直後のdownloadを投入すると、network worker並列度1のFIFOで先頭の最大250 Taskが後続discoverより先行し、二相同期の意図に反する
- 根拠: 要件定義 §8.1 / Phase 7-8指示書 §14.1、§16.2 #14
- 提案: 全Playlistのdiscoverを先に完了し、成功・非空のPlaylistだけを後半のdownloadフェーズで投入する
- 対応: 二相順序へ変更し、2 Playlistの実行順を`discover, discover, download, download`に固定するテストを追加した

### [中・対応済み] 過去のdownloadedがあると今回のblocked全件がRun succeededになる

- 該当箇所: `src/sluicery/core/sync.py` の`execute_download_run()`
- 内容: 累積のdownloaded件数で成否を判定すると、今回の全候補がStorage事前確認でblockedになってもRunが成功する
- 根拠: 要件定義 N-2 / Phase 7-8指示書 §13.4
- 提案: Runを過去の累積ではなく今回の投入として判定し、`targets_queued=0` かつ`blocked>0`をfailedにする
- 対応: 判定を修正し、既存downloadedがある読取専用Storageの回帰試験を追加した

### [中・対応済み] パイプライン再試行がcoreの状態遷移表を短絡する

- 該当箇所: `src/sluicery/tasks/handlers/download.py`、`verify.py`、`publish.py`、`index.py`、`tasks/pipeline.py`
- 内容: Phase 7ハンドラのCAS直接更新は、failed / blockedから実行段階へ直接進む経路を持ち、Phase 8で導入した不正遷移拒否を一貫して適用できない
- 根拠: Phase 7-8指示書 §15.1〜15.3 / Phase 6の所有権付き更新の前提
- 提案: 再試行でも`failed / blocked → pending → queued → downloading → processing`を1段ずつCASし、逆行を拒否する
- 対応: `core/target_state.py` の`advance_target()`へ集約し、正常な再試行と逆行拒否のテストを追加した

### [軽微・対応済み] discoverエラーと形式非互換の追跡が不十分

- 該当箇所: `src/sluicery/tasks/handlers/discover.py`、`src/sluicery/downloader/errors.py`
- 内容: discoverエラーのRunに`empty_result=true`がなく空振り保護が履歴から読めなかった。また`Requested format is not available`を一時失敗とすると、回復不能なProfile非互換を反復する
- 根拠: Phase 7-8指示書 §11.3、§13.2 / 要件定義 §7.2
- 提案: discoverエラーはRun failedと`empty_result=true`を併記し、実機で確認したformat不足をunavailableへ分類する
- 対応: 両方を分類表とテストに反映した

### [中・検証制限] 実運用規模の全downloadは完走していない

- 該当箇所: Phase 7-8指示書 §16.2 #14 / #16、`docs/基本設計.md` Phase 8実機検証
- 内容: 5 Playlistのdiscoverとdownload投入は完了したが、HTTP 403が多発し、7 / 8件の直近エラーが同分類となった時点で中断した。停止時はdownloaded 224 / failed 68 / pending 320 / unavailable 7で、全件完走と「downloadedが大半」は未確認
- 根拠: Phase 7-8指示書 §16.3（エラー多発時は続行せず記録して停止）
- 提案: 並列度・レートを上げず、配信元の制限が解消した将来の実機検証で少数ずつ再開する。現フェーズではコード修正の根拠としない

## 観点別の確認結果

- 要件との齟齬: 二相同期、8統計、Run確定時点、上限50、対象status、空振り保護は整合。実機完走の制限は上記のみ
- 設計原則違反: delisted / 空結果はArtifact・Target・ファイルを変更しない。失敗・中断分のStagingを保持し、安全停止でも自動削除なし
- 前フェーズの前提の破壊: Task所有権、blocked attempts不変、shutdown pending、index後だけのStaging削除、Artifactのindex限定を維持
- ドキュメントと実装の乖離: README / Makefileの`sync`、基本設計の二相シーケンス、D-041〜D-043は実装と一致
- ドキュメント更新漏れ: 基本設、変更履歴、README、deployment、troubleshooting、本レビューを更新。AISTATEはレビュー対応コミットで全文書き換える
- 完了条件の未達: §16.2 #14 / #16は上記の外部要因で制限付き。コードと単体・制御差分検証に残存未達なし
- 用語のドリフト: Item / Target / Artifact / Run / Task / discover / download / delisted / blocked / unavailableは要件定義と整合
- コミット粒度: 状態遷移、discover、download、Run、CLIを指示書§19の順で分離し、各コミットに対応テストと文書を同梱。実機とレビューは別コミット
- 未記録の設計判断: 空振り、投入上限・Storage事前確認、Run確定をD-041〜D-043に記録。二相順序は基本設計§4.9に記録

## 対応後の再確認

- `make test`: 306件成功
- `make lint`: Ruff成功、mypy 80 source files成功
- runtime再ビルド: app healthy、network / compute worker稼働
- dry-run再実行: 新規0 / delisted 0、Item / Target / Playlist同期時刻不変
- 値一致監査: 実Playlist URL 5件と認証・接続値13件は追跡ファイル、全履歴、compose log、`/data/logs`の一致0件
- gitleaks: 136 commits / 約1.48 MBを走査し、leak 0件
