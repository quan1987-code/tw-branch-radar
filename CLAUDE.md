# CLAUDE.md — tw-branch-radar 台股分點雷達
## 鐵律（與全域治理一致）
1. 禁止整檔重寫既有檔案，一律精準編輯；讀大檔先 grep -n 定位。
2. dataset 名稱、API 端點、版本號、欄位名不憑記憶填；查不到標「未查證 TODO」，不編造。
3. 超過 3 步先寫 PLAN.md 並逐段存檔更新進度；session 開頭先找 PLAN.md 續作。（雲端環境：存檔＝commit+push）

## 專案事實
- 用途：每日自動更新的台股資訊面板——高勝率券商分點排行、鉅額交易買賣、大盤與個股成交資訊。
- 技術棧：Python + SQLite（用 actions/cache 保存）+ FinMind API（Sponsor 方案）+ GitHub Actions 每日排程 + 單檔 HTML 讀 data/*.json。
- 關鍵指令：
  - 安裝依賴：`pip install -r requirements.txt`（Python 3.11、pandas<2）。
  - 抓取＋彙總：`FINMIND_TOKEN=<token> python collector.py`（token 只走環境變數；可加 `--days N --anchor YYYY-MM-DD`）。
  - 雲端執行：GitHub Actions workflow「collector (Phase 1)」手動 `workflow_dispatch`；需先設 repo secret `FINMIND_TOKEN`；產物為 artifact `phase1-data`（data/*.json）。
  - SQLite cache 路徑：`.cache/branch.db`（actions/cache 保存，不 commit）。
- 結構：collector.py（抓取＋勝率計算）、data/*.json（彙總輸出）、index.html（面板）、PLAN.md（進度）。
- 禁區：FINMIND_TOKEN 只走 Actions secrets，不得出現在程式碼或任何 commit；
  原始分點逐筆明細不得 commit 進 repo（只留 SQLite cache，repo 只放彙總結果）。

## 授權（FinMind Sponsor，必遵守）
- 僅限個人非商業用途；面板頁尾與 README 固定標示「資料來源：FinMind」。
- 商業用途需 Sponsor Pro，本專案不得作商業使用。

## 慣例
- 回覆用繁體中文；HTML 超過 300 行用 <!-- SECTION: --> 錨點註解。
