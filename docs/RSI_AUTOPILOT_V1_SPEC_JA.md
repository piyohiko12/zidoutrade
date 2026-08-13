# RSI_AUTOPILOT_V1 仕様書

状態: 分析・選択・判断基盤を実装 / 注文RPCは無条件停止 / SHADOW既定
対象: 既存OpenDと既存SIMULATE口座だけ
運用モード: `SUPERVISED_ONLY`

## 0. 現在版の能力境界

現在版は、候補適格性、ユーザー選択、確定足検証、RSI/ATR、戦略判断、費用・shadow集計、
activation/実行証跡の検査までを提供します。`TradingRunner`と`MoomooPaperBroker`はどちらも
SIMULATE発注の直前で常にfail closedとなり、SDKの注文RPCへ到達しません。設定、環境変数、
lock、markerでこの停止を解除する方法はありません。

以下の注文、取消、約定・再起動処理は**凍結した目標設計**です。position basis、risk anchor、
ownership ledger、取消/late-fill、fresh-processコード証明を実装して独立監査を通すまで、
「実装済みの発注機能」や「完全自動売買」とは扱いません。

## 1. 目的と非目的

将来は、ユーザーが事前に選んだ1銘柄について、確定15分足のRSIを主条件として、正常系の
データ取得、判断、paper発注、照合、退出、記録を自動化します。現在版が実行できるのは
データ検証・判断・記録までで、paper発注以降は無効です。

「完全自動」は正常系の処理に人手の注文確認が不要という意味であり、利益、当日flat、
障害時の執行、外部効果のexactly-onceを保証する言葉ではありません。独立したposition保護が
実証されるまでは無人運転を許可しません。

次は対象外です。

- REAL、取引パスワード、`unlock_trade`
- システムによる銘柄の利益予測ランキング
- 空売り、オプション、暗号資産、OTC、時間外、インバース、初期版の2倍ETF
- ナンピン、買い増し、翌日持ち越しを前提とする戦略
- 自動install、登録、契約、trial、課金、権限拡張
- LLMがセッション中に条件やパラメータを変更する機能

## 2. 候補一覧とユーザー選択

### 2.1 責任分担

1. ユーザーが最大20銘柄のmaster watchlistと優先順を作る。
2. システムは適格性だけを検査し、PASS/FAILと理由を出す。
3. PASS候補はユーザー優先順、その後ticker順で表示する。
4. 予測リターン、バックテスト利益、勝率による順位は表示しない。
5. ユーザーが翌営業日のactive symbolを1つ選び、二段階確認でARMする。
6. セッション開始後は変更・自動代替しない。選択銘柄が不適格なら注文0件。
7. 候補全件、選択・非選択、生成時刻、対象日、設定をcanonical JSONとSHA-256で固定する。

状態は次だけです。

```text
DRAFT → VALIDATED → ARMED_NEXT_SESSION → SESSION_LOCKED → EXPIRED
```

変更できるのはflat、未決注文なし、runner停止中だけです。選択がなければ注文しません。

### 2.2 適格性

全項目が必要です。

- `US.<TICKER>`
- US通常株または明示的に許可した非レバレッジETF
- 前日終値5 USD以上
- 20営業日の売買代金中央値50,000,000 USD以上
- 確定日足252本以上、確定15分足100本以上
- halt、delist、unknown status、adjustment不整合なし
- 前日終値 > SMA200
- 前日SMA50 > 5営業日前SMA50
- SPY前日終値 > SPY SMA200
- 注文時のbid/ask midpoint基準spread 10 bp（0.10%）以下
  （これは候補生成時でなくaction直前にも再検証するgate）

契約や追加market-data entitlementが必要なら、不適格として停止します。

## 3. 指標の正本

### 3.1 bar

- 米国RTHの終了済み15分足だけ
- 通常日は09:30 ET開始、公式closeまでの完全な区間
- OpenDの米国15分足`time_key`はbar終了境界として扱う（09:45は09:30--09:45）
- 休日、短縮日、DSTは凍結済み取引所calendarとOpenD market stateの双方で確認
- 重複、欠損、順序逆転、非有限値、未来時刻、形成中barを拒否
- RSIとATRは同じQFQ系列、発注はraw quote

### 3.2 Wilder RSI(14)

- changeは前のRTH確定closeとの差
- 日をまたいでもresetせず、前RTH最終close→翌RTH最初closeの差を含む
- 最初の14 changeはgain/lossの算術平均
- 以降はWilder平滑化
- gain=loss=0なら50
- loss=0かつgain>0なら100
- gain=0かつloss>0なら0

### 3.3 Wilder ATR(14)

```text
TR = max(high-low, abs(high-prev_close), abs(low-prev_close))
```

最初の14 TRは算術平均、その後Wilder平滑化です。entry時のATRはsignal足でfreezeします。
QFQ→rawはsession開始時に固定した有限正のadjustment scaleを使い、途中で変われば停止します。

## 4. 売買判断と目標執行設計

### 4.1 ENTER_LONG

次をすべて満たした最初の確定足だけです。

1. 直近3本のRSI最小値 <= 30
2. 1本前RSI <= 35、最新RSI > 35
3. 最新close > 1本前high
4. `(最新close - 1本前high) / 1本前high <= 0.005`（0.50%以下。境界を含む）
5. 最新確定足volume >= その直前13本の確定RTH足volume中央値 × 1.5
6. 2.2の環境gate合格
7. fresh data、spread、market state、calendar合格
8. 当日未取引、flat、open/pendingなし
9. risk/notional/daily/weekly/dispatch上限内
10. 10:00–15:15 ET

volume比較の直前13本は、セッションをまたいでも途切れないRTH確定足系列から取ります。
13本未満、最新または比較対象に0 volumeがある場合は推測や短いfallbackを使わず`WAIT`です。
1本前highがない、非有限、0以下の場合も`WAIT`です。RSIが低いだけでは買わず、反発、出来高、
価格突破、追い掛け防止の全確認を必須にします。

将来の発注実装では、entryはfresh askを基準にtickへ切り上げた`ask × 1.0010`以下の
マーケッタブル指値です。
30秒後も残れば一度cancelし、terminal・late fill・positionを再照合します。価格を追いません。

### 4.2 EXIT_LONG

優先順は次です。

1. 緊急リスク退出
2. `entry_raw - 1.5 × ATR_raw`
3. RSI >= 60
4. 8本または2時間
5. 公式closeの15分前

将来の発注実装では、通常exitはfresh bidの`bid × 0.9985`、stop/close exitは`bid × 0.9950`を下限とする
売り指値です。10秒後にcancel・terminal確認・再照合します。全種類のexitとflattenは共有して
最大2 dispatchです。上限枯渇は`RECOVERY_REQUIRED`です。

## 5. リスク（RSI_RISK_POLICY_V2）

- planned risk既定: session開始時純資産の0.25%（設定可能なhard max 1%）
- 1銘柄notional: 10%以下
- 総notional: 20%以下
- 最大同時保有: 1
- 1日: 最大1往復
- 日次の新規entry停止線既定: realized + executable-bid unrealizedで0.75%（hard max 2%）
- 週次の新規entry停止線既定: 週初純資産基準で2.0%（hard max 5%）
- maximum investment: BUY notional + BUY feeの絶対上限（正の整数cents）
- 初期paper canary: 1株

`planned <= daily <= weekly`を必須とし、整数basis points以外は拒否します。maximum investment未設定
（`null`）は「無制限」でなく、新規entryをfail-closedでブロックします。残りの日次/週次予算も同時に
数量上限へ反映します。

0.25%等の割合は予定予算と新規entry停止線で、gap、slippage、fee、通信・市場障害時の実現損失上限を
保証しません。これらに到達しても既知long positionの照合・exitを妨げません。整数数量`q`は、stop距離、
保守slippage、entry/exit費用、maximum investment、日次/週次の残予算を満たす最大値です。1株未満ならWAIT。

### 5.1 次回セッション用リスク設定UI

UIは既定read-onlyです。明示されたリポジトリ外・絶対パス・owner-onlyのruntime rootがある場合だけ、
loopback Host/Origin、CSRF、exact JSON schema、exact int、上限、順序、optimistic concurrencyを検証して
保存します。canonical JSON + hash + parent hashの追記型revisionを先にdurable化し、その後`latest.json`を
公開します。当日・過去日への変更を拒否し、未来の対象日にだけ保存します。

UI storeは保存日を「対象日」として扱い、営業日を推測しません。運用時には監査済みexchange calendarで
営業日を検証し、storeの`policy_for_session(session_date)`がexact matchを返した場合だけ当該policy候補を
使います。latestを別日へ暗黙適用してはいけません。現在版ではこの設定は表示・永続化までで、注文不能
hard stopやSHADOW状態を解除せず、runtime decision/order executionには接続しません。

手数料モデルは版を固定します。2026-08-13確認版では、paper評価はmoomoo Japanの米国株デモ
公開ルール（system usage、settlement、売却時SEC・activity fee）、report-onlyの実口座stressは
ベーシックコースの税込0.132%、最低0.01 USD、上限22 USDを使用します。実際の口座コースや
公開料金が一致しない場合はARMせず、新しい料金版とテストへ更新します。デモの注文・利益・費用は
すべてシミュレーションで、本番成功を意味しません。

純粋な`run_decision_cycle`は、`IndicatorBarSeries`、raw quote、`SESSION_LOCKED`選択、hash固定calendar、
trend、risk、任意のlong positionを入力し、RSI/ATR、QFQ→raw ATR、market gate、戦略、上記paper
BUY/SELL費用込み数量を一度に再計算します。出力は`ENTER` / `EXIT` / 理由付き`WAIT`の判定レポート
だけで、broker/OpenD import、保存、注文は行いません。`RiskState`、trend、positionは呼び出し側から
渡された純粋入力であり、権威あるbroker equity/PnL anchorやdurable position basisの証明では
ありません。そのため、この判定層の追加だけでは§0の注文停止を解除しません。

### 5.2 moomooAI Q012見直しの扱い

2026-08-13のQ012見直しでは、moomooAIが出来高確認、spread縮小、価格追随防止に加え、
ATR比率`0.02%–1.50%`とturnover rate `0.50%`を提案しました。前3項目だけを決定論的な
fail-closed条件として採用しました。ATR比率とturnover rateの数値には、この戦略での
独立した性能根拠がなく、turnoverによる並べ替えは「適格候補からユーザーが選ぶ」非ランキング設計や
既存の20日売買代金中央値50,000,000 USD gateとも役割が競合するため`RESEARCH_ONLY`です。
sealed prospective OOSを事前登録して合格するまでproduction条件へ加えません。

moomooAIの回答は検証候補であり、収益性、勝率、損失上限の証明ではありません。割合の損失設定は
新規entry停止と数量計算に使う予算で、gap、slippage、障害時の実損失を保証しません。質問、回答要約、
公式仕様との照合は[Q012見直し記録](research/MOOMOO_AI_Q012_REVIEW_JA.md)に保存します。

- [moomoo Japan 米国株デモ取引ルール](https://www.moomoo.com/jp/support/topic7_320)
- [moomoo Japan 米国株・ETF手数料](https://www.moomoo.com/jp/support/topic7_184)
- [moomoo Japan 料金一覧](https://www.moomoo.com/jp/pricing)

### 5.3 moomooAI Q013バックテスト後見直しの扱い

2026-08-14のQ013では、口座等を除いたowner-only固定レポートの集計をmoomooAIへ渡しました。数値、結果の
方向、取引内訳は公開リポジトリへ保存しません。回答には複数条件の同時変更と、bar count等を損益や取引数に
誤読したperformance表が含まれたため、performance主張と複合変更は採用しません。

先読みなしで原子的に比較できる最初の候補だけを、research-only ID
`RSI_AUTOPILOT_V1_Q013_ATR_CAP_0050_SHADOW`として固定します。最新の確定済みQFQ・RTH 15分足に対する
Wilder ATR(14)を同じ足のcloseで割り、有限かつ正で`latest_atr_qfq / latest_close_qfq <= 0.0050`
の場合だけ研究gateを`PASS`とします。exact境界は含みます。必要本数不足、欠損、非有限値、0以下、または
上限超過はすべて`WAIT`です。

これは将来の注文0 paired shadow候補に限定します。現releaseでは探索的backtest variantと説明表示だけが
実装済みで、prospective recorderは未接続です。現行V1、production判断、Q012条件、risk sizing、退出条件、
runner/broker双方の注文hard stopは変更しません。同じ履歴を見た後の再利用結果は
`IN_SAMPLE_POST_HOC`かつ`EXPLORATORY_ONLY`であり、採用判断には使いません。事前固定した将来データで
controlとpaired比較し、さらに独立した再現確認が終わるまで`RESEARCH_ONLY`です。質問のサニタイズ要約と
誤読の扱いは[Q013見直し記録](research/MOOMOO_AI_Q013_REVIEW_JA.md)に保存します。

## 6. 目標状態機械と停止

controlとexposureを分けます。

```text
control: DISARMED / ARMED / PAUSED / HALTED / EMERGENCY
```

```text
exposure:
FLAT
ENTRY_INTENT_DURABLE → ENTRY_PENDING → ENTRY_RECONCILING
LONG_UNPROTECTED → LONG_GUARDED_LOCAL_ONLY
EXIT_INTENT_DURABLE → EXIT_PENDING → EXIT_RECONCILING
CANCEL_PENDING / PARTIAL_POSITION
FLATTEN_INTENT_DURABLE → FLATTEN_PENDING → FLATTEN_RECONCILING
COMPLETE / RECOVERY_REQUIRED
```

`PAUSED`は新規BUYだけを止めます。query/reconcileと、安全層が健全な場合の既知position退出を
止めません。policy/adapter/account/state identityが壊れた場合はguessしたSELLも出しません。

### at-most-once dispatch

SDK import/RPCより先にintentをO_EXCL作成し、fileとparentをfsyncします。そのintentを新規作成した
同一writerだけが1回dispatchできます。再起動で既存intentを読んだwriterはquery-onlyで
reconcileへ進み、再送しません。ACK喪失の0件は未受理と確定できないため盲目的retryしません。

起動時、session開始、毎判断前、action直前、INTENT/PENDING/UNKNOWN/RECOVERY中に、指定した
SIMULATE口座の対象symbol ordersとpositionをrefreshします。SELL qtyは再照合した実position以下です。

## 7. OpenD境界

- host `127.0.0.1`、port `11111`をコードでexact検証
- `TrdEnv.SIMULATE`だけ
- US、RTH、long-only、active symbolだけ
- raw account IDは環境変数からプロセス内で読み、永続化はsalted bindingだけ
- moomoo SDKはpaper adapterだけがlazy import
- testは必ずfake broker
- `REAL`と`unlock_trade`は静的scanと実行時の双方で拒否
- 現在版は上記に加えてSIMULATE注文RPC自体を無条件拒否

公開リポジトリにはaccount-bound lock/markerを置きません。cloneは常にdisarmedです。

## 8. UI

サイドバー:

1. Overview
2. Candidates
3. Decision
4. Risk
5. Journal
6. System

UIは各条件をPASS/WAIT/FAILで説明します。BUY/SELLボタンは設けず、active symbolの翌日ARMだけを
二段階確認します。mutationはloopback Host/Origin、CSRF、サイズ、exact JSON schemaを検証します。
public statusには口座ID、order ID、残高、position、価格、PnLを出しません。

## 9. 総利益とユーザー選択の評価

費用控除後総利益は事前に断定しません。次を注文0で並走します。

- `RSI_USER_SELECTED`: ユーザーが選んだ1銘柄
- `RSI_ALL_CANDIDATES_EQUAL`: 当日提示した候補を等ウェイトとしたshadow
- `RSI_FIXED_BASELINE`: 事前固定した決定論的baseline

全候補、非選択、選択時刻、no-fill、WAITも保存し、「RSI signal」と「ユーザー選択」の寄与を
分離します。V7との比較は、同じ銘柄/日付/費用/1Rにそろえたsignal-onlyと、native sizingを使う
end-to-endを分けます。

主要performance endpointは同じ固定暦期間の費用控除後terminal returnです。併記はnet daily
return、trade数、expectancy、PF、MDD、Sharpe、exposure、fill率、平均win/loss、tail loss、
safety violation。勝率だけでは採用しません。

履歴を見て候補・銘柄を選んだ場合はhistorical final OOSと呼びません。設定と選択規則をfreezeした
後のprospective shadowを0件目から集めます。変更は新versionです。

### 9.1 moomoo履歴ローソク足による探索的バックテスト

`HISTORICAL_CANDLE_PROXY_V1`は、既存OpenDのquote-only
`request_history_kline`出力を使うheadless検証です。口座、残高、position、order、約定履歴を照会せず、
結果JSONやグラフもリポジトリ外のowner-only runtimeへ保存します。対象銘柄を履歴確認後に固定したrunは
`FIXED_BASELINE`であり、`USER_SELECTED`やsealed OOSとは表示しません。

入力は対象銘柄のRTH 15分QFQ/RAW、対象銘柄の日足QFQ/RAW、SPY日足QFQ、米国取引日・短縮日です。
取得時の役割、adjustment、session、期間、bytes、row count、SHA-256を外部manifestで固定します。
15分足のOpenD時刻はbar終了境界として読み、QFQ/RAWの時刻集合は完全一致を要求します。

各取引日の環境条件は前取引日までの日足だけで計算し、確定したsignal barより前の足だけを参照します。
entryはsignal確定後の次15分足openより前には置きません。RSI/ATR、出来高、breakout、1日1往復、
短縮日close、費用は本仕様の固定条件を使います。同一15分足内の価格順序が不明な場合はstopを優先する
悲観的な代理処理とし、結果に仮定を列挙します。

履歴K線には当時のbid/ask、10bp spread、30秒/10秒以内のfull/partial/no-fill、late fill、
OpenD freshnessや当時の候補・ユーザー選択がありません。そのため上限spreadとfull fillの保守的代理を
明示して計算し、classificationを常に`EXPLORATORY_ONLY`とします。現在のfee scheduleを過去価格へ
適用した比較であり、当時の実料金やmoomooデモ約定を再現したものではありません。この結果だけで条件を
変更せず、§10のprospective shadow OOSを置き換えません。

取得日時点のQFQ履歴は、後日のsplit・配当等で再計算された値を含み得て、当時利用可能だった
point-in-time調整系列であることを証明できません。日付・足時刻に対するfuture row混入は拒否しますが、
この企業行動調整の限界までは解消しないため、no-lookaheadの主張はrow timestampの範囲に限定します。

## 10. リリースgate

1. pure functions/state machineの合成テスト
2. 発注前の技術blocker（§0）を全解消し、独立安全監査に合格
3. sealed prospective shadow OOS 200完結往復（先頭150 + final 50を一度だけ評価）
4. safety integration shadow 20取引日以上、注文0、安全事故0
5. 人が監視する1銘柄・1株・1往復SIMULATE（現在は実行不可）
6. 60取引日かつ30往復のsupervised SIMULATE（現在は実行不可）
7. 100往復かつ3か月のsupervised拡張（現在は実行不可）

`UNATTENDED`はbroker常駐stopまたは同等の独立保護をSIMULATEで実証するまで到達不能です。
REALへの昇格機能は作りません。

## 11. 完成条件

- 選択symbol以外、REAL、unlock、short、時間外へ到達不能
- indicatorが既知vectorと境界で一致
- 起動/再起動ごとにbroker state照合
- crash境界でat-most-once dispatch、blind retry 0
- PAUSE中もreconcileと許可されたexitを継続
- risk/notional/order/holding/calendar/data freshnessを機械制限
- UIと正本stateが一致し、秘密・口座・市場・performance値を公開しない
- 上記release gateを順番に満たす
- 現在の公開版では注文RPCへ到達不能であること

ここまで満たしても名称は「監視付きSIMULATE自動化」です。障害耐性を含む無人運転や利益を
保証するシステムとは表現しません。
