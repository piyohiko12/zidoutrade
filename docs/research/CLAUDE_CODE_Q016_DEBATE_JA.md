# Claude Code Q016 改善策討論の記録

記録日: 2026-08-14

種別: サニタイズ済み要約（逐語録ではない）

分類: `DRAFT_ONLY / RESEARCH_ONLY / RECORDER_FIRST_NO_NEW_RULE / ORDER_IMPACT_ZERO`

対象: `RSI_AUTOPILOT_V1` の次の研究工程

この文書には、口座、注文、残高、position、価格、銘柄、検証期間、個別取引、正確なperformance値、
Claude Codeの認証情報を含めません。正確な集計はowner-onlyの固定レポートからClaude Codeへ伝えましたが、
公開リポジトリへ再掲しません。Claude Codeには`Read/Grep/Glob`だけを許可し、コード・文書・gitの変更、
Bash、web search、OpenD、moomoo API、口座、position、注文、追加backtestを禁止しました。

## 1. 討論の目的

既閲覧の小さな消費済み履歴標本と固定済みの研究variantについて、owner-onlyレポートを討論の固定入力に
しました。結果の方向、件数、損益、採否判定は公開せず、同じ標本を使った救済調整も行いません。

この状態でさらに売買条件を加えるべきか、それとも未観測の将来データを欠落なく残す基盤を先に
作るべきかを、CodexとClaude Codeが反証を交えながら討論しました。

## 2. 討論の進め方

1. **Claude Codeによる別モデル反証**: 原因仮説を最大3個に限定し、entry、exit、filter、費用のどれを
   現標本から識別できるかを問い直しました。
2. **Codexの赤チーム攻撃**: 15分足MFE/MAEの順序不明、Q014 V1の長期化、Q015診断reasonの情報消失、
   複数銘柄の疑似反復、既存runnerのbroker照合、既存shadow集計の不足を反論しました。
3. **Claude Codeの自己反証**: 自身の第1回答のfailure modeを列挙し、次の1 PRへscopeを縮小しました。
4. **共同裁定**: hash chainだけではdomain順序やsession丸ごとの欠落を証明できない点を追加し、
   deterministic replayとEXPECTED session ledgerを正本にしました。
5. **停止時欠落の訂正**: collectorが完全停止した場合、collector自身は`MISSING`を追記できません。
   Claude Codeはこの誤りを認め、独立verifierによる後続検知へ契約を修正しました。

Claude Codeの回答は設計案を反証する材料であり、performance evidenceでも独立した安全・統計監査でも
ありません。この文書は、後続の独立監査を置き換えません。

## 3. Claude Codeが最初に示した原因仮説

Claude Codeは次の3仮説を挙げましたが、いずれも消費済み標本では識別不能と判断しました。

- entry後の勝ち幅と負け幅が非対称である可能性
- exitが利益を早く切り、損失をstopまで保有する可能性
- Q012適用前後のfunnelが未記録で、機会不足と機会品質を区別できない可能性

小標本からsignal、exit、filter、費用のいずれかを原因と断定することはできません。Claude Codeは、
新threshold、新exit、Q013採用、Q015緩和ではなく
`RECORDER_FIRST`を選びました。

## 4. 赤チームで棄却・修正した点

### 4.1 15分足MFE/MAE

15分足high/lowは値動きの外形を記述できますが、同じ足の中でstopとhigh/lowのどちらが先だったかを
決められません。したがって次には使いません。

- exit変更の因果証拠
- fill可能性の証拠
- stop幅や利確条件の調整根拠

MFE/MAEは今回の次PRにフィールド予約もせず、完全に後続の記述的研究へ送ります。

### 4.2 Q014 V1の取引件数基準

完結往復数だけを停止条件にすると、終了するcalendar時点が事前に定まらず、検証が長期化し得ます。
Q014 V1のStage 1/Stage 2を上書きせず、別protocol IDのQ014 V2候補では次を採用します。

- primary analysis unitは、取引ではなくselectionが固定された全対象session
- 固定calendar期間とminimum informationの両方を事前登録
- 固定期間終了時に情報不足なら延長せず`INCONCLUSIVE`
- 延長や条件変更は新protocol IDで0件から開始
- 複数候補shadowはsession内clusterとして扱い、銘柄数を独立標本数へ水増ししない
- `ALL_CANDIDATES_EQUAL`と`FIXED_BASELINE`はsecondary descriptiveであり、
  `RSI_USER_SELECTED`のprimary結果へpoolしない

固定期間の具体値は、既閲覧performanceから逆算しません。運用上の監査周期を根拠に、収集開始前の
manifest承認時にのみ確定します。

### 4.3 runner、shadow、storageの再利用境界

既存`TradingRunner`はSHADOW起動でもbroker照合を行うため、観測専用recorderに流用しません。
既存`shadow.py`は完了済みtradeの集計helperであり、EXPECTED session、pair reservation、arm別state、
restart replay、terminal record、sealを持たないため流用しません。

一方、canonical JSON、owner-only file、O_EXCLなど、broker非依存のstorage primitiveは安全条件を再検証した
上で利用できます。正本はsequenceごとのO_EXCL immutable recordとし、単一append JSONLは必要なら生成する
派生viewに限定します。各recordはmanifest hash、study/protocol identity、session/pair identity、sequence、
expected prior headをbindします。既存reservationやrecordを発見した再起動processはquery-onlyとし、
同じeffectを再生成しません。

hash chainはlocal bytesの連鎖を補助的に検査するだけで、外部時刻、現実世界での発生、domain eventの
正しさを証明しません。immutable record集合、sealed expected ledger、保持されたpredecessor receipt、
独立replay、外部seal artifactを組み合わせ、通常のcrash、tail欠落、別tailへのforkをfail closedで
検出します。final seal前に同一ownerが全local anchorとrecordを意図的に置換する攻撃はlocal artifactsだけでは
証明不能であり、より強い保証には別管理の外部attestationが必要です。

## 5. 共同裁定

CodexとClaude Codeの共同結論は次のとおりです。

> 新しい売買条件を追加せず、次の1 PRをbroker、runner、SDK、market-data adapterから構造的に分離した
> Q014 V2観測ledgerのschema、session reservation、deterministic replay state machineに限定する。

Q013/Q015のcounter、式、結果、production pathには触れません。runnerとbrokerの無条件注文hard stopも
変更しません。実データ収集、performance計算、UIの「改善済み」表示はこのPRに含めません。

## 6. 次の1 PRの提案scope

名前は実装前監査で変更できますが、候補は次の3 componentです。

- `src/zidoutrade/research_events.py`
  - study lifecycle、event、arm outcomeの型
  - canonical field allowlist
  - production `ReasonCode`と分離した研究専用namespace
- `src/zidoutrade/research_ledger.py`
  - EXPECTED session ledger
  - pair/sessionのO_EXCL reservationとsequenceごとのimmutable record
  - deterministic replay state machine
  - terminal uniqueness、fork、tail、deadline欠落を検査するpure verifier
- `tests/test_research_events.py`、`tests/test_research_ledger.py`、
  `tests/test_research_boundary.py`
  - schema、replay、fault injection、import境界、安全情報の非公開を検証

### 6.1 non-goals

- 新entry、exit、RSI、ATR、volume、spread、chase、risk、fee条件
- Q013またはQ015の変更・組合せ・production採用
- Q015の`WAIT`理由を既存engineへ追加する変更
- MFE/MAEフィールド
- market-data adapter、OpenD、口座、position、注文への接続
- runner、broker、dashboard、CLIの変更
- 実データ収集、backtest、performance表示
- 固定期間とminimum informationの数値確定

## 7. EXPECTED sessionとevent state machine

対象calendar sessionの一覧とhashは、収集開始前にmanifestへsealします。選択が無い日も
`NO_SELECTION`として対象から落としません。selection cutoff後に、選択済みsymbolまたは
`NO_SELECTION` sentinelを含むprivate pair keyを作成します。pair keyはdomain separatorを付けたcanonical
objectのSHA-256で、少なくとも`study_id`、`protocol_id`、`target_session`、選択済みsymbolまたは
`NO_SELECTION` sentinel、`selection_record_sha256`をexact fieldとしてbindします。単純な文字列連結は使わず、
pair keyとそのpreimageを公開statusへ出しません。

概念上の遷移は次です。

```text
SESSION_EXPECTED -> PAIR_RESERVED
PAIR_RESERVED -> OBSERVED
OBSERVED -> ARM_TERMINAL(BASELINE) + ARM_TERMINAL(CANDIDATE) -> SESSION_COMPLETE
PAIR_RESERVED -> OBSERVATION_MISSING -> SESSION_MISSING
any invalid state/event -> replay failure -> external FAILURE_REPORT -> DATASET_BURN
all EXPECTED sessions terminal + clean verifier verdict -> external STUDY_SEAL artifact
```

exact前提は次です。

- `SESSION_EXPECTED`はsealed calendar coverageに存在するsessionだけを許可
- `PAIR_RESERVED`は同じstudy/session/pairに1回だけ許可し、manifest hash、expected prior head、
  study/session/pair identityをbind
- `OBSERVED`と`OBSERVATION_MISSING`は相互排他
- `OBSERVED`のsessionはbaseline/candidate両armにexactly oneのterminalが必要
- `OBSERVATION_MISSING`なら、推測したdecisionを作らずsession全体を`SESSION_MISSING`へ倒す
- `SESSION_COMPLETE`は両arm terminalより前には作成不可
- 各EXPECTED sessionはexactly oneのsession terminalを持つ
- 全EXPECTED sessionがterminalになり、独立verifierがclean verdictを出すまで正常なseal artifactを作成不可
- 既存eventの順序変更、重複、tail切断、fork、未知eventはreplay失敗
- replay失敗時は壊れたrecord集合へ`PROTOCOL_VIOLATION`を追記せず、別O_EXCL failure reportを作り、
  normal sealを禁止してdatasetをburn

単純なlinear enumだけでなく、両arm terminalの集合を検証するdeterministic replay reducerを正本にします。

## 8. 完全停止時の欠落検知

writer heartbeatは補助証拠です。collectorが完全停止すれば、そのcollectorは`MISSING`を追記できません。
そのため安全契約を次のように分けます。

1. 収集前にEXPECTED session list、calendar coverage、deadline、genesisをsealする。
2. collectorは観測eventをappendするが、欠落の正本判定者にはしない。
3. 独立verifierはmanifestにbindしたcanonical UTCの`as_of_utc`、deadline source、EXPECTED ledgerを比較し、
   deadline超過かつterminal無しを検出する。local clockは外部時刻証明ではなく、rollback、noncanonical、
   manifest外sourceはfail closedとする。
4. verifierはwriter record集合を書き換えず、別のO_EXCL reportへ`MISSING/INTEGRITY_FAILURE`を出す。
   collector完全停止によるterminal欠落を、後からwriter eventとして補完しない。
5. terminal欠落時はnormal sealを禁止してdatasetをburnする。部分sessionの救済はしない。
6. 正常時の外部O_EXCL study seal artifactは、manifest hash、expected-ledger root/count、最後のpre-seal
   immutable record hash/count、verifier report hash/verdictをbindする。seal自体はwriter recordではなく、
   journal rootを自己包含しない。

次のPRはschemaとpure replay/verifierの骨格だけです。daemon、自動append、独立verifier process、
「自動MISSING確定」を実装済みとは表示しません。

## 9. private observationと公開境界

将来adapterを接続する場合、判断時点のprivate evidenceには少なくとも次が必要です。

- provider timestamp、capture開始・終了時刻
- best bid/askと可能な場合のsize、spread
- 完了済みRTH足、RAW/QFQ payload hash、凍結したadjustment mapping
- quote failure、timeout、stale、欠損
- baseline/candidateそれぞれのarm outcomeと独立virtual state
- WAIT、NO_SELECTION、no-fill、missing、data-quality terminal
- calendar、selection、risk、fee、execution-model revision/hash

価格、symbol実値、symbol別PnL、performanceはrepo外owner-only record集合だけに置きます。
order/account/position情報はrecorderが取得も保存もしません。
公開statusは価格や識別子を持たないallowlist DTOとし、収集中の主・副performanceを表示しません。

## 10. 必須テスト

- recorder層がbroker、runner、SDK、market-data adapter、CLI、dashboardをimportしない
- public DTOがprice、symbol実値、PnL、order/account/position fieldを拒否
- 同一pair/sessionの二重reservationを拒否
- `ARM_TERMINAL`より前の`SESSION_COMPLETE`を拒否
- 各EXPECTED sessionのterminal重複・欠落を検出
- sequenceごとのO_EXCL record、previous hash、record hash、保持されたpredecessor receiptの不一致を検出
- tail切断をexpected list/count、保持されたcheckpoint receipt、final sealの不一致として検出
- fsync前後の切断、partial record、再起動forkをfail closedで検出
- sealed manifest、expected ledger、final reportのsymlink/hardlink/ownership/permission違反を拒否
- unknown event、余計なfield、duplicate JSON key、noncanonical bytesを拒否
- Q013/Q015、production strategy、runner、brokerに差分が無いことを回帰検査
- REAL、`unlock_trade`、注文RPCの到達不能を既存testで再確認

## 11. 22年問題の正直な結論

短縮できるのは、signal mechanics、gate attrition、データ品質、欠損率を理解するまでの時間です。
全対象sessionとsecondary multi-symbol armを同時に記録すれば、実取引機会だけを待つより早く構造を
反証できます。

短縮できないのは、ユーザー選択を含むprimary戦略の収益優位を証明するために必要な独立情報量です。
複数銘柄shadowを増やしても、それをユーザー選択strategyの独立標本として数えることはできません。
短い固定期間の結果が`INCONCLUSIVE`になる可能性を受け入れます。これは失敗を隠すための設計ではなく、
不足した証拠を利益保証へ変換しないための設計です。

## 12. 実装前のGO条件

次をすべて満たすまで実装PRへ進みません。

- Q014 V2のprotocol/schema IDとnon-goalsが独立監査済み
- repo外owner-only/O_EXCL領域をtest fixtureだけで検証可能
- expected-session ledgerとdeadlineの正本が一意
- deterministic replayが不正順序・重複・欠落を拒否
- collector完全停止を独立verifierが後から検出できる契約
- public/private DTOのallowlistが固定済み
- Q013/Q015とproduction/order sourceへ変更0件
- synthetic fault-injectionと全既存testが合格
- UIは引き続き`SHADOW / ORDER DISABLED`を表示

## 13. 今後も棄却する提案

- 同じ履歴でQ015 ratio、anchor、RSI、ATR、volume、spread、stopを調整する
- Q013とQ015をstackする
- 0取引を改善または利益と呼ぶ
- 手数料・slippageを都合よく小さくする
- 複数候補を独立標本として水増しする
- WAIT、NO_SELECTION、no-fill、missingを分母から除外する
- 途中performanceを見て期間、minimum information、条件を変える
- dataset violation後に一部の都合の良いsessionだけを救済する
- Claude Codeが提案したこと自体を性能根拠にする
- recorder実装を注文hard stop解除の理由にする

## 14. 現在の状態

この討論で売買条件、production、UI、注文経路は変更していません。Q014 V2 recorderもまだ未実装です。
共同裁定は、次の実装候補を安全な観測基盤の1 PRへ限定しただけであり、収益改善や採用を意味しません。
