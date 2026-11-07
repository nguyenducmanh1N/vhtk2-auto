# HOWTORUN — Cài đặt & test trên máy Windows trắng

Hướng dẫn dựng VHTK2 Auto Tool từ số 0 trên server Windows và nghiệm thu
điều khiển đa instance LDPlayer (giai đoạn W1 → W2). Nguyên tắc: **đi từng
tầng, tầng dưới PASS mới lên tầng trên** — code không cần sửa, chỉ cài đặt
và đổi config.

Tài liệu liên quan: `PLAN.md` (thiết kế + DoD từng giai đoạn), `PROGRESS.md`
(trạng thái hiện tại), `CLAUDE.md` (quy tắc kỹ thuật).

---

## Bước 0 — Cài nền (30–60 phút)

1. **Bật VT (Intel VT-x / AMD-V) trong BIOS.** Thiếu cái này LDPlayer không
   chạy — kiểm tra ĐẦU TIÊN, kẻo cài hết rồi mới phát hiện.
2. Cài **Git** và **Python 3.11+** (tick *Add python.exe to PATH* khi cài).
3. Cài **LDPlayer 9** — mặc định `C:\LDPlayer\LDPlayer9\`. Cài chỗ khác thì
   ghi `ldplayer.ldconsole_path` vào `config\settings.local.yaml` (file cấu
   hình riêng máy, không commit — mẫu: `config\settings.local.example.yaml`).
4. **Thêm adb vào PATH** (code gọi `adb` trực tiếp ở vài chỗ): LDPlayer có
   sẵn `adb.exe` trong thư mục cài — thêm `C:\LDPlayer\LDPlayer9` vào PATH,
   mở cmd MỚI, gõ `adb version` phải ra kết quả.
5. Lấy code + môi trường Python:

   ```bat
   git clone https://github.com/nguyenducmanh1N/vhtk2-auto.git
   cd vhtk2-auto
   python -m venv .venv
   .venv\Scripts\activate
   pip install -e .[dev]
   ```

   Lưu ý: `paddleocr` nặng (kéo cả paddlepaddle, vài GB) — Windows là nơi nó
   được cài lần đầu, cứ để nó chạy hết.
6. Verify môi trường trước khi đụng tới game:

   ```bat
   pytest && ruff check . && mypy
   ```

   Phải **pass toàn bộ** (281 test tại thời điểm viết).

> Mọi lệnh phía dưới chạy trong cửa sổ cmd đã `.venv\Scripts\activate`.
> Sau mỗi lần `git pull` về sau: **restart GUI/worker** — code cũ trong RAM
> không biết thay đổi (kể cả file settings.local.yaml mới thêm).

---

## Bước 1 — Instance mẫu + smoke tầng thiết bị

1. Trong LDPlayer tạo 1 instance, chỉnh đúng chuẩn: **960x540, DPI 160,
   landscape**. Cài game (`com.blackhole.jx2mobile`), login tay 1 lần tới
   màn chơi — xác nhận game chạy được trên máy này.
2. Instance này là **instance MẪU** — acc mới sẽ được `ldconsole copy` từ nó.
   Xem index:

   ```bat
   "C:\LDPlayer\LDPlayer9\ldconsole.exe" list2
   ```
3. Kiểm mục `orchestrator` — `settings.yaml` đã để sẵn giá trị Windows
   (`manage_instances: true`, `template_instance_index: 0`). Instance mẫu KHÔNG
   phải index 0 thì ghi đè trong `config\settings.local.yaml`, đừng sửa
   settings.yaml (file commit dùng chung, sửa là đè cấu hình máy khác):

   ```yaml
   orchestrator:
     template_instance_index: 3    # index instance mẫu vừa xem ở trên
   ```
4. Smoke tầng thiết bị (instance đang mở):

   ```bat
   python tools\smoke_device.py
   ```

   PASS = frame đúng 960x540, 3 ảnh chụp có latency in ra, tap không lỗi.
   Đây là lần đầu `ldconsole`/`resolve_serial` chạy thật — lỗi serial sẽ
   lộ ngay ở đây.

---

## Bước 2 — Xác minh bộ template `v1.0`

Bộ template `v1.0` (chụp trên LDPlayer) và kho map `data\maps\v1.0`
**đã nằm sẵn trong git** từ máy Windows đầu tiên — máy mới KHÔNG phải chụp
lại từ số 0, chỉ cần xác minh chúng còn khớp trên màn LDPlayer của máy này
(`templates.version` trong settings.yaml đã là `v1.0`; máy Mac dev đè thành
`v1.0-avd` qua settings.local.yaml của nó):

```bat
python tools\template_check.py
```

- **OK toàn bộ** → sang bước 3.
- **Có MISS** (hiếm — cùng LDPlayer 960x540 thì render giống nhau): chụp lại
  đúng template đó bằng `python tools\capture.py`, ghi đè trong
  `templates\v1.0\`. Game update làm vỡ hàng loạt thì tạo bộ `v1.1\` mới,
  KHÔNG sửa đè `v1.0\`.
- Nhóm còn thiếu tại thời điểm viết: `bag\` và `trade\` (trade cần cho W3,
  chụp khi đã có 2 acc) — checklist đầy đủ: giai đoạn 3b trong `PLAN.md`.

> Kho map đã quét đủ (~88 map). Chỉ chạy lại `python tools\harvest_maps.py`
> khi game update thêm/đổi map; hiệu chuẩn toạ độ map mới:
> `python tools\calibrate_map.py`.

---

## Bước 3 — Một tài khoản, một instance (chạy lại DoD cũ)

1. Mở GUI:

   ```bat
   python -m vhtk.ui --browser
   ```

   Tab **Cài đặt chung** → khối **An toàn**: xác nhận `dry_run` đang **BẬT**
   (từng bị tắt khi test bán thật trên AVD — kiểm tra lại!). `confirm_mode`
   hiện tắt là đúng: notifier còn là Null (chưa nối Telegram) — bật lại khi
   làm xong 5a.
2. Cùng tab **Cài đặt chung** → khối **Farm mặc định cho mọi tài khoản**: đặt
   map farm + điểm farm, ngưỡng HP, map/điểm rút về, ngưỡng vàng MỘT LẦN cho
   cả dàn acc (ghi vào `settings.yaml` mục `account_defaults.farm`). Khối
   **Thông số farm** và **Chiêu thức** cũng ở đây.
3. Tab **Tài khoản** → tạo acc test: server, user/pass 3CID, slot nhân vật.
   Mục Farm mặc định dùng giá trị chung; acc nào cần khác (map farm riêng...)
   thì tích ô **"riêng"** cạnh trường đó rồi sửa. File acc chỉ ghi trường
   "riêng"; bỏ tích là trường bị xoá khỏi file và dùng chung trở lại.
   (Acc phải ĐÃ tạo nhân vật tay 1 lần — gặp màn "Tạo nhân vật" tool sẽ
   ABORT, đúng thiết kế.) Worker đang chạy chỉ đọc cấu hình lúc khởi động —
   đổi cài đặt chung xong nhớ **Chạy lại** ở tab Thiết bị.
4. Login 5 lần liên tiếp:

   ```bat
   python tools\run_login.py --account config\accounts\farm01.yaml --runs 5
   ```
5. Test bán đồ (an toàn — `dry_run` bật thì KHÔNG bán thật, chỉ in danh
   sách WOULD SELL + lưu ảnh bằng chứng vào `logs\dry_run\`):

   ```bat
   python tools\run_sell.py --account config\accounts\farm01.yaml
   ```

   Runner tự login (idempotent) rồi chạy SellTask: đi tới NPC Chủ Tạp hóa
   → mở Cửa hiệu → chế độ Bán → quét túi → in báo cáo
   (`sold / would_sell / kept_unknown / kept_quality`). Đạt khi status
   `success` và mọi món ngoài whitelist đều nằm ở `kept_unknown`.
6. Farm chu kỳ đầu: tab **Thiết bị** → nút **"Mở & đăng nhập"**. Đây là test
   supervisor thật đầu tiên: `ldconsole launch` → chờ boot → resolve serial
   (lệch thì tự ghi lại YAML) → spawn worker → login → farm. Theo dõi:

   - `logs\workers\farm01.state.json` — task/state hiện tại (GUI cũng hiện);
   - `logs\workers\farm01.out` — stdout worker;
   - `logs\vhtk.jsonl` — log JSON chi tiết.

   Chạy tay không qua GUI: `python -m vhtk.worker --account config\accounts\farm01.yaml`

---

## Bước 4 — Đa instance (nghiệm thu DoD W2)

| # | Test                                                                               | Đạt khi                                                                              |
| - | ---------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------- |
| 1 | Tạo acc thứ 2 trên GUI → nút **"Cấp máy mới"**                       | LDPlayer tự clone instance mẫu, index mới ghi vào YAML acc, tự mở + login + farm |
| 2 | 2 farm chạy song song (mục tiêu 4 giờ)                                         | 2 state file cập nhật độc lập, log riêng từng acc                               |
| 3 | Tạo file `STOP.farm01` ở thư mục gốc (hoặc nút **Dừng** trên GUI) | CHỈ farm01 dừng sạch ở điểm an toàn, farm02 chạy tiếp                         |
| 4 | Tạo file `STOP`                                                                 | Cả hai dừng sạch < 2 phút                                                          |
| 5 | Kill tiến trình 1 worker bằng Task Manager                                      | Worker kia không ảnh hưởng; GUI hiện "dừng" (registry nhận ra PID chết)        |
| 6 | Sửa cấu hình farm02 trên GUI → lưu                                           | CHỈ farm02 restart, farm01 không gián đoạn                                        |

Cả bảng xanh = W2 nghiệm thu xong → giai đoạn kế tiếp là W3 (chuyển vàng
giao dịch 2 nhân vật).

---

**Điều khiển toàn hệ thống + tự phục hồi (D1c)** — tab **Thiết bị**:

- Khung **Toàn hệ thống**: *Chạy tất cả* (bật máy + worker mọi acc farm, acc
  main chỉ mở khi chuyển vàng) · *Chạy lại tất cả* · *Dừng tất cả* (dừng sạch
  + tắt máy LDPlayer). Không vượt `max_concurrent_instances`.
- Thẻ **đỏ "Lỗi — đang tự phục hồi"**: worker chết (mất kết nối, kẹt, crash)
  hoặc treo; tool tự chạy lại sau 1 → 3 → 10 phút, từ lần hỏng thứ 2 liên
  tiếp thì khởi động lại cả máy. Thẻ **đỏ "Cần người xử lý"**: quá 5 lần/giờ
  hoặc lỗi chạy lại cũng không hết (thiếu template, sai server, túi đầy...) —
  đọc lý do trên thẻ, sửa rồi bấm *Chạy lại*.
- Bấm **Dừng** (một máy hoặc tất cả) = tool thôi tự phục hồi máy đó.
- Cấu hình: `settings.yaml` → `orchestrator.watchdog`.

## Những chỗ dễ vấp trên máy trắng

- **`./vhtk` là script bash, KHÔNG chạy trên Windows** — dùng GUI (tab
  Thiết bị) hoặc `python -m vhtk.worker --account ...` trực tiếp. Đây là
  chủ đích: quản lý tiến trình đã chuyển vào Python (psutil), script bash
  chỉ còn là vỏ dev trên Mac.
- **Antivirus/Defender** có thể chặn `ldconsole.exe` hoặc chặn spawn tiến
  trình con — nếu "Mở & đăng nhập" im lặng thất bại, thêm exclusion cho thư
  mục LDPlayer + thư mục repo.
- **PaddleOCR lần chạy đầu tự tải model** (cần mạng) — chạy thử OCR 1 lần
  trước khi test bán đồ, kẻo nghẽn giữa phiên.
- **Cổng ADB lệch**: máy có Android Studio/Nox thì serial có thể lệch công
  thức `5554+2i`. Supervisor tự hỏi ldconsole và ghi lại YAML; nếu log báo
  `cannot resolve adb serial` thì tắt emulator lạ đi.
- **Captcha/màn xác minh** → tool DỪNG và báo người, đúng thiết kế trong
  `CLAUDE.md` — không cố cho nó vượt.
- **Trước khi chạy acc thật có đồ giá trị**: `dry_run` BẬT ít nhất 1 tuần
  đầu, soát log "would sell" mỗi ngày; rồi mới tắt dry_run nhưng giữ
  `confirm_mode`; lộ trình đầy đủ xem W4 trong `PLAN.md`.

## Khi gặp lỗi

Thu thập gửi kèm khi hỏi:

1. Đoạn `logs\workers\<acc>.out` quanh thời điểm lỗi.
2. Đoạn `logs\vhtk.jsonl` tương ứng.
3. Ảnh trong `logs\unknown\` / `logs\abort\` nếu có.
4. Kết quả `python tools\template_check.py` (nếu nghi template vỡ sau khi
   game update — khi đó chụp bộ `v1.1\` mới, KHÔNG sửa đè `v1.0\`).
