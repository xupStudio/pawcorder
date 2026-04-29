# pawcorder

[English](README.md) · [繁體中文](README.zh-TW.md)

自架的寵物攝影機 NVR。**影片留在你家網路內**，不上廠商雲端。雲端備份用你
自己的 Google Drive / Dropbox / S3 / WebDAV——買完硬體之後，每月支出 NT$0。

[![CI](https://github.com/xupStudio/pawcorder/actions/workflows/ci.yml/badge.svg)](https://github.com/xupStudio/pawcorder/actions/workflows/ci.yml)

## 你會得到什麼

### 錄影 + AI
- **攝影機相容**——6 個品牌 key 走原廠 HTTP API 自動設定（Reolink +
  Hikvision + Dahua + Amcrest + Axis + Foscam，Amcrest 是 Dahua OEM
  共用同一個模組）。UniFi Protect、TP-Link Tapo、Imou、Wyze 在後台
  直接給逐步引導，做一次性的 in-app 設定。其他任何 ONVIF / RTSP IP
  cam 走 ONVIF Profile S 自動探測。Reolink E 系列仍是推薦新買款。
  想接幾支都行。
- **NVR + AI**——基於 [Frigate](https://frigate.video/)（MIT），會根據
  你的硬體自動選 detector：Intel iGPU 走 OpenVINO、NVIDIA 走 TensorRT、
  Coral 走 Edge TPU、Pi5 + Hailo 走 Hailo、其他走純 CPU。
- **即時直播**——後台儀表板與單機畫面內建 HLS / WebRTC 播放器，支援
  雙向對講（按住按鈕說話），看相容的攝影機是否支援。
- **音訊偵測**——Frigate 內建音訊模型偵測吠叫、喵叫、玻璃破裂、尖叫、
  煙霧警報，跟視覺事件走同一條推播。

### 寵物個體辨識
- **每隻有自己的身分**——上傳幾張寵物照片，MobileNetV3 抽 576 維 ONNX
  embedding 認得它，跨攝影機追同一隻。餘弦相似度 0.78 以上才認定，
  介於門檻之間的會單獨標為「tentative」。
- **跨攝影機時間軸**——每隻寵物的一日行動軌跡，所有攝影機合在一個
  可捲動的視圖裡。
- **回溯辨識**——新增寵物後一鍵重新跑過去 7 天的事件，補回原本叫
  「unknown」的紀錄，附即時進度條。
- **健康異常警示**——每隻寵物有自己的活動量基準，z-score ≥ 2σ 偏低或
  低於硬性下限就推播提醒。

### 後台介面
- **多使用者 + 角色權限**——admin / family / kid 三層權限。最後一個
  admin 不能刪不能降，避免把自己鎖在外面。
- **儀表板**——即時縮圖、今日精選時刻、最近事件、Frigate log 即時瀏覽、
  容器 CPU / RAM / 磁碟使用率。
- **區域編輯器**——Canvas 畫多邊形：偵測區域和隱私遮罩都可以拖拉編輯，
  按一下加端點，拖動端點調形狀。
- **PTZ 預設位置**——把鏡頭轉到「餵食點」「貓砂盆」按下儲存，下次
  一鍵跳回。走 ONVIF 標準，不綁特定品牌。
- **隱私模式**——手機在家裡 Wi-Fi 自動暫停錄影（也支援手動切換或排程）。
- **每日精選**——凌晨 2 點自動 ffmpeg stream-copy 出 30 秒精華片段
  （無重編碼，避開專利）。保留 14 天。
- **縮時影片**——每分鐘抽幀，每天 02:30 把昨日 24 小時壓成 ~48 秒
  mp4。每支攝影機一片，保留 30 天。
- **活動熱力圖**——每支攝影機 30 天的移動密度，疊加在縮圖上的半透明 PNG。
- **省電模式**——設定一個低 FPS / 動態觸發的時段（例：02–06 點），
  睡覺時 iGPU 不全速跑。
- **Schema 版本遷移**——每個設定檔有 schema_version，啟動時自動跑
  遷移；升級不會把舊資料弄壞。
- **API key + Web Push**——bearer token 給腳本用，PWA 走 VAPID 標準
  推播；iOS 因 Safari 限制自動退回 Telegram。

### 儲存
- **NAS 掛載精靈**——瀏覽器直接設定 NFS / SMB：先試掛確認沒打錯字、
  寫進 fstab（重複設定會自動取代舊的，不會堆）、立刻掛上。SMB 密碼
  放在 0600 憑證檔，不會被寫進 fstab。
- **雲端備份**——`rclone` 8 種後端：Drive / Dropbox / OneDrive / B2 /
  S3 / Wasabi / R2 / WebDAV。可選 AES-256-GCM 加密，密語自己設。
- **排程備份**——每天自動把 `cameras.yml` / `pets.yml` / `users.yml`
  等設定上傳到你選的雲端。

### 通知
- **Telegram**（附快照）或 **LINE**（純文字）——貓狗出現後幾秒內通知。
  不需要 MQTT broker，也不需要其他服務。
- **Web Push (PWA)**——桌面 / Android 走 VAPID 標準推播；iOS 因 Safari
  限制自動退回 Telegram。
- **Frigate Webhook**——事件秒級到達，取代輪詢；含 dedup 不會重複推。

### 隨時隨地看
- **手機**——PWA 可加到主畫面，看起來像 App。
- **Tailscale**——一行指令加進你的 tailnet，外網看貓不用 port forward、
  不用 DDNS。
- **雙語**——繁體中文 / English 完整翻譯，日文 / 韓文骨架已開好等翻譯。

### 維運品質
- **一行安裝**——`curl | bash`（見下）或 `git clone && ./install.sh`。
- **OTA 更新**——`make update` 拉最新 image；後台會看 GitHub Releases
  顯示「有新版」徽章。
- **備份 / 還原**——一鍵下載整個後台狀態的加密 tarball，新機還原直接
  接手。
- **乾淨解除安裝**——soft reset（保留錄影、清設定）或完全移除，每個
  檔案清單都列出來給你看。
- **API + 整合文件**——後台 `/docs/api` 頁面含 curl、Home Assistant、
  iOS 捷徑範例。

## 跟你原本會買的東西比

| | Furbo Dog Nanny | Wyze Cam Plus | Apple HKSV | UniFi Protect | **pawcorder** |
| --- | :---: | :---: | :---: | :---: | :---: |
| 硬體（4 支）| NT$24,000 | NT$3,600 | 看設備 | NT$22,000 | **NT$11,200** |
| 月費 | NT$199 | NT$120 | NT$99（iCloud+）| $0 | **$0** |
| 儲存位置 | 廠商雲 | 廠商雲 | 你的 iCloud | 本機 | **你的 Drive / S3 / NAS** |
| 寵物 AI | ✓ | ✗ | ✗ | ✗ | ✓ |
| 多攝影機 | 加價 | 是 | 是 | 是 | ✓ |
| 隱私 | ✗ | ✗ | ✓ | ✓ | ✓ |
| 安裝難度 | 簡單 | 簡單 | 中 | 難 | **中→簡單（USB image）** |

## 硬體

pawcorder 在任何能跑 Docker 的東西上都能跑。`install.sh` 第一次跑的
時候會自己探測主機並挑最適合的 Frigate detector，你不用選：

| 主機 | 選擇的 detector | 備註 |
| --- | --- | --- |
| **Intel x86_64 + iGPU**（N100 / NUC / J5005…）| OpenVINO | CP 值最高，**推薦** |
| **Linux + NVIDIA GPU** | TensorRT | 用既有的遊戲機 / homelab |
| **Raspberry Pi 5 + Hailo-8L AI Kit** | Hailo | 低功耗 ARM + AI 加速 |
| **Raspberry Pi 5 + Coral USB** | Edge TPU | 比較便宜的 Pi 路線 |
| **AMD x86_64**, NAS（Synology / QNAP x86）| CPU | 能跑，無硬體加速 |
| **Mac**（Apple Silicon 或 Intel）| CPU | 開發 / 測試用——Docker Desktop 跑 Linux VM，沒有 iGPU 直通；1–2 支可以 |
| **Windows + Docker Desktop** | CPU | 同 Mac |

不滿意自動選的話，後台 **/hardware** 頁可隨時手動覆寫。

### 推薦入門組合（台灣價格）

從零開始買的話，建議用 **Intel N100** 迷你 PC，因為它在閒置 ~10 W 的
功耗下提供 OpenVINO 級的 AI 偵測，整機不到 NT$7,000——這是 24/7 NVR
最佳的 CP 值。但 pawcorder **不是只能用 N100**——已經有 Pi 5、Synology
x86、AMD homelab、甚至閒置筆電都行，把 `install.sh` 指過去就好，
省一筆。

| 項目 | 備註 | NT$ |
| --- | --- | ---: |
| 主機（擇一）：| | |
| &nbsp;&nbsp;Intel N100 mini PC | Beelink / GMKtec / MINISFORUM（8GB / 256GB）| 5,500–6,500 |
| &nbsp;&nbsp;Raspberry Pi 5 + Hailo-8L | 小尺寸 ARM + AI 加速 | ~6,000 |
| &nbsp;&nbsp;既有 NAS / homelab | x86 Synology, TrueNAS, Proxmox VM 等 | 0 |
| Reolink E1 Outdoor PoE × N | 4MP / 2K, PoE, 全幅 PTZ + 變焦；室內外都能用 | 2,500–3,200 一支 |
| TP-Link TL-SG1005P PoE switch | 5 port, 4× PoE 802.3af | 1,500 |
| 每支攝影機的 Cat6 線 | 長度看擺放 | 100–200 一條 |
| 既有 NAS | TrueNAS / OMV / Synology / 自組 | — |

- **1 支攝影機（N100 路線）**：約 NT$10,000 一次性，之後 **$0/月**。
- **4 支攝影機（N100 路線）**：約 NT$19,000 一次性，之後 **$0/月**。
- **已經有 Pi 5 / NAS / homelab？** 只要買攝影機 + PoE switch：
  NT$3,500 / 11,500——更便宜。

（同樣 4 支 Furbo：~NT$24,000 + NT$199/月 × 4，3 年下來 ≈ NT$48,000。）

## 一行安裝

任何剛裝好的 Linux（Ubuntu 24.04 / Debian 12 / Fedora / Arch）、
macOS、或 Windows + WSL2：

```sh
curl -fsSL https://raw.githubusercontent.com/xupStudio/pawcorder/main/scripts/bootstrap.sh | bash
```

就這樣。bootstrap script 會 clone 到 `/opt/pawcorder`（macOS 是
`$HOME/pawcorder`），然後交給 `install.sh`——它會偵測你的平台
（OS + 架構 + 加速器）、自動裝 Docker（Linux 走 `get.docker.com`；
macOS 走 `brew install --cask docker` 然後等 Docker Desktop 啟動；
WSL2 走 distro 內的 Docker Engine）、產生隨機密碼、選對你硬體的
Frigate detector、把後台跑起來，最後印出網址 + admin 密碼。

不放心 `curl | bash`？先讀 source：

```sh
curl -fsSL https://raw.githubusercontent.com/xupStudio/pawcorder/main/scripts/bootstrap.sh -o bootstrap.sh
less bootstrap.sh   # ← 跑之前先看一下
bash bootstrap.sh
```

或走老派路線：

```sh
git clone https://github.com/xupStudio/pawcorder.git
cd pawcorder
./install.sh
```

自訂安裝位置（預設 `/opt/pawcorder`）：

```sh
PAWCORDER_DIR=$HOME/pawcorder curl -fsSL https://raw.githubusercontent.com/xupStudio/pawcorder/main/scripts/bootstrap.sh | bash
```

### 用預先燒好的 USB 映像一步到位

不會 Linux 的買家可以建一張可開機 USB：

```sh
cd boot-image/
./build.sh
```

把產出的 `output/pawcorder-ubuntu-24.04.iso` `dd` 進 USB 隨身碟，插上
目標機器（任何 x86_64——N100、NUC、舊筆電都行）。約 10 分鐘後主機會
在 LAN 上以 `pawcorder.local` 廣播自己，後台就在
`http://pawcorder.local:8080`。詳見
[boot-image/README.md](boot-image/README.md)。

## 硬體設定一步步來

### 1. 挑一台主機

在你要用的主機上裝 **Ubuntu Server 24.04 LTS**（或用上面的預燒映像）
——Intel mini PC、Pi 5、NAS x86 等都可以。在路由器上幫主機 IP 設一個
DHCP reservation，避免 IP 飄移。

### 2. PoE switch + 攝影機接線

把 TP-Link TL-SG1005P 插上電源。uplink port 接路由器。每支 Reolink
E1 Outdoor PoE 用 Cat6 線接到 PoE port——攝影機會自動上電。

### 3. Reolink 一次性設定（Reolink 手機 App）

1. 在 Reolink App 加入攝影機。
2. 設定一個強的 **admin 密碼**（記下來）。
3. **重要：** **設定 → 顯示 → 編碼**，主串流切到 **H.264**（不是 H.265）。
4. 在路由器設 DHCP reservation。

設定精靈跑完之後，pawcorder 後台會透過 Reolink HTTP API 自動把 RTSP
打開。

### 4. 在瀏覽器裡完成

開 `http://<主機-IP>:8080`（用了預燒映像的話是 `http://pawcorder.local:8080`），
用印出的密碼登入，跑 5 步驟精靈：

1. **攝影機** —— 掃網段、加入每支攝影機。連線型態（Wi-Fi / 有線）會
   透過 Reolink API 自動偵測。
2. **儲存** —— 把 Frigate 指到 NAS 掛載點。
3. **偵測** —— 選靈敏度 preset。切換要追的物種（貓 / 狗 / 人）。
4. **管理員密碼** —— 把隨機密碼換掉。
5. **完成。** Frigate 重啟，打開直播。

## 日常操作

```sh
make ps           # 看在跑什麼
make logs         # 全部 log
make frigate-logs # 只看 Frigate 的 log
make restart      # 重啟整個 stack
make update       # 拉新 image 並重新建立
make password     # 從 .env 印出 admin 密碼
make test         # 跑測試 + shellcheck
```

同樣的事在後台都做得到：儀表板看狀態、System 頁重啟 Frigate、瀏覽器
裡看最近 log。

## 架構

```
+----------+ +----------+ +----------+ +----------+
|  cam 1   | |  cam 2   | |  cam 3   | |  cam 4   |   PoE 供電的 Reolink
+----+-----+ +----+-----+ +----+-----+ +----+-----+
     |            |            |            |
     +------+-----+------+-----+------+-----+
            |            |            |
            v            v            v
            +-------- PoE switch -----+
                         |
                         | LAN
                         v
                +-----------------+      HLS / WebRTC    +-------+
                |  Frigate (host) | -------------------> | Phone |
                |  (auto-picked   |                      | / Web |
                |   detector)     |                      +-------+
                +-----------------+
                  |              |
                  | NFS / SMB    | rclone
                  v              v
          +-------------+   +--------------+
          |    NAS      |   |  Your cloud  |
          | (full keep) |   |  (events    |
          +-------------+   |   only)      |
                            +--------------+

+-------------------+
| pawcorder admin   |  <- 你，在瀏覽器裡
| (FastAPI + UI)    |     管攝影機 / 偵測 / 雲端 / 通知 /
+-------------------+     硬體設定，重啟 Frigate
```

## 測試

```sh
make test
```

~525 個測試覆蓋攝影機 CRUD、設定 round-trip 含 escape、Frigate template
跨 detector / 物種組合的渲染、Reolink link-classifier、network-scan
驗證、i18n key 覆蓋（每個 key 都同時有 `en` 和 `zh-TW`）、cookie + bearer
auth、RBAC、多使用者生命週期、NAS fstab idempotency、回溯辨識
single-flight、縮時 retention、schema migration、webhook dedup，外加
透過 `TestClient` 的完整 FastAPI route 煙霧測試（所有外部依賴
——Docker / Reolink HTTP / ffprobe / Telegram / LINE / rclone / nmap /
ONNX——都已 stub）。CI 也用 shellcheck 檢查 bash，並驗證 Packer
template + docker compose 語法。

## 授權

[MIT](LICENSE)。

建構在這些優秀的開源專案之上（全部寬鬆授權）：
- [Frigate](https://frigate.video/)（MIT）—— NVR + AI 引擎
- [go2rtc](https://github.com/AlexxIT/go2rtc)（MIT）—— RTSP 重串流
- [FastAPI](https://fastapi.tiangolo.com/)（MIT）—— 後台伺服器
- [rclone](https://rclone.org/)（MIT）—— 雲端上傳
- [Tailwind CSS](https://tailwindcss.com/)（MIT）+ [Alpine.js](https://alpinejs.dev/)（MIT）—— UI
- [HashiCorp Packer](https://www.packer.io/)（MPL 2.0）—— USB 映像建立
- [Inter](https://rsms.me/inter/)（OFL）—— UI 字型

## 貢獻

歡迎 bug report 和 PR。提交前請跑 `make test`。完整指南見
[CONTRIBUTING.md](CONTRIBUTING.md)。
