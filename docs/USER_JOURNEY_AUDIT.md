# Pawcorder — 白痴使用者旅程審視

模擬一位**完全非技術背景**的買家從零開始用 pawcorder 的全程,
找出每個會讓她放棄的卡關點。日期:2026-05-02。

---

## 人物誌

**林雅婷,38 歲,設計助理。**

- 台北租屋族,養一隻八歲橘貓「小橘」
- 過去用 Wyze 但被資安新聞嚇到
- 家裡有一台 Synology DS220+(只用過內建相簿)
- 主力裝置:iPhone 14、MacBook Air(只用瀏覽器跟 Office)
- 不知道 Docker、沒開過 terminal、看到 `curl | bash` 會問「會不會中毒」
- 上限:能複製貼上指令、會在路由器後台改 Wi-Fi 密碼
- 動機:白天上班焦慮小橘狀況,願意一次花兩萬解決,但**極度抗拒月費**
- 主要溝通:LINE

---

## Stage 1 · 發現產品

### 雅婷在做什麼
朋友在 Dcard 分享或在 IG 看到貼文。打開連結。

### 卡關點

1. **`pawcorder.app` / `get.pawcorder.io` 都還沒上線**(OPERATIONS.md
   🔴 任務 1, 12)。連結會 404,雅婷會直接關掉。
2. **marketing/index.html 還沒部署到 GitHub Pages 或 Cloudflare Pages**。
3. **README 跟 marketing 的 GitHub 連結指向 `xupStudio/pawcorder`,
   但實際 remote 是 `xupStudio/pawcorder-pro`** — CI 徽章、`curl | bash`、
   `git clone` URL 全部 404。

### 改善
- **A1** 修一致性:選 `pawcorder` 或 `pawcorder-pro` 一個,把 README /
  scripts/bootstrap.sh / install.ps1 的 URL 全部改齊
- **A2** 部署 marketing(Cloudflare Pages → 連 GitHub repo,自動部署
  `marketing/` 目錄)
- **A3** 在 marketing hero 區塊放一個明確的「我不會技術 → 看這裡」
  的 CTA,連到一個簡化版安裝指南(不是 `curl | bash`)

---

## Stage 2 · 評估購買

### 雅婷在做什麼
看完 hero,滑下來想知道「這個會不會偷看我家?」「我家貓白天我看得到嗎?」

### 卡關點

4. **README 結構是 feature-first(功能列表)而非 problem-first**。雅婷
   的問題沒被先回答 — 她想知道:
   - 我家網路安全嗎?(隱私)
   - 影片誰看得到?(privacy 設計)
   - 故障了我打給誰?(支援)
   - 我家貓躲起來看不到怎麼辦?(多攝影機 = 多孔位)
5. **比較表格用「Furbo / Wyze / Apple HKSV / UniFi Protect / pawcorder」**
   雅婷只認得 Furbo 跟 Wyze。其他兩個她沒聽過 → 加深「這是給技術人用的」感覺
6. **沒有實際使用情境的影片或 GIF** — 30 秒看到「貓走進廚房 → LINE
   收通知 → 點開看影片」會比 1000 字功能列表有效十倍

### 改善
- **B1** README 開頭加一個「常見疑問」區塊,3-5 題用她的話問:
  「會不會被駭?」「我家網路爛 video lag 嗎?」「壞掉怎麼辦?」
- **B2** 比較表格只留 Furbo + Wyze + pawcorder 三欄(雅婷認得的)
- **B3** 在 marketing 加一段 30 秒 demo 影片或動圖(從錄到 LINE 通知
  全流程)

---

## Stage 3 · 買硬體

### 雅婷在做什麼
決定要買。看 README 的硬體表,計算 NT$。

### 卡關點

7. **沒有去哪買的連結**。N100 的「Beelink / GMKtec / MINISFORUM」
   雅婷不知道在 PChome 還是 Shopee 還是露天能買到。
8. **PoE / Cat6 / DHCP reservation 是術語**。README 用「PoE switch」
   「Cat6 patch cable」「DHCP reservation」沒解釋。雅婷不會配線。
9. **沒有「不想自己買零件」的選項**。應該推 "等預組好的硬體包" 等候
   名單(配合 OPERATIONS.md 🟡 task 14-18 的需求驗證)。

### 改善
- **C1** Hardware 表格每一項加 PChome / Shopee / momo 直連連結(寫
  「以下連結為示意,實際請自行比價」),含 affiliate disclosure
- **C2** 「PoE 是什麼?」「Cat6 是什麼?」加 collapsible 解釋(一行
  講完):「PoE = 一條網路線同時送電送資料,攝影機不用另接電源」
- **C3** README 結尾加「想直接買組好的整機?填這個 form」連到
  Tally / Google Form 收 email — 收 50+ 個就能驗證需求

---

## Stage 4 · 開箱安裝

### 雅婷在做什麼
4 支攝影機 + N100 + PoE switch 寄到了。打開包裝。「然後呢?」

### 卡關點

10. **README 的安裝流程默認雅婷會裝 Ubuntu Server**。她不會。
11. **預燒 USB 映像是 README 推薦給「非技術買家」的路徑,但要她自己
    執行 `cd boot-image/ && ./build.sh`**。她沒裝 packer、不知道什麼
    是 dd、沒有現成 Linux 機器去 build ISO。**這條路實際上不通。**
12. **macOS 安裝路徑需要 Homebrew + Docker Desktop**。雅婷沒有 Homebrew、
    `brew install --cask docker` 對她是天書。
13. **`curl -fsSL ... | bash`** 這條命令對非技術人有強烈警戒感
    (各種社群常見警告「不要 curl bash」)。沒有給她「為什麼這個是
    安全的」的視覺說明。
14. **HANDOFF.md 第 9 條提到 macOS 沒 Docker desktop 時的退路是「Day 0
    gap closed by macOS auto-install」(commit 867caba)** — 但這對
    macOS 安裝完不知道要去哪打開的雅婷沒幫助。

### 改善 (這是最大槓桿區)
- **D1** 在 GitHub Releases 釋出**預燒好的** ISO(用 CI 自動建構 → 上傳
  到 Release assets)。讓雅婷可以**直接下載 ISO** 不用跑 build.sh。
- **D2** 寫一份「給非技術買家的紙本快速指南」(放 docs/QUICKSTART_NON_TECH.zh-TW.md):
  - 步驟 1:把 USB 插主機 → 開機 → 等 10 分鐘
  - 步驟 2:打開瀏覽器輸入 `pawcorder.local`
  - 步驟 3:跑精靈
  - 沒有任何 terminal 指令
- **D3** 出貨硬體包的話,USB 隨身碟內建 ISO + 紙本說明。讓「插入 USB →
  開機」就是全部。
- **D4** marketing/index.html 上把這條「USB 一插就好」路徑放最上面,
  把 `curl | bash` 移到「給有經驗的使用者」次要區塊
- **D5** 對於要走 macOS 安裝路徑的人,寫一個原生 `.pkg` 安裝包(會包
  Docker Desktop 安裝引導),取代「請先裝 Homebrew」

---

## Stage 5 · 找到後台

### 雅婷在做什麼
USB 開機完成,看到主機螢幕顯示一個 IP 或 `pawcorder.local`。
打開 MacBook 的 Safari,輸入網址。

### 卡關點

15. **登入頁要密碼。雅婷沒看到 setup 過程印出的密碼**(USB 開機路徑是
    headless 的)。
16. **i18n: `LOGIN_LOST_PASSWORD = "忘記密碼?查看主機上的 .env 或重新
    執行 ./install.sh"`** — 對雅婷是火星文。`.env` 是什麼?她要怎麼
    在主機上「查看 .env」?她有可能根本沒有螢幕接到那台主機。
17. **沒有「忘記密碼」的網頁端救援流程** — 只能 SSH 進去看 .env

### 改善
- **E1** 把 `LOGIN_LOST_PASSWORD` 改成兩段式訊息:
  1. 給技術人:「主機上執行 `make password` 印出當前密碼」
  2. 給非技術人:「在 USB 開機畫面找一張名為 `password.txt` 的隨身碟
     檔案;或重置:在主機螢幕按 F11 進入救援模式」
- **E2** USB ISO 開機完成後在主機螢幕大字顯示 admin URL + 密碼,且把
  密碼寫進 USB 隨身碟根目錄的 `pawcorder-password.txt` 一份(雅婷可以
  把 USB 拔下來插到 Mac 看密碼)
- **E3** 後台加一條「重設密碼」的網頁端流程:
  - 認證方式:在主機螢幕上顯示一個 6 位數確認碼 → 雅婷在後台輸入這個碼
    → 重設成功(這需要主機有螢幕,但能擋掉所有遠端攻擊)

---

## Stage 6 · 5 步驟設定精靈

### 雅婷在做什麼
登入後,進入 setup wizard。

### 觀察(這部分做得很好)

✅ Step 1 歡迎頁面有清楚的進度條
✅ Step 2 攝影機 — 有自動掃網段功能(SETUP_CAM_HEAD)
✅ Step 3 Storage 有候選清單(不用打路徑),有「眼睛」icon 可看完整路徑
✅ i18n 用詞很注意:`Video port` → `影像連接埠`、`Pan/tilt port` →
   `鏡頭控制連接埠`,沒有 RTSP / ONVIF 直接出現給使用者看

### 卡關點

18. **Step 2 攝影機:Reolink 預設密碼欄要她貼從 Reolink App 設的
    admin 密碼**。但 README 說「在 Reolink App 設一個強密碼」,雅婷
    可能還沒做這步。順序會讓她卡在這裡。
19. **Step 2 攝影機:對於沒有 Reolink App 經驗的買家,RTSP 串流路徑
    要怎麼填**?Wyze 的指引「寫信給 Wyze 客服索取非官方 RTSP 韌體檔」
    — 雅婷看到這句話會直接放棄這台。
20. **Step 3 Storage:候選清單列出 `/var/lib/...` 之類的 Linux 路徑**
    — 雅婷不知道這些路徑代表什麼。沒有「我要存到 NAS」「我要存本機」
    這種選擇題。
21. **Step 3 沒有 NAS 整合 inline**。要連 Synology DS220+ 必須跳到
    `/storage` 頁面。雅婷會直接選預設,不會發現自己錯過了 NAS 選項。

### 改善
- **F1** Step 2 攝影機加一個前置選擇題:「這台攝影機是新的嗎?」
  如果新 → 引導她到 Reolink App 先設密碼;如果舊 → 直接填表。
- **F2** Wyze 那段直接改成:「pawcorder 不建議用 Wyze。如果你已經有
  Wyze,點這裡看進階教學;否則建議改買 Reolink」(避免讓初學者
  陷入 docker-wyze-bridge 的陷阱)
- **F3** Step 3 改用「決策樹」式 UI:
  ```
  [○] 我要把影片存在這台主機 (推薦給單機家用)
  [○] 我家有 NAS 想存到那 → 開設定向導
  [○] 我有外接硬碟接到主機上 → 顯示 mount 候選
  ```
- **F4** Step 3 內嵌 NAS 設定,不要逼她跳到 `/storage` 頁面再回來

---

## Stage 7 · 設定通知 / 雲端

### 雅婷在做什麼
精靈跑完。回到 dashboard。看到 onboarding 提示說「下一步:設定通知」。
進入 `/notifications`。

### 卡關點

22. **LINE 設定要 LINE Messaging API token**。雅婷看到「Channel access
    token」「Channel ID」「Webhook URL」直接放棄。她以為加 LINE 就是
    像加好友那樣加。
23. **rclone Google Drive 設定要 OAuth client_id / client_secret 跟
    access token**。雅婷只用過點擊「使用 Google 帳戶登入」按鈕的版本。
    要她去 Google Cloud Console 建 OAuth client 是不可能的。
24. **沒有「我什麼通知都不要」的明確路徑**。萬一她不想設,onboarding
    一直 nag 她。

### 改善
- **G1** LINE 設定改寫:加一個「使用 pawcorder 官方 LINE 通知頻道」
  選項(這需要你有 LINE 官方帳號 + 後台 relay,呼應 OPERATIONS.md
  task 24-25)。雅婷只要掃 QR 加官方帳號 → 輸入她的 admin URL → 完成。
  進階使用者才走「自己設 channel token」路徑
- **G2** Google Drive 整合改寫:用 OAuth Authorization Code Flow
  (使用者點「連 Google Drive」 → 跳轉到 Google → 同意 → 回來)
  讓 rclone 在背後處理,雅婷不要看到 client_id/secret。**這需要
  Anthropic-style verified Google OAuth app**(OPERATIONS.md task,
  2-3 個月驗證流程,要先開始)
- **G3** Onboarding tracker 加上「永遠不顯示這個」按鈕(現在有
  ONBOARDING_SKIP_ALL 但要找)
- **G4** 通知設定加一個「我先不設,之後再說」按鈕

---

## Stage 8 · 第一週日常

### 雅婷在做什麼
裝完一週了。每天上班滑手機看 dashboard。

### 卡關點

25. **Dashboard 有一個 "Open Frigate UI" 按鈕(`OPEN_FRIGATE_UI`,
    翻成「看即時影像」)點下去 → 跳到 Frigate 的英文介面**。雅婷會
    嚇到:「這個畫面跟我剛剛看的 pawcorder 不一樣!我點錯了什麼?」
26. **Hardware 頁面對雅婷是雜訊**。她不會用「detector override」。
    這個頁面對她應該預設不顯示(或藏在 advanced 裡)
27. **PWA「加到主畫面」沒有 inline 引導**。雅婷不會自己想到。她可能
    每次上班都重新打開瀏覽器、輸入網址、登入。
28. **iOS Safari 的 Web Push 不支援,自動退回 Telegram**(從 i18n 看
    得到這個邏輯)— 但雅婷沒有 Telegram!她只有 LINE。所以她會以為
    通知壞了。
29. **沒有「家裡網路斷掉時 Pro 用戶可以走 relay」的故事**(目前 Pro
    relay 還沒上線)。但這對雅婷的「安心感」很重要。

### 改善
- **H1** 把 "Open Frigate UI" 按鈕從 dashboard 移除(技術使用者去
  /system 找)。或加 modal 警告:「你即將前往技術介面(英文,給
  進階使用者)」
- **H2** Hardware 頁面預設只給 admin 角色看,family / kid 看不到
- **H3** Dashboard 第一次造訪時跳一個 "Add to Home Screen" 引導 modal
  (有 Apple 設計指南可參考的 PWA install banner pattern)。iOS 用戶
  尤其需要(Safari 不會自動跳 PWA 提示)
- **H4** iOS 用戶設定通知時:強制要她選 Telegram **或** LINE 其中一個,
  不能兩個都不選(避免「以為設好了但沒有」)。LINE 在 G1 落地後是
  雅婷的首選

---

## Stage 9 · 出狀況

### 雅婷在做什麼
某天回家發現「貓砂盆相機沒在錄」。

### 卡關點

30. **Dashboard 顯示「Recording: stopped」她不知道為什麼。**
31. **沒有「常見問題」頁**。她必須去看 logs(英文)、去 GitHub Issues
    (英文)、或在 Reolink App 看相機是不是離線。
32. **錯誤訊息不夠 user-friendly**。例如 RTSP 連不上時,後台應該
    顯示「攝影機 X 連不上,可能原因:沒電、網路線鬆了、攝影機重新
    啟動中」,而不是 connection timeout。

### 改善
- **I1** 寫 `docs/TROUBLESHOOTING.zh-TW.md`,涵蓋:
  - 「錄影狀態顯示停止」→ 怎麼診斷
  - 「攝影機掉線」→ 物理檢查清單(電 / 網路線 / 重開機)
  - 「LINE 沒收到通知」→ 一步一步檢查
  - 「app 開不起來」→ 重啟容器
- **I2** Dashboard 在每個元件「異常」狀態旁加一個 (?) 按鈕,點開
  顯示「常見原因 + 第一招怎麼修」
- **I3** 錯誤訊息系統化(專屬一個 `errors.py` 把技術錯誤碼對到
  user-friendly 中文 + 建議動作)
- **I4** 把「重啟相機」「重啟 Frigate」按鈕加到 dashboard 異常元件
  旁邊(現在要去 /system 找)

---

## 改善優先順序(我建議的)

| # | 編號 | 改善項目 | 工作量 | 槓桿 | 阻塞? |
|---|---|---|---|---|---|
| 1 | A1 | 修 repo 名字一致性 | 5 min | 高 | 🔴 OSS 公開前必修 |
| 2 | E1, I3 | 錯誤訊息系統化 + 改寫 LOGIN_LOST_PASSWORD | 半天 | 高 | 簡單 |
| 3 | I1 | 寫 TROUBLESHOOTING.zh-TW.md | 半天 | 高 | 簡單 |
| 4 | D1 | CI 自動建構 ISO 上傳 GitHub Releases | 1 天 | **極高** | 解開 D2/D3 |
| 5 | D2, F1, F3 | 非技術買家路徑(QUICKSTART + 精靈改造) | 1-2 天 | 高 | 需 D1 先做 |
| 6 | B1, B3 | README 加 FAQ + 30 秒 demo 影片 | 半天 + 拍片 | 中 | 需相機在手 |
| 7 | H1, H3 | Dashboard 移除 Frigate UI 按鈕 + PWA 安裝引導 | 半天 | 中 | 簡單 |
| 8 | G1 | LINE 官方帳號通知 relay | 1-2 天 | 高 | 需 LINE 帳號 + relay 上線 |
| 9 | F2 | Wyze 那段改寫(不推薦初學者) | 30 min | 中 | 簡單 |
| 10 | C1, C2 | Hardware 表加購買連結 + 術語解釋 | 半天 | 中 | 需 affiliate 政策 |
| 11 | G2 | Google Drive OAuth 走 verified app | 2-3 個月 | **極高** | OPERATIONS task,先啟動 |
| 12 | A2 | Cloudflare Pages 部署 marketing | 30 min | 中 | OPERATIONS task |

「**極高**」槓桿的兩項(D1 + G2)分別是「能不能讓非技術人裝起來」
跟「能不能讓非技術人完整用起來」的真正瓶頸。其他都是周邊優化。

---

## 我建議先做這 4 項

如果只能挑 4 個一週內動手:

1. **A1** 修 repo 名字 — 5 分鐘解掉一個會炸的雷
2. **E1 + I3** 錯誤訊息 + LOGIN 救援文案 — 半天,立即提升非技術人體驗
3. **I1** 寫 TROUBLESHOOTING.zh-TW.md — 半天,寫一次受益所有人
4. **D1** CI 自動建 ISO + 上 GitHub Releases — 1 天,徹底解開「USB 一插就好」這條路

做完這 4 項,雅婷從「絕對裝不起來」變成「插 USB 就成功」+ 出狀況時
有救命稻草。
