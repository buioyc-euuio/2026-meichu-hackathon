# 網球追蹤 + 人偵測（即時 JSON 串流）

用相機找出畫面裡**所有的網球和人**：位置、大小、固定編號（id），再用 FastAPI 即時串流 JSON 給其他程式。

- **YOLO 為主**（跑在 GPU）：同一次推論一起找「sports ball」和「person」
- **傳統 CV 幫忙**（球）：修正框、用顏色驗證、拆開黏在一起的球、YOLO 漏抓時補回來
- **前後畫面**：每顆球、每個人都有固定編號，換畫面不會變

## 安裝（只要一次）

```bash
uv pip install --python .venv/bin/python torch torchvision --index-url https://download.pytorch.org/whl/cpu
uv pip install --python .venv/bin/python ultralytics ncnn pnnx fastapi "uvicorn[standard]"
```

第一次跑會自動下載 `yolo11n.pt` 到 `tennis_tracking/models/`，並轉成 GPU 用的 ncnn 格式（幾秒鐘）。

## 伺服器：即時串流 JSON（`server.py`）

```bash
.venv/bin/python tennis_tracking/server.py                    # http://127.0.0.1:8000
.venv/bin/python tennis_tracking/server.py --host 0.0.0.0     # 讓同一個網路的其他裝置也能連
.venv/bin/python tennis_tracking/server.py --video test.mp4   # 用影片代替相機（會一直重播）
```

| 網址 | 用途 |
|---|---|
| `WS /ws` | **WebSocket，每張畫面推一個 JSON**（約 30 次／秒，給程式用，最即時） |
| `GET /stream` | Server-Sent Events，一樣的 JSON；`curl -N http://127.0.0.1:8000/stream` 就能看 |
| `GET /latest` | 最新一張畫面的 JSON（想自己決定多久問一次的時候用） |
| `GET /video` | 標好框的即時影像（MJPEG，瀏覽器直接開；沒人看時不會浪費 CPU 去畫） |
| `GET /` | 網頁：影像 + 即時 JSON（除錯用） |
| `GET /health` | 伺服器狀態 |

用戶端範例：`client_example.py`（`.venv/bin/python tennis_tracking/client_example.py`）

> ⚠️ **用戶端要「只留最新的一筆」**：伺服器每張畫面都推。如果用戶端處理一次比 1/30 秒久（控制馬達、問 LLM），
> 訊息會在用戶端越積越多。實測：慢的用戶端直接一筆一筆讀，延遲會到 1～2 秒；
> 改成背景一直收、只留最新的（`client_example.py` 的寫法），延遲約 40 ms。

實測（相機 640×480、YOLO 在 GPU）：30 fps，整個伺服器約 27% CPU（一顆核心的四分之一）。

### JSON 格式

```json
{
  "frame": 123, "time": 1726740000.1, "width": 640, "height": 480, "fps": 30.0,
  "balls": [
    {"id": 1, "x": 320.5, "y": 240.1, "r": 40.6, "d": 81.2, "size": 0.127,
     "ex": 0.001, "ey": 0.0, "box": [279.9, 199.5, 361.1, 280.7], "vx": 1.2, "vy": -0.5,
     "source": "yolo+cv", "conf": 0.62, "misses": 0}
  ],
  "people": [
    {"id": 3, "x": 300.0, "y": 250.0, "ex": -0.063, "ey": 0.042, "h": 0.71,
     "box": [220.0, 80.0, 380.0, 420.0], "conf": 0.88, "misses": 0}
  ]
}
```

座標都是像素，畫面左上角是 (0, 0)，往右 x 變大、往下 y 變大。`balls`、`people` 都是**大（近）的排前面**。

| 欄位 | 意思 |
|---|---|
| `id` | 固定編號：同一顆球／同一個人換畫面還是同一個號碼 |
| `x`, `y` | 中心座標 |
| `ex`, `ey` | 在畫面哪裡：-1 = 最左／最上、0 = 正中間、+1 = 最右／最下（跟 `camera_follow` 一樣） |
| `box` | 框 `[x1, y1, x2, y2]` |
| `r`, `d`（球） | 球在畫面上的半徑、直徑（像素）；越大代表越近 |
| `size`（球） | 直徑佔畫面寬度的比例（0～1），換解析度也不會變 |
| `vx`, `vy`（球） | 速度（像素／每張畫面） |
| `h`（人） | 人框高度佔畫面比例，越大代表越近 |
| `source`（球） | `yolo+cv`（YOLO + 顏色修正，最準）、`yolo`（顏色不對但 YOLO 很有信心）、`cv`（YOLO 沒抓到，用顏色補的）、`predict`（這張沒看到，用預測的位置） |
| `conf` | YOLO 的信心（`cv`、`predict` 是 0） |
| `misses` | 連續幾張沒看到；> 0 表示這筆是預測／上一張的位置。只要「這張真的看到的」就過濾 `misses == 0` |

球連續 8 張沒看到、或預測跑出畫面就刪掉；人是 5 張。

## 看畫面用（`track_ball.py`）

```bash
.venv/bin/python tennis_tracking/track_ball.py                    # 開視窗，按 q 離開
.venv/bin/python tennis_tracking/track_ball.py --json             # 每張畫面印一行 JSON（跟 server 送的一樣）
.venv/bin/python tennis_tracking/track_ball.py --video test.mp4   # 用影片測試（每張畫面都會印）
.venv/bin/python tennis_tracking/track_ball.py --image photo.jpg  # 用一張照片測試（按任意鍵關掉）
.venv/bin/python tennis_tracking/track_ball.py --headless         # 不開視窗，跑 5 秒印數值並存截圖
.venv/bin/python tennis_tracking/track_ball.py --no-yolo          # 不用 YOLO，只靠顏色 + 前後畫面（不找人）
.venv/bin/python tennis_tracking/track_ball.py --raw              # 只用 YOLO，不修正（比較用）
.venv/bin/python tennis_tracking/track_ball.py --device cpu       # YOLO 用 CPU（預設是 GPU）
.venv/bin/python tennis_tracking/track_ball.py --tune             # 用滑桿調網球的顏色範圍，按 s 存檔
```

相機編號不是 0 的話加 `--camera N`（用 `camera_follow/find_camera.py` 找）。

畫面上：綠圈 = 球（旁邊寫 `#編號` 和來源；預測的是橘色）、紫框 = 人、藍／紅細框 = YOLO 的球框（通過／被淘汰）。

## CV 怎麼幫 YOLO（`tracking_helper.py`）

| 步驟 | 做什麼 |
|---|---|
| 修正 | YOLO 的框通常比球大一圈 → 在框裡用顏色遮罩重新找圓，得到準確的球心和直徑 |
| 驗證 | 框裡真的有網球顏色、而且夠圓才算數；所以 YOLO 信心門檻放很低（0.05），抓錯的交給顏色淘汰 |
| 拆開黏在一起的 | 兩顆球碰在一起、或球碰到黃色長條，顏色會連成一塊 → 距離轉換找「塞得下的最大圓」，一顆一顆挖出來；接觸的地方要有「細腰」才算兩顆球（長條沒有細腰） |
| 配對 | 每顆球有自己的卡爾曼濾波器預測下一張在哪；這張的候選配給「離預測最近、大小最像」的那顆，所有球一起配（不是輪流挑），編號才不會對調 |
| 補抓 | YOLO 沒抓到某顆球 → 只在它的預測位置附近用顏色找（範圍比 YOLO 的小，免得被旁邊顏色像的東西搶走）；也找不到就先用預測值 |
| 開新球 | 一定要 YOLO 找到（YOLO 為主）：顏色也對 → 馬上算數；顏色不對（燈光怪）→ 被 YOLO 看到 5 次、平均信心 ≥ 0.35 才算數。`--no-yolo` 時改成顏色要連續看到 3 次 |
| 刪掉 | 連續 8 張都沒看到、跑出畫面、或連續 3 秒都沒被 YOLO 看到（只靠顏色撐著的黃色東西不會一直留著） |
| 還編號 | 球跟丟（例如被手擋住）2 秒內在附近又出現、大小差不多 → 還它原本的編號 |

### 門檻怎麼來的（實際錄影）

門檻是用 `camera-stream/recordings` 的兩段錄影（場館燈光、手拿球、桌上的球，共 1096 張畫面）一格一格標出球的位置後調的：

| 門檻 | 原本 | 現在 | 為什麼 |
|---|---|---|---|
| 顏色 S 下限 | 100 | **60** | 真的球在場館燈光下 S 只有 92～152（中位數 119），100 會把球比較暗的半邊切掉 → 球心、大小算歪 |
| 顏色 H、V | 25～45、≥70 | **28～46、≥90** | 球是 H 36～38、V 197～246；這兩個在錄影裡影響很小 |
| YOLO 信心 | 0.1 | **0.05** | 信心 0.01～0.05 的框有 90% 真的是球，丟掉很可惜；假的交給顏色驗證 |
| 新球觀察期、YOLO 支持率 | 有 | **拿掉** | 會把真的球刪掉（球小、遠的時候 YOLO 本來就不常看到） |
| 沒被 YOLO 看到多久刪掉 | 1.5 秒 | **3 秒** | 同上 |
| 還編號 | 沒有 | **2 秒內** | 手擋住球再放開，編號不會變 |

結果：

| | 只用 YOLO | 改之前 | **現在** |
|---|---|---|---|
| 抓到球的畫面 | 70.6% | 81.4% | **97.2%** |
| 球心誤差 | 7.5 px | 4.4 px | **3.2 px** |
| 直徑誤差 | 13.1 px | 9.1 px | **5.8 px** |
| 球的編號換了幾次 | — | 8 | **2** |

第二段錄影裡的「假球」幾乎都是**筆電螢幕上的相機預覽畫面裡的球**（真的是一顆球的影像，不是追蹤錯）。
追蹤時把預覽視窗關掉或縮小就不會有。

### 燈光（`AdaptiveColor`）

燈光一變，球的顏色就變：太亮 → 變白（飽和度 S 變低）、太暗 → 亮度 V 變低、黃光／白光 → 色相 H 偏移。
固定的顏色範圍會把 YOLO 明明找到的球刷掉，所以：

1. **顏色不對也不直接丟**：YOLO 的框只要落在「已經在追的球」的預測位置，就算顏色不對也拿來更新那顆球
2. **從 YOLO 找到的球學顏色**：每張畫面從球心附近取樣，顏色範圍跟著燈光移動（約十張畫面跟上；燈光突然換會學快一點）
   - 色相只能在黃綠色系（12～60）裡移動，S、V 的下限只會放寬、不會比 `settings.json` 更嚴
3. 伺服器的 `GET /health` 會顯示現在學到的範圍（`ball_color_hsv`），可以看它有沒有跟著燈光變

模擬測試（一顆球在 5 種燈光下各 60 張畫面）：過曝（球變白）從只抓到 7～13% 變成 100%。
模擬的 YOLO 誤認比真的多很多，所以模擬裡燈光怪的時候假球比較多；實際錄影裡假球很少（見上面）。

人（`PersonTracker`）：用框的重疊（IoU）判斷是不是同一個人，框做一點平滑。

### 模擬測試結果

一顆球（300 張畫面，YOLO 故意漏抓 25%、框不準、拖影、會抓錯）：

| | 只用 YOLO（`--raw`） | YOLO + CV 幫忙（預設） |
|---|---|---|
| 抓對的畫面 | 約 190 / 300 | 約 298 / 300 |
| 抓錯位置 | 約 40 | 0 |
| 球心誤差 | 約 6 px | 約 2.7 px |
| 直徑誤差 | 約 10 px | 約 3.4 px |
| 編號 | — | 300 張都是同一個 |

三顆球路徑交叉（8 次，每次 300 張）：抓到 99.4%、球心誤差約 2.5 px、多出來的假球約 0.5%。

### 已知限制

- **兩顆一模一樣的球幾乎完全重疊**（中心距離 < 兩顆半徑和的一半）再分開時，編號可能對調：
  光看畫面分不出誰是誰。模擬裡剩下的編號對調 100% 都是這種情況；只是碰到、沒有重疊太多的不會對調。
- 兩個人的框大部分重疊時，人的編號也可能對調（同樣的原因）。
- 人沒有顏色可以驗證，信心門檻比球高（0.4，`yolo_detector.py` 的 `PERSON_CONF`）：被切掉一大半的人可能抓不到。
- 一大片跟網球同顏色的東西（黃色牆壁）在「已經在追的球」附近時可能被誤認；新球一定要 YOLO 確認，所以不會憑空多出來。
- **燈光讓旁邊的東西變得跟球同色**（例如白光下橘黃色的東西色相跟球只差幾度）、球又剛好從旁邊經過時，
  追蹤可能被那個東西搶走，最多約 3 秒（沒被 YOLO 看到就刪掉）。
- YOLO 如果把一個跟球同顏色的東西認成球，它馬上會被當成球（錄影裡這樣最準，代價是這種情況會有假球）。
- **螢幕上顯示的球**（例如相機預覽畫面）也是球，會被抓到。
- 球被畫面邊緣切掉一大半、或幾乎被手整個握住時，YOLO 認不出來。
- 門檻是在場館燈光下調的；換到燈光差很多的地方，先錄一段影片測，或用 `track_ball.py --tune` 重調顏色。

## GPU

預設 YOLO 跑在 GPU 上。這台是 AMD 內顯（Radeon 860M），沒有 NVIDIA 的 CUDA，
所以用 **ncnn + Vulkan**（不需要 ROCm、不用 sudo）。找不到 GPU 會自動改用 CPU。

| YOLO 跑在 | 速度 | CPU 用量 |
|---|---|---|
| GPU（預設） | 約 12 ms／張（84 fps） | 約 50% |
| CPU（`--device cpu`） | 約 18 ms／張（56 fps） | 約 800%（8 顆核心全滿） |

相機本身是 30 fps，所以畫面不會變快；GPU 的好處是 **CPU 幾乎空出來**，給藍牙、LLM 等其他程式用，
也可以換更大更準的模型（`yolo_detector.py` 裡 `YOLO_MODEL = "yolo11s.pt"`，GPU 上約 30 fps）。
GPU 算出來的框位置跟 CPU 一樣，但信心分數會比較高一點（例如 0.4 → 0.6），是正常的。

## 檔案

| 檔案 | 用途 |
|---|---|
| `server.py` | FastAPI 伺服器：相機 → 追蹤 → WebSocket／SSE 即時串流 JSON |
| `client_example.py` | 連 server 的範例（只留最新一筆的寫法） |
| `track_ball.py` | 看畫面用：開視窗、印數值、調顏色 |
| `pipeline.py` | 整條流程：一張畫面 → JSON；server 和 track_ball 共用 |
| `yolo_detector.py` | YOLO 找球（sports ball）和人（person）；GPU／CPU 切換 |
| `tracking_helper.py` | CV 幫忙 + 多顆球、多個人的追蹤和編號 |
| `ball_color.py` | 網球顏色範圍、顏色遮罩（`--tune` 存到 `settings.json`） |

## 抓不到球的時候

1. 先跑 `track_ball.py --tune`，右半邊是遮罩：調到只有網球是白色、其他都是黑色，按 `s` 存檔
2. 燈光太暗或太黃會影響顏色，換個光線試試
3. 可以改的設定都在各檔案最上面：`tracking_helper.py`（驗證門檻、預測範圍、幾張沒看到算跟丟）、
   `yolo_detector.py`（模型、信心門檻）、`ball_color.py`（顏色範圍）、`track_ball.py`（相機、解析度）
