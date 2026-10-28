# Kế hoạch triển khai VHTK2 Auto Tool

Cập nhật: 2026-10-25. Trạng thái từng mục xem `PROGRESS.md`; quy tắc kỹ thuật xem `CLAUDE.md`.

**Cách dùng file này:** mỗi giai đoạn có *Mục tiêu → Hạng mục → Thiết kế chính → Test
thuần logic → Tiêu chí nghiệm thu (DoD)*. Chỉ chuyển giai đoạn khi DoD của giai đoạn
trước đạt đủ. Sau mỗi giai đoạn xong: cập nhật `PROGRESS.md`, chạy
`pytest && ruff check . && mypy` phải sạch.

## Nguyên tắc hai môi trường

- **Môi trường dev = Mac + AVD `vhtk2-dev`** (960x540/DPI 160, serial `emulator-5554`,
  đã smoke test PASS). Chỉ có **1 thiết bị** — mọi giai đoạn dev phải chạy được với
  1 instance, 1 tài khoản. Template chụp ở đây để version `v1.0-avd/`.
- **Môi trường production = server Windows x86 + LDPlayer** (4–5 acc farm + 1 acc
  main nhận vàng/đồ). Mọi thứ cần **từ 2 instance trở lên** (orchestrator đa instance,
  chuyển vàng/đồ về main) dồn hết vào các giai đoạn `W*`, làm SAU khi phần 1-thiết-bị
  đã chạy tốt trên Mac. Template production chụp lại trên LDPlayer, version `v1.0/`.
- Code viết một lần chạy cả hai nơi: khác biệt duy nhất là `templates.version` và
  danh sách instance trong config — tuyệt đối không if/else theo môi trường trong logic.

## Lộ trình tổng

| GĐ | Nội dung | Môi trường | Phụ thuộc | Trạng thái |
| --- | --- | --- | --- | --- |
| 0 | Bộ khung (config, logging) | Mac | — | ✅ Xong |
| 1 | Tầng thiết bị (device, ldplayer) | Mac | 0 | ✅ Xong (smoke PASS trên AVD) |
| 2 | Tầng nhận diện (vision, OCR, quality) | Mac | 0 | ✅ Xong |
| 3 | FSM + safety + recovery | Mac | 1, 2 | ✅ Xong (110 test pass) |
| 3b | Bộ template `v1.0-avd` trên AVD | Mac | game đã cài vào AVD | ⬜ |
| 4.1 | R1 Login | Mac | 3, 3b | ⬜ |
| 4.2 | R3 Farm (monitor) | Mac | 4.1 | ⬜ |
| 4.3 | R3b Sell (bán đồ) | Mac | 4.2 | ⬜ |
| 4.4 | R5 Equipment (vũ khí + áo sư môn) | Mac | 4.3 | ⬜ |
| 4.5 | R2 Skill verify | Mac | 4.1 | ⬜ |
| 5a | Notify Telegram thật | Mac | 3 | ⬜ (làm sớm được, cần cho confirm_mode) |
| 5b | Runner 1 tài khoản (vòng đời đầy đủ trên 1 thiết bị) | Mac | 4.1–4.5 | ⬜ |
| D1 | App desktop — quản lý tài khoản & cấu hình (CRUD trên YAML) | Mac | 3 | ⬜ |
| W1 | Dựng server Win: LDPlayer, smoke, template `v1.0` | Win | 5b | ⬜ |
| W2 | Orchestrator đa instance (ldconsole, scheduler, main on-demand) + API điều khiển | Win | W1 | ⬜ |
| D2 | App desktop — quản lý thiết bị: gán acc ↔ instance, mở máy tự login | Win | W2, D1 | ⬜ |
| D3 | App desktop — dashboard giám sát: trạng thái worker, log, ảnh màn, kill-switch, duyệt confirm | Win | D2 | ⬜ |
| W3 | R4 Gold/item transfer — giao dịch 2 nhân vật đứng cạnh nhau | Win | W2 | ⬜ |
| W4 | Vận hành: dry_run → confirm_mode → chạy thật | Win | W3, D3 | ⬜ |

Thứ tự 4.x theo giá trị/rủi ro: login là cửa ngõ; farm tạo giá trị chính; sell/equipment
giữ farm chạy liên tục; skill verify đơn giản, không chặn (acc đã cài skill tay lần đầu).
R4 cần 2 thiết bị online cùng lúc (giao dịch mặt-đối-mặt, đã xác nhận trong game) nên
thuộc nhóm W — nhưng **thiết kế FSM 2 phía viết sẵn từ bây giờ** để W3 chỉ là lắp ráp.

---

## Giai đoạn 0–3 — ĐÃ XONG (tóm tắt)

- **GĐ 0:** cấu trúc repo, `config.py` (pydantic v2, `extra="forbid"`), `logging_setup.py`
  (structlog JSON lines + console), `settings.yaml` đầy đủ key có comment.
- **GĐ 1:** `core/errors.py` (cây exception), `core/device.py` (`InputGate` kill-switch +
  rate limit + delay ngẫu nhiên; screenshot raw RGBA; reconnect 3 lần backoff),
  `core/ldplayer.py` (parse `list2`, `resolve_serial` không bao giờ đoán),
  `tools/smoke_device.py` — **smoke PASS trên AVD Mac** (frame 960x540, ~235–290ms/khung).
- **GĐ 2:** `core/vision.py` (`TemplateStore` + manifest, `find/wait_any/wait_vanish`...,
  NMS; `OCR` PaddleOCR lazy; `detect_quality` theo dải HSV), `tools/capture.py`,
  `tools/template_check.py`.
- **GĐ 3:** `core/fsm.py` (`BaseTask`: kill-switch ranh giới state → recovery trước mọi
  state → handler → validate transition; định tuyến lỗi RECOVER/ABORT/RELOGIN/KILLED),
  `core/recovery.py`, `core/safety.py` (`guard_irreversible` + dry_run/confirm/trần phiên),
  `notify.py` (protocol + NullNotifier).
- **Kiểm tra hiện tại:** 110 test pass, ruff sạch, mypy --strict sạch.

---

## Giai đoạn 3b — Bộ template `v1.0-avd` trên AVD Mac

**Mục tiêu:** đủ nguyên liệu template để code và nghiệm thu GĐ 4 ngay trên Mac.

**Điều kiện vào:** game đã cài trong AVD `vhtk2-dev`, login tay được tới màn chơi.

**Hạng mục:** chụp bằng `tools/capture.py --serial emulator-5554`, lưu vào
`templates/v1.0-avd/`, mỗi template match thử ≥ 0.9 ngay khi cắt:

- `login/`: logo/nút vào game, màn chọn server, màn chọn nhân vật, HUD in-game.
- `common/`: nút X đóng popup, nút Xác nhận, nút Huỷ, biểu tượng mất kết nối,
  nút hồi sinh, khung hội thoại NPC.
- `bag/`: nút mở túi, tiêu đề túi, nút bán, ô trống, icon thuốc, icon phù về thành.
- `map/`: nút mở bản đồ, tên map farm mục tiêu, nút auto đánh (2 trạng thái bật/tắt).
- `shop/`: NPC tạp hoá, NPC bán trang bị, icon vũ khí môn phái, icon áo sư môn, nút Mua.
- `trade/`: (chụp trước được thì tốt, không thì dồn sang W1 — cần 2 nhân vật mới mở
  được khung giao dịch).

Khai `templates/v1.0-avd/manifest.yaml`; đổi `templates.version: v1.0-avd` trong
`settings.yaml` khi dev trên Mac. Chạy `tools/template_check.py` xác nhận.

**DoD:** ✅ đủ template 5 nhóm đầu, match ≥ ngưỡng trên màn AVD thật · ✅ OCR đọc được
số vàng + độ bền trên 3 ảnh chụp mẫu từ game thật.

---

## Giai đoạn 4 — Task nghiệp vụ (toàn bộ dev + nghiệm thu trên AVD, 1 tài khoản)

Khung chung: kế thừa `BaseTask`; tham số trong YAML; test thuần logic không cần thiết
bị; **nghiệm thu live chạy trên AVD với `dry_run: true`**. Khi sang W1 chỉ nghiệm thu
lại nhanh trên LDPlayer với template `v1.0` (code không đổi).

### 4.1 — R1 Login (`tasks/login.py`)

- **States:** `LAUNCH_APP → WAIT_TITLE → SELECT_SERVER → SELECT_ACCOUNT →
  SELECT_CHARACTER → VERIFY_INGAME`.
- Config acc (`config/accounts/*.yaml`): server, định danh acc, slot nhân vật.
- Gặp captcha/màn xác minh → `ABORT` + báo Telegram (tuyệt đối không xử lý).
- **Test:** transition, chọn đúng slot theo config, nhánh captcha → ABORT.
- **DoD (trên AVD):** ✅ từ emulator vừa mở, login tới HUD in-game 5/5 lần liên tiếp
  không can thiệp tay · ✅ đang ở màn bất kỳ trong flow, chạy lại task vẫn về đích.

### 4.2 — R3 Farm (`tasks/farm.py`)

- **States:** `OPEN_MAP → SELECT_SPOT → ARRIVE_VERIFY → ENABLE_AUTO → MONITOR`.
- `MONITOR` (state chạy lâu duy nhất): mỗi `monitor_interval` giây chụp màn 1 lần,
  kiểm: auto còn bật, còn trong map farm, túi mấy phần, vàng hiện tại (OCR), **mức
  máu/mana** (đọc thanh HP), popup hỏng đồ, chết/văng. Sự kiện chỉ TRẢ TÍN HIỆU
  (`BAG_FULL`, `EQUIP_BROKEN`, `GOLD_THRESHOLD`, `HP_LOW`) cho runner, không tự xử lý.
- **Mức máu quay về** (`hp_retreat_percent`): HP dưới ngưỡng liên tiếp N chu kỳ dù
  auto có sẵn tự uống thuốc → tín hiệu `HP_LOW` → runner cho về thành nghỉ/mua thuốc
  rồi quay lại farm (an toàn hơn là đứng chết đi chết lại).
- **Cài đặt nhặt đồ**: dùng cấu hình auto-nhặt CÓ SẴN của game (bật trong bảng auto,
  cùng chỗ cài skill R2) — task chỉ xác minh nó đang bật đúng theo YAML, không tự
  cài lại logic nhặt.
- Config mỗi acc: tên map, toạ độ đứng farm, ngưỡng túi (%), ngưỡng vàng X,
  `hp_retreat_percent`, interval — tất cả sẽ sửa được qua app desktop (D1).
- **Test:** logic ra tín hiệu theo dữ liệu monitor giả, transition.
- **DoD (trên AVD):** ✅ farm liên tục ≥ 2 giờ không can thiệp · ✅ tắt auto tay trong
  game → tool phát hiện và bật lại · ✅ ra đúng tín hiệu khi túi gần đầy thật.

### 4.3 — R3b Sell (`tasks/sell.py`)

- **States:** `USE_TOWN_SCROLL → FIND_NPC → OPEN_SHOP → SCAN_BAG → SELL_ITEMS →
  VERIFY_GOLD → RETURN_TO_FARM` (nối lại R3 từ `OPEN_MAP`).
- Quyết định bán qua **blacklist 2 tầng độc lập** (quy tắc #4 CLAUDE.md): tầng 1 loại
  item (đồ đang mặc, thuốc, phù, quest); tầng 2 ngưỡng phẩm chất từ `detect_quality`.
  Cả 2 PASS mới được đưa vào `guard_irreversible`. **Item giá trị cao không bán** sẽ
  được giữ trong túi — sau này W3 chuyển về main qua giao dịch.
- **Test (trọng tâm nhất GĐ 4):** bảng quyết định bán/không bán đủ tổ hợp loại × phẩm
  chất; 2 tầng độc lập; đếm số item bán khớp log.
- **DoD (trên AVD):** ✅ dry_run: log "would sell" đúng 100% so với người soát túi trong
  3 phiên liền · ✅ confirm_mode hoạt động (từ chối → không bán) · ✅ lần bán thật đầu
  (trên acc test không có đồ quý): KHÔNG mất item blacklist nào.

### 4.4 — R5 Equipment (`tasks/equipment.py`)

- **Phạm vi khoá cứng: CHỈ vũ khí + áo sư môn.** Mọi slot khác không đụng.
- Phát hiện hỏng 2 kênh: popup/icon đỏ trong MONITOR + OCR độ bền khi mở túi.
- **States:** `GO_TO_SHOP → FIND_ITEM (template icon + giá ≤ trần YAML) → BUY (qua
  guard) → OPEN_BAG → EQUIP → VERIFY_SLOT`.
- Config dùng chung mọi acc (cùng môn phái): NPC shop, 2 template item, trần giá.
- **Test:** quyết định mua (giá vượt trần → không mua + báo), chỉ 2 slot hợp lệ.
- **DoD (trên AVD):** ✅ để vũ khí hỏng/độ bền 0 → tool tự mua đúng item, mặc vào, slot
  hết đỏ · ✅ trần giá thấp hơn giá shop → tool KHÔNG mua, báo Telegram.

### 4.5 — R2 Skill verify (`tasks/skill_verify.py`)

- **States:** `OPEN_AUTO_SETTINGS → READ_CURRENT → COMPARE (danh sách YAML của acc) →
  FIX → VERIFY → CLOSE`. Chạy 1 lần sau mỗi login.
- **Test:** so khớp danh sách skill, chỉ sửa ô lệch.
- **DoD (trên AVD):** ✅ xoá 1 skill khỏi auto bằng tay → task gán lại đúng ô, không
  đụng ô đang đúng.

---

## Giai đoạn 5 — Notify + Runner 1 tài khoản (vẫn trên Mac)

### 5a — Telegram thật trong `notify.py` (làm sớm — confirm_mode cần nó)

- `TelegramNotifier` cài `Notifier` protocol: `send_text`, `send_photo`,
  `wait_confirm(timeout)` bằng nút inline Yes/No. Lỗi mạng không được làm chết task
  (queue + retry); confirm timeout = từ chối.
- **Test:** format message, timeout → từ chối, retry khi lỗi mạng (mock httpx).
- **DoD:** ✅ nhận tin + ảnh thật trên Telegram · ✅ bấm Yes/No điều khiển được nhánh task.

### 5b — Runner 1 tài khoản (`orchestrator.py`, phần lõi)

Vòng đời đầy đủ của MỘT acc trên MỘT thiết bị — chạy trọn vẹn trên AVD:

```
LOGIN → SKILL_VERIFY → FARM ⇄ (SELL | EQUIPMENT) → lặp
```

- Scheduler nhận tín hiệu từ MONITOR, chọn task kế tiếp (ưu tiên EQUIPMENT > SELL khi
  trùng); `RELOGIN_REQUIRED` từ bất kỳ task nào → quay về LOGIN; đếm lần ABORT liên
  tiếp, quá ngưỡng → dừng acc + báo Telegram.
- Tín hiệu `GOLD_THRESHOLD` lúc này **chỉ ghi log + báo Telegram** ("đã tích đủ X
  vàng, chờ triển khai W3") — KHÔNG kẹt vòng lặp vì thiếu R4.
- Viết dưới dạng `AccountWorker` độc lập, không biết gì về đa instance — W2 chỉ việc
  bọc N worker + điều phối, không sửa lõi.
- Kill-switch `STOP`: dừng ở ranh giới state.
- **Test:** scheduler chọn đúng task theo tín hiệu (fake task), đếm ABORT, xử lý RELOGIN.
- **DoD (trên AVD):** ✅ 1 acc chạy 4 giờ không can thiệp: farm → đầy túi tự bán →
  hỏng đồ tự thay → quay lại farm, log đủ chu kỳ · ✅ file STOP dừng sạch < 2 phút.

---

## Nhóm D — App desktop quản lý (bổ sung 2026-10-25)

**Mục tiêu:** người vận hành quản lý mọi thứ qua giao diện, không phải sửa YAML tay:
cấu hình từng tài khoản, gán tài khoản ↔ thiết bị, mở thiết bị là tự login, theo dõi
và điều khiển toàn bộ hệ thống đang chạy.

**Nguyên tắc kiến trúc (bắt buộc):**
- GUI là **lớp vỏ mỏng**. Nguồn sự thật về cấu hình vẫn là YAML (pydantic validate);
  GUI đọc/ghi qua đúng các hàm `load_*` hiện có và một module `config_writer` mới.
  Sửa tay YAML vẫn hoạt động như cũ — GUI hỏng không làm tool tê liệt.
- GUI nói chuyện với orchestrator qua **API điều khiển cục bộ** (thêm ở W2): lấy
  trạng thái worker, start/stop instance, bật/tắt dry_run/confirm, kill-switch,
  nhận ảnh màn hình + sự kiện. KHÔNG gọi thẳng vào Device/Vision từ UI.
- Không logic nghiệp vụ trong UI — mọi quyết định (bán gì, khi nào về thành) vẫn
  nằm trong tasks/orchestrator.

**Stack (đã chốt 2026-10-25):** FastAPI làm API cục bộ + giao diện web (HTML/JS
gọn, không framework nặng), gói thành cửa sổ desktop bằng `pywebview`. Lý do:
cùng process Python với orchestrator; cùng một UI vừa là app desktop trên server
vừa mở được từ máy khác qua trình duyệt (bind localhost mặc định, muốn xem từ xa
thì tự mở bind + đặt token trong settings). Giao diện đặt tại `src/vhtk/ui/`.

### D1 — Quản lý tài khoản & cấu hình (chạy được ngay trên Mac, chỉ cần GĐ 3)

- Danh sách tài khoản (đọc `config/accounts/*.yaml`): thêm/sửa/xoá/nhân bản.
- Form theo đúng schema pydantic, validate ngay khi nhập:
  - *Đăng nhập:* server, username/password 3CID (để trống = dùng acc SDK đã lưu),
    slot nhân vật, vai trò main/farm.
  - *Farm:* map, toạ độ đứng, ngưỡng túi, ngưỡng vàng chuyển về main,
    `hp_retreat_percent`, danh sách skill (R2).
  - *Thiết bị:* index LDPlayer, ADB serial.
- Sửa `settings.yaml` phần an toàn (dry_run, confirm_mode, ngưỡng, delay) có cảnh
  báo rõ khi tắt các lớp bảo vệ.
- Mật khẩu trong YAML: giữ nguyên plaintext file local (đã gitignore) + cảnh báo
  trong UI; không phát minh mã hoá nửa vời.
- **Test:** round-trip đọc→sửa→ghi→đọc giữ nguyên dữ liệu + comment không mất
  (dùng `ruamel.yaml` để giữ comment); validate chặn giá trị sai.
- **DoD:** ✅ tạo acc mới hoàn chỉnh chỉ bằng UI, file YAML sinh ra pass
  `load_account` · ✅ sửa ngưỡng vàng qua UI → task đọc được giá trị mới ở lần chạy kế.

### D2 — Quản lý thiết bị (cần W2 vì phụ thuộc ldconsole + orchestrator API)

- Bảng instance LDPlayer (từ `ldconsole list2`): trạng thái chạy/tắt, acc đang gán,
  task đang chạy, state hiện tại.
- Gán tài khoản ↔ instance (ghi vào account YAML); kiểm tra trùng gán.
- Nút **"Mở & đăng nhập"**: start instance → đợi boot → chạy LoginTask của acc đã
  gán → báo kết quả — đúng yêu cầu "mở thiết bị thì tự đăng nhập tài khoản".
- Nút dừng sạch từng worker (dừng ở ranh giới state, không giết giữa hành động).
- **DoD:** ✅ từ UI mở 2 instance, cả 2 tự login đúng acc được gán · ✅ dừng 1 worker
  không ảnh hưởng worker kia.

### D3 — Dashboard giám sát & điều khiển

- Mỗi worker một thẻ: acc, instance, task/state hiện tại, vàng hiện có, độ đầy túi,
  HP, ảnh màn hình mới nhất (cập nhật theo chu kỳ monitor), số recover/abort.
- Dòng sự kiện (từ log JSON lines): bán đồ, mua đồ, chuyển vàng, recover, cảnh báo.
- Điều khiển: kill-switch toàn cục (tạo file STOP), bật/tắt dry_run & confirm_mode,
  và **hàng đợi duyệt confirm ngay trong app** (bấm Yes/No thay vì chỉ qua Telegram —
  cùng cơ chế `Notifier`, Telegram vẫn là kênh khi vắng mặt).
- **DoD:** ✅ nhìn dashboard biết được từng acc đang làm gì không cần mở emulator
  · ✅ bấm kill-switch từ UI → mọi worker dừng sạch < 2 phút · ✅ duyệt một lệnh bán
  qua UI hoạt động như duyệt qua Telegram.

## Nhóm W — Triển khai Windows đa instance (làm sau, khi phần Mac đã xong)

### W1 — Dựng server + template production

- Server Windows **x86 vật lý** (Intel/AMD, 32GB RAM — KHÔNG phải VM trên Mac M1:
  thiếu nested virtualization, LDPlayer không chạy được), bật VT trong BIOS.
- Cài LDPlayer 9, tạo instance chuẩn 960x540/DPI 160; clone đủ 4–5 farm + 1 main.
- Chạy lại `tools/smoke_device.py`; chụp bộ template `v1.0/` (lặp checklist 3b trên
  LDPlayer — thêm nhóm `trade/` giờ đã mở được bằng 2 acc); đổi `templates.version`.
- **DoD:** ✅ smoke PASS trên LDPlayer · ✅ `template_check.py` OK toàn bộ · ✅ chạy lại
  DoD của 4.1 + 5b trên 1 instance LDPlayer (code không đổi, chỉ đổi config).

### W2 — Orchestrator đa instance

- **Mô hình worker (chốt 2026-10-28, phương án A): mỗi acc một TIẾN TRÌNH riêng**
  (`python -m vhtk.worker --account <yaml>`) — cô lập tuyệt đối (crash/kẹt OCR một
  acc không kéo acc khác), restart đúng worker = kill + spawn lại PID trong registry.
  Orchestrator/GUI đọc trạng thái qua file `logs/workers/<stem>.state.json` worker
  tự ghi mỗi lần đổi state (không IPC trực tiếp).
- Supervisor: `ldconsole launch` → đợi boot (`sys.boot_completed`) → resolve serial
  (ghi ngược YAML nếu lệch) → spawn worker.
- Instance main **on-demand**: hàng đợi yêu cầu chuyển vàng/đồ; gom nhiều acc đạt
  ngưỡng gần nhau vào 1 lần mở main; `max_concurrent_instances` (mặc định 5) — mở
  main khi kín slot thì tạm dừng 1 worker farm ở state an toàn.
- Kill-switch toàn cục; 1 worker crash không ảnh hưởng worker khác.
- **API điều khiển cục bộ** (nền cho app desktop D2/D3): trạng thái từng worker
  (task/state/vàng/túi/HP/ảnh mới nhất), start/stop instance, bật tắt dry_run &
  confirm, hàng đợi confirm. Chỉ bind localhost; UI là client duy nhất.
- **Vòng đời theo-từng-tài-khoản từ GUI (yêu cầu chốt 2026-10-26):**
  - "Thêm tài khoản" trên GUI → `POST /api/workers/{stem}/provision`: `ldconsole copy`
    nhân bản từ **instance mẫu** (đã cài game, 960x540/DPI 160) → gán index/cổng ADB
    mới ghi ngược vào YAML acc → launch → chờ ADB → spawn worker riêng (login bằng
    user/pass trong YAML → farm độc lập).
  - **Sổ đăng ký worker** `{stem → PID}` (file + psutil, chạy cả Mac/Win) thay cho
    pgrep toàn cục hiện tại; sửa thông tin acc → restart ĐÚNG worker đó
    (`POST /api/workers/{stem}/restart`), các instance khác không gián đoạn.
  - Kill-switch tách tầng: `STOP` toàn cục + `STOP.{stem}` theo từng acc.
  - Quản lý tiến trình chuyển vào Python (psutil) để GUI dùng chung một đường
    trên Mac lẫn Win; script `./vhtk` bash chỉ còn là lớp vỏ dev trên Mac.
- **Test:** scheduler gom hàng đợi main, nhường slot, dừng sạch khi STOP (fake worker).
- **DoD:** ✅ 2 farm chạy song song 4 giờ, log riêng từng acc · ✅ STOP dừng tất cả
  < 2 phút · ✅ 1 worker crash → worker khác chạy tiếp, Telegram có cảnh báo ·
  ✅ thêm acc trên GUI → instance mới tự mở, tự login, tự farm; sửa acc → chỉ
  instance đó restart.

### W3 — R4 Gold/item transfer (`tasks/gold_transfer.py`)

Giao dịch trực tiếp — **đã xác nhận trong game: 2 nhân vật phải đứng cạnh nhau**.

- Hai FSM phối hợp qua orchestrator:
  - *Farm side:* `GO_RENDEZVOUS → WAIT_PARTNER → INVITE_TRADE → PUT_GOLD (giữ lại
    `gold_keep`) [+ PUT_ITEMS nếu bật] → LOCK → CONFIRM → VERIFY_AFTER`.
  - *Main side:* `LOGIN (tái dùng R1) → GO_RENDEZVOUS → ACCEPT_TRADE → LOCK →
    CONFIRM → VERIFY_AFTER → LOGOUT`.
- Điểm hẹn cố định trong thành (toạ độ YAML); barrier 2 bên qua orchestrator — cả hai
  báo "đã tới" mới mời giao dịch; chờ quá timeout → huỷ sạch, về farm, thử lại sau.
- **Chuyển đồ (tuỳ chọn, config `transfer_items`):** item giá trị cao mà R3b không bán
  (phẩm chất ≥ ngưỡng) được đặt vào khung giao dịch cùng đợt với vàng — acc main là
  nơi "nhận đồ" đúng như yêu cầu.
- Xác minh: OCR vàng CẢ 2 BÊN trước/sau, `sent == received` (dung sai phí) mới ghi
  nhận thành công; lệch → ABORT + ảnh + Telegram ngay. `PUT_GOLD`/`CONFIRM` qua
  `guard_irreversible` (dry_run dừng trước CONFIRM).
- **Test (viết sẵn từ giai đoạn Mac được):** tính `amount = gold - gold_keep`, so khớp
  trước/sau có dung sai, nhánh timeout chờ bạn giao dịch → huỷ an toàn, barrier 2 bên.
- **DoD:** ✅ dry_run: 2 instance gặp nhau, mở khung giao dịch, dừng đúng trước xác
  nhận · ✅ chuyển thật lần đầu với confirm_mode: vàng về main khớp OCR 2 bên · ✅ main
  không tới được điểm hẹn → farm huỷ sạch, quay lại farm, không kẹt.

### W4 — Vận hành & bàn giao

1. **Tuần đầu — dry_run toàn bộ:** soát log "would sell/buy/trade" mỗi ngày.
2. **5 phiên kế — tắt dry_run, confirm_mode BẬT:** duyệt từng lệnh qua Telegram.
3. **Chạy thật:** tắt confirm với bán đồ trước; chuyển vàng giữ confirm thêm 1 tuần.
4. **Sau mỗi lần game update:** `template_check.py` → vỡ thì chụp bộ `v1.x` mới
   (không sửa đè), đổi version, chạy 1 chu kỳ dry_run trước khi chạy thật.
5. **Theo dõi thường trực:** `logs/unknown/`, tỉ lệ dùng toạ độ fallback, latency.

**DoD toàn dự án:** 4 acc farm + 1 main chạy 24h không can thiệp tay: farm → đầy túi
tự bán (không mất đồ blacklist) → hỏng vũ khí/áo tự mua mặc lại → đạt ngưỡng vàng tự
gom vàng + đồ quý về main, mọi sự kiện có log + Telegram, file STOP dừng toàn hệ thống.
