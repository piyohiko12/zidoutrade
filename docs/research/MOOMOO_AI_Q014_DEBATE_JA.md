# moomooAI Q014 改善策討論の記録

記録日: 2026-08-14

種別: サニタイズ済み要約（逐語録ではない）

対象: `RSI_AUTOPILOT_V1` の次の研究工程

この文書には口座、注文、残高、position、価格、銘柄、検証期間、個別取引、正確なperformance値、
cookieその他の秘密を含めません。討論中もOpenD、口座、注文にはアクセスしていません。

## 1. 討論の目的

Q013は、同じ小さな履歴標本を見た後に固定した`RESEARCH_ONLY`候補です。見かけの差がごく少数の
除外だけで生じた可能性を区別できないため、次に別の売買条件を加えるべきか、独立した将来データを
残す基盤を先に作るべきかをmoomooAIと3ラウンドで討論しました。

正確な集計はowner-onlyの固定レポートからmoomooAIへ渡しましたが、公開リポジトリへ再掲しません。

## 2. 3ラウンドの進め方

1. **反証**: Q013が将来失敗し得る理由を列挙し、新entry条件、exit/risk変更、将来比較記録の優先順位を
   選ばせました。
2. **回答への赤チーム攻撃**: 第1回答も信頼せず、不適切な検定、事後選択、QFQのpoint-in-time限界、
   local clock、UUID、WAIT/no-fillの脱落、注文API混入を再点検させました。
3. **最終裁定**: ユーザーが固定した同一銘柄・同一selection snapshotを両armへ使い、注文・口座APIを
   0件に固定した上で、次工程を1つだけ選ばせました。

最終裁定は`RECORDER_FIRST`でした。新しい売買条件は加えず、Q013もproductionへ採用しません。

## 3. moomooAI回答から棄却したもの

途中回答には、次の不適切または未検証な提案が混ざりました。これらは取り入れません。

- paper注文を使った比較
- 独立でない小標本へのFisher検定・t検定と、根拠のない分散・検出力の数値
- ランダムUUIDを正本event IDにすること
- local hash chainを外部時刻証明または改ざん防止と呼ぶこと
- ユーザー選択を自動の別universeへ置き換えること
- 現行のversioned fee/fill proxyとは異なる手数料・slippage値を作ること
- 同じ履歴で新しいthresholdを探索すること
- recorder実装前に「比較中」または「改善済み」と表示すること

moomooAIは設計案の反証相手であり、仕様、統計結果、公式API保証の正本ではありません。

## 4. 採用する次工程

次のPR候補を、注文機能と分離した`Q014_PROSPECTIVE_PAIRED_SHADOW_V1` recorder基盤に限定します。
現時点では設計判断だけで、recorderは未実装・未接続です。

### 4.1 事前登録manifest

repo外のowner-only領域へ、上書き不可で次を固定します。

- study/schema ID、開始session、code/protocol hash
- baseline IDと固定済みQ013 ID・式・exact境界・欠損時`WAIT`
- selection policy、calendar、risk policy、fee schedule、virtual execution modelのversion/hash
- analysis population、primary estimand、Stage 1 futility rule、Stage 2単独と200件合算のpromotion rule
- Stage 1=`150`、Stage 2=`50`（件数不足・判定不能時はpromotionしない）
- 途中performance非開示、注文・口座・position・trade API禁止
- 設定変更時は別studyとして0件から開始

### 4.2 pair identityとevent

pair keyは次の3要素をexact fieldとして持つcanonical JSONへdomain separator
`Q014_PAIR_V1\0`を付け、SHA-256で固定します。単純な文字列連結は使いません。

```text
target_session + selected_symbol + selection_record_sha256
```

対象はsignalが出た日だけではなく、selectionが固定された全sessionです。ユーザーが明示的に
`NO_SELECTION`を選んだ対象sessionもledgerへ残し、signalが無いことと記録欠損を区別します。各eventは
少なくとも次を持ちます。

- study ID、sequence、event type、pair key、決定論的event ID
- provider/source timestamp、capture開始・終了時刻、calendar/session hash
- RAW/QFQ入力hash、凍結したadjustment mapping、indicator/input hash
- baselineとQ013それぞれの独立した仮想state・decision・reason code
- `BASELINE_ONLY`、`CANDIDATE_WAIT`、`NO_FILL`、`MISSING_DATA`、`DATA_QUALITY`
- 各対象sessionにexactly oneのterminal record（`SESSION_COMPLETE`または`SESSION_MISSING`）
- fee/risk/execution-model revision、previous-event hash、record hash

event IDはcontent-addressed SHA-256とし、ランダムUUIDを使いません。local hash chainは
`LOCAL_CHAIN_ONLY`であり、local改ざん検知の補助に限定します。外部時刻や実世界の発生を証明したとは
表現しません。

### 4.3 先読み・脱落防止

- 判断は完了済みRTH足と、その時点で保存した入力だけを使用
- 後日取得したQFQで過去indicatorを再計算せず、当時のRAW値とadjustment mappingをimmutable保存
- baseline/Q013は同じselection・確定足・費用・risk revisionを共有
- armごとに仮想positionと1日枠を独立して進め、Q013の`WAIT`後に起きる後続signalも正しく扱う
- missing、stale、不整合は推測せず`WAIT`またはdataset violationとして記録
- WAIT、no-fill、control-only、欠損sessionを後から分母から落とさない
- terminal recordがない対象sessionは成功扱いせずdataset violationとする。0取引sessionも分析集合に残す
- top-of-bookの同時性や実約定を証明できない間は`PROXY_ONLY`と表示

## 5. Stageの意味

Stage 1は候補版の最初の150完結往復までです。境界で事前登録済みのfutility判定を一度だけ行い、
通過しても成功とは呼びません。途中の比較performanceは開示せず、thresholdも変更しません。

Stage 1を通過した場合だけ、その後の時系列的に後続し、Stage 1と重複しない50完結往復をStage 2として
sealします。「独立」は統計的独立を保証する意味ではありません。150件目を含むsessionのclose後に全armと
全eventをsealして収集を停止し、Stage 1判定を固定します。通過時だけ、最初のStage 2対象sessionより前に
O_EXCLのStage 2 activation markerを作成して再開します。

最終判定はStage 2単独と200件合算のmanifestにexact記載した条件を一度だけ適用します。200件は最低限の
governance gateであり、統計的な十分性、時系列的独立性、利益を保証しません。不足・不一致・判定不能は
promotionしない側へ倒します。

## 6. 変更しないもの

- productionのRSI/Q012/exit/risk/ユーザー選択
- Q013の固定`0.0050`と`RESEARCH_ONLY`分類
- long-only、RTH、確定足、先読み禁止、1日1往復
- versioned fee/risk/execution proxy
- runner/broker双方の無条件注文hard stop

recorderは注文、口座、position、trade APIを一切呼びません。Q014のproduction注文への影響は0件です。

## 7. 実装前の停止条件

次のどれかが満たせなければ、recorder開始を許可しません。

- repo外owner-only・O_EXCL manifestとappend-only event/checkpointを安全に作れない
- 同一selection snapshotと共通入力を両armへbindできない
- RAW/QFQとadjustment mappingを同時点証拠として保存できない
- eventの重複、欠落、tail切断、再起動後forkを検出できない
- WAIT/no-fill/control-only/missingを保持できない
- primary estimand、analysis population、futility/promotionのexact式・境界・欠損時動作をmanifestへ固定できない
- Stage 1 sealとStage 2 activation markerの非重複境界を検証できない
- broker、runner、注文・口座APIから構造的に分離できない
- synthetic fault-injectionと独立監査が完了していない
