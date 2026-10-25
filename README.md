# VHTK2 Auto Tool

Tool điều phối auto in-game cho **Võ Hiệp Truyền Kỳ 2 Mobile**, chạy trên
LDPlayer 9 (Windows). Tool KHÔNG chơi game thay người — nó bật đúng auto có sẵn
của game đúng lúc, và xử lý các việc auto in-game không làm được (bán đồ, mua
trang bị, chuyển vàng, phục hồi sự cố). Mọi tương tác chỉ qua ADB input và đọc
ảnh màn hình, như một công cụ UI testing.

## Yêu cầu môi trường

- Windows + **LDPlayer 9** (`C:\LDPlayer\LDPlayer9\ldconsole.exe`)
- Mọi instance cấu hình giống hệt nhau: **960x540, DPI 160**, Android 9, xoay ngang
- Python **3.11+**
- ADB có trong PATH (hoặc dùng adb kèm LDPlayer)

## Cài đặt

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -e ".[dev]"
```

Lưu ý: PaddleOCR cần cài thêm `paddlepaddle` (CPU):

```bash
pip install paddlepaddle
```

## Cấu hình

1. Sửa `config/settings.yaml` (đường dẫn ldconsole, ngưỡng an toàn, Telegram...).
2. Tạo file tài khoản từ mẫu:

   ```bash
   copy config\accounts\example.yaml config\accounts\main.yaml
   ```

   Các file trong `config/accounts/` (trừ `example.yaml`) không được commit.

## Chạy

```bash
python -m vhtk.orchestrator
```

- **Kill-switch:** tạo file tên `STOP` ở thư mục gốc → mọi task dừng sạch.
- Mặc định `dry_run: true` — tool không thực hiện hành động không thể hoàn tác
  (bán/mua/giao dịch), chỉ chụp màn và ghi log. Tắt trong `settings.yaml` khi đã tin tưởng.

## Kiểm tra bộ khung

```bash
python -c "from vhtk.config import load_settings; print(load_settings())"
pytest
ruff check .
mypy
```
