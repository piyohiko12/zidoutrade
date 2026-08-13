# moomooAI Q012 選定・売買条件見直し記録

記録日: 2026-08-13
種別: サニタイズ済み要約（逐語録ではない）
対象: `RSI_AUTOPILOT_V1` の研究用ルール

この文書には口座、注文、残高、position、価格、PnL、cookieその他の秘密を含めません。
moomooAIへの質問と公式仕様の確認は情報取得だけで、OpenD、口座、注文にはアクセスしていません。

## 1. 質問の要約

次の既存設計を前提に、候補適格性と売買条件を過学習、look-ahead、流動性、費用、gap、
データ欠損の観点から見直し、productionに直ちに固定できる条件と、検証が必要な仮説を分けるよう
moomooAIへ質問しました。

- ユーザーが適格候補から翌営業日の1銘柄を選ぶ（利益予測ランキングなし）
- RTHの終了済み15分足、rolling Wilder RSI(14)、ATR(14)
- RSIが直近3本で30以下、1本前35以下から最新35超へ回復
- 最新closeが1本前highを突破、日足trend、SPY trend、流動性、spreadを全件gate
- long-only、最大1往復、費用・slippage込みrisk sizing
- 新規登録、契約、trial、課金、権限追加、REAL注文を行わない

## 2. moomooAI回答の要約

回答は、既存のRSI反発とtrendの組合せを残したうえで、次の定量条件を検討する案でした。

1. signal確定足のvolumeを、その直前13本の確定15分足volume中央値の1.5倍以上にする。
2. bid/ask midpoint基準spreadを0.10%以下に縮める。
3. high突破後の追い掛けを、`(signal close - prior high) / prior high <= 0.50%`に制限する。
4. ATR/priceを0.02%–1.50%に限定する案を検討する。
5. turnover rate 0.50%以上を候補条件または並べ替えに使う案を検討する。

これは戦略仮説であり、回答自体にこの戦略固有のprospective OOS、費用控除後利益、勝率、
threshold感応度の証拠はありません。

## 3. 公式仕様との照合

- [Get Historical Candlesticks](https://openapi.moomoo.com/moomoo-api-doc/en/quote/request-history-kline.html)
  は、K線の時刻、OHLC、volume、turnover、turnover rateを返し、US株のsessionと
  extended-time指定を持ちます。したがってRTH確定足のvolumeを入力として取得できることは確認できます。
  ただし「13本」「1.5倍」や収益性は公式仕様の主張ではありません。
- [Get Market Snapshot](https://openapi.moomoo.com/moomoo-api-doc/en/quote/get-market-snapshot.html)
  はupdate time、ask price、bid price、security status等を返します。bid/askからmidpoint spreadを
  計算できることは確認できますが、10 bpが安全または有利だという性能根拠ではありません。
- [Filter Stocks by Condition](https://openapi.moomoo.com/moomoo-api-doc/en/quote/get-stock-filter.html)
  はturnover rateのfilter/sortと返却値を定義します。これは技術的な利用可能性の確認に限られ、
  0.50%というthresholdや、ユーザー選択より良い順位を保証しません。

公式仕様が確認するのはfield、単位、API契約です。欠損、形成中bar、権限不足、timestamp、RTH系列、
adjustment、freshnessはアプリ側で厳格に検証し、曖昧なら`WAIT`にします。

## 4. 採用判断

### productionの純粋判断層へ採用

- 直前13本を必須にしたvolume中央値×1.5（不足、0、欠損は`WAIT`）
- midpoint spread 10 bp以下（境界を含む）
- prior high突破幅0.50%以下（境界を含む。参照high不正は`WAIT`）

これらは利益改善を前提にせず、弱い出来高、広いspread、過度な追い掛けを機械的に拒否する
保守的gateとして採用しました。既存のrolling Wilder RSI、RTH-only、現セッション最新確定足、
ユーザー選択、非ランキング設計は維持します。

### `RESEARCH_ONLY`

- ATR/price 0.02%–1.50%
- turnover rate 0.50% gateまたはturnoverによる並べ替え

数値の根拠とthreshold感応度がなく、turnover並べ替えは現在の20日売買代金中央値gateおよび
ユーザーが非ランキング候補から選ぶ責任分担と競合します。事前登録したshadow/OOS検証が終わるまで
productionへ入れません。

## 5. 安全上の限界

moomooAIは仮説の情報源であり、投資助言、収益性、勝率、再現性の証明ではありません。
1回・1日・1週間の割合上限は、新規entry停止とposition sizingのための設定であり、gap、
slippage、取引停止、通信・broker障害により実損失が上回らないことを保証しません。
現在の公開版は引き続き注文RPC直前で無条件停止し、この見直しで注文機能は有効になりません。
