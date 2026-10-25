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
| 4.2–4.5 | farm, sell, equipment, skill_verify | ⬜ Chưa làm (stub) |
| 5 | Orchestrator đa instance + notify Telegram | ⬜ Notify: đã có protocol + NullNotifier; Telegram thật và orchestrator chưa làm |

Kiểm tra hiện tại: **124 test pass**, `ruff check` sạch, `mypy --strict` sạch (20 file).

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

## Test (124 test, không cần thiết bị)

- `tests/test_config.py` — load/validate settings, thông báo lỗi thiếu key/sai kiểu.
- `tests/test_vision.py` (37) — NMS, parse số/phân số OCR, preprocess auto-invert, detect_quality với ảnh tổng hợp, TemplateStore/find/ROI/wait trên screenshot giả.
- `tests/test_device.py` (14) — InputGate với fake clock (delay đúng biên, rate limit chờ slot trong grace, raise `RateLimited` khi quá, kill-switch, quên lịch sử hết hạn), decode screencap (header 12/16 byte, format lạ, thiếu byte), Device với fake ADB (tap đúng lệnh + validate biên, long_press = swipe tại chỗ, reconnect hồi phục lỗi tạm, `DeviceLost` khi hết retry, screenshot không qua gate).
- `tests/test_ldplayer.py` (13) — parse `list2` (chuẩn/mở rộng/tên có phẩy/dòng hỏng), resolve_serial (ưu tiên ldconsole, fallback đối chiếu adb, trả None thay vì đoán), start/stop/set_resolution đúng tham số.
- `tests/test_safety.py` (8) — dry_run bỏ qua + lưu bằng chứng + ưu tiên hơn confirm, confirm duyệt/từ chối, trần phiên raise `SafetyLimitExceeded`, hành động bị chặn không ăn quota, kill-switch chặn guard.
- `tests/test_recovery.py` (10) — trên TemplateStore/Vision thật với ảnh tổng hợp: màn sạch OK, đóng popup (kể cả popup lì — chạm trần 3 lần), hồi sinh, disconnect ưu tiên hơn popup thường, văng login → RELOGIN, màn lạ lưu ảnh + báo, store thiếu template không nổ lỗi.
- `tests/test_fsm.py` (17) — happy path + recovery chạy trước mỗi state, transition không khai báo / DONE không khai báo / đồ thị sai → `TaskDefinitionError`, lỗi recoverable retry đúng state, quá quota → ABORT, reset quota sau state thành công, handler tự xin RECOVER, `DeviceLost` → ABORT thẳng kèm ảnh bằng chứng, RELOGIN ở cả 2 đường, kill-switch giữa các state và trong handler → KILLED, `recovery_resume_state` override, `relogin_is_normal`.
- `tests/test_login.py` (8) — happy path với creds đã lưu/creds trong config, màn tạo nhân vật → ABORT không bấm gì, sai server → ABORT, thiếu template server → bỏ qua check, Sign In kẹt → ABORT sau quota, slot ngoài phạm vi, bắt buộc có account.
- `tests/test_device.py` (+4) — `app_in_foreground` parse dumpsys, `input_text` validate charset, `press_delete` gửi batch keyevent.

## Cách verify

```bash
cd "VHTK2 Auto Tool"
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pytest && ruff check . && mypy
```

## Việc kế tiếp (chi tiết trong PLAN.md — lộ trình đã tách Mac/Win)

1. **Người dùng:** cài game vào AVD `vhtk2-dev` (Play Store hoặc `adb install` APK),
   login tay 1 acc tới màn chơi.
2. **GĐ 3b:** chụp bộ template `v1.0-avd/` trên AVD (5 nhóm, nhóm `trade/` dồn sang W1).
3. **GĐ 4.1:** `tasks/login.py` (R1) — dev + nghiệm thu ngay trên AVD.
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
