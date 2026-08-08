# 現バージョンでは PostProcessor は実装しない（要件定義 §14.3）。
# postprocess_chain_json は常に空、Task type postprocess と worker-compute キューは
# 空のまま素通りする。将来のトランスコード拡張のためインターフェースのみ将来定義する。
