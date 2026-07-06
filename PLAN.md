# PLAN.md — tw-branch-radar 台股分點雷達

> 本檔為進度主檔。每完成一段即更新「進度追蹤」勾選並 commit+push（雲端環境：存檔＝commit+push）。
> Session 開頭先讀本檔續作。狀態：**Phase 1、2、3A、4、5 ✅ 完成（Actions run #5–#10 實跑驗收）。collector 產出 4 個 JSON：status/ranking/block_trade/market。實測定案：分點非彙總逐筆精算；勝率＝事件+5日close+Wilson（合庫1020 勝率54.8%）；鉅額全市場空stock_id可查+折溢價；大盤 TWSE FMTQIK（鍵 TAIEX/TradeValue/Change…）。Sponsor 上限=6000。Phase 6/7 ✅ 完成（PR #2/#3 併入 main）。Phase 3B 全市場回補 ✅實作（--branches ALL 列舉全部分點＋backfill 三守衛＋branch_daily 聚合 schema），待 Actions 實跑填滿（每 30 分推進、約 3 日）後自動彙總多分點真排行。**

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
- **所需層級（已由實跑證實）**：(a) 整日物件 `use_object` **需 Sponsor Pro**（回 400 "update your user level"）；(b) `TaiwanStockTradingDailyReportSecIdAgg` 在 Sponsor 可用但**須「股+分點」兩者**（只給其一被拒 `... can't be none`），對全市場排行不合用；(c) **非彙總 `TaiwanStockTradingDailyReport` 以 `(securities_trader_id, date)` 查詢在 Sponsor 可用**（官方測試 tests/data/test_data_loader.py 有此實例），回該分點當日各股逐筆買賣。**本專案採 (c) 逐筆精算**。Sponsor 每小時上限實測=6000。
- **參數**：`stock_id`／`securities_trader_id`／`date`（單日；內部 start=end=date）；或 `use_object=True` 直接抓整日物件。
- **欄位（逐字）**：`date`、`stock_id`、`securities_trader_id`（券商代碼）、`securities_trader`（券商名稱）、`price`（成交價）、`buy`（買進股數）、`sell`（賣出股數）。
- **重要口徑**：以 (c) 非彙總報表查詢，同一 (分點,股,日) 有多列（不同成交價），**逐筆精算**淨買超金額＝ Σ(buy×price) − Σ(sell×price)（＝使用者原偏好、較準口徑，且在 Sponsor 可用；整日物件才需 Pro）。
- **輔助 dataset**：
  - `TaiwanSecuritiesTraderInfo`（券商代碼↔名稱對照；欄位 `securities_trader_id,securities_trader,date,address,phone`）。
  - `TaiwanStockTradingDailyReportSecIdAgg`（分點統計表，已預彙總、支援日期範圍；欄位 `date,stock_id,securities_trader_id,securities_trader,buy_volume,sell_volume,buy_price(買進均價),sell_price(賣出均價)`）——**本專案採用之主來源**。實跑證實：**必須以 `securities_trader_id`（分點代碼）查詢**（只給 stock_id 會被拒 `securities_trader_id can't be none`），故逐分點抓；分點代碼清單取自 `TaiwanSecuritiesTraderInfo`。於 Sponsor 可用（已實跑證實）。

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
- **做法**：只用 `TaiwanStockTradingDailyReport`（非彙總），對數個分點（`PHASE1_BRANCHES` 預設 `["1020"]`，官方測試之真實代碼；可 `--branches` 覆寫）以 `(securities_trader_id, date)` 逐日查近 3 個交易日（以 branches[0] 偵測交易日、跳過假日）→ 落地 SQLite（`branch_daily`）→ actions/cache 保存 → 產出**一個 JSON**（逐筆精算 Σ(buy×price)−Σ(sell×price) top N）。同時印 `api_request_limit`（實測=6000）。
- **選此 dataset 理由**：分點是勝率旗艦功能骨幹；SecIdAgg 是 Sponsor 可行且省請求的路徑（逐股一次抓日期範圍）。整日物件因需 Sponsor Pro 已排除。
- **驗收**：(1) DB 有 3 個交易日資料；(2) 重跑不重抓（整窗已涵蓋則 skip，log 顯示）；(3) 輸出 JSON 內含 (分點,股,日,淨買超金額)；(4) log 印出實際每小時上限數字；(5) 單次執行 < 15 分；(6) 確認 SecIdAgg 於 Sponsor 可用（若層級不足會明確報 user level 錯誤）。「關鍵指令」已補進 CLAUDE.md。

### Phase 2 — 增量抓取 + 交易日曆 + 120 日回補
- **做法**：用 `TaiwanStockTradingDate(start,end)`（回單欄 `date` 交易日清單）取代 Phase 1 的探測法，取最近 **120 個交易日**；對 branches × 120 交易日補齊缺的 (分點,日)（`fetched_keys` 判缺、只補缺、空日也標記避免重查）；`branch_daily` schema 沿用。加每次執行請求上限 `MAX_REQ_PER_RUN`（env 可調，安全界：達上限則本次停、下次續跑——為 Phase 3 大宇宙分批回補鋪路）。小宇宙（預設 1020）120 日≈120 請求，單次可完成。輸出 `data/phase2_status.json`（各分點涵蓋交易日數＋回補進度＋top 淨買超）。
- **驗收**：(1) DB 覆蓋 120 交易日（JSON/log 顯示）；(2) 二次執行零重抓（log 全 skip）；(3) 單次 Actions < 15 分；(4) DB 不進 repo；(5) 交易日曆來自 `TaiwanStockTradingDate`（非探測）。

### Phase 3 — 功能 A：勝率分點排行
- **做法**：四參數常數化（`LOOKBACK_DAYS=120`／`EVENT_MIN_AMOUNT=5_000_000`／`HOLD_DAYS=5`／`MIN_EVENTS=10`）。
  - **金額口徑（決定 A，受層級限制）**：本專案 Sponsor 用 SecIdAgg 均價估算 `淨買超金額 = buy_volume×buy_price − sell_volume×sell_price`，資料取自 Phase 1/2 落地的 `branch_daily_agg`。逐筆精算 `Σ(buy×price)−Σ(sell×price)` 需 Sponsor Pro（升級後可無痛切換為更精確來源）。
  - 事件抽取：某 (分點,股,日) 淨買超金額 ≥ 500 萬計 1 次事件；以 `TaiwanStockPrice` `close` 判定「事件日+5 交易日 close > 事件日 close」為勝；每分點事件數 ≥ 10 才列入；近 5 交易日未到期事件標 pending 不計。
  - **排序（已確認升級）**：不用原始勝率排序，改用**勝率的 Wilson 分數下界**（95%）排序，並在輸出一律帶 `events`(N)、`wins`、`win_rate`、`wilson_lb`，避免「10 場 8 勝」小樣本灌水贏過「200 場 62%」。門檻值四參數不變。
  - 輸出 `data/ranking.json`。
- **v1.1 建議（先不做，記錄待議）**：勝負改「相對加權指數的超額報酬」（事件後 5 日個股報酬 > 同期大盤報酬才算勝），以剔除多頭市場的 beta 假象；門檻可評估改「佔當日成交額 X%」相對值以濾除權值股雜訊。
- **驗收**：(1) 排行可重現；(2) 改四參數任一，輸出隨之變動；(3) 附**一筆手算驗證樣本**（單一分點單一事件的金額與+5日勝負人工核對相符）；(4) 事件數 <10 的分點確實被排除；(5) 輸出每列含 N 且排序依 `wilson_lb`（同勝率不同 N 者順序正確）。

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
- **做法**：每日 cron；`FINMIND_TOKEN` 走 secrets；actions/cache 保存 SQLite；每日產 `data/*.json` 並 commit；**部署走公開 GitHub Pages（已確認）**——repo 需 public，`data/*.json` 與 `index.html` 對外可見，故禁區（token 不外洩、原始逐筆明細不進 repo）更須嚴守；公開個人面板屬 FinMind 授權之個人非商業用途，頁尾維持來源標示。
- **驗收**：(1) 排程綠燈且產物更新；(2) 單次 < 15 分；(3) TOKEN 不外洩（掃 commit／log）；(4) 原始明細未進 repo；(5) 授權標示（FinMind、非商業）在頁尾與 README。

---

## 已確認決定（本輪）
1. **勝率四參數**：✔ 維持預設 `120 交易日／單日淨買超 ≥ 500 萬／持有 5 交易日／事件數 ≥ 10`（皆常數化可隨時改）。另採兩項「不改參數、只改排序/勝負口徑」的升級：排序改 Wilson 下界＋一律顯示事件數 N（見 Phase 3）；「超額報酬」勝負與相對成交額門檻列 v1.1 待議。
2. **部署**：✔ 公開 GitHub Pages（repo 需 public；禁區更須嚴守，見 Phase 7）。
3. **勝率「買超金額」計算口徑**：✔ **逐筆精算** `Σ(buy×price)−Σ(sell×price)`，資料取自非彙總 `TaiwanStockTradingDailyReport`（以分點+日期查詢，Sponsor 可用）。（決定 A 的 SecIdAgg 均價估算已被實跑推翻：SecIdAgg 須股+分點兩者、對排行不合用；逐筆精算反而在 Sponsor 可行，即使用者原偏好、較準。）
4. **勝率涵蓋範圍**：SecIdAgg 為**逐分點**查詢（securities_trader_id），需列舉分點宇宙——分點清單取自 `TaiwanSecuritiesTraderInfo`（Phase 1 取樣 5 個分點，Phase 3 擴至全部分點）；功能 C 追蹤清單另給小清單——**待使用者提供追蹤清單內容**（Phase 5 前再定，不擋 Phase 1）。
5. **大盤替代來源**：✔ 採 TWSE FMTQIK（免 token）取代 FinMind 缺項。

---

## 未查證 TODO（Actions 實測補齊）
- [x] FinMind Sponsor 每小時上限＝**6000**（2026-07-05 `user_info.api_request_limit` 實測）。
- [x] 整日物件 `use_object` 層級＝**需 Sponsor Pro**（實跑回 `400 "update your user level"`）。
- [x] `TaiwanStockTradingDailyReportSecIdAgg`：Sponsor 可用但**須「股+分點」兩者**（先後回 `securities_trader_id can't be none`、`data_id can't be none`）→ 對排行不合用，改用非彙總報表。
- [x] `TaiwanSecuritiesTraderInfo` 於 Sponsor **可用**（run #4 回分點清單，如 075T/087T/1020…）；Phase 3 全市場列舉可用它。
- [x] 非彙總 `TaiwanStockTradingDailyReport` 以 `(securities_trader_id, date)` 查詢在 Sponsor **可用**（run #5/#6 實跑成功，欄位 `date,stock_id,securities_trader_id,securities_trader,price,buy,sell` 逐字吻合；buy/sell 為股數、量級合理）。
- [ ] 抽查 2026-07-03 聯發科(2454) 分點1020 淨買超≈1.2 億（隱含均價偏高，Phase 3 對照當日 close 確認無異常）。
- [ ] Phase 3 門檻檢討：活躍分點(如 1020)單股單日淨買超常達數千萬~億，500 萬門檻可能偏低、事件過多——Phase 3 視分佈微調（四參數仍可調）。
- [ ] 非彙總報表逐 (分點,日) 在全市場規模的請求數/耗時（Phase 2/3；6000/hr 下 ~千分點×120日需分批回補）。
- [x] `TaiwanStockPrice` 於 Sponsor **可用**（run #9 抓 443 檔成功，欄位含 close）。`TaiwanStockTotalReturnIndex` 待 Phase 5 確認。
- [x] TWSE FMTQIK 欄位鍵（run #10 實測）：`Date, TradeVolume(成交股數), TradeValue(成交金額), Transaction(成交筆數), TAIEX(發行量加權股價指數), Change(漲跌點數)`；OpenAPI 僅回最近 3 筆。
- [x] `TaiwanStockBlockTrade` 空 stock_id **可查全市場**（run #10 得 41 筆；欄位 date/price/stock_id/trade_type/trading_money/volume）。
- [ ] SecIdAgg 逐股查詢在全市場規模的請求數與耗時（驗證 15 分預算；Phase 2/3）。

---

## 進度追蹤
- [x] Phase 0 資料查證（本檔資料清單）
- [x] Phase 1 最小垂直切片 ✅（Actions run #5 冷跑抓 07-01/02/03 共 5007/4473/5626 列、run #6 熱跑增量零重抓；印出 api 上限=6000；輸出 data/phase1_sample.json top50；單次 <15 分；三次失敗迭代已釐清正確 dataset 取法）
- [x] Phase 2 增量 + 交易日曆 + 120 日回補 ✅（run #7：`TaiwanStockTradingDate` 取 120 交易日、分點 1020 回補全涵蓋、6 分鐘 <15 分；run #8：重跑零重抓；分批續跑機制離線＋設計驗證）
- [~] Phase 3 功能 A 勝率排行
  - 3A 演算法 ✅ run #9：合庫1020 勝率54.8%/Wilson0.532/事件3693。
  - 3B 全市場 ✅實作：`--branches ALL` 由 `TaiwanSecuritiesTraderInfo` 列舉全部分點；`backfill` 三守衛（`MAX_REQ_PER_RUN` 對數上限／`RUN_BUDGET_SEC` 牆鐘 660s／`QUOTA_MARGIN` api 剩餘 300）皆可續跑（增量零重抓）；`branch_daily` 改**聚合 schema**〔(分點,股,日) 買賣金額/股數，逐筆價位列寫入前 GROUP BY 折算，~20× 壓縮〕使全市場（實測 1020 單分點 120 日=60 萬逐筆列 → 聚合後大減）裝得進 actions/cache；回補未完成僅純回補、寫 `remaining.txt` 供 workflow gate（不 commit、不彙總）。cache key v2；離線測試 phase3/phase3b 全綠。
  - **待**：Actions 實跑全市場回補（每 30 分推進 07–23 UTC，約 3 日填滿 120 日）→ `remaining=0` 後自動彙總多分點真排行並 commit。
- [x] Phase 4 功能 B 鉅額看板 ✅（run #10：全市場 41 筆＋折溢價，block_trade.json）
- [x] Phase 5 功能 C 成交資訊 ✅（run #10：追蹤 7 檔價量＋TWSE 大盤，market.json）
- [x] Phase 6 功能 D HTML 面板 ✅（index.html 單檔讀 4 JSON、手機優先、台股紅漲綠跌、缺檔降級＋內嵌示意；Playwright 實測渲染五區塊正常。真實資料待 Phase 7 commit 產物＋Pages）
- [~] Phase 7 Actions 排程 + 部署（每日 cron 12:00 UTC＋commit data/*.json **已實跑驗證**：run #11 bot commit「chore(data)」成功、真實 4 JSON 進 repo；面板讀真實資料渲染正常。**剩使用者動作**：Settings→Pages 啟用公開 Pages(main)＋合併分支到 main 讓 cron 生效）

---

## 授權與來源標示（必遵守）
- 僅限個人非商業用途；面板頁尾與 README 固定標示「資料來源：FinMind」（大盤另標 TWSE）。
- 商業用途需 Sponsor Pro，本專案不得作商業使用。
- FINMIND_TOKEN 只走 Actions secrets；原始分點逐筆明細只留 SQLite cache，不 commit 進 repo。
</content>
</invoke>
