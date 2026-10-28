# Tiến độ dự án VHTK2 Auto Tool

Cập nhật lần cuối: 2026-10-25. Kế hoạch chi tiết từng giai đoạn + tiêu chí nghiệm thu: xem `PLAN.md`.

## Trạng thái tổng quan

| Giai đoạn | Nội dung | Trạng thái |
| --- | --- | --- |
| 0 | Bộ khung dự án (cấu trúc, config, logging) | ✅ Xong, đã verify |
| 1 | Tầng điều khiển thiết bị (device, ldplayer) | ✅ Xong, đã verify logic — chờ smoke test trên máy Windows |
| 2 | Tầng nhận diện màn hình (vision, OCR, phẩm chất đồ) | ✅ Xong, đã verify |
| 3 | FSM base + safety + recovery | ✅ Xong, đã verify |
| 3b | Template `v1.0-avd`: login 16 cái + common 4 cái (disconnected/btn_reconnect/patch_net_error/btn_retry); bag/map/shop chụp khi làm task tương ứng | 🔶 Đủ cho R1 |
| 4.1 | R1 Login — **✅ NGHIỆM THU ĐẠT trên AVD**: 5/5 lần login liên tiếp (24–29s/lần), idempotent (chạy khi đang in-game → DONE sau 1,6s) | ✅ Xong |
| 4.2 | R3 Farm + Retreat + navigate (di chuyển, bật auto, giám sát HP, rút về) | 🔶 Code + test xong; **live trên AVD cả chu trình tự nhiên**: tới map farm → bật auto → MONITOR → quái đánh tụt HP < 30% thật → HP_LOW → tắt auto → đi bộ về Tuyền Châu → REST hồi ≥ 90% → tự quay lại farm chu kỳ 2; kill-switch dừng sạch 4s. Còn thiếu DoD: chạy bền ≥ 2h |
| 4.3–4.5 | sell, equipment, skill_verify | ⬜ Chưa làm (stub) |
| D1 | App desktop — quản lý tài khoản & an toàn (`python -m vhtk.ui`): CRUD acc trên YAML giữ comment, tab An toàn có dải trạng thái bảo vệ; FastAPI + web UI, pywebview tuỳ chọn | ✅ Xong, đã verify bằng trình duyệt |
| 5 | Orchestrator đa instance + notify Telegram | 🔶 W2-prep XONG trên Mac (2026-10-28): worker process + registry + supervisor + scheduler + API/GUI; còn nghiệm thu thật trên Windows (W1→W2). Notify Telegram thật chưa làm |

Kiểm tra hiện tại: **267 test pass**, `ruff check` sạch, `mypy --strict` sạch (31 file).

## Giai đoạn 0 — Bộ khung

- Cấu trúc thư mục đúng CLAUDE.md; `templates/v1.0/` chia nhóm login/common/bag/map/shop/trade.
- `pyproject.toml`: Python 3.11+, hatchling, ruff + mypy strict + pytest cấu hình sẵn.
- `.gitignore`: ignore `logs/`, `config/accounts/*.yaml` (giữ `example.yaml`), file `STOP`; template PNG được commit.
- `config/settings.yaml`: toàn bộ key runtime kèm comment tiếng Việt — ldplayer, game, templates, vision, ocr, safety (dry_run mặc định BẬT), sell, quality (dải HSV), telegram, logging, paths.
- `src/vhtk/config.py`: load + validate YAML bằng pydantic v2, `extra="forbid"` bắt key gõ nhầm, lỗi báo rõ từng key (`game.package_name: Input should be a valid string`). Có schema account (`load_account`, `load_all_accounts`).
- `src/vhtk/logging_setup.py`: structlog ghi JSON lines vào `logs/vhtk.jsonl` (rotate 10MB×5) + console màu người đọc được.

## Giai đoạn 1 — Tầng điều khiển thiết bị

### `src/vhtk/core/errors.py`
Cây exception: `VhtkError` (gốc) → `ADBError` → `DeviceLost`; `LDConsoleError`; `RateLimited`; `KillSwitchTriggered` (tách riêng để FSM phân biệt "người ra lệnh dừng" với "lỗi cần recover").

### `src/vhtk/core/device.py`
- **`InputGate`** — mọi hành động input đi qua: kiểm tra kill-switch (file `STOP`) → rate limit cửa sổ trượt 60s (`safety.max_actions_per_minute`; đầy quota thì ngủ chờ slot, chờ quá grace 10s coi như bug đang spam → `RateLimited`) → delay ngẫu nhiên `[min_seconds, max_seconds]` → kiểm tra kill-switch lần nữa. Clock/sleep/rng inject được nên test không cần thời gian thật.
- **`ScreenshotBackend`** (Protocol) — tách backend chụp màn để sau đổi sang minicap/scrcpy không sửa code gọi. Backend mặc định `AdbScreencapBackend`: `screencap` raw RGBA (không PNG, ~100–250ms/khung ở 960×540, đủ cho poll 0.5s), tự xử lý header 12/16 byte, tự sửa lỗi pty mangle `\r\n`, fallback cuối cùng sang PNG.
- **`Device`** — `screenshot()` (BGR ndarray, khớp thẳng `capture` của Vision, KHÔNG qua gate vì là đọc thụ động), `tap/swipe/long_press` (validate toạ độ trong 960×540), `press_back/press_home`, `app_is_running/start_app/stop_app`, `is_screen_on/wake`. Mọi lệnh ADB bọc reconnect: 3 lần retry backoff 1s/2s/4s (kèm `adb connect` lại theo cổng suy từ serial) → hết thì `DeviceLost`.

### `src/vhtk/core/ldplayer.py`
- **`LDPlayerManager`** — wrapper `ldconsole.exe`: `list_instances()` (parse `list2`, chịu được dòng rác và tên có dấu phẩy), `start/stop/is_running`, `set_resolution(index, 960, 540, 160)` (từ chối khi instance đang chạy — LDPlayer chỉ áp `modify` cho instance đã tắt).
- **Ánh xạ index → serial**: quy ước mỗi emulator chiếm cặp cổng `(5554+2i, 5555+2i)` → `emulator-{5554+2i}`. Vì công thức có thể vỡ (cổng bị AVD/Nox chiếm, ADB qua TCP), `resolve_serial` hỏi thẳng `ldconsole adb --index i get-serialno` trước; fallback mới dùng công thức và phải đối chiếu `adb devices`; không xác minh được thì trả `None` chứ **không đoán** (tap nhầm instance là rủi ro tệ nhất với tài khoản thật).

### `tools/smoke_device.py`
Script kiểm tra trên máy Windows: liệt kê instance → kết nối (tự chọn instance đang chạy, hoặc `--index N` / `--serial S`) → chụp 3 ảnh in latency từng ảnh, lưu `logs/smoke_screenshot.png`, cảnh báo nếu kích thước khác 960×540 → tap giữa màn hình qua đúng InputGate thật → in trạng thái màn hình + game. Chạy:

```bash
python tools/smoke_device.py            # tự chọn instance đang chạy
python tools/smoke_device.py --serial emulator-5554
```

## Giai đoạn 2 — Tầng nhận diện màn hình

### `src/vhtk/core/vision.py`
- **`TemplateStore`** — load toàn bộ PNG trong `templates/<version>/` vào memory lúc khởi động (cache sẵn cả bản xám), truy cập theo `"common/btn_confirm"`. Metadata từng template (ngưỡng riêng, ROI, grayscale, multi-scale) khai trong `templates/<version>/manifest.yaml`.
- **`Vision`** — `find` / `find_all` (có NMS theo IoU, chặn trần 500 candidate) / `wait_for` / `wait_any` (mỗi chu kỳ chụp màn đúng 1 lần, check mọi template trên cùng frame) / `wait_vanish`. Nhận `capture` callable nên test offline được và cắm thẳng `Device.screenshot`.
- **Grayscale**: mặc định BẬT (nhanh ~3×); template phải phân biệt bằng màu thì khai `grayscale: false` riêng trong manifest. Phẩm chất đồ không dùng template matching mà đi đường màu riêng.
- **`OCR`** — bọc PaddleOCR lazy init; `read_text` / `read_number` (phân cách nghìn kiểu VN `1.234.567`, hậu tố `12K`/`1,5M`) / `read_fraction` (`56/60`, chịu OCR nhầm `/`→`|`). Tiền xử lý: upscale 3× + Otsu + tự đảo màu khi chữ sáng nền tối.
- **`detect_quality(cell, cfg)`** — đọc màu viền ô item theo dải HSV cấu hình trong `settings.yaml` mục `quality:` (hiệu chỉnh được theo thực tế), trả tên phẩm chất hoặc `None`.

### Công cụ
- `tools/capture.py` — chụp màn từ instance, cắt template bằng toạ độ (`--rect x y w h`) hoặc kéo chuột (cửa sổ OpenCV), lưu đúng chỗ, match thử lại ngay và in score.
- `tools/template_check.py` — quét toàn bộ template trên màn hình hiện tại, in OK/MISS từng cái; dùng kiểm tra nhanh sau khi game update.

## Giai đoạn 3 — FSM + Safety + Recovery

### `src/vhtk/core/errors.py` (mở rộng)
Thêm: `TaskError` (gốc lỗi trong-task, FSM đưa về RECOVER) → `StateTimeout`, `VerifyFailed`;
`TaskDefinitionError` (đồ thị state sai — lỗi lập trình, không bao giờ recover);
`SafetyLimitExceeded` (vượt trần hành động không hoàn tác — gần như chắc là bug lặp).

### `src/vhtk/notify.py`
- **`Notifier`** (Protocol) — `send_text` / `send_photo` / `wait_confirm(timeout)`; timeout
  PHẢI tính là từ chối. Core chỉ phụ thuộc protocol này, Telegram thật cắm vào ở GĐ 5a.
- **`NullNotifier`** — khi Telegram tắt: log thay vì gửi, từ chối mọi confirm (an toàn mặc định).

### `src/vhtk/core/safety.py`
- **`Safety.guard_irreversible(action, detail, screenshot)`** — cổng DUY NHẤT cho bán/mua/
  giao dịch. Thứ tự: kill-switch → trần phiên (`max_irreversible_per_session`, chỉ đếm hành
  động THỰC THI) → `dry_run` (lưu ảnh bằng chứng vào `logs/dry_run/`, trả False) →
  `confirm_mode` (hỏi người qua notifier, hết `confirm_timeout_seconds` coi như từ chối).
- Key mới trong `settings.yaml` mục `safety:`: `confirm_timeout_seconds`,
  `max_irreversible_per_session`, `max_recover_attempts`.

### `src/vhtk/core/recovery.py`
- **`Recovery.check()`** — pass nhanh TRƯỚC mỗi state, thứ tự ưu tiên: mất kết nối (tap
  reconnect nếu có) → nút hồi sinh (tap) → màn login (`RELOGIN`, orchestrator chạy lại login)
  → popup thường (đóng tối đa 3 cái/lần). Trả `OK / RECOVERED / RELOGIN / UNKNOWN`.
- **`Recovery.on_state_failure()`** — pass sâu cho state RECOVER: như check; không nhận ra
  gì → lưu ảnh `logs/unknown/` + gửi ảnh Telegram, trả `UNKNOWN` (vẫn cho retry trong quota).
- Template name là "UI contract" khai sẵn trong code; template chưa có trong store được bỏ
  qua êm — chạy được với bộ template thiếu.

### `src/vhtk/core/fsm.py`
- **`State`** — tên, handler, time budget (budget để log cảnh báo, handler tự quản wait),
  `next_states` hợp lệ. **`TaskContext`** — device/vision/safety/recovery/notifier/settings/
  account/log_dir bơm vào task.
- **`BaseTask.run()`** — mỗi vòng: kill-switch ở ranh giới state → `recovery.check()` →
  handler → validate transition. Đồ thị được validate ngay khi khởi tạo (tên trùng/dành riêng,
  next state lạ, initial sai, không đường tới DONE → `TaskDefinitionError`).
- Định tuyến lỗi: `TaskError`/`VisionError`/`ADBError` → RECOVER (retry state hỏng, quota
  `max_recover_attempts` liên tiếp, reset khi một state hoàn tất); `DeviceLost`/`RateLimited`/
  `SafetyLimitExceeded` → ABORT thẳng (lưu ảnh `logs/abort/` + báo người); văng login →
  kết quả `RELOGIN_REQUIRED`; kill-switch → `KILLED` (không phải lỗi).
- **`TaskResult`**: `SUCCESS / ABORTED / KILLED / RELOGIN_REQUIRED` + state cuối + lý do +
  số lần recover.

## Giai đoạn 4.1 — R1 Login (✅ nghiệm thu đạt trên AVD)

### Flow thực tế đã khám phá (game v1.0.32)
`Title (nút Đăng nhập, server đã lưu hiện sẵn) → dialog SIGN IN (3C/3CID | Play Now)
→ form "SIGN IN WITH 3CID" (SDK tự nhớ tài khoản nếu "Save Account Information")
→ Sign In → màn "Chọn nhân vật" → Vào game → HUD`.
**Cấm kỵ:** không bao giờ bấm "Play Now" (tạo acc khách MỚI); đáp vào màn "Tạo nhân vật"
= acc chưa có nhân vật trên server → ABORT, người tạo tay một lần.

### `src/vhtk/tasks/login.py`
- 9 state; mọi bước đáp màn đều qua `wait_any` nên **idempotent** — khởi động từ màn
  bất kỳ của flow đều tự định tuyến về đích.
- `relogin_is_normal = True` (cờ mới ở BaseTask): màn login là sân nhà của task này,
  recovery không được kết thúc task vì "văng về login".
- `recovery_resume_state() → WAIT_TITLE` (hook mới ở BaseTask): sau recovery, màn hình
  có thể đã trôi (popup mạng đá về title) nên resume ở state tự định tuyến, không retry
  mù state cũ.
- Credentials trống trong account YAML = dùng tài khoản SDK đã lưu; có điền thì tool
  tự dọn ô nhập (`press_delete`) và gõ lại (`input_text` — 2 method mới của Device,
  kèm `app_in_foreground` vì game có thể văng về launcher mà process vẫn sống).
- Toạ độ fallback (ô nhập form, slot nhân vật) trong `config/coordinates.yaml`,
  mỗi lần dùng có log cảnh báo đúng quy tắc #1.

### Nghiệm thu (2026-10-25, AVD vhtk2-dev)
- 5/5 lần login liên tiếp từ app tắt hẳn, 24–29s/lần, 0 can thiệp tay.
- Idempotent: chạy task khi đang trong thế giới → DONE sau 1,6s.
- Trong quá trình nghiệm thu đã bắt + xử lý được 2 sự cố thật: popup "Liên kết máy chủ
  thất bại" (recovery bấm Trở lại) và "Lỗi mạng khi tải filelist.ini" của bộ cập nhật
  (recovery bấm Thử lại) — template nằm trong `common/`.
- Công cụ mới: `tools/crop_template.py` (cắt template offline từ ảnh full-frame),
  `tools/run_login.py` (runner nghiệm thu, `--runs N`, `--keep-app`).

## Giai đoạn 4.2 — R3 Farm + Retreat

### Cơ chế di chuyển (khám phá + kiểm chứng tay trên AVD, 2026-10-26)

- **Mở bản đồ:** tap minimap góc phải trên (fallback coords `farm.minimap`).
- **Màn bản đồ:** 2 tab "Tìm map"/"Tìm NPC"; tab Tìm map liệt kê map lân cận;
  tap dòng map = **preview** map đích (chưa di chuyển).
- **Di chuyển = tap lên ẢNH bản đồ** → map tự đóng, dòng đỏ `Đi: <map>(x, y)`
  hiện ở đáy màn, game tự tìm đường (xuyên map được). Đã đi thật
  Tuyền Châu → Long Tuyền Thôn bằng cách này.
- **ĐÃ LOẠI** phương án gõ ô "Nhập toạ độ X/Y + Xác nhận": toạ độ ngoài phạm vi bị
  bỏ qua im lặng, giá trị cũ dính lại, bàn phím số đẩy cả layout lên trên.
- **Bẫy lớn:** nhân vật **ngồi luyện công (đả toạ) thì không tìm đường được** —
  game toast "Trạng thái hiện tại không thể bắt đầu tự tìm đường". Nút góc trái
  dưới toggle Đứng/Ngồi → `Navigator.ensure_standing()` chạy trước mọi điều hướng.
- **Nút auto HUD:** 2 template on/off gần giống hệt (chỉ đổi màu) — match ngưỡng
  thường cả 2 đều > 0.9. `Navigator.auto_state()` match CẢ HAI trên cùng frame,
  điểm cao hơn thắng; `set_auto(want_on)` tap + verify.
- **Đọc HP không cần OCR:** `bar_fill_percent` (vision.py) đếm tỉ lệ cột pixel đỏ
  trong ROI thanh máu (`farm.hp_bar_rect` = [72, 26, 70, 8], dải đỏ quấn vòng hue).
- **Phát hiện "đã tới nơi":** template `map/travel_indicator` (dòng "Đi:") biến mất
  + tên map HUD khớp + vùng toạ độ HUD đứng yên N chu kỳ (pixel-diff, không OCR).

### Code

- `tasks/navigate.py` — `Navigator` dùng chung farm/retreat: ensure_standing,
  auto_state/set_auto, open/close map, select_map (template `map/row_<slug>`),
  tap_map_point, in_map (template `map/hud_name_<slug>`), wait_arrival;
  `map_slug("Long Tuyền Thôn") = long_tuyen_thon` sinh tên template từ tên map.
- `tasks/farm.py` — FarmTask: `VERIFY_HUD → OPEN_MAP → SELECT_MAP → TAP_SPOT →
  TRAVEL_WAIT → ENABLE_AUTO → MONITOR → DISABLE_AUTO → DONE`; idempotent (đang
  farm đúng map + auto bật → vào thẳng MONITOR). MONITOR chỉ PHÁT TÍN HIỆU
  (`FarmSignal.HP_LOW`; bag/gold/equip để stub) — logic quyết định tách thành
  `MonitorPolicy` thuần (đếm chu kỳ HP thấp liên tiếp, chu kỳ mù, quota bật lại
  auto); HP thấp ưu tiên trên mọi tín hiệu khác.
- `tasks/retreat.py` — RetreatTask (HP_LOW → chạy): tắt auto → đi bộ về
  `retreat_map`/`retreat_spot` qua bản đồ → REST tới khi HP ≥ `hp_resume_percent`.
  Chết dọc đường đã có revive của recovery + RECOVER quota lo.
- `tools/run_farm.py` — mini-runner nghiệm thu: lặp FARM → (HP_LOW → RETREAT) →
  FARM; **chạy farm trước, không login trước** (farm tự xử lý màn bất kỳ, kể cả
  bản đồ đang mở che HUD; văng login thì `run_with_relogin` lo).
- Config: `settings.yaml` mục `farm:` (interval, ngưỡng chu kỳ, travel/rest
  timeout, dải màu HP); `AccountFarm` thêm `spot` (PIXEL trên ảnh bản đồ),
  `hp_retreat_percent`/`hp_resume_percent` (validator resume > retreat),
  `retreat_map`/`retreat_spot`; `coordinates.yaml` mục `farm:` (minimap,
  hp_bar_rect, hud_coords_rect).
- App desktop (D1): form tài khoản thêm đủ trường farm (map, điểm farm X/Y,
  2 ngưỡng HP, map/điểm rút về) — đúng yêu cầu cấu hình map farm qua GUI.
- Template mới `v1.0-avd/map/` (12 cái): btn_confirm_coords, btn_close,
  tab_find_map_on/off, row_long_tuyen_thon, row_tuyen_chau,
  hud_name_tuyen_chau, hud_name_long_tuyen_thon, travel_indicator,
  btn_stand, btn_sit (+ btn_auto_on/off có sẵn). `manifest.yaml` đầu tiên:
  nhóm chữ-trên-nền-thế-giới hạ ngưỡng 0.70 + khoá ROI (score đo thực tế dao
  động 0.83–1.00 khi nền thay đổi); nút auto khoá ROI [790,100,70,70].

### Nghiệm thu live đầu tiên (2026-10-26, AVD, acc dev @ Long Tuyền Thôn)

- Chuỗi đầy đủ chạy không can thiệp: VERIFY_HUD (tự đóng bản đồ đang mở che HUD)
  → OPEN_MAP → tap điểm farm → map tự đóng → **arrived sau 9s** → **auto-combat
  switched on** (verify bằng so điểm on/off) → MONITOR chu kỳ 5s; EXP tăng xác
  nhận auto thật sự đánh.
- Kill-switch: tạo file `STOP` → task dừng sạch ở MONITOR sau **4 giây**, runner
  thoát mã 0.
- 2 bug thật bắt được nhờ chạy live và đã sửa: (1) `login/hud_auto_btn` tụt score
  0.822 ở map khác (nền sau nút đổi) → manifest hạ ngưỡng 0.70 + ROI; (2) runner
  login-trước bị kẹt khi màn bản đồ che HUD → đổi thành farm-trước.
- **Chu trình HP_LOW tự nhiên đã chạy live (00:33–00:36):** quái đánh tụt HP < 30%
  đủ 3 chu kỳ → farm tắt auto, DONE với tín hiệu HP_LOW → RetreatTask đi bộ về
  Tuyền Châu (template `row_tuyen_chau`) → REST tới HP ≥ 90% → runner tự mở chu kỳ
  farm 2. Dọc đường có 1 lần ENABLE_AUTO không thấy nút (bị che đúng lúc tới nơi)
  → RECOVER tự định tuyến lại và thành công.
- **Gia cố sau phiên chạy thật đầu tiên (đêm 2026-10-26, từ lỗi thực tế):**
  - *Farm đến chết:* thêm `hp_emergency_percent` (dưới 15% rút NGAY, không chờ đủ
    chu kỳ) + VERIFY_HUD kiểm tra máu TRƯỚC khi hành quân (vừa hồi sinh máu cạn
    thì nghỉ trước, không lao về map farm chết tiếp giữa đường).
  - *Trọng thương:* chụp được hộp thoại "Bạn đã bị trọng thương!" → template
    `common/btn_revive` ("Về thành") đúng tên recovery chờ sẵn — chết là tự hồi sinh.
  - *Auto bật nhưng đứng im (AI game đơ khi bật ngay lúc vừa dừng chân):* chờ 2s
    ổn định → bật → theo dõi di chuyển 10s → đứng im thì tự tắt/bật lại
    (`engage_settle_seconds`/`engage_timeout`/`engage_retries`). Đã thấy tự xử lý
    thành công trên AVD.
  - *Đọc trạng thái nút auto:* 2 template on/off phải match MÀU (manifest
    `grayscale: false`) + biên `auto_state_margin` 0.02 — match xám chênh ~0.03
    mập mờ, màu chênh ~0.04–0.05 ổn định; mập mờ = "không rõ", không tap bừa.
  - *REST hồi máu quá chậm khi đứng* (26%→84% mất >10 phút, quá rest_timeout):
    `ensure_sitting()` — ngồi đả toạ khi nghỉ, farm cycle sau `ensure_standing()`
    đứng dậy trước khi đi.
  - *Vận hành:* script `./vhtk start|restart|stop|status` (chạy nền runner + GUI,
    guard chống 2 runner cùng thiết bị — 2 bản giành màn hình gây loạn thao tác);
    GUI trả 422 kèm thông báo rõ khi settings.yaml lệch schema (trước là 500) và
    PHẢI restart GUI sau khi nâng cấp code đổi schema.
  - Chu trình tự nhiên đã chạy nhiều vòng trên map farm mới "Bắc Tuyền Châu"
    (người vận hành đổi qua GUI; template row/hud_name chụp bổ sung).
- **Gia cố vòng 2 (2026-10-26 sáng→trưa, từ vận hành thực tế):**
  - Ngưỡng khẩn cấp 15→25%, soi máu 5s→2s/chu kỳ, acc dev rút về từ 50%
    (quái rút 20–30% máu trong 5s — lưới cũ quá thưa, nhân vật chết trước khi phát hiện).
  - Không tin HP 0% khi không thấy mỏ neo HUD — hiệu ứng LÊN CẤP che thanh máu
    từng gây 0% giả → rút về oan.
  - `select_map` poll tab thay vì chụp 1 khung (màn bản đồ mở còn animation →
    recover lặp vô hạn vì quota reset qua mỗi state thành công).
  - Điểm tap minimap dời (925,125): mép minimap sát dòng NHIỆM VỤ, trượt trúng là
    game auto-route kéo nhân vật sang vùng khác (đã từng bị kéo sang Tương Dương).
  - **Tìm đường XUYÊN VÙNG hoạt động** (kiểm chứng tay Tương Dương → Tuyền Châu,
    acc không có Phù về thành) → `travel_timeout` 300→600s.
  - REST ngồi đả toạ (đứng hồi 26→84% mất >10 phút, quá rest_timeout).
  - VERIFY_HUD 3 mỏ neo (nút Auto + nhãn Đứng/Ngồi): thành đông người chơi, nhãn
    tên che nút Auto → từng abort oan.
  - ROI cho `common/btn_close`: X túi đồ (921,58) giống hệt X màn bản đồ (852,38),
    recovery từng đóng nhầm bản đồ giữa lúc điều hướng.
- **GUI (D1+):** "Map farm"/"Map rút về" = dropdown từ `GET /api/farm-maps` (chỉ map
  đủ cặp template; tên từ `display_name` trong manifest); lưu tài khoản → GUI tự
  restart runner qua `POST /api/runner/restart` + `GET /api/runner` (giải pháp tạm
  cho D1, W2 chuyển sang API orchestrator).
- **Tổng kiểm tra regression (2026-10-26 trưa):** ✅ 171 test + ruff + mypy ·
  ✅ login lạnh 80,9s/0 recover · ✅ farm điều hướng→engage→MONITOR · ✅ nhiều chu kỳ
  HP_LOW → rút về → ngồi nghỉ → quay lại + tự hồi sinh (tự nhiên, trong 1 giờ chạy) ·
  ✅ toàn bộ endpoint GUI · ✅ kill-switch dừng sạch 4–10s.
- **Còn lại để đóng DoD 4.2:** chạy bền ≥ 2 giờ liên tục không abort (gần nhất ~1 giờ,
  1 abort do thành đông người → đã vá đa-mỏ-neo); thử tắt auto tay → tool tự bật lại
  (REENABLE trong MONITOR — logic có test nhưng chưa thử tay trên AVD).

### Kho dữ liệu map + hướng toạ độ game (bắt đầu 2026-10-27)

- **Khảo sát màn bản đồ:** tap pixel hợp lệ → game in "Đi: <Map>(x, y)" ở đáy
  màn = nguồn quy đổi pixel↔toạ độ game; điểm không đi được → toast "Không thể
  đến được vị trí này"; nhân vật NGỒI thì mọi lệnh đi bị lờ (phải đứng trước).
  Ô nhập X/Y + nút Xác nhận có trong game nhưng nút KHÔNG nhận tap qua ADB
  (thử tap/đúp/Enter/nhấn giữ) — điều hướng vẫn bằng tap pixel.
- **`tools/harvest_maps.py` (mảnh 1 — XONG):** quét tab "Tìm map" (hình học
  `farm.map_list` trong coordinates.yaml), tap từng dòng + cuộn trang, chống
  trùng bằng so khớp ảnh dòng. Kết quả: **86 map** trong `data/maps/v1.0-avd/`
  (ảnh map + crop dòng tên + crop tiêu đề + manifest.yaml ĐÃ điền đủ tên).
- **`tools/calibrate_map.py` (mảnh 2 — XONG, đã hiệu chuẩn Tuyền Châu):**
  chọn map bằng match MẶT NẠ CHỮ của crop dòng (match màu thẳng tụt về 0.31 vì
  nền highlight; nhị phân >200 cho 1.00/0.37), xác minh bằng crop tiêu đề, tap
  lưới điểm qua `tap_map_point`, chụp dòng "Đi: (x,y)" → samples.yaml; người
  đọc ảnh điền số (OCR trên Windows sau) → `--fit` (bình phương tối thiểu từng
  trục, `src/vhtk/mapcal.py`) → ghi `calibration` vào manifest. Tuyền Châu:
  sx=0.0938 sy=0.0873, residual 1.0 đơn vị. Lưu ý vận hành: game NHỚ vị trí
  cuộn danh sách; swipe bắt đầu sát mép trên bị nuốt; nhân vật NGỒI thì mọi
  lệnh tìm đường bị lờ.
- **GUI chọn điểm trên ảnh map (mảnh 3 — XONG):** nút "Chọn trên ảnh" của
  điểm farm/điểm rút giờ hiện ẢNH MAP từ kho (`GET /api/maps`,
  `/api/maps/{i}/image`) theo map đang chọn trong form; click ra pixel màn
  hình (cộng offset `map_image_rect`) kèm TOẠ ĐỘ GAME nếu map đã hiệu chuẩn.
  Nút "Dùng ảnh thiết bị" quay về chế độ chụp màn cũ (vẫn là chế độ mặc định
  cho các điểm HUD như ô chiêu). Kiểm chứng Playwright: Tuyền Châu click giữa
  thành ra "pixel 587/262 — toạ độ game 181/187" (nhân vật thật đứng 182/189).
- **Nợ:** harvest 0.92 từng nuốt dòng tên gần giống ("Đại Thảo Nguyên 2",
  "Tàn Lăng", "Yến Tử động-2"..., xác nhận thêm 28/10: thiếu cả "Thiếu Lâm",
  "Nga My") — đã nâng ngưỡng 0.97, CẦN quét lại và điền tên bổ sung.

### Điều hướng qua kho map — mọi map đã quét đều farm được (2026-10-28)

- **Kho là nguồn sự thật:** map KHÔNG có cặp template `row_`/`hud_name_` giờ
  điều hướng bằng kho (`src/vhtk/mapstore.py` — `MapStore` đọc manifest, cache
  ảnh, parse calibration thành `MapTransform`). Dropdown "Map farm" trong GUI
  liệt kê đủ 86 map (`/api/farm-maps` gộp kho + cặp template, template
  `display_name` thắng tên kho).
- **Chọn map:** `Navigator.select_map` ưu tiên template (3 map cũ, nhanh);
  không có thì `select_map_from_store` — match MẶT NẠ CHỮ crop dòng (nhị phân
  160, đo 28/10: nền thường p95≈136 / highlight p95≈99 / chữ tới 245; ngưỡng
  cũ 200 làm tên ngắn như "Võ Đang" chỉ còn 101px chữ → score 0.78 rớt oan),
  cuộn NỬA trang (dòng ở mép trang bị cắt → match tụt), dừng sớm khi chạm đáy
  (khung không đổi), tap xong XÁC MINH crop tiêu đề (nới biên 8px — match
  cùng cỡ không có chỗ trượt, lệch 1-2px là rớt); tap nhầm dòng tên gần giống
  (tiêu đề lệch) thì quét tiếp chứ không nhận bừa.
- **Bẫy quán tính (lỗi thực tế 28/10):** sau cú vuốt danh sách còn TRÔI —
  tap theo khung cũ trúng dòng bên cạnh (định tap "Võ Đang" trúng "Thiên Sư
  động-1"). Fix: chờ bbox dòng đứng yên qua 2 khung liên tiếp mới tap.
- **Xác minh vị trí không cần template HUD:** probe AVD 28/10 chốt — mở màn
  bản đồ MỚI thì tiêu đề luôn reset về map HIỆN TẠI (preview cũ không dính).
  `in_map` thành 3 trạng thái (None = không có template HUD, khung đơn không
  đọc được); `confirm_in_map` đắt hơn (mở/đóng bản đồ so tiêu đề kho) dùng ở:
  VERIFY_HUD idempotent-check, fallback "đã ở trong map" của TAP_SPOT, và
  `wait_arrival` (xác minh 1 lần/chuỗi ổn định; 2 lần sai tiêu đề liên tiếp
  → TravelStalled). MONITOR/REST với map kho: in_map=None được bỏ qua (không
  phá farm/giấc nghỉ bằng cách mở bản đồ mỗi chu kỳ — phát hiện sai map và
  bị-kéo-đi tạm mất với map kho, đổi lấy không cần chụp HUD 86 lần).
- `tools/calibrate_map.py` dùng lại `select_map_from_store` + `mapstore` (bỏ
  bản sao hàm chọn dòng + rect tiêu đề hardcode).
- **Nghiệm thu sống AVD 28/10:** chọn "Đào Hoa đảo" (kho, trang đầu) ✅; chọn
  "Võ Đang" (kho, cuộn ~7 bước nửa trang) ✅ tiêu đề khớp; `confirm_in_map`
  sai map → False ✅, đúng map (Bắc Tuyền Châu, giả lập thiếu template HUD)
  → True ✅. Test 227, ruff + mypy --strict sạch.
- **Phát hiện mới (chưa dùng):** màn bản đồ có ô "Nhập tên cần tìm" lọc danh
  sách — nhanh hơn cuộn nhưng ADB `input text` không gõ được tiếng Việt có
  dấu; để ngỏ, cân nhắc khi lên Windows (LDPlayer có thể khác đường nhập).

### Phát hiện túi đầy → về thành bán (thêm 2026-10-27, phương án A)

- **Vấn đề:** hành trang quá nặng thì game KHOÁ võ công ("Hành trang quá nặng,
  không thể sử dụng võ công này!") — auto farm chết đứng, phải có người can thiệp.
- **Tra cứu (27/10):** game có 3 kênh bán — NPC tạp hoá (rẻ, tức thì), bày bán
  sạp (tự định giá, nhân vật phải đứng chờ người mua), giao dịch trực tiếp (R4).
  Chốt phương án A: đồ rác bán NPC bằng SellTask có sẵn; bày bán sạp để sau.
- **Phát hiện:** icon Túi đồ trên HUD chuyển vòng đỏ gạch chéo "Đầy". Xác nhận
  KÉP mới tin: template `common/bag_full_icon` (match màu, ROI khoá, ngưỡng 0.88)
  VÀ tỉ lệ pixel đỏ trong `farm.bag_full_icon_rect` ≥ 0.12 — một vế riêng lẻ đều
  dính giả (template 0.806 trên frame âm; mảng đỏ trùng vị trí ở màn login/shop).
  Chỉ kiểm khi khung hình là HUD thật (mỏ neo đọc được).
- **Luồng:** MONITOR phát `BAG_FULL` sau `bag_full_cycles` chu kỳ liên tiếp
  (ưu tiên: HP thấp > túi đầy > sai map); VERIFY_HUD cũng kiểm ngay lúc xuất phát
  để khỏi hành quân vô ích. Runner: BAG_FULL → retreat → sell NGAY (không chờ
  `every_n_retreats`) → farm lại. Chặn lặp: BAG_FULL 3 lần liên tiếp (bán không
  làm nhẹ túi — dry_run hoặc blacklist giữ hết) → dừng báo người.
- **Nghiệm thu AVD 27/10 (túi đầy thật, bán THẬT):** farm phát hiện ngay tại
  VERIFY_HUD (14:23:15) → retreat về Tuyền Châu, nghỉ hồi 97% HP → sell bán
  thật 7 món whitelist (5 chén, 1 áo, 1 nông cụ) trong ~14s, OCR xác nhận
  tiền tăng, 11 ô "lạ" được giữ đúng thiết kế → icon Túi đồ hết đỏ
  (template None, red_ratio 0.0) → farm tự hành quân đi farm lại (14:29:09).
  Trọn chu trình không cần người can thiệp. Test 205/205 pass.
  Lưu ý: 27/10 đã tắt `dry_run` + `confirm_mode` để test bán thật — BẬT LẠI
  khi sang production / trước khi chạy acc thật có đồ giá trị.

### Auto nhấn chiêu + vật phẩm hồi máu (thêm 2026-10-27)

- **Phương án A (xoay vòng):** `SkillCaster` (pure logic, có test) trong `tasks/farm.py`
  — mỗi chu kỳ MONITOR tap ô kế tiếp trong `farm.skills.slots`; chiêu đang hồi thì cú
  tap vô hại. Chỉ tap khi chu kỳ yên ổn (KEEP_FARMING, đúng map, auto ON, khung đọc được).
- **Ô hồi máu (`heal_slot`, thường ô 8 trên cùng):** KHÔNG xoay vòng — chỉ nhấn khi HP
  dưới `heal_hp_percent` (mặc định 60%, cao hơn ngưỡng rút về: thuốc cứu trước, hết cứu
  nổi mới HP_LOW), có `heal_cooldown` riêng; ưu tiên trước mọi chiêu trong chu kỳ.
- **Config:** `farm.skills` trong settings.yaml (dùng chung mọi acc farm — cùng môn phái);
  toạ độ tâm 8 ô: `farm.skill_slots` trong coordinates.yaml, ĐÃ ĐO trên AVD 27/10 (kiểm
  chứng bằng ảnh chấm điểm). Bật auto chiêu khi ô chưa đo (0,0) → VERIFY_HUD ABORT.
- **GUI:** tab "Chiêu" mới — bật/tắt, tích ô xoay vòng, chọn ô thuốc + ngưỡng HP, sửa
  toạ độ 8 ô bằng "Chọn trên ảnh" (chụp màn thiết bị chọn từ dropdown). API
  `GET/PUT /api/settings/farm-skills` ghi cả 2 file YAML qua `config_writer`, giữ comment.
- **Tốc độ nhấn:** `cast_interval` mặc định 1s; sàn thực tế do `safety.action_delay`
  (0.5–2s ngẫu nhiên) + `max_actions_per_minute` (30) quyết định ≈ 1 tap/2s — muốn nhanh
  hơn phải nới 2 giới hạn an toàn đó, không phải giảm cast_interval.

## W2-prep — Điều khiển đa instance LDPlayer (code xong trên Mac, 2026-10-28)

Toàn bộ phần KHÔNG cần ≥2 thiết bị của W2 đã viết + test trên Mac; trên Windows
chỉ còn cài đặt và nghiệm thu thật. **Chốt phương án A: mỗi acc một TIẾN TRÌNH
worker riêng** (cô lập crash, restart đúng worker); PLAN.md đã sửa hết mâu thuẫn
thread/process.

- **`src/vhtk/worker.py`** — `AccountWorker` (lõi 5b): vòng đời 1 acc trên 1 thiết
  bị (farm → HP_LOW/BAG_FULL → retreat/sell → farm; GOLD_THRESHOLD ghi cờ
  `gold_request` vào state file + báo notifier — W3 mới giao dịch). Chạy bằng
  `python -m vhtk.worker --account <yaml>`; tự ghi `logs/workers/<stem>.state.json`
  (atomic, mỗi lần đổi state qua hook `TaskContext.on_state` mới của FSM) và tự
  đăng ký PID. `tools/run_farm.py` giờ là lớp vỏ gọi worker (CLI giữ nguyên).
- **Kill-switch 2 tầng:** `STOP` (toàn cục) + `STOP.<stem>` (theo acc) — InputGate,
  Safety, Device nhận `stem`; supervisor dừng worker bằng `STOP.<stem>` trước,
  hết `orchestrator.stop_timeout` mới terminate/kill (psutil).
- **`src/vhtk/registry.py`** — sổ `{stem → PID}` bằng file JSON + psutil (chạy cả
  Mac/Win), chống PID-reuse bằng đối chiếu cmdline với đường dẫn account. Thay
  hẳn pgrep; `./vhtk` và GUI cùng dùng một đường.
- **`src/vhtk/orchestrator.py`** — `WorkerSupervisor`: start (launch instance qua
  ldconsole nếu `orchestrator.manage_instances: true` → đợi `sys.boot_completed`
  → resolve serial, lệch thì ghi ngược YAML → spawn worker), stop (kill-switch
  trước, leo thang sau), restart đúng 1 worker, **provision** (`ldconsole copy`
  từ `template_instance_index` → ghi index mới vào YAML → start). `MainScheduler`
  (pure logic cho W3): gom yêu cầu vàng trong `main_batch_window_seconds`, nhường
  slot `max_concurrent_instances` bằng cách tạm dừng 1 farm KHÔNG trong chuyến.
  `LDPlayerManager.clone()` mới (diff list2 trước/sau copy).
- **Config:** mục `orchestrator:` mới trong settings.yaml (manage_instances —
  Mac false/Win true, template_instance_index, max_concurrent_instances,
  boot_timeout, stop_timeout, workers_dir, main_batch_window_seconds); schema có
  default đầy đủ nên YAML cũ vẫn load.
- **API + GUI:** `GET /api/workers`, `POST /api/workers/{stem}/start|stop|restart|provision`;
  `/api/runner*` cũ giữ tương thích nhưng chạy trên registry/supervisor. GUI thêm
  tab **Thiết bị**: mỗi acc một dòng (trạng thái, pid, task/state), nút
  "Mở & đăng nhập" / "Chạy lại" / "Dừng" (dừng sạch) / "Cấp máy mới" (provision,
  chỉ Windows), tự làm mới 5s.
- **Test mới (100):** registry (6), orchestrator supervisor + scheduler (17 — fake
  ldconsole/spawn/boot/psutil, kiểm cả ghi-ngược serial, leo thang kill, dừng 1
  worker không đụng worker khác), worker loop (7 — fake tasks đủ nhánh tín hiệu),
  FSM on_state (2), kill-switch theo acc (1), ldconsole clone (3), API workers (4).

**Checklist Windows (W1 → nghiệm thu W2), dành cho người cài đặt:**
1. Cài Python 3.11+, `python -m venv .venv && .venv\Scripts\pip install -e .[dev]`
   (psutil đã nằm trong dependencies), cài LDPlayer 9, bật VT trong BIOS.
2. Tạo instance MẪU 960x540/DPI 160 đã cài game; ghi index của nó vào
   `orchestrator.template_instance_index`; đặt `orchestrator.manage_instances: true`;
   kiểm `ldplayer.ldconsole_path`.
3. `python tools/smoke_device.py` trên 1 instance; chụp bộ template `v1.0/`
   (checklist 3b + `trade/`), quét lại kho map, đổi `templates.version: v1.0`.
4. Tạo acc qua GUI (`python -m vhtk.ui --browser`) → tab Thiết bị → "Mở & đăng
   nhập" từng acc; thêm acc mới bằng "Cấp máy mới" (clone instance mẫu).
5. Nghiệm thu DoD W2: 2 farm song song 4h; `STOP` dừng tất cả <2 phút;
   `STOP.<stem>` dừng đúng 1 acc; kill 1 worker → worker khác chạy tiếp;
   sửa acc trên GUI → chỉ instance đó restart. `dry_run` BẬT lại trước khi
   chạy acc thật (27/10 đang tắt để test bán trên AVD).

## Test (267 test, không cần thiết bị)

- `tests/test_config.py` — load/validate settings, thông báo lỗi thiếu key/sai kiểu.
- `tests/test_vision.py` (37) — NMS, parse số/phân số OCR, preprocess auto-invert, detect_quality với ảnh tổng hợp, TemplateStore/find/ROI/wait trên screenshot giả.
- `tests/test_device.py` (14) — InputGate với fake clock (delay đúng biên, rate limit chờ slot trong grace, raise `RateLimited` khi quá, kill-switch, quên lịch sử hết hạn), decode screencap (header 12/16 byte, format lạ, thiếu byte), Device với fake ADB (tap đúng lệnh + validate biên, long_press = swipe tại chỗ, reconnect hồi phục lỗi tạm, `DeviceLost` khi hết retry, screenshot không qua gate).
- `tests/test_ldplayer.py` (13) — parse `list2` (chuẩn/mở rộng/tên có phẩy/dòng hỏng), resolve_serial (ưu tiên ldconsole, fallback đối chiếu adb, trả None thay vì đoán), start/stop/set_resolution đúng tham số.
- `tests/test_safety.py` (8) — dry_run bỏ qua + lưu bằng chứng + ưu tiên hơn confirm, confirm duyệt/từ chối, trần phiên raise `SafetyLimitExceeded`, hành động bị chặn không ăn quota, kill-switch chặn guard.
- `tests/test_recovery.py` (10) — trên TemplateStore/Vision thật với ảnh tổng hợp: màn sạch OK, đóng popup (kể cả popup lì — chạm trần 3 lần), hồi sinh, disconnect ưu tiên hơn popup thường, văng login → RELOGIN, màn lạ lưu ảnh + báo, store thiếu template không nổ lỗi.
- `tests/test_fsm.py` (17) — happy path + recovery chạy trước mỗi state, transition không khai báo / DONE không khai báo / đồ thị sai → `TaskDefinitionError`, lỗi recoverable retry đúng state, quá quota → ABORT, reset quota sau state thành công, handler tự xin RECOVER, `DeviceLost` → ABORT thẳng kèm ảnh bằng chứng, RELOGIN ở cả 2 đường, kill-switch giữa các state và trong handler → KILLED, `recovery_resume_state` override, `relogin_is_normal`.
- `tests/test_login.py` (8) — happy path với creds đã lưu/creds trong config, màn tạo nhân vật → ABORT không bấm gì, sai server → ABORT, thiếu template server → bỏ qua check, Sign In kẹt → ABORT sau quota, slot ngoài phạm vi, bắt buộc có account.
- `tests/test_farm.py` (31) — MonitorPolicy (HP thấp cần N chu kỳ liên tiếp, hồi máu reset streak, mù màn hình → LOST, quota bật lại auto, HP thấp thắng mọi tín hiệu, sai map); map_slug tiếng Việt; bar_fill_percent (đầy/nửa/cạn, hue quấn vòng, rect ngoài frame); Navigator trên "world fake" (tap làm thế giới đổi trạng thái): open/timeout, chuyển tab + tap dòng map, thiếu template row, tap_map_point đóng map / kẹt map → VerifyFailed, ensure_standing, wait_arrival ổn định/timeout; FarmTask (chưa cấu hình spot → ABORT, thiếu template → ABORT, đang farm → MONITOR → HP_LOW → tắt auto → DONE); RetreatTask (chưa cấu hình → ABORT, chu kỳ đầy đủ về thành nghỉ hồi máu).
- `tests/test_device.py` (+4) — `app_in_foreground` parse dumpsys, `input_text` validate charset, `press_delete` gửi batch keyevent.

## Cách verify

```bash
cd "VHTK2 Auto Tool"
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pytest && ruff check . && mypy
```

## Việc kế tiếp (chi tiết trong PLAN.md — lộ trình đã tách Mac/Win)

1. **Đóng DoD 4.2:** chạy `python tools/run_farm.py --account config/accounts/dev.yaml`
   bền ≥ 2 giờ; thử tắt auto tay giữa chừng; kích hoạt HP_LOW thật (hạ tạm
   `hp_retreat_percent` hoặc farm map quái mạnh hơn) để thấy trọn chu trình
   farm → rút về → nghỉ → quay lại farm.
2. **GĐ 4.3 R3b Sell:** chụp template nhóm `bag/`, blacklist 2 tầng, dry_run.
3. **GĐ 4.4–4.5:** equipment, skill_verify; **5a** Telegram thật (confirm_mode cần).
4. Nhóm `W*` (server Windows, đa instance, R4 chuyển vàng/đồ): làm sau khi 5b xong.

## Ghi chú môi trường

- **AVD dev trên Mac (tạo 2026-10-25):** tên `vhtk2-dev` — Android 13 ARM64 (API 33,
  có Play Store), 960x540, DPI 160, landscape. Khởi động:
  `~/Library/Android/sdk/emulator/emulator -avd vhtk2-dev`, serial `emulator-5554`.
  Smoke test tầng thiết bị PASS trên AVD này: frame đúng 960x540, screenshot
  ~235–290ms/khung, tap qua InputGate OK. Lưu ý: LDPlayer KHÔNG chạy được trên Mac
  M1 (không có bản macOS, Windows VM thiếu nested virtualization) — AVD là môi trường
  dev duy nhất trên Mac; template chụp từ AVD để version `v1.0-avd/`, nghiệm thu
  cuối vẫn trên LDPlayer Windows x86 thật.

- `.venv` trên máy macOS này đã có: pydantic, structlog, pyyaml, numpy, opencv, adbutils, httpx, pytest, ruff, mypy. **Chưa cài `paddleocr`** (nặng, kén macOS) — OCR lazy init nên không ảnh hưởng test; cài trên máy Windows chạy thật.
- `ldconsole.exe` chỉ có trên Windows — `LDPlayerManager` và smoke test phải chạy ở đó; toàn bộ logic còn lại test được trên máy này.
