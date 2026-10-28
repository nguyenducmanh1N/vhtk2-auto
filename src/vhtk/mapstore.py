"""Runtime access to the harvested map store (``data/maps/<version>/``).

Kho map do ``tools/harvest_maps.py`` quét: mỗi map có ảnh preview, crop dòng
tên trong tab "Tìm map", crop thanh TIÊU ĐỀ màn bản đồ, và (sau khi chạy
``tools/calibrate_map.py``) phép quy đổi pixel<->toạ độ game. Navigator dùng
kho này để chọn map và xác minh vị trí cho những map CHƯA có cặp template
``row_``/``hud_name_`` thủ công — kho là nguồn sự thật về danh sách map.

Hai cơ chế so khớp (đo trên AVD 27-28/10):

- Dòng tên trong danh sách: so trên MẶT NẠ CHỮ (nhị phân > 200) vì dòng đang
  chọn có nền highlight còn crop đã quét thì không — match màu thẳng tụt về
  ~0.31, match mặt nạ chữ cho 1.00 có mặt / ~0.37 vắng mặt.
- Thanh tiêu đề màn bản đồ: match màu thẳng (score 1.00 khi đúng map). Mở
  màn bản đồ MỚI thì tiêu đề luôn reset về map HIỆN TẠI, preview của lần
  trước không dính lại (kiểm chứng AVD 28/10) — đây là chỗ dựa để xác minh
  "đang đứng ở map nào" không cần template HUD.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import cast

import cv2
import yaml

from vhtk.core.vision import Image, best_match
from vhtk.mapcal import MapTransform

# Gốc kho dữ liệu map, chia theo version template (cùng quy ước templates/)
MAPS_DATA_ROOT = Path("data/maps")
# Ngưỡng nhị phân tách chữ sáng khỏi nền/highlight của dòng danh sách.
# Đo trên AVD 28/10: nền thường p95≈136, nền highlight p95≈99, chữ lên tới
# 245 — 160 giữ đủ nét chữ mảnh ("Võ Đang" 101px@200 -> 167px@160, score
# 0.78 -> 0.87) mà vẫn trên nền
ROW_TEXT_BIN_THRESHOLD = 160


def maps_dir(template_version: str, root: Path = MAPS_DATA_ROOT) -> Path:
    """Directory of the map store for one template version."""
    return root / template_version


def text_mask(img: Image) -> Image:
    """Binary mask of the bright row text (background/highlight removed)."""
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
    return cast(Image, cv2.threshold(gray, ROW_TEXT_BIN_THRESHOLD, 255, cv2.THRESH_BINARY)[1])


def flexible_match(a: Image, b: Image) -> float:
    """Best-match score with the smaller image searched inside the larger.

    Crop từ khung hình và crop trong kho có thể lệch kích thước vài pixel
    (rect đo lại giữa các đợt) — ảnh nhỏ hơn làm needle thì vẫn so được.
    """
    if a.shape[0] >= b.shape[0] and a.shape[1] >= b.shape[1]:
        hay, needle = a, b
    elif b.shape[0] >= a.shape[0] and b.shape[1] >= a.shape[1]:
        hay, needle = b, a
    else:  # mỗi chiều lớn nhỏ một kiểu — rect đo sai hẳn, coi như không khớp
        return 0.0
    score, _ = best_match(hay, needle)
    return score


@dataclass(frozen=True)
class MapEntry:
    """One harvested map: its name, image files and optional calibration."""

    index: int
    name: str
    map_file: Path
    row_file: Path
    title_file: Path
    transform: MapTransform | None


class MapStore:
    """Read-only view over one version's harvested map manifest.

    Thiếu thư mục/manifest thì kho RỖNG chứ không nổ lỗi — máy chưa đồng bộ
    ``data/`` vẫn chạy được các map có template thủ công.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self._entries: dict[str, MapEntry] = {}
        self._images: dict[Path, Image] = {}
        manifest = root / "manifest.yaml"
        if not manifest.is_file():
            return
        raw = yaml.safe_load(manifest.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return
        for e in raw.get("maps", []):
            name = e.get("name")
            files = e.get("files", {})
            if not name or not files:
                continue  # map quét được nhưng chưa điền tên — chưa dùng được
            calib = e.get("calibration")
            transform = (
                MapTransform(sx=calib["sx"], ox=calib["ox"], sy=calib["sy"], oy=calib["oy"])
                if calib
                else None
            )
            self._entries[name] = MapEntry(
                index=e["index"],
                name=name,
                map_file=root / files["map"],
                row_file=root / files["row"],
                title_file=root / files["title"],
                transform=transform,
            )

    def __contains__(self, name: str) -> bool:
        return name in self._entries

    def get(self, name: str) -> MapEntry | None:
        """Entry for an exact in-game map name, or None."""
        return self._entries.get(name)

    def names(self) -> list[str]:
        """All named maps, in manifest (scan) order."""
        return list(self._entries)

    def image(self, path: Path) -> Image:
        """Load (and cache) one of the store's image files.

        Raises:
            FileNotFoundError: the file is missing or unreadable — kho hỏng
                một nửa nguy hiểm hơn kho vắng, nên báo to.
        """
        cached = self._images.get(path)
        if cached is not None:
            return cached
        raw = cv2.imread(str(path))
        if raw is None:
            raise FileNotFoundError(f"map store image missing/unreadable: {path}")
        img = cast(Image, raw)
        self._images[path] = img
        return img
