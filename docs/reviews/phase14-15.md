# Phase 14–15 独立レビュー（format検査・yt-dlp更新）

## 総評

Phase 14のformat検査とPhase 15のyt-dlp自動更新を、指示書 §9〜§11の観点で静的レビューした。
重大指摘はなく、中程度4件・軽微2件はすべて回帰試験付きで対応済みである。残存指摘はない。

## 指摘

### [中・対応済み] 外部yt-dlp設定で専用Staging境界を迂回できた

- 内容: format検査とsmokeが利用者のyt-dlp設定を読み、追加出力や外部commandを実行し得た
- 対応: 両経路へ固定の`--ignore-config`を追加し、外部設定を合成しない
- 確認: format検査とsmokeの全呼出しに固定引数が含まれる回帰試験を追加した

### [中・対応済み] 元ファイル既存tagだけでmetadata埋込み成功になった

- 内容: ffprobeのtagが1件でもあれば成功し、`--embed-metadata`が動かなくても通り得た。既定素材はthumbnailを提供せず、埋込み処理も実証できなかった
- 対応: 固定markerを`--parse-metadata`で注入して値を照合する。thumbnail非提供時は専用Stagingに短いM4Aと画像を生成し、yt-dlp自身の`EmbedThumbnailPP`でcoverを埋めてvenvのmutagenで確認する
- 確認: 既存tagだけを拒否する試験、thumbnail非提供時の合成検証分岐、実環境で`metadata_embedded=true / thumbnail_embedded=true`を確認した

### [中・対応済み] 最新版と現在版が同じ場合に直前版を検査しなかった

- 内容: 同版の再検証失敗では、deactivate済みの直前版があっても検査・rollbackしなかった
- 対応: 現在symlinkとは別に、除外version以外で`deactivated_at`が最新の版をrollback候補として解決する
- 確認: 同版失敗から直前版を検査し、成功時だけ切り替える障害注入試験を追加した。候補なしと両版失敗のUI文言も分けた

### [中・対応済み] 正規化・redirect後のURLを保存ログへ残し得た

- 内容: 入力URLの完全一致置換だけでは、yt-dlpが変形して出力したHTTP(S) URLを伏せられなかった
- 対応: Runnerへ保存前に全HTTP(S) URLを伏せる専用modeを追加し、format検査とsmokeだけで有効にした
- 確認: stdout JSONとstderr保存ログにredirect先URLを出す疑似CLI試験で、双方が伏せられることを固定した。実スモークの最新2ログにもHTTP(S) URLは無かった

### [軽微・対応済み] formatサイズの部分合計を総量として表示した

- 内容: 複数選択formatの一部だけサイズ不明でも、既知分だけを総推定サイズとして表示した
- 対応: 全要素のサイズが揃う場合だけ合計し、aggregate値も無ければ不明表示にする
- 確認: video既知・audio不明の組合せを`None`にする試験を追加した

### [軽微・対応済み] 強化smokeの負方向試験が不足した

- 内容: URLログ、元tag、同版失敗、thumbnail非提供時などの回帰試験が無かった
- 対応: 各問題の再現試験に加え、固定引数、marker、全URLマスク、合成thumbnail経路を追加した

## 観点別の確認結果

- format検査: ProfileのL2〜L4合成とdiscover timeoutを再利用し、全Profile共通の既定10秒制限を持つ。出力先引数とpublish経路はない
- smoke: version、Deno、simulate metadata、実ダウンロード、challenge警告、default extras、metadata、thumbnailを検査する
- 副作用境界: `--ignore-config`、固定の専用Staging、path containment確認、`finally` cleanupでStorage / Artifactへpublishしない
- rollback: candidate失敗後に直前版を未切替で再検証し、成功時だけ戻す。両版失敗ではcandidateを維持する
- scheduler: app専用の週次jobで、永続引数は空。cron空文字で無効化できる
- 秘密情報: smoke結果とHook payloadは安全なversion、reason、真偽値だけで、URLやCookieを含まない

## 対応後の再確認

- 指摘対応のfocused test 53件成功、Ruff成功、mypy 85 source files成功
- 最終の全437テスト成功、Ruff成功、mypy 85 source files成功
- 実環境のyt-dlp `2026.07.04`で強化smoke成功。Deno検出、marker metadata、thumbnail cover、cleanupを確認した
- Artifact 599件のまま、smoke専用ディレクトリ0件、最新2保存ログのHTTP(S) URL 0件を確認した

独立レビュー担当は静的確認のみを行い、テストと実機確認は指摘対応後に実装担当が実施した。
