# Phase 9 独立レビュー（セキュリティ重点）

## 総評

単一ユーザー認証、CSRF、ホワイトリスト認証、Cookie暗号化とtmpfs展開を要件・指示書と照合した。
レビューで検出した中指摘2件は回帰試験付きで対応済みで、重大な残存指摘はない。Cookieを使う
少数実機再検証は5 Targetすべて成功し、403を再現しなかった。

## 指摘

### [中・対応済み] パスワード更新と全セッション失効が別transactionだった

- 該当箇所: `src/sluicery/web/auth.py` の`AuthService.change_password()`
- 内容: パスワードハッシュをcommitした後に全セッション削除をcommitしており、後半のDBエラー時に旧セッションだけが有効なまま残り得た
- 根拠: Phase 9-12指示書 §4.5 / セキュリティ上の原子性
- 提案: 同じSessionの単一transactionでハッシュ更新と`auth_session`削除を実行する
- 対応: 単一commitへ統合し、セッション削除への障害注入時に両方がrollbackされるテストを追加した

### [中・対応済み] 期限切れセッション行が通常運用で蓄積する

- 該当箇所: `src/sluicery/web/auth.py` の`AuthService.create_session()`
- 内容: 期限切れCookieを提示した行だけ削除しており、再訪しないクライアントの匿名・認証済みセッション行が残る
- 根拠: 要件定義 §1.4「システムに残骸を残さない」/ Phase 9-12指示書 §4.2
- 提案: セッション発行時に期限切れ行を削除する
- 対応: 新規発行transactionの前に期限切れ行を回収し、回帰試験を追加した

### [軽微・対応済み] CSRFの将来ルート付け忘れを構造的に検査していなかった

- 該当箇所: `tests/test_web_auth.py`
- 内容: 個別のPOST拒否試験はあったが、全状態変更APIRouteへの共通依存適用を列挙検査していなかった
- 根拠: Phase 9-12指示書 §4.4 / §7
- 提案: 全APIRouteを走査し、GET以外に`require_csrf`が共通依存として存在することを検査する
- 対応: ルート集合を走査するテストを追加した

## 観点別の確認結果

- 要件との齟齬: argon2、単一ユーザー、5回・15分DBロック、固定30日セッション、ログイン時再生成、現在パスワード必須の変更、全セッション失効は整合
- 設計原則違反: Cookie一時ファイルはUUID名・600・tmpfsで、成功・失敗・例外時に`finally`削除する。yt-dlp書き戻しはDBへ反映しない
- 前フェーズの前提の破壊: Task attempts不変の403 blocked、Staging保持、Artifact・メディア非削除を維持
- ドキュメントと実装の乖離: README / deployment / legal / 基本設計D-046〜D-047と実装が一致
- ドキュメント更新漏れ: 管理者初期作成、Secure設定、Cookieリスク・保存・展開・書き戻し方針を更新済み
- 完了条件の未達: 外部検証VMのSSH認証は環境資格情報の拒否で未実施。ローカルCompose実機では完了条件を確認
- 用語のドリフト: Playlist / Item / Target / Task / Run / blocked / unavailable / Cookieは要件定義と整合
- コミット粒度: 認証、CSRF、UI骨格、Cookieを指示書§19どおり分離し、対応テストと文書を同梱
- 未記録の設計判断: セッション鍵導出をD-046、Cookie書き戻し非反映をD-047へ記録済み

## 対応後の再確認

- レビュー修正後の全340テスト成功
- Ruff成功、mypy 82 source files成功
- runtime再ビルド: app healthy、network / compute worker稼働、DB head一致、管理者argon2ハッシュ確認
- Cookie実機スモーク: D-022の単一動画1件が成功、403なし、実行後のtmpfs残存0、ログ内のCookie値・一時パス一致0
- 既存403 Targetの限定再検証: 指示書上限5件を投入し、5件すべてdownloaded、25 Taskすべてsucceeded、attempts最大0
- DB値一致監査: Cookie平文は暗号化列の生値とTask payloadに存在しない
