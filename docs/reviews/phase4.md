# Phase 4 独立レビュー

## 総評

Phase 4 の主要機能と対応テスト・設計判断は概ね揃っていますが、任意コマンド実行ゲートの迂回とシークレットマスク漏れという重大な問題があります。  
また、AISTATE の完了更新などが残っているため、現時点では Phase 4 完了条件を満たしていません。レビューは静的確認のみで、テストは再実行していません。

## 指摘

### [重大] `--exec` の二重ゲートを迂回できる実行経路が残っている

- 該当箇所: `src/sluicery/cli.py:690`, `src/sluicery/cli.py:751`, `src/sluicery/core/options.py:529`, `src/sluicery/core/options.py:655`
- 内容:
  - `sluicery ytdlp exec` は入力をそのまま `YtdlpRunner` に渡すため、`ALLOW_EXEC` と `Profile.allow_exec` を確認せず `--exec` / `--exec-before-download` を実行できます。
  - discover/download ビルダーは URL を `--` 区切りなしで末尾へ追加します。直接指定 URL は `http(s)` 検証もなく、`-` から始まる値を yt-dlp のオプションとして解釈させ、予約引数ガードを迂回できる構造です。
- 根拠: 要件定義 §9.3、N-10 / Phase 4 指示書 §5.4、完了条件 #10
- 提案:
  - すべての URL の直前へオプション終端 `--` を追加し、URL の形式も検証する。
  - `ytdlp exec` でも予約引数ガードを適用する。デバッグ用の例外を残すなら、要件変更の合意と記録を先に行う。
  - `-` 始まりの URL と raw exec の `--exec` を回帰テストへ加える。

### [重大] 共通マスク層が yt-dlp の認証入力を網羅していない

- 該当箇所: `src/sluicery/downloader/ytdlp.py:34-63`, `src/sluicery/cli_crud.py:125-130`
- 内容:
  - マスク対象は一部の長形式だけです。例えばパスワードの短縮形 `-p`、短縮形に値を連結した指定、認証情報を含む URL などは平文のままプレビューや実行コマンドへ出ます。
  - 自由入力を広く許可しているため、現在の限定的なフラグ表では「クレデンシャルを自動マスクする」という完了条件を保証できません。
- 根拠: 要件定義 §6.5、受け入れ条件 #22 / Phase 4 指示書 §6.2、完了条件 #13
- 提案:
  - yt-dlp の認証・Cookie・Proxy 系オプションについて長短両形式を共通マスク層へ集約する。
  - URL の userinfo も伏せる。
  - preview、Profile/Playlist show、fetch、raw exec の全経路を同じパラメータ化テストで確認する。

### [中] AISTATE が Phase 4 完了時の状態へ更新されていない

- 該当箇所: `AISTATE.md:6-69`
- 内容:
  - 対応コミットが Phase 4 前の `779c9b6` のままです。
  - 進捗 #4 は「作業中」で、「次にやること」に既に完了した実装・実機検証が残っています。
  - Phase 5 の着手点、今回のレビュー、監査・push 承認待ちの状態が反映されていません。
- 根拠: CLAUDE.md §2.1、§3.2、§8.4 / Phase 4 指示書 完了条件 #29
- 提案: 指摘対応後、AISTATE を全文更新し、Phase 4 完了状況、Phase 5 の着手点、最新コミット、未解決事項を反映する。

### [中] nullable な Profile 値を編集で「継承」に戻せない

- 該当箇所: `src/sluicery/cli_crud.py:168-175`, `src/sluicery/cli_crud.py:347-395`, `src/sluicery/cli_crud.py:538-555`, `src/sluicery/cli_crud.py:638-654`
- 内容:
  - 真偽値には `--inherit-*` がありますが、`format_selector`、`container`、`audio_format`、`audio_quality`、`subtitle_langs`、`concurrent_fragments` は、一度設定すると CLI から `None` に戻せません。
  - `Profile show` にも上記の一部が表示されず、「詳細表示」として現在値を確認できません。
  - Playlist の `ytdlp_args` も同様に空へ戻す明示経路がありません。
- 根拠: 要件定義 §9.1、§9.4 / Phase 4 指示書 §3.1、§4.2、§10.1、§10.4
- 提案: nullable 値と自由入力に `--inherit-*` または `--clear-*` を追加し、show ですべての編集対象を表示する。

### [中] 非 current の旧 yt-dlp venv を検証せず active 化できる

- 該当箇所: `src/sluicery/downloader/version.py:120-145`, `src/sluicery/downloader/version.py:288-301`, `src/sluicery/downloader/version.py:335-348`
- 内容:
  - 導入契約の確認は `current` symlink に対してだけ行われます。
  - Phase 3 由来の非 current venv は契約マーカーがなくても、`ytdlp use` がディレクトリと DB レコードだけを確認して active 化します。
  - `install --version` も、current が壊れていて対象が別バージョンなら、対象側の契約を確認せず切り替える分岐があります。
- 根拠: 基本設計 D-023、§4.1 / AISTATE.md 重要な前提
- 提案: バージョンディレクトリ単位の検証関数を設け、`use` と既存版採用前に契約・実行可能性を確認する。旧契約なら再構築してから切り替える。

### [中] README が未実装の管理者初期作成を実装済みとして説明している

- 該当箇所: `README.md:116-117`, `docs/deployment.md:58-59`
- 内容:
  - 初回起動時の管理者作成とランダムパスワード出力が現在動作するように記載されています。
  - 実装には `UserRepository.create_single()` の定義はありますが、起動時に呼び出す処理は存在せず、認証は実装順序 #9 です。
- 根拠: 要件定義 §12、§20 #9 / reviewer の「ドキュメントと実装の乖離」観点
- 提案: Phase 9 で実装予定である旨を明記し、現時点の起動手順として読めない表現へ直す。

### [軽微] `options preview` のバリデーションエラーが traceback になる

- 該当箇所: `src/sluicery/cli_crud.py:734-776`, `src/sluicery/cli_crud.py:797-800`
- 内容:
  - preview のビルダーは `OptionValidationError` や `LayoutValidationError` を送出できますが、dispatch は `CliValidationError` しか捕捉しません。
  - Playlist に予約引数が保存されているケースなどで、意図した日本語エラーと終了コードではなく traceback が表示されます。
- 根拠: CLAUDE.md §6 / Phase 4 指示書 §5.4、§6.2
- 提案: preview 内で両例外を `CliValidationError` に変換するか、dispatch の捕捉対象へ追加する。

## 観点別の確認結果

- 要件との齟齬: `--exec` 二重ゲートの迂回、マスク漏れ、nullable 値を継承へ戻せない問題を確認した。6層合成、予約引数の通常ガード、flat/custom、subpath、欠損値 fallback は仕様に沿っている。
- 設計原則違反: CRUD 削除経路が実ファイル API を呼ばないことを静的確認した。ローカルメディア削除や新たなホスト書き込み先は見つからない。任意実行ゲートの迂回は安全性上の重大問題である。
- 前フェーズの前提の破壊: ファイル名末尾 ID、`SECRET_KEY`、プロセスグループ、リポジトリ層へ状態遷移を置かない前提は維持されている。旧 venv の非 current 版だけは導入契約を保証できない。
- ドキュメントと実装の乖離: README/deployment の管理者初期作成記述と、旧 venv の自動修復範囲に乖離がある。Phase 4 実機検証結果と実装内容の対応は概ね確認できた。
- ドキュメント更新漏れ: 基本設計、変更履歴、README、deployment、legal、CLAUDE.md は更新済み。Phase 4 完了時の AISTATE 更新と、今回のレビューを `docs/reviews/phase4.md` へ保存する作業が残っている。
- 完了条件の未達: #26〜#27 は本レビューの保存・対応後に成立する。#28 の監査・push 承認依頼、#29 の AISTATE 更新は未達。#4 の着手前確認結果は成果物から実施報告を確認できない。テスト・lint・実機検証は成功記録を確認したが、禁止事項に従い再実行していない。
- 用語のドリフト: Playlist / Profile / Item / Target / Artifact、Discover / Download、Staging などの主要用語に新たなドリフトは見つからない。
- コミット粒度: 13コミットに分割され、Conventional Commits と日本語要約を満たす。大きいコミットはあるが、概ね意味単位で分かれており重大な粒度違反はない。
- 未記録の設計判断: 三状態化、命名境界、欠損値、expert mode、Playlist 削除、試験素材、yt-dlp extras は D-017〜D-023 に記録済み。上記のゲート迂回やマスク漏れは設計判断ではなく修正対象と判断する。
