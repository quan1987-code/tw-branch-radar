# tw-branch-radar 台股分點雷達

每日自動更新的台股資訊面板：高勝率券商分點排行、鉅額交易買賣、大盤與個股成交資訊。

## 技術棧
Python + SQLite（actions/cache 保存）+ FinMind API（Sponsor 方案）+ GitHub Actions 每日排程 + 單檔 HTML 讀 `data/*.json`。

## 執行方式
1. 安裝依賴：`pip install -r requirements.txt`（Python 3.11）。
2. 設定 FinMind token（只走環境變數）：`export FINMIND_TOKEN=<你的 token>`。
3. 抓取並彙總：`python collector.py`。
4. 雲端：GitHub Actions workflow「collector (Phase 1)」手動觸發，需先設 repo secret `FINMIND_TOKEN`。

進度與規劃見 `PLAN.md`。

## 授權與資料來源
- 資料來源：**FinMind**（https://finmind.github.io/）。大盤成交資訊另採臺灣證券交易所（TWSE）公開資料。
- 僅限個人非商業用途；商業用途需 FinMind Sponsor Pro，本專案不得作商業使用。
