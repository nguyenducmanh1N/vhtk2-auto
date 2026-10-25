
# VHTK2 Auto Tool

## Bối cảnh dự án

Tool tự động hoá thao tác cho game **Võ Hiệp Truyền Kỳ 2 Mobile** (3C Game,
package `com.blackhole.jx2mobile`), chạy trên trình giả lập **LDPlayer 9** ở Windows.
Game là bản chuyển thể Võ Lâm Truyền Kỳ 2 PC 2008, đồ hoạ 2.5D, có sẵn hệ thống
auto in-game (auto đánh, auto tìm đường, auto nhặt đồ).

**Triết lý thiết kế cốt lõi:** tool KHÔNG chơi game thay người. Tool là lớp
**điều phối** đứng trên auto có sẵn của game — bật đúng auto, đúng lúc, và xử lý
những việc auto in-game không làm được (bán đồ, mua trang bị, chuyển vàng giữa acc,
phục hồi khi sự cố). Bất cứ khi nào game đã có tính năng tự động, tool phải dùng lại
thay vì tự cài đặt lại từ đầu.

## Ngôn ngữ

- Trao đổi với tôi bằng **tiếng Việt**.
- Code, tên biến, tên hàm, docstring: **tiếng Anh**.
- Comment giải thích logic nghiệp vụ game: **tiếng Việt** (vì thuật ngữ game là tiếng Việt).
- Log message: tiếng Anh, ngắn gọn, có mã lỗi.

## Stack kỹ thuật

| Thành phần             | Lựa chọn                            | Ghi chú                                |
| ------------------------ | ------------------------------------- | --------------------------------------- |
| Ngôn ngữ               | Python 3.11+                          |                                         |
| Điều khiển thiết bị | `adbutils`                          | qua ADB tới LDPlayer instance          |
| Quản lý instance       | `ldconsole.exe` (CLI của LDPlayer) | mở/tắt/liệt kê instance             |
| Nhận diện ảnh         | OpenCV (`cv2.matchTemplate`)        | template matching                       |
| OCR                      | PaddleOCR (`lang='vi'`)             | đọc số vàng, tên map, độ bền    |
| Cấu hình               | YAML (`pyyaml`)                     | không hardcode bất cứ toạ độ nào |
| Validate config          | `pydantic` v2                       |                                         |
| Logging                  | `structlog` → JSON lines           |                                         |
| Thông báo              | Telegram Bot API (`httpx`)          |                                         |
| Test                     | `pytest`                            |                                         |

## Môi trường chuẩn hoá

Mọi instance LDPlayer PHẢI cấu hình giống hệt nhau, nếu không template sẽ vỡ:

- Độ phân giải: **960 x 540**, DPI **160**
- Android 9 (LDPlayer 9)
- Xoay ngang (landscape)
- Đường dẫn ldconsole mặc định: `C:\LDPlayer\LDPlayer9\ldconsole.exe`
- Mỗi instance một cổng ADB: `emulator-5554`, `emulator-5556`, ...

## Quyết định đã chốt (2026-10-25)

- **Hạ tầng production:** server Windows 32GB RAM, CPU/GPU tầm trung → chạy đồng thời
  tối đa 5–6 instance LDPlayer. Cấu hình chuẩn: **4–5 acc farm + 1 acc main** (nhận vàng).
- **Instance main KHÔNG chạy thường trực** — orchestrator chỉ bật khi có acc farm đạt
  ngưỡng vàng, giao dịch xong thì tắt để tiết kiệm tài nguyên.
- **Chuyển vàng (R4) = giao dịch trực tiếp** giữa 2 nhân vật tại điểm hẹn cố định trong
  thành. Đã tra cứu (10/2026): game KHÔNG có gửi vàng qua thư — hộp thư chỉ nhận thưởng
  hệ thống. Orchestrator điều phối 2 instance; OCR xác minh số vàng 2 bên trước/sau
  giao dịch mới tính là thành công.
- **Trang bị (R5): CHỈ vũ khí + áo sư môn.** Không mua/thay bất kỳ trang bị nào khác.
- **Mọi acc farm cùng 1 môn phái** → dùng chung 1 bộ config shop + template trang bị.
- **Môi trường dev:** máy macOS với AVD `vhtk2-dev` (960x540/DPI 160, serial
  `emulator-5554`) — CHỈ CÓ 1 THIẾT BỊ. Toàn bộ phần 1-tài-khoản (login, farm, sell,
  equipment, skill, runner 5b) phát triển và nghiệm thu tại đây với template
  `templates/v1.0-avd/`. LDPlayer KHÔNG chạy được trên Mac M1 (kể cả qua Windows VM —
  thiếu nested virtualization).
- **Mọi thứ cần ≥ 2 instance** (orchestrator đa instance, R4 chuyển vàng/đồ về main —
  giao dịch yêu cầu 2 nhân vật đứng cạnh nhau, đã xác nhận trong game) dồn vào nhóm
  giai đoạn `W*` trong PLAN.md, triển khai sau trên server Windows x86 vật lý với
  template `templates/v1.0/`. Code không if/else theo môi trường — chỉ khác config.
- **Acc main nhận cả vàng lẫn đồ quý:** item phẩm chất cao mà sell không bán sẽ được
  chuyển về main cùng đợt giao dịch vàng (config `transfer_items`, làm ở W3).

## Tài liệu dự án

- `PLAN.md` — kế hoạch chi tiết từng giai đoạn: hạng mục, thiết kế, test, tiêu chí nghiệm thu.
- `PROGRESS.md` — trạng thái hiện tại; cập nhật sau khi hoàn thành mỗi giai đoạn/task.

## Cấu trúc thư mục

```
vhtk2-auto/
├── CLAUDE.md
├── PLAN.md                     # kế hoạch chi tiết từng giai đoạn
├── PROGRESS.md                 # trạng thái hiện tại
├── pyproject.toml
├── config/
│   ├── settings.yaml           # cấu hình chung
│   ├── accounts/
│   │   ├── main.yaml           # acc nhận vàng
│   │   ├── farm01.yaml
│   │   └── farm02.yaml
│   └── coordinates.yaml        # toạ độ UI (fallback khi template fail)
├── templates/
│   └── v1.0/                   # ảnh mẫu theo phiên bản game
│       ├── login/
│       ├── common/             # popup, nút X, nút xác nhận
│       ├── bag/
│       ├── map/
│       ├── shop/
│       └── trade/
├── src/vhtk/
│   ├── core/
│   │   ├── device.py           # ADB wrapper
│   │   ├── ldplayer.py         # ldconsole wrapper
│   │   ├── vision.py           # template matching + OCR
│   │   ├── fsm.py              # state machine base
│   │   ├── recovery.py         # kiểm tra & phục hồi trước mọi state
│   │   └── safety.py           # kill-switch, dry_run, confirm_mode
│   ├── tasks/
│   │   ├── login.py            # R1
│   │   ├── skill_verify.py     # R2
│   │   ├── farm.py             # R3
│   │   ├── sell.py             # R3b
│   │   ├── equipment.py        # R5
│   │   └── gold_transfer.py    # R4
│   ├── orchestrator.py         # điều phối đa instance
│   └── notify.py
├── tools/
│   ├── capture.py              # công cụ chụp & cắt template
│   └── template_check.py       # kiểm tra template còn khớp sau update game
├── logs/
└── tests/
```

## Quy tắc kỹ thuật bắt buộc

### 1. Không bao giờ tap mù

Mọi hành động phải theo chu trình: **tìm → tap → xác minh**.

```python
# SAI
device.tap(450, 320)

# ĐÚNG
btn = vision.wait_for("common/btn_confirm", timeout=5)
if not btn:
    raise StateTimeout("confirm button not found")
device.tap(*btn.center)
if not vision.wait_for("bag/bag_closed", timeout=5):
    raise VerifyFailed("bag did not close after confirm")
```

Toạ độ cứng chỉ được dùng làm phương án dự phòng khi template thất bại, và
phải ghi log cảnh báo mỗi lần dùng.

### 2. Mọi task là một máy trạng thái

Kế thừa `BaseTask`. Mỗi state có: tên, hàm thực thi, timeout riêng, và danh sách
state kế tiếp hợp lệ. Luôn có state `RECOVER` và `ABORT`. Không viết task dạng
script tuần tự dài.

### 3. Recovery chạy trước mọi state

Trước khi vào bất kỳ state nào, gọi `recovery.check()`:

- Có popup che màn hình → đóng
- Bị văng về màn login → chạy lại task login
- Nhân vật chết → hồi sinh
- Mất kết nối → chờ + reconnect
- Màn hình lạ không nhận diện được → chụp ảnh, lưu vào `logs/unknown/`, báo Telegram

### 4. An toàn dữ liệu người chơi

Đây là ưu tiên cao hơn cả việc tool chạy được:

- **Blacklist bán đồ:** không bao giờ bán item đang trang bị, thuốc, phù về thành,
  hoặc item có phẩm chất trên ngưỡng cấu hình. Blacklist kiểm tra ở 2 tầng độc lập.
- **Chế độ `dry_run`:** mặc định BẬT. Ở chế độ này tool làm mọi thứ trừ các hành động
  không thể hoàn tác (bán, mua, giao dịch) — thay vào đó chụp màn và ghi log.
- **Chế độ `confirm_mode`:** với bán đồ và chuyển vàng, gửi ảnh lên Telegram và chờ
  người xác nhận trước khi thực hiện. Bật mặc định trong 5 phiên chạy đầu.
- **Kill-switch:** file `STOP` xuất hiện trong thư mục gốc → mọi task dừng sạch ở
  state an toàn gần nhất.
- **Giới hạn tốc độ:** tối đa N hành động/phút (cấu hình được), delay ngẫu nhiên
  giữa các thao tác để tránh spam server.

### 5. Không hardcode

Toạ độ, ngưỡng match, timeout, tên map, ngưỡng vàng, danh sách item — tất cả
nằm trong YAML. Code chỉ chứa logic.

### 6. Template có version

Thư mục `templates/v1.0/`. Khi game update, tạo `v1.1/` mới, không sửa đè.
`settings.yaml` trỏ tới version đang dùng.

## Phạm vi KHÔNG làm

- Không viết code can thiệp bộ nhớ game, sửa gói tin, hook API, hoặc chỉnh sửa APK.
- Không viết code nhằm né/đánh lừa cơ chế phát hiện của nhà phát hành.
- Không xử lý captcha hoặc màn xác minh người chơi — gặp thì DỪNG và báo người.
- Toàn bộ tương tác chỉ qua ADB input và đọc ảnh màn hình, đúng như một công cụ
  UI testing.

## Cách làm việc tôi mong muốn

- Trước khi code một module lớn, trình bày thiết kế ngắn gọn để tôi duyệt.
- Viết từng module một, chạy được rồi mới sang module kế.
- Mỗi hàm public có docstring và type hint đầy đủ.
- Đề xuất test cho phần logic thuần (không cần thiết bị) — parse config, state
  transition, quyết định bán/không bán.
- Khi có nhiều cách làm, nêu 2 phương án kèm đánh đổi rồi hỏi tôi, đừng tự chọn.
- Nói thẳng khi thấy yêu cầu của tôi có vấn đề về thiết kế hay rủi ro.
