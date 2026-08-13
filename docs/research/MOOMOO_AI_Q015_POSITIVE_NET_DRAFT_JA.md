# moomooAI Q015 費用控除後プラスを目標にした研究原案

記録日: 2026-08-14

種別: サニタイズ済み要約（逐語録ではない）

状態: `DRAFT_ONLY / RESEARCH_BACKTEST_IMPLEMENTED / NOT_ADOPTED`

この文書には口座、注文、残高、position、価格、銘柄、検証期間、個別取引、正確なperformance値、
cookieその他の秘密を含めません。討論中もOpenD、口座、注文にはアクセスしていません。

## 1. 目的をどう定義するか

目的は「同じバックテストを調整して、プラスの組合せを見つける」ことではありません。

> 事前に1つだけ固定した候補が、未使用データにおいてモデル化された全費用控除後で絶対損益を
> プラスにし、同じselection snapshotを使う現行baselineも上回るかを反証可能な形で検証する。

費用控除後プラスは、探索を止める条件ではなく、候補を固定した後の合格条件です。プラスになるまで
threshold、期間、銘柄、費用、執行proxyを変更しません。ここでいう損益は
`MODELLED_NET_AFTER_COSTS`であり、税、為替転換、実queue、実slippageを含む実利益ではありません。

owner-onlyレポートではgross結果とモデル費用を分離してmoomooAIへ渡しました。moomooAIは、feeだけを
変更してもsignalのedgeを証明できないと診断しました。正確な値、符号、取引数、期間、銘柄は公開
リポジトリへ再掲しません。したがって、fee削減だけで損益を正にできるとは扱いません。

## 2. moomooAIとの2段階討論

### 2.1 第1段階: 提案、反証、裁定

moomooAIには、RSIを主条件に保ち、完了済みRTH足だけを使い、複数条件やthreshold sweepを禁止して
原子的な変更を1つだけ提案させました。最初の提案は、含み益が`1 ATR`へ達した後に`0.5 ATR`を
確保するtrailing stopでした。

この案は次の理由で棄却しました。

- 小標本では、含み益が`1 ATR`へ達したwinnerが十分にあるか判定できない
- 大きなwinnerを早く切り、payoffを悪化させる可能性がある
- 15分足だけでは、同一足内のtrail発動と反落順序を復元できない
- Q013の後に別mechanismを選ぶこと自体が、研究者自由度を増やす
- 独立に確認されていないedgeを、exit形状だけで新しく証明することはできない

第1段階の裁定は`RECORDER_FIRST_NO_RULE`でした。

### 2.2 第2段階: 費用込み回復余地gateへの赤チーム攻撃

独立レビューで、後述する`PRIOR_CLOSE_NET_REWARD_RISK_GATE_V1`を提示し、moomooAIに再反証させました。
この案は、entry判断時点で既知の情報だけを使い、exitを変更せず、全signalで判定できる点ではtrailing
stop案より明確と評価されました。一方、前RTH終値は実際のexit条件ではなく、価格がそこまで戻る根拠も
ないため、最終裁定は`DRAFT_REWARD_RISK_NOTE_ONLY`でした。

この候補をproduction条件、Q013との合成条件、または現在収集中のarmへ追加しません。Q014で決めた
recorder-firstが、引き続き唯一の次工程です。

## 3. Q015原案

研究ID案:

```text
Q015_PRIOR_CLOSE_NET_REWARD_RISK_GATE_V1
```

仮説:

> RSI/Q012の反発signalが成立しても、前RTH終値までのモデル化された費用控除後回復余地が、現行の
> `1.5 ATR` stopによるモデル化された費用込みstress loss以上でなければentry候補にしない。

これはprofit targetではありません。現行のRSI、time、stop、close-approaching exitを一切変更せず、
entry前の研究gateとしてだけ定義します。

### 3.1 情報時点

- `N`: baselineのRSI/Q012 signalが成立した最新の完了済みQFQ・RTH 15分足
- `N+1`: 同一RTH session内の次の15分足
- historical proxyでは、`N+1`のRAW openが観測された時点で判定する
- prospective recorderでは、同じentry判断時点のfresh RAW quoteだけを使用する
- `N+1`のhigh、low、close、volumeや、それ以後のbarは判定へ使わない

`N+1`のopenを使うことは、signal時点の判断ではなくentry直前の判断であることをmanifestへ固定します。
次足の始値が存在しない、entry window外、stale、不整合の場合は`WAIT`です。

historical取得時点のQFQには、検証対象時点より後のcorporate actionが遡及反映される可能性があります。
bar timestampだけの先読み防止では、このpoint-in-time限界を解消できません。historical sanityは引き続き
`EXPLORATORY_ONLY`であり、prospectiveでは判断時点のRAW/QFQとadjustment mappingを同時に固定します。

### 3.2 固定式

現行の`HISTORICAL_CANDLE_PROXY_V1`と同じ費用・執行proxyを使います。

```text
b_entry = (10 bp spread / 2 + 10 bp entry cushion) / 10,000
b_normal_exit = (10 bp spread / 2 + 15 bp exit cushion) / 10,000
b_stressed_exit = (10 bp spread / 2 + 50 bp stressed cushion) / 10,000

scale_N = RAW_close_N / QFQ_close_N
A = Wilder_ATR_QFQ(14)_N * scale_N

E0 = RAW_open_(N+1)
E = E0 * (1 + b_entry)
S = E - 1.5 * A

T = prior_completed_RTH_final_QFQ_close * scale_N
X = T * (1 - b_normal_exit)
Z = S * (1 - b_stressed_exit)

if any required value is invalid, or A/S/T/X/Z <= 0, or X <= E:
    WAIT before constructing SizingRequest

size_result = size_position(SizingRequest(
    state = arm_specific_manifest_bound_entry_time_RiskState,
    entry_limit = E,
    stop_trigger = S,
    entry_fees = PAPER_FEE_SCHEDULE,
    exit_fees = PAPER_FEE_SCHEDULE,
    stress = ExecutionStress(),
    policy = manifest_bound_RiskPolicy_V2
))
q = size_result.qty

if q < 1: WAIT before any BUY_fee or SELL_fee calculation

net_reward =
    q * (X - E)
    - BUY_fee(q, E)
    - SELL_fee(q, X)

stressed_loss =
    q * (E - Z)
    + BUY_fee(q, E)
    + SELL_fee(q, Z)

PASS iff q >= 1 and net_reward >= stressed_loss
```

`RiskState`はentry判断時点のday/week anchor、daily/weekly PnL、当日完結往復数を持つarm固有snapshotです。
両armは同じ開始anchor、calendar、policyを共有しますが、過去の仮想取引差によるPnLとroundtrip stateは混ぜず、
各armで独立して進めます。`RiskPolicy V2`、`ExecutionStress()`、`PAPER_FEE_SCHEDULE`のexact payload/hashを
manifestへ固定し、`size_position`以外の数量式へ置換しません。`q < 1`はfee engineを呼ぶ前に`WAIT`へ
確定します。

現行risk sizingとcandle proxyはstop後slippageの表現が同一ではないため、
`size_result.components.planned_total_loss`をQ015の`stressed_loss`として流用しません。数量は現行risk sizingで
決め、Q015の比較値は上記`Z`と同じversioned candle proxyから別途再計算します。

境界は含みます。比率`1.0`はこの履歴から選んだ最適値ではなく、「モデル上の費用控除後upsideが
モデル上の費用込みdownside未満なら入らない」という規範的な対称条件です。収益性を示す経験的根拠とは
呼びません。`0.8`、`1.2`、`1.5`等の近傍探索は行いません。

### 3.3 fail-closed条件

次のどれかに該当すれば`WAIT`です。

- RSI/Q012 baseline signalが成立していない
- 必要なQFQ/RAW足、前RTH最終close、adjustment mapping、次足openが欠ける
- 値が非有限、0以下、stale、重複、順序不整合、session不整合
- `scale_N`を同時点RAW/QFQから一意に固定できない
- session中のcorporate-action mapping変化を検出した
- `A <= 0`、`S <= 0`、`T <= 0`、`X <= 0`、`Z <= 0`、`X <= E`、または`q < 1`
- fee、calendar、risk、selection、execution-modelのrevision/hashがmanifestと一致しない

Q013とは重ねません。比較armはbaselineと、baselineへQ015だけを追加した候補の2本です。

## 4. 最も強い反証

この原案の最大の弱点は、`T`が実際のexit価格ではないことです。現行戦略はRSI、time、stop、
close-approachingでexitし、前RTH終値へ到達しても自動exitしません。したがって`net_reward`は実現可能な
利益の保証ではなく、「平均回帰余地」のproxyにすぎません。

さらに、downtrendでは前RTH終値が現在値より高く、大きな見かけ上のrewardを与える一方、価格が戻らず
stopへ進む可能性があります。逆に、前RTH終値より上へ伸びるwinnerを過小評価して除外する可能性もあります。
gateがほぼ常時`PASS`または常時`WAIT`なら、識別力がないとして棄却します。

この問題を同じ履歴のthreshold調整で直してはいけません。前RTH終値、当日VWAP、固定profit target等を
結果後に入れ替えた時点で、別studyとして将来データを0件から収集します。

## 5. 同じ履歴の利用範囲

既に閲覧した履歴全体は`DEVELOPMENT_ONLY / IN_SAMPLE_POST_HOC / EXPLORATORY_ONLY`です。

許可:

- データ整合性、時刻、費用、QFQ/RAW変換、先読みがないことの検査
- gateがbaseline signalの部分集合になることの検査
- fixed formulaを1回だけ実行するsanity backtest
- 0件、常時PASS、常時WAIT、非有限値等の実装不備の検出

禁止:

- 損益がプラスになるまで式、倍率、期間、銘柄を変更する
- 同じ履歴で最良のanchor、ratio、exit、RSI値を選ぶ
- プラスになったvariantだけを残す
- random splitや未使用に見える一部期間をfinal OOSと呼ぶ
- backtestがプラスならproductionへ採用する

同じ履歴で費用控除後損益が0以下ならQ015を棄却し、調整しません。プラスでも実装sanity以上の証拠価値を
与えず、採用判断には使いません。

## 6. sealed評価原案

Q014 recorderを実装・独立監査し、Q015用に別manifestを作るまでは収集を開始しません。selectionが固定された
全対象sessionを分析集合とし、`WAIT`、`NO_SELECTION`、no-fill、0取引、baseline-only、missingを落としません。

主指標:

```text
R_arm = ending_equity_arm / fixed_starting_equity - 1
delta = R_Q015 - R_baseline
```

Stage 1はQ015候補の最初の150完結往復でfutilityだけを一度判定します。Q015単体の
`MODELLED_NET_AFTER_COSTS <= 0`、`delta <= 0`、cost stress非正、closed-trade MDD悪化、最大単発損失悪化、
safety/data violationのどれかで棄却します。通過しても成功とは呼びません。

Stage 1をsealして収集を停止し、通過した場合だけ、後続で非重複の50完結往復をStage 2として開始します。
最初のStage 2対象sessionより前にO_EXCL activation markerを作ります。最終合格には、Stage 2単独と200件合算の
両方で次をすべて要求します。

- `R_Q015 > 0`
- `delta > 0`
- version固定feeと執行proxyを控除後も上記2条件を満たす
- report-onlyの2倍execution-bps stressでも上記2条件を満たす
- closed-trade MDDと最大単発損失がbaselineより悪化しない
- safety/data violationが0

不足、欠損、判定不能はpromotionしません。150/50は最低限のgovernance gateであり、統計的十分性、独立性、
将来利益を保証しません。

### 6.1 Stage間の資本とstate

manifestへ`fixed_starting_equity`を1つ固定します。Stage 1では両armをこの同額から開始します。Stage 2は
Stage 1のequity、日次/週次PnL、roundtrip stateをcarryせず、最初のStage 2 sessionより前に両armを同じ
`fixed_starting_equity`、daily/weekly PnL=`0`、completed roundtrips=`0`へresetします。Stage 2の最初の
sessionをday/week anchorの開始点とし、以降は通常のcalendar境界でresetします。

Stage 1とStage 2のreturnをそれぞれ`R1_arm`、`R2_arm`とし、200件合算は連続口座残高ではなく次の
正規化合成値として固定します。

```text
R200_arm = (1 + R1_arm) * (1 + R2_arm) - 1
delta_stage2 = R2_Q015 - R2_baseline
delta_200 = R200_Q015 - R200_baseline
```

これによりStage 1の偶然の損益やrisk budgetがStage 2の数量へ流入しません。Stageごとのvirtual state、reset、
equity curve、anchor hashを別々に保存します。

### 6.2 cost stressと不確実性

2倍execution-bps stressはsignal、Q015の`PASS/WAIT`、数量、exit reason、保有期間を一切変えない
report-only replayです。同じ取引集合と同じ`q`に対し、spread、entry cushion、normal-exit cushion、
stressed-exit cushionだけをそれぞれ`10/10/15/50 bp`から`20/20/30/100 bp`へ倍化し、stressed価格で同じ
`PAPER_FEE_SCHEDULE`のfeeを再計算します。再selection、再sizing、再entry判定は行いません。

`R > 0`と`delta > 0`が微小な場合を合格にしないため、全対象sessionの日次net returnを使うprecision gateを
analysis manifestへ固定します。`WAIT`、no-fill、0取引は`0` returnのsessionとして残します。

```text
d_t = r_Q015,t - r_baseline,t
method = stage-stratified circular moving-block bootstrap
block_length = 20 consecutive target sessions
resamples = 10,000
seed(scope) = unsigned first 64 bits of
    SHA256("Q015_BOOTSTRAP_V1\0" + study_id + "\0" + scope)
one_sided_lower_bound = sorted_bootstrap_means[499]
```

Stage 2単独とStage 1+2合算の双方で、Q015の平均日次net returnと`d_t`のone-sided 95% lower boundが
strictly `> 0`でなければ`INCONCLUSIVE_OR_REJECT`です。bootstrapのexact sampling、wrap、percentile、tie、
NaN処理を実装・合成test・独立監査してからmanifestをsealします。Stage 2単独ではStage 2内だけでblockを
作ります。合算ではStage 1とStage 2を別々に同じ回数resampleし、各stageの元のtarget-session数を保って
加重平均します。blockはstage境界を跨がず、circular wrapも同じstage内だけです。tieは残し、非有限値または
必要session不足はpromotion不可です。未実装のまま収集を開始しません。

これらを満たしてもresearch gateを通過するだけで、production採用や注文hard stop解除はできません。
別のproduction safety review、権威あるrisk/position基盤、supervised SIMULATE検証が必要です。

## 7. 保存する証拠

- study/candidate/schema ID、formula、ratio`1.0`、code/protocol/test hash
- selection snapshot、選択/非選択候補、`NO_SELECTION`、target session、pair key
- calendar、fee、risk、execution model、instrument classifierのversion/hash
- signal bar、前RTH最終bar、次足openのtimestampとRAW/QFQ payload hash
- `scale_N`、ATR、`E0/E/S/T/X/Z/q`、BUY/SELL fee内訳
- `net_reward`、`stressed_loss`、`PASS/WAIT`、stable reason code
- baseline/Q015それぞれのsignal、WAIT、no-fill、virtual position、exit、session return
- `SESSION_COMPLETE`または`SESSION_MISSING`のexactly one terminal record
- append-only previous hash、record hash、checkpoint、Stage seal/activation marker

local hash chainは`LOCAL_CHAIN_ONLY`であり、外部時刻や実世界の発生を証明しません。

## 8. 今回変更しないもの

- productionのRSI/Q012、Q013、entry、exit、risk、user selection
- `HISTORICAL_CANDLE_PROXY_V1`、fee schedule、baseline default
- long-only、RTH、確定足、1日1往復、先読み禁止
- runner/broker双方の無条件注文hard stop

この原案のproduction注文影響は0件です。固定式は注文0の探索的backtest variantとしてのみ実装済みです。
production判断、Q014 prospective recorder、CLIの自由なthreshold指定には接続しておらず、UIへ
「改善済み」または「比較中」と表示しません。
