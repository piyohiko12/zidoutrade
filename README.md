# zidoutrade — RSI_AUTOPILOT_V1

ユーザーが候補一覧から翌営業日の銘柄を1つ選び、Wilder RSIを中心に売買判断する、
**監視付き・SIMULATE専用**の自動売買研究システムです。

> 利益や勝率を保証するシステムではありません。クローン直後は`SHADOW`かつ
> `DISARMED`で、注文を送れる設定・口座情報・有効化マーカーを含みません。
> `REAL`、`unlock_trade`、空売り、時間外取引への対応予定はありません。

> **現在の公開版は分析・候補選択・RSI判断・安全性検証までです。** ランナーと
> ブローカーの双方に解除手段のないhard stopがあり、SIMULATE注文も送信できません。
> 再起動可能なposition basis、日次/週次risk anchor、注文所有権台帳、取消/late-fill処理、
> fresh-processコード証明が完成・再監査されるまで、この停止は解除しません。

## 実装済みの安全な基盤

- 最大20銘柄のユーザーwatchlist
- 適格性で絞った候補一覧（上昇予測ランキングはしない）
- ユーザーが翌営業日のactive symbolを1つ選択・固定
- 確定15分足によるWilder RSI(14)とATR(14)
- RSI 30以下 → 35回復 → 前足高値突破を必須とするエントリー
- RSI 60、1.5 ATR stop、8本/2時間、引け15分前による退出
- V2既定値は0.25%予定リスク、日次0.75%・週次2%の新規買い停止線、1日1往復
- durable intent、注文・position照合、at-most-once設計の検証コード（送信は停止中）
- ローカルUI、判断理由、リスク状態、株式日記への導線
- 注文を出さないshadow比較（選択銘柄・全候補等ウェイト・固定baseline）
- 注文・実市場データから分離したQ014 V2研究ledgerのschema、immutable record、replay/verifier骨格

システムが行うのは候補の**適格性判定**です。「上がりそうな順」には並べません。
ユーザーの優先順、その後ticker順で表示し、選択・非選択候補と時刻を保存します。

## 安全上の重要な制限

- 正常系の処理は自動化できますが、PC、回線、OpenD停止中の損切りを保証できません。
- ブローカー常駐stop等をpaperで実証するまで`SUPERVISED_ONLY`です。
- `PAUSE_ENTRIES`はBUYだけを止め、照合と安全な既知positionの退出は継続します。
- ACK喪失やorder/position不一致は再注文せず`RECOVERY_REQUIRED`になります。
- 合成テストは収益性やOpenD互換性を証明しません。

詳しい仕様は [RSI_AUTOPILOT_V1仕様](docs/RSI_AUTOPILOT_V1_SPEC_JA.md) を参照してください。

## 開発環境

Python 3.9以上、コア依存関係なしです。テストはネットワークもOpenDも使用しません。

```bash
PYTHONPATH=src python3 -B -W error -m unittest discover -s tests -v
```

ローカル開発用にeditable installする場合だけ、既存のPython環境で次を実行します。
このプロジェクト自身がinstallを自動実行することはありません。

```bash
python3 -m pip install -e .
```

CLIはinstallせずにも使えます。

```bash
PYTHONPATH=src python3 -m zidoutrade --help
PYTHONPATH=src python3 -m zidoutrade dashboard --host 127.0.0.1 --port 8765
```

### moomoo履歴による探索的バックテスト

取得済みのmoomoo OpenD quote履歴だけを読み、口座・残高・position・orderへ接続しない
`HISTORICAL_CANDLE_PROXY_V1`を実装しています。入力はリポジトリ外のowner-onlyディレクトリに置き、
各ファイルの役割、QFQ/RAW、RTH、期間、行数、bytes、SHA-256をcanonical manifestへ固定します。

```bash
PYTHONPATH=src python3 -B -W error -m zidoutrade backtest \
  --manifest /absolute/private/backtest_input_manifest.json \
  --expected-manifest-sha256 <64桁のSHA-256> \
  --report /absolute/private/backtest_report.json \
  --initial-equity-cents 10000000 \
  --maximum-investment-cents 1000000
```

RSI/ATRとsignalは確定QFQ 15分足、価格・損益は同時刻のRAW足を使い、signal確定後の次足open
より前にはentryしません。現在のpaper fee、既定0.25%予定リスク、日次0.75%・週次2%停止線、
投資上限を反映します。損切りと他のexitが同じ足にある場合は損切りを優先します。

ただし履歴K線には当時のbid/ask、実spread、注文拒否、partial/no-fill、late fillがありません。
そのため固定spreadと不利な価格cushionを使う**ローソク足代理評価**で、結果は常に
`EXPLORATORY_ONLY`です。ユーザー選択の有効性、実デモ約定、sealed OOS、将来利益を証明せず、
注文不能hard stopも解除しません。また取得日時点のQFQは後日の企業行動を反映して再計算され得るため、
当時利用可能だったpoint-in-time調整系列であることも証明しません。生の市場データとperformance reportは
GitHubへ保存しません。

### Q014 V2研究ledgerの構造リプレイ

将来のpaired shadowを後から都合よく書き換えないため、EXPECTED session、観測開始時刻とdeadline、
sessionごとのreservation、sequenceごとのO_EXCL recordとretained anchor、deterministic replay、
writer record集合とは別のclean/failure reportとsealの骨格を実装しています。明示的な`SESSION_MISSING`は
欠損を隠さない有効terminal、
deadline後もterminalが無い場合やchain不整合はdataset failureです。

次の固定スクリプトは合成データだけで正常・NO_SELECTION・明示missing・故障注入を再生します。

```bash
PYTHONPATH=src /usr/bin/python3 -B -W error scripts/run_q014_v2_structural_replay.py
```

これは`STRUCTURAL_REPLAY_ONLY`であり、戦略の収益バックテスト、OOS、性能改善の証拠ではありません。
ledgerはbroker、runner、SDK、market-data adapterをimportせず、口座・position・orderを取得しません。
既存ledgerを`open()`したprocessは安全のためquery-onlyとなるため、再起動後も収集を継続するoperational
collectorやprospective adapterはまだ未実装です。保証範囲は`LOCAL_CHAIN_ONLY`で、外部時刻や同一ownerによる
全local artifactの意図的置換を証明しません。注文不能hard stopを解除する理由にもなりません。

### 安全設定UI

ダッシュボードは既定で**表示専用**です。表示する保守的な既定値と、ユーザーが変更できる絶対上限を
分けています。

| 項目 | 保守的な既定値 | 絶対上限 |
|---|---:|---:|
| 1回の予定損失 | 0.25% | 1% |
| 1日の新規買い停止線 | 0.75% | 2% |
| 1週間の新規買い停止線 | 2% | 5% |

これらは数量計算と新規買いを止める基準であり、実現損失の保証上限ではありません。窓開け、
スリッページ、手数料、通信・市場障害により実損失は超過し得ます。保有positionの売却は停止線で
妨げません。`maximum_investment_cents`は**BUY notional + BUY fee**の上限で、未設定（`null`）は
無制限ではなく、新規買いをブロックします。

保存を有効にする場合だけ、リポジトリ外の既存・絶対パス・owner-onlyディレクトリを明示します。
登録、契約、課金、追加インストールは不要です。

```bash
mkdir -m 700 /absolute/private/zidoutrade-risk-settings
PYTHONPATH=src python3 -m zidoutrade dashboard \
  --host 127.0.0.1 --port 8765 \
  --risk-settings-runtime-root /absolute/private/zidoutrade-risk-settings
```

保存物はcanonical JSON、SHA-256、親hashを持つ追記型revisionです。変更は未来の対象日にだけ保存し、
当日・過去日の変更は拒否します。保存した日付が取引所営業日かは、このUIでは推測しません。運用側が
監査済み取引所カレンダーと完全一致させ、`policy_for_session()`も対象日の完全一致時だけpolicyを返します。
この設定UIは現時点の注文不能hard stopを解除せず、設定を保存しても注文は0件です。

## SIMULATE注文を有効にする前の必須作業

- 実行中のPythonコードと監査済みsource hashをfresh processで一致させるlauncher
- 自システムの注文・約定だけを識別するセッション別ownership ledger
- 実約定価格、固定ATR、QFQ→raw係数、entry時刻を永続化したposition basis
- exact SIMULATE口座から凍結する日次/週次equity・PnL anchor
- exact order IDの取消、terminal/partial/late fill、stop後の既知position管理
- version固定fee engineとmarketable-limit/tick/cancel時刻のend-to-end結合
- 合成fault injectionと独立安全監査

これらが終わるまでは、分析結果が`ENTER`でも注文は0件です。

## 運用開始まで

1. 合成テストと独立レビュー
2. 注文ゼロのprospective shadow OOS
3. safety integration shadow（20取引日以上）
4. 人が監視する1銘柄・1株SIMULATEカナリア（現在版では実行不可）
5. 監視付き小規模SIMULATE（現在版では実行不可）

コードをmergeしただけではARMされず、現在版はlocal artifactを作っても発注できません。
将来の口座固有設定や有効化成果物もGitHubへ置かず、ローカルで別途監査・生成します。

## Codex / Claude共同編集

- Codex向け: [AGENTS.md](AGENTS.md)
- Claude向け: [CLAUDE.md](CLAUDE.md)
- 共通手順: [CONTRIBUTING.md](CONTRIBUTING.md)
- セキュリティ: [SECURITY.md](SECURITY.md)

branchとDraft PRを使い、安全境界の変更には必ず合成回帰テストを付けてください。
