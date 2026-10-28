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
   sửa `ldplayer.ldconsole_path` trong `config\settings.yaml`.
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

   Phải **pass toàn bộ** (267 test tại thời điểm viết).

> Mọi lệnh phía dưới chạy trong cửa sổ cmd đã `.venv\Scripts\activate`.

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
3. Sửa `config\settings.yaml`, mục `orchestrator`:

   ```yaml
   orchestrator:
     manage_instances: true        # Windows: orchestrator tự mở/tắt instance
     template_instance_index: 0    # index instance mẫu vừa xem ở trên
   ```
4. Smoke tầng thiết bị (instance đang mở):

   ```bat
   python tools\smoke_device.py
   ```

   PASS = frame đúng 960x540, 3 ảnh chụp có latency in ra, tap không lỗi.
   Đây là lần đầu `ldconsole`/`resolve_serial` chạy thật — lỗi serial sẽ
   lộ ngay ở đây.

---

## Bước 2 — Bộ template `v1.0` (việc tay nhiều nhất)

Template `v1.0-avd` chụp trên AVD Mac **sẽ không khớp** trên LDPlayer
(render khác) — phải chụp lại:

1. Chụp theo checklist giai đoạn 3b trong `PLAN.md` bằng
   `python tools\capture.py`, lưu vào `templates\v1.0\`: nhóm `login/`,
   `common/`, `map/`, `bag/`, `shop/` (và `trade/` khi đã có 2 acc).
2. Tạo `templates\v1.0\manifest.yaml` — chép cấu trúc từ
   `templates\v1.0-avd\manifest.yaml` làm mẫu (ROI/ngưỡng đã tinh chỉnh sẵn,
   chỉ thay ảnh).
3. Quét lại kho map trên LDPlayer: `python tools\harvest_maps.py`; hiệu
   chuẩn map chính: `python tools\calibrate_map.py`.
4. Đổi `templates.version: v1.0` trong settings.yaml, rồi:

   ```bat
   python tools\template_check.py
   ```

   phải OK toàn bộ.

> Mẹo: không cần đủ 100% ngay — đủ `login/` + `common/` + `map/` là test
> được bước 3; `bag/`/`shop/` cần khi test bán đồ.

---

## Bước 3 — Một tài khoản, một instance (chạy lại DoD cũ)

1. Mở GUI:

   ```bat
   python -m vhtk.ui --browser
   ```

   Tab **An toàn**: xác nhận `dry_run` + `confirm_mode` đang **BẬT**
   (từng bị tắt khi test bán thật trên AVD — kiểm tra lại!).
2. Tab **Tài khoản** → tạo acc test: server, user/pass 3CID, slot nhân vật,
   map farm + điểm farm, map rút về. (Acc phải ĐÃ tạo nhân vật tay 1 lần —
   gặp màn "Tạo nhân vật" tool sẽ ABORT, đúng thiết kế.)
3. Login 5 lần liên tiếp:

   ```bat
   python tools\run_login.py --account config\accounts\farm01.yaml --runs 5
   ```
4. Farm chu kỳ đầu: tab **Thiết bị** → nút **"Mở & đăng nhập"**. Đây là test
   supervisor thật đầu tiên: `ldconsole launch` → chờ boot → resolve serial
   (lệch thì tự ghi lại YAML) → spawn worker → login → farm. Theo dõi:
   - `logs\workers\farm01.state.json` — task/state hiện tại (GUI cũng hiện);
   - `logs\workers\farm01.out` — stdout worker;
   - `logs\vhtk.jsonl` — log JSON chi tiết.

   Chạy tay không qua GUI: `python -m vhtk.worker --account config\accounts\farm01.yaml`

---

## Bước 4 — Đa instance (nghiệm thu DoD W2)

| # | Test | Đạt khi |
|---|------|---------|
| 1 | Tạo acc thứ 2 trên GUI → nút **"Cấp máy mới"** | LDPlayer tự clone instance mẫu, index mới ghi vào YAML acc, tự mở + login + farm |
| 2 | 2 farm chạy song song (mục tiêu 4 giờ) | 2 state file cập nhật độc lập, log riêng từng acc |
| 3 | Tạo file `STOP.farm01` ở thư mục gốc (hoặc nút **Dừng** trên GUI) | CHỈ farm01 dừng sạch ở điểm an toàn, farm02 chạy tiếp |
| 4 | Tạo file `STOP` | Cả hai dừng sạch < 2 phút |
| 5 | Kill tiến trình 1 worker bằng Task Manager | Worker kia không ảnh hưởng; GUI hiện "dừng" (registry nhận ra PID chết) |
| 6 | Sửa cấu hình farm02 trên GUI → lưu | CHỈ farm02 restart, farm01 không gián đoạn |

Cả bảng xanh = W2 nghiệm thu xong → giai đoạn kế tiếp là W3 (chuyển vàng
giao dịch 2 nhân vật).

---

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
