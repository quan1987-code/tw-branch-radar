# CLAUDE.md — <專案名>
## 鐵律（與全域治理一致）
1. 禁止整檔重寫既有檔案，一律精準編輯；讀大檔先 grep -n 定位。
2. 版本號、API 端點、欄位名不憑記憶填；查不到就標「未查證 TODO」，不編造。
3. 超過 3 步先寫 PLAN.md 並逐段存檔更新進度；session 開頭先找 PLAN.md 續作。（雲端環境：存檔＝commit+push）

## 專案事實
- 用途：＿＿（一句話）
- 技術棧：＿＿（例：Python + SQLite + FinMind API + GitHub Actions + 單檔 HTML）
- 關鍵指令：本機測試＝＿＿；部署＝＿＿
- 結構：＿＿（主要檔案各一行說明）
- 禁區：＿＿（例：不得動 data/ 下的歷史快取；不得改 Actions 的 cron）

## 慣例
- 回覆用繁體中文；HTML 超過 300 行用 <!-- SECTION: --> 錨點註解。
