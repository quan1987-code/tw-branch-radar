# PLAN.md — tw-branch-radar 台股分點雷達

> 本檔為進度主檔。每完成一段即更新「進度追蹤」勾選並 commit+push（雲端環境：存檔＝commit+push）。
> Session 開頭先讀本檔續作。狀態：**規劃階段（Phase 0 已執行，等使用者確認後才進 Phase 1）**。

---

## Phase 0 — 資料查證（規劃時已執行）

### 查證方法與可信度聲明（重要）
- 本 sandbox 的對外 egress 政策**封鎖** `finmind.github.io`、`api.finmindtrade.com`、`finmindtrade.com`、`openapi.twse.com.tw`、`www.tpex.org.tw`（實測 CONNECT 403）。故**無法在規劃階段直接抓官方 tutor 文件頁做即時驗證**。
- 替代作法：改用**官方 Python 客戶端原始碼**驗證 dataset 名稱與欄位——`pip download finmind==1.9.12`（PyPI，允許清單內）解壓後讀 `FinMind/schema/data.py`（Dataset enum）與 `FinMind/data/data_loader.py`（各方法 docstring 列出精確欄位）。這是官方發行的 client，比二手部落格權威。**dataset 名稱、欄位、參數皆逐字取自此原始碼，非憑記憶**。
- 會員層級與 API 每小時上限的「精確數字」不在 client 原始碼內，官方 pricing 頁又被封鎖，故以 WebSearch 佐證並標為「未查證 TODO」，在 Phase 1 於 GitHub Actions（runner 可達這些 host）以 `user_info` 端點實測確認。
- **runtime 可行性不受本 sandbox 封鎖影響**：實際管線在 GitHub Actions 執行，runner 具開放網路，可達 FinMind／TWSE 各 host。

### 官方文件 URL（canonical，正常網路／Actions 可達）
- 總覽：https://finmind.github.io/tutor/TaiwanMarket/DataList/
- 籌碼面（分點、鉅額）：https://finmind.github.io/tutor/TaiwanMarket/Chip/
- 技術面（個股價量、指數）：https://finmind.github.io/tutor/TaiwanMarket/Technical/
- 指數代碼表：https://finmind.github.io/tutor/TaiwanMarket/IndexCodes/
- 驗證所用原始碼：https://github.com/FinMind/FinMind （PyPI `finmind==1.9.12`，`FinMind/data/data_loader.py`）
- 資料 API 端點：`https://api.finmindtrade.com/api/v4/data`（參數 `dataset,data_id,start_date,end_date,token`）
- 用量／上限查詢：`https://api.web.finmindtrade.com/v2/user_info`（回 `user_count` 已用、`api_request_limit` 上限）
- 認證：HTTP header `Authorization: Bearer <FINMIND_TOKEN>`

---

### 資料清單（四類）

#### 1. 券商分點每日買賣明細（勝率計算底層）
- **Dataset**：`TaiwanStockTradingDailyReport`（當日券商分點表）
- **文件**：https://finmind.github.io/tutor/TaiwanMarket/Chip/
- **所需層級**：整日一次下載（`use_object=True`，signed-URL parquet 整日全券商×全股）屬**付費／Sponsor 專屬**功能（WebSearch 佐證「一次取回某日全部股票需付費會員」）。逐股／逐分點查詢的免費層級細節＝**未查證 TODO**。本專案有 Sponsor，採整日下載。
- **參數**：`stock_id`／`securities_trader_id`／`date`（單日；內部 start=end=date）；或 `use_object=True` 直接抓整日物件。
- **欄位（逐字）**：`date`、`stock_id`、`securities_trader_id`（券商代碼）、`securities_trader`（券商名稱）、`price`（成交價）、`buy`（買進股數）、`sell`（賣出股數）。
- **重要口徑**：同一 (分點,股,日) 會有多列（不同成交價）。單日某分點對某股「淨買超**金額**」＝ Σ(buy×price) − Σ(sell×price)。
- **輔助 dataset**：
  - `TaiwanSecuritiesTraderInfo`（券商代碼↔名稱對照；欄位 `securities_trader_id,securities_trader,date,address,phone`）。
  - `TaiwanStockTradingDailyReportSecIdAgg`（分點統計表，已預彙總、支援日期範圍；欄位 `date,stock_id,securities_trader_id,securities_trader,buy_volume,sell_volume,buy_price(買進均價),sell_price(賣出均價)`）——為 Phase 3 金額口徑的備選（見「需確認決定」）。

#### 2. 鉅額交易（盤後鉅額買賣）
- **Dataset**：`TaiwanStockBlockTrade`（鉅額交易日成交資訊，逐筆）
- **文件**：https://finmind.github.io/tutor/TaiwanMarket/Chip/
- **所需層級**：**Sponsor**（client docstring 明載「（逐筆，Sponsor）」——已確認，非推測）。
- **參數**：`stock_id`、`start_date`、`end_date`（支援日期範圍）。
- **欄位（逐字）**：`date`、`stock_id`、`trade_type`（交易別）、`price`（成交價）、`volume`（成交股數）、`trading_money`（成交金額）。
- **用途對映功能 B**：鉅額 `price` 對當日個股 `close` 算折溢價。

#### 3. 個股每日成交價量
- **Dataset**：`TaiwanStockPrice`（台灣股價資料表）
- **文件**：https://finmind.github.io/tutor/TaiwanMarket/Technical/
- **所需層級**：一般（免費）層級可取＝**未查證 TODO**（Phase 1 實測確認）。
- **參數**：`stock_id`、`start_date`、`end_date`。
- **欄位（逐字）**：`date`、`stock_id`、`Trading_Volume`（成交量/股數）、`Trading_money`（成交金額）、`open`、`max`、`min`、`close`、`spread`（漲跌幅）、`Trading_turnover`（成交筆數）。
- **用途**：功能 A 需 `close`（事件日 vs +5 交易日）；功能 B 需 `close`（折溢價基準）；功能 C 追蹤清單價量。

#### 4. 大盤／加權指數成交資訊
- **FinMind 現況**：**無**「每日加權股價指數收盤＋大盤成交量值」的直接 dataset。既有 `TaiwanStockTotalReturnIndex`（加權/櫃買**報酬**指數，含息、**無成交量**；欄位 `date,stock_id,price`，index_id `TAIEX`/`TPEx`）與 `TaiwanStockEvery5SecondsIndex`（**產業別**每5秒指數，data_id 為產業代碼如 `Automobile`，盤中過細）皆不符「大盤價量」需求。
- **替代來源（TWSE 公開資料，依鐵律採替代並註明）**：TWSE OpenAPI「每日市場成交資訊」FMTQIK。
  - 端點：`https://openapi.twse.com.tw/v1/exchangeReport/FMTQIK`
  - 文件：https://openapi.twse.com.tw/ （Swagger）／ https://www.twse.com.tw/zh/trading/historical/fmtqik.html
  - 欄位（WebSearch，**未逐一實測，Phase 5 於 Actions 確認**）：日期、成交股數、成交金額、成交筆數、發行量加權股價指數（收盤指數）、漲跌點數。
  - 所需層級：TWSE 公開資料，**免 token**。
- **輔助（趨勢線可選）**：FinMind `TaiwanStockTotalReturnIndex`（報酬指數走勢，補充用）。
- **上櫃（如需要）**：TPEX OpenAPI `https://www.tpex.org.tw/openapi/`（免 token）。

---

## 架構約束（沿用既有 PWA 成功模式）
- **增量抓取**：以交易日曆（FinMind `TaiwanStockTradingDate`）判缺，只補缺的交易日；重跑不重抓。
- **SQLite 用 actions/cache 保存**：原始逐筆分點明細**只留 SQLite cache、不 commit 進 repo**；repo 只放 `data/*.json` 彙總結果。
- **單次 Actions ≤ 15 分鐘**（歷史教訓：曾有 37 分鐘逾時）。關鍵設計：分點用整日物件下載（`use_object`），120 交易日回補≈120 次請求、每日增量 1 次，而非逐股數千次。
- **FINMIND_TOKEN 只從環境變數讀**（Actions secrets）；不得出現在程式碼或任何 commit。

---

## 分段規劃（7 段，每段有可判定驗收）

### Phase 1 — 最小垂直切片（單一 dataset，證明整條管線通）★必為最小切片
- **做法**：只用 `TaiwanStockTradingDailyReport`（整日物件 `use_object`）抓**3 個交易日** → 落地 SQLite（schema：raw 分點列）→ 以 actions/cache 保存 DB → 產出**一個 JSON**（當日各分點對個股「淨買超金額」彙總 top N）。同時呼叫 `user_info` 印出 `api_request_limit`（實測 Sponsor 上限）。
- **選此 dataset 理由**：它是勝率旗艦功能的骨幹，也是最大技術風險（Sponsor 整日物件、資料量、15 分預算），最該最早證明。
- **驗收**：(1) DB 有 3 個交易日資料；(2) 重跑同日不重抓；(3) 輸出 JSON 內含 (分點,股,日,淨買超金額)；(4) log 印出實際每小時上限數字；(5) 單次執行 < 15 分。同時把「關鍵指令」補進 CLAUDE.md。

### Phase 2 — 增量抓取 + 交易日曆 + 120 日回補
- **做法**：用 `TaiwanStockTradingDate` 建交易日曆；實作「只補缺日」增量；DB schema 定稿（分點 raw + 交易日曆表）；回補最近 **120 交易日**分點資料。
- **驗收**：(1) DB 覆蓋 120 交易日；(2) 二次執行零重抓（log 顯示 skip）；(3) 單次 Actions < 15 分；(4) DB 不進 repo（.gitignore 驗證）。

### Phase 3 — 功能 A：勝率分點排行
- **做法**：四參數常數化（`LOOKBACK_DAYS=120`／`EVENT_MIN_AMOUNT=5_000_000`／`HOLD_DAYS=5`／`MIN_EVENTS=10`）。事件抽取：某 (分點,股,日) 淨買超金額 ≥ 500 萬計 1 次事件；以 `TaiwanStockPrice` `close` 判定「事件日+5 交易日 close > 事件日 close」為勝；每分點事件數 ≥ 10 才列入；勝率＝勝/事件。近 5 交易日未到期事件標 pending 不計。輸出 `data/ranking.json`。
- **驗收**：(1) 排行可重現；(2) 改四參數任一，輸出隨之變動；(3) 附**一筆手算驗證樣本**（單一分點單一事件的金額與+5日勝負人工核對相符）；(4) 事件數 <10 的分點確實被排除。

### Phase 4 — 功能 B：鉅額交易看板
- **做法**：`TaiwanStockBlockTrade` 抓當日鉅額逐筆；對當日 `TaiwanStockPrice` `close` 算折溢價％＝(price−close)/close；輸出 `data/block_trade.json`（列表＋折溢價，含買賣別 `trade_type`）。
- **驗收**：(1) 當日鉅額列表完整；(2) 折溢價正負號正確（price>close 為溢價）；(3) 無鉅額交易日輸出空列表不報錯。

### Phase 5 — 功能 C：成交資訊（大盤＋追蹤清單）
- **做法**：追蹤清單個股用 `TaiwanStockPrice` 出當日價量摘要；大盤用 **TWSE FMTQIK**（加權股價指數收盤＋成交金額＋漲跌點數），Phase 5 於 Actions 實測確認欄位；輸出 `data/market.json`。頁尾資料來源需加註 TWSE。
- **驗收**：(1) 大盤加權指數收盤與成交金額與 TWSE 官網當日 FMTQIK 公告數值逐位相符（同日比對，誤差 0）；(2) 追蹤清單每檔有 open/max/min/close/量；(3) TWSE 欄位對映已在程式註解記錄實測結果。

### Phase 6 — 功能 D：單檔 HTML 面板
- **做法**：`index.html` 讀 `data/*.json` 渲染三區塊（勝率排行／鉅額看板／成交資訊），手機優先（RWD），頁尾固定「資料來源：FinMind（大盤成交資訊：臺灣證券交易所 TWSE）」。HTML 超過 300 行用 `<!-- SECTION: -->` 錨點。
- **驗收**：(1) 手機視圖三區塊皆可讀；(2) 純讀本地 JSON 即可渲染（無後端）；(3) 頁尾來源標示齊全；(4) JSON 缺檔時有降級提示不白屏。

### Phase 7 — GitHub Actions 每日排程 + 部署
- **做法**：每日 cron；`FINMIND_TOKEN` 走 secrets；actions/cache 保存 SQLite；每日產 `data/*.json` 並 commit；部署（Pages 或私有，見「需確認決定」）。
- **驗收**：(1) 排程綠燈且產物更新；(2) 單次 < 15 分；(3) TOKEN 不外洩（掃 commit／log）；(4) 原始明細未進 repo；(5) 授權標示（FinMind、非商業）在頁尾與 README。

---

## 需使用者確認的決定
1. **勝率四參數是否調整**：預設 `120 交易日／單日淨買超 ≥ 500 萬／持有 5 交易日／事件數 ≥ 10`。是否照舊？（皆已常數化，可隨時改）
2. **部署走公開 GitHub Pages 或維持私有**：影響是否對外可見與 CI 設定。
3. **勝率排行的個股與分點涵蓋範圍**：全市場（分點整日物件天然涵蓋全市場，但 +5 日 close 需對應個股 120 日 `TaiwanStockPrice`，全市場資料量較大）vs 指定追蹤清單／高流動性子集。建議：勝率用全市場，功能 C 追蹤清單另給一份小清單。
4. **勝率「買超金額」計算口徑**：用 `TaiwanStockTradingDailyReport` 逐筆精算 Σ(buy×price)−Σ(sell×price)（建議，精確）vs 用 `TaiwanStockTradingDailyReportSecIdAgg` 均價估算（資料量小、支援日期範圍，但為均價近似）。
5. **大盤替代來源確認**：因 FinMind 無現成大盤價量，改用 **TWSE FMTQIK**（免 token）。確認採用。

---

## 未查證 TODO（Phase 1 於 Actions 實測補齊）
- [ ] FinMind 各層級每小時請求上限精確值（`user_info.api_request_limit` 實測；WebSearch 概估：未註冊 300／免費+token 600／Sponsor 更高、另有新 Sponsor Pro）。
- [ ] `TaiwanStockTradingDailyReport` 逐股／逐分點查詢的免費層級門檻（整日物件已確定需付費）。
- [ ] `TaiwanStockPrice`／`TaiwanStockTotalReturnIndex` 是否免費層級可取。
- [ ] TWSE FMTQIK 回傳 JSON 的實際欄位鍵名與型別（Phase 5 實測對映）。
- [ ] 分點整日物件單日資料量與下載耗時（驗證 15 分預算）。

---

## 進度追蹤
- [x] Phase 0 資料查證（本檔資料清單）
- [ ] Phase 1 最小垂直切片
- [ ] Phase 2 增量 + 交易日曆 + 120 日回補
- [ ] Phase 3 功能 A 勝率排行
- [ ] Phase 4 功能 B 鉅額看板
- [ ] Phase 5 功能 C 成交資訊
- [ ] Phase 6 功能 D HTML 面板
- [ ] Phase 7 Actions 排程 + 部署

---

## 授權與來源標示（必遵守）
- 僅限個人非商業用途；面板頁尾與 README 固定標示「資料來源：FinMind」（大盤另標 TWSE）。
- 商業用途需 Sponsor Pro，本專案不得作商業使用。
- FINMIND_TOKEN 只走 Actions secrets；原始分點逐筆明細只留 SQLite cache，不 commit 進 repo。
</content>
</invoke>
