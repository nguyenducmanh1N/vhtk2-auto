# Kế hoạch triển khai VHTK2 Auto Tool

Cập nhật: 2026-10-25. Trạng thái từng mục xem `PROGRESS.md`; quy tắc kỹ thuật xem `CLAUDE.md`.

**Cách dùng file này:** mỗi giai đoạn có *Mục tiêu → Hạng mục → Thiết kế chính → Test
thuần logic → Tiêu chí nghiệm thu (DoD)*. Chỉ chuyển giai đoạn khi DoD của giai đoạn
trước đạt đủ. Sau mỗi giai đoạn xong: cập nhật `PROGRESS.md`, chạy
`pytest && ruff check . && mypy` phải sạch.

## Lộ trình tổng

| GĐ | Nội dung | Phụ thuộc | Trạng thái |
| --- | --- | --- | --- |
| 0 | Bộ khung (config, logging) | — | ✅ Xong |
| 1 | Tầng thiết bị (device, ldplayer) | 0 | ✅ Xong — nợ smoke test Windows |
| 2 | Tầng nhận diện (vision, OCR, quality) | 0 | ✅ Xong |
| 3 | FSM + safety + recovery | 1, 2 | ✅ Xong (110 test pass) |
| 3b | Smoke test Windows + chụp bộ template đầu | 1 | ⬜ (làm song song GĐ 3) |
| 4.1 | R1 Login | 3, 3b | ⬜ |
| 4.2 | R3 Farm (monitor) | 4.1 | ⬜ |
| 4.3 | R3b Sell (bán đồ) | 4.2 | ⬜ |
| 4.4 | R5 Equipment (vũ khí + áo sư môn) | 4.3 | ⬜ |
| 4.5 | R2 Skill verify | 4.1 | ⬜ |
| 4.6 | R4 Gold transfer (giao dịch 2 instance) | 4.2, GĐ 5 một phần | ⬜ |
| 5 | Notify Telegram + Orchestrator đa instance | 3 | ⬜ (notify làm sớm, orchestrator sau 4.2) |
| 6 | Vận hành: dry_run → confirm_mode → chạy thật | tất cả | ⬜ |

Thứ tự 4.x theo giá trị/rủi ro: login là cửa ngõ mọi thứ; farm tạo giá trị chính;
sell/equipment giữ farm chạy liên tục; skill verify đơn giản và không chặn (acc đã cài
skill tay lần đầu); gold transfer phức tạp nhất (2 thiết bị) nên làm cuối cùng.

---

## Giai đoạn 0–2 — ĐÃ XONG (tóm tắt)

- **GĐ 0:** cấu trúc repo, `config.py` (pydantic v2, `extra="forbid"`), `logging_setup.py`
  (structlog JSON lines + console), `settings.yaml` đầy đủ key có comment.
- **GĐ 1:** `core/errors.py` (cây exception), `core/device.py` (`InputGate` kill-switch +
  rate limit + delay ngẫu nhiên; screenshot raw RGBA; reconnect 3 lần backoff),
  `core/ldplayer.py` (parse `list2`, `resolve_serial` không bao giờ đoán),
  `tools/smoke_device.py`.
- **GĐ 2:** `core/vision.py` (`TemplateStore` + manifest, `find/find_all/wait_for/
  wait_any/wait_vanish`, NMS; `OCR` PaddleOCR lazy, `read_text/read_number/read_fraction`;
  `detect_quality` theo dải HSV), `tools/capture.py`, `tools/template_check.py`.
- **Kiểm tra hiện tại:** 75 test pass, ruff sạch, mypy --strict sạch.

**Nợ tồn đọng:** smoke test GĐ 1 trên máy Windows thật (gộp vào GĐ 3b).

---

## Giai đoạn 3 — FSM + Safety + Recovery

> Trình thiết kế chi tiết API để duyệt trước khi code (đúng quy trình CLAUDE.md).
> Dưới đây là khung đã thống nhất ở mức kiến trúc.

**Mục tiêu:** nền tảng cho mọi task — máy trạng thái có kiểm soát, an toàn dữ liệu
người chơi áp ở một chỗ duy nhất, phục hồi sự cố trước mọi bước.

**Hạng mục:**

1. `core/fsm.py`
   - `State`: tên, handler, timeout riêng, danh sách state kế tiếp hợp lệ.
   - `TaskContext`: device, vision, settings, account, logger, safety — bơm vào handler.
   - `BaseTask.run()`: vòng lặp `kill-switch → recovery.check() → handler → validate
     transition`. Handler trả state kế tiếp; chuyển sang state không khai báo → lỗi lập trình, ABORT.
   - State bắt buộc: `RECOVER` (đếm số lần, quá `max_recover_attempts` → ABORT) và
     `ABORT` (chụp màn cuối, log lý do, báo Telegram).
   - `TaskResult`: SUCCESS / ABORTED / KILLED kèm state cuối + lý do.
2. `core/recovery.py` — `Recovery.check(frame)` trả hành động, thứ tự kiểm tra:
   popup che màn (template `common/`) → văng về login → nhân vật chết → mất kết nối →
   màn hình lạ (lưu `logs/unknown/` + báo Telegram, trả `UNKNOWN`).
3. `core/safety.py`
   - `Safety.guard_irreversible(action, screenshot)`: cổng duy nhất cho bán/mua/giao dịch.
     `dry_run` BẬT → chụp màn + log, KHÔNG thực hiện, trả `skipped`. `confirm_mode` →
     gửi ảnh Telegram, chờ xác nhận (timeout → coi như từ chối).
   - Đếm hành động không hoàn tác mỗi phiên, trần cấu hình được.

**Test thuần logic (fake device/vision/clock):** transition hợp lệ/không hợp lệ, timeout
state → RECOVER, recovery được gọi trước MỌI state, quá max_recover → ABORT, kill-switch
giữa chừng → KILLED ở ranh giới state, dry_run chặn đủ 3 loại hành động, confirm_mode
timeout → từ chối, recovery ưu tiên đúng thứ tự khi nhiều sự cố cùng lúc.

**DoD:** ✅ thiết kế API được duyệt · ✅ một `DemoTask` giả chạy hết vòng đời bằng fake
device trong test · ✅ pytest/ruff/mypy sạch · ✅ không task nghiệp vụ nào gọi được
hành động không hoàn tác mà né `guard_irreversible` (kiểm bằng review + grep).

---

## Giai đoạn 3b — Smoke test Windows + bộ template đầu tiên

**Mục tiêu:** xác nhận tầng thiết bị với phần cứng thật, có nguyên liệu template cho GĐ 4.

**Hạng mục (làm trên server Windows, cần người thao tác cùng):**

1. Cài LDPlayer 9, tạo instance chuẩn 960x540/DPI 160, cài game, login tay 1 acc.
2. `python tools/smoke_device.py` — ghi lại latency screenshot thực tế (mục tiêu < 300ms).
3. Chụp template bằng `tools/capture.py`, mỗi template match thử ≥ 0.9 ngay khi cắt:
   - `login/`: logo/nút vào game, màn chọn server, màn chọn nhân vật, HUD in-game.
   - `common/`: nút X đóng popup, nút Xác nhận, nút Huỷ, biểu tượng mất kết nối,
     nút hồi sinh, khung hội thoại NPC.
   - `bag/`: nút mở túi, tiêu đề túi, nút bán, ô trống, icon thuốc, icon phù về thành.
   - `map/`: nút mở bản đồ, tên map farm mục tiêu, nút auto đánh (bật/tắt — 2 trạng thái).
   - `shop/`: NPC tạp hoá, NPC bán trang bị, icon vũ khí môn phái, icon áo sư môn, nút Mua.
   - `trade/`: nút mời giao dịch, khung giao dịch, ô nhập vàng, nút Khoá, nút Đồng ý.
4. Khai `templates/v1.0/manifest.yaml` (ngưỡng/ROI/grayscale riêng nếu cần).
5. `python tools/template_check.py` trên màn hình thật: mọi template nhóm liên quan OK.

**DoD:** ✅ smoke test pass, frame đúng 960x540 · ✅ đủ template 6 nhóm trên, mỗi cái
match ≥ ngưỡng trên màn thật · ✅ số vàng/độ bền đọc được bằng OCR trên 3 ảnh chụp mẫu.

---

## Giai đoạn 4 — Task nghiệp vụ

Khung chung mọi task: kế thừa `BaseTask`; mọi tham số trong YAML; test thuần logic cho
quyết định (không cần thiết bị); nghiệm thu live luôn chạy với `dry_run: true` trước.

### 4.1 — R1 Login (`tasks/login.py`)

- **States:** `LAUNCH_APP → WAIT_TITLE → SELECT_SERVER → SELECT_ACCOUNT →
  SELECT_CHARACTER → VERIFY_INGAME`.
- Config acc (`config/accounts/*.yaml`): server, định danh acc, slot nhân vật.
- Gặp captcha/màn xác minh → `ABORT` + báo Telegram (tuyệt đối không xử lý).
- **Test:** transition, chọn đúng slot theo config, nhánh captcha → ABORT.
- **DoD:** ✅ từ instance vừa mở, login tới HUD in-game 5/5 lần liên tiếp không can thiệp
  tay · ✅ đang ở màn bất kỳ trong flow login, chạy lại task vẫn về đích (idempotent).

### 4.2 — R3 Farm (`tasks/farm.py`)

- **States:** `OPEN_MAP → SELECT_SPOT → ARRIVE_VERIFY → ENABLE_AUTO → MONITOR`.
- `MONITOR` (state chạy lâu duy nhất): mỗi `monitor_interval` giây chụp màn 1 lần, kiểm:
  auto còn bật, còn trong map farm, túi mấy phần (OCR/đếm ô), vàng hiện tại (OCR),
  popup hỏng đồ, chết/văng. Sự kiện chỉ TRẢ TÍN HIỆU cho scheduler
  (`BAG_FULL`, `EQUIP_BROKEN`, `GOLD_THRESHOLD`), không tự xử lý tại chỗ.
- Config: tên map, toạ độ đứng farm, ngưỡng túi (%), ngưỡng vàng X, interval.
- **Test:** logic ra tín hiệu theo dữ liệu monitor giả (túi 95% → BAG_FULL...), transition.
- **DoD:** ✅ farm liên tục ≥ 2 giờ không can thiệp, log monitor đủ chu kỳ · ✅ giết auto
  giữa chừng (tắt tay trong game) → tool phát hiện và bật lại · ✅ ra đúng tín hiệu khi
  túi gần đầy thật.

### 4.3 — R3b Sell (`tasks/sell.py`)

- **States:** `USE_TOWN_SCROLL → FIND_NPC → OPEN_SHOP → SCAN_BAG → SELL_ITEMS →
  VERIFY_GOLD → RETURN_TO_FARM` (nối lại R3 từ `OPEN_MAP`).
- Quyết định bán qua **blacklist 2 tầng độc lập** (quy tắc #4 CLAUDE.md): tầng 1 loại
  item (đồ đang mặc, thuốc, phù, quest); tầng 2 ngưỡng phẩm chất từ `detect_quality`.
  Cả 2 PASS mới được đưa vào `guard_irreversible`.
- **Test (trọng tâm nhất GĐ 4):** bảng quyết định bán/không bán đủ tổ hợp loại × phẩm chất;
  2 tầng độc lập (tầng 1 chặn dù tầng 2 cho phép và ngược lại); đếm số item bán ra khớp log.
- **DoD:** ✅ dry_run: ảnh chụp + log "would sell" đúng 100% so với người soát lại túi
  trong 3 phiên liền · ✅ confirm_mode hoạt động (ảnh lên Telegram, từ chối → không bán)
  · ✅ sau bán thật đầu tiên: KHÔNG mất bất kỳ item blacklist nào (soát tay).

### 4.4 — R5 Equipment (`tasks/equipment.py`)

- **Phạm vi khoá cứng: CHỈ vũ khí + áo sư môn.** Mọi slot khác không đụng tới.
- Phát hiện hỏng 2 kênh: popup/icon đỏ trong `MONITOR` (R3) + OCR độ bền khi mở túi (R3b).
- **States:** `GO_TO_SHOP → FIND_ITEM (template icon + kiểm giá ≤ trần YAML) → BUY
  (qua guard) → OPEN_BAG → EQUIP → VERIFY_SLOT`.
- Config dùng chung mọi acc (cùng môn phái): NPC shop, 2 template item, trần giá.
- **Test:** quyết định mua (giá vượt trần → không mua + báo), chỉ 2 slot hợp lệ, transition.
- **DoD:** ✅ kịch bản thật: tháo vũ khí cho hỏng/để độ bền 0 → tool tự mua đúng item,
  mặc vào, slot hết đỏ · ✅ giá shop bị đặt cao hơn trần → tool KHÔNG mua, báo Telegram.

### 4.5 — R2 Skill verify (`tasks/skill_verify.py`)

- **States:** `OPEN_AUTO_SETTINGS → READ_CURRENT → COMPARE (với danh sách YAML của acc)
  → FIX (nếu lệch) → VERIFY → CLOSE`. Chạy 1 lần sau mỗi login.
- **Test:** so khớp danh sách skill, chỉ sửa ô lệch.
- **DoD:** ✅ xoá 1 skill khỏi cấu hình auto trong game bằng tay → chạy task → skill được
  gán lại đúng ô, không đụng ô đang đúng.

### 4.6 — R4 Gold transfer (`tasks/gold_transfer.py`) — phức tạp nhất, làm cuối

- **Phương án đã chốt: giao dịch trực tiếp** (game không có gửi vàng qua thư).
- Hai FSM phối hợp qua orchestrator:
  - *Farm side:* `GO_RENDEZVOUS → WAIT_PARTNER → INVITE_TRADE → PUT_GOLD (giữ lại
    `gold_keep` cho phí sinh hoạt) → LOCK → CONFIRM → VERIFY_GOLD_AFTER`.
  - *Main side:* `LOGIN (tái dùng R1) → GO_RENDEZVOUS → ACCEPT_TRADE → LOCK → CONFIRM →
    VERIFY_GOLD_AFTER → LOGOUT`.
- Điểm hẹn: toạ độ cố định trong thành, khai YAML. Đồng bộ qua barrier của orchestrator
  (hai bên đều báo "đã tới" mới sang bước mời giao dịch; chờ quá timeout → huỷ, về farm,
  thử lại chu kỳ sau).
- Xác minh: OCR vàng CẢ 2 BÊN trước/sau; `sent == received` (trong sai số phí nếu có)
  mới ghi nhận thành công; lệch → ABORT + ảnh + báo Telegram ngay.
- `PUT_GOLD`/`CONFIRM` đi qua `guard_irreversible` (dry_run dừng trước CONFIRM).
- **Test:** tính `amount = gold - gold_keep`, so khớp trước/sau có dung sai phí,
  nhánh timeout chờ bạn giao dịch → huỷ an toàn, barrier 2 bên.
- **DoD:** ✅ dry_run: 2 instance gặp nhau, mở khung giao dịch, dừng đúng trước xác nhận
  · ✅ chuyển thật lần đầu với confirm_mode: vàng về main khớp OCR 2 bên · ✅ kịch bản
  main không tới được điểm hẹn → farm huỷ sạch, quay lại farm, không kẹt.

---

## Giai đoạn 5 — Notify + Orchestrator

### 5a — `notify.py` (làm ngay sau GĐ 3 — safety/recovery cần nó)

- Telegram Bot API qua `httpx`: `send_text`, `send_photo`, `wait_confirm(timeout)`
  (nút inline Yes/No cho confirm_mode). Lỗi mạng Telegram không được làm chết task
  (queue + retry, confirm timeout = từ chối).
- **Test:** format message, confirm timeout → từ chối, retry khi lỗi mạng (mock httpx).
- **DoD:** ✅ nhận được tin + ảnh thật trên Telegram · ✅ bấm Yes/No điều khiển được nhánh task.

### 5b — `orchestrator.py`

- Mỗi instance farm = 1 worker thread chạy vòng đời:
  `ldconsole launch → đợi boot → LOGIN → SKILL_VERIFY → FARM ⇄ (SELL | EQUIPMENT |
  GOLD_TRANSFER) → lặp`. Scheduler nhận tín hiệu từ MONITOR và quyết định task kế tiếp
  (ưu tiên: GOLD_TRANSFER > EQUIPMENT > SELL khi trùng).
- Instance main **on-demand**: hàng đợi yêu cầu chuyển vàng; gom nhiều acc đạt ngưỡng
  gần nhau vào 1 lần mở main (mở 1 lần, nhận lần lượt từng acc, xong tắt).
- Giới hạn `max_concurrent_instances` (mặc định 5) — mở main khi đã kín thì tạm dừng
  1 worker farm ở state an toàn.
- Kill-switch `STOP`: mọi worker dừng ở ranh giới state, orchestrator thoát sạch, log tổng kết.
- **Test:** scheduler chọn task theo ưu tiên, gom hàng đợi main, logic nhường slot,
  dừng sạch khi STOP (toàn bộ bằng fake worker).
- **DoD:** ✅ 2 instance farm chạy song song 4 giờ, log riêng từng acc, không dẫm nhau
  · ✅ tạo file STOP → tất cả dừng ở state an toàn < 2 phút · ✅ 1 worker crash → worker
  khác không ảnh hưởng, Telegram có cảnh báo.

---

## Giai đoạn 6 — Vận hành & bàn giao

1. **Tuần đầu — dry_run toàn bộ:** soát log "would sell/buy/trade" mỗi ngày so với thực tế túi đồ.
2. **5 phiên kế — tắt dry_run, confirm_mode BẬT** (mặc định theo CLAUDE.md): duyệt từng
   lệnh bán/mua/chuyển qua Telegram.
3. **Chạy thật:** tắt confirm với bán đồ trước; chuyển vàng giữ confirm thêm 1 tuần.
4. **Checklist sau mỗi lần game update:** `tools/template_check.py` → template vỡ thì
   chụp bộ `v1.x` mới (không sửa đè), đổi version trong `settings.yaml`, chạy lại 1 chu
   kỳ dry_run trước khi cho chạy thật.
5. **Theo dõi thường trực:** ảnh trong `logs/unknown/` (màn lạ cần thêm vào recovery),
   tỉ lệ dùng toạ độ fallback (cao = template xuống cấp), latency screenshot.

**DoD toàn dự án:** 4 acc farm + 1 main chạy 24h không can thiệp tay: farm → đầy túi tự
bán (không mất đồ blacklist) → hỏng vũ khí/áo tự mua mặc lại → đạt ngưỡng vàng tự gom về
main, mọi sự kiện có log + thông báo Telegram, file STOP dừng được toàn hệ thống.
