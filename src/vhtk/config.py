"""Load and validate YAML configuration with pydantic v2.

All runtime values (paths, thresholds, timeouts, blacklists) live in YAML;
code contains logic only. Validation errors are re-raised as ``ConfigError``
with a human-readable message pointing at the exact key and problem.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

DEFAULT_SETTINGS_PATH = Path("config/settings.yaml")


class ConfigError(Exception):
    """Raised when a config file is missing, unreadable, or invalid."""


class _StrictModel(BaseModel):
    # Cấm key lạ để bắt lỗi gõ nhầm tên key ngay khi load
    model_config = ConfigDict(extra="forbid")


class Resolution(_StrictModel):
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    dpi: int = Field(gt=0)


class LDPlayerSettings(_StrictModel):
    ldconsole_path: str
    resolution: Resolution


class GameSettings(_StrictModel):
    package_name: str


class TemplateSettings(_StrictModel):
    dir: Path
    version: str
    match_threshold: float = Field(ge=0.0, le=1.0)

    @property
    def root(self) -> Path:
        """Root directory of the active template version."""
        return self.dir / self.version


class VisionSettings(_StrictModel):
    default_timeout: float = Field(gt=0)
    poll_interval: float = Field(gt=0)
    grayscale_match: bool
    nms_overlap_threshold: float = Field(ge=0.0, le=1.0)


class OcrSettings(_StrictModel):
    lang: str


class ActionDelay(_StrictModel):
    min_seconds: float = Field(ge=0)
    max_seconds: float = Field(ge=0)


class SafetySettings(_StrictModel):
    # Mặc định schema PHẢI an toàn: thiếu khoá trong YAML -> dry_run/confirm
    # BẬT. File settings.yaml có thể tắt tạm khi test có giám sát.
    dry_run: bool = True
    confirm_mode: bool = True
    confirm_timeout_seconds: float = Field(gt=0)
    kill_switch_file: str
    max_actions_per_minute: int = Field(gt=0)
    max_irreversible_per_session: int = Field(gt=0)
    max_recover_attempts: int = Field(gt=0)
    action_delay: ActionDelay


class HSVRange(_StrictModel):
    """One HSV color range, OpenCV convention: H in 0..179, S/V in 0..255."""

    h: tuple[int, int]
    s: tuple[int, int]
    v: tuple[int, int]


class SellSettings(_StrictModel):
    """Knobs of the sell task (R3b) — the most safety-critical task.

    Discovered on the AVD (2026-10-26): in the NPC shop's sell mode a tap
    sells INSTANTLY, the game asks nothing — every protection lives here.
    """

    quality_blacklist_threshold: int = Field(ge=0)
    # Danh sách tên item cấm bán — dùng khi có OCR (Windows); trên Mac lớp
    # bảo vệ chính là WHITELIST icon bên dưới
    item_blacklist: list[str]
    # Bán 1 lần sau mỗi N chu kỳ rút-về-thành (đằng nào cũng đang ở thành)
    every_n_retreats: int = Field(gt=0)
    # WHITELIST: chỉ bán ô khớp một template icon ``sell/ok_*`` — đồ lạ,
    # đồ mới, đồ chưa có template thì KHÔNG BAO GIỜ bán
    icon_match_threshold: float = Field(ge=0.0, le=1.0)
    # Màu nền ô "bán được" game tô trong chế độ Bán + tỉ lệ pixel tối thiểu
    highlight: HSVRange
    highlight_min_fraction: float = Field(ge=0.0, le=1.0)
    # Số trang túi quét mỗi lần bán (1 = chỉ trang đầu)
    max_pages: int = Field(ge=1)
    # Chờ các bước mở shop/chuyển chế độ (giây)
    shop_timeout: float = Field(gt=0)
    # Trần thời gian game tự tìm đường tới NPC trong thành (giây)
    npc_route_timeout: float = Field(gt=0)


class QualitySettings(_StrictModel):
    """Item-quality detection via border color of an item cell."""

    border_width: int = Field(gt=0)
    min_match_fraction: float = Field(ge=0.0, le=1.0)
    colors: dict[str, HSVRange]


class FarmSkillSettings(_StrictModel):
    """Auto skill-casting inside the farm MONITOR loop.

    Phương án A (đã chốt 2026-10-27): xoay vòng đơn giản — nhấn lần lượt các
    ô trong ``slots``; chiêu đang hồi thì cú tap vô hại, game tự bỏ qua.
    Ô ``heal_slot`` tách riêng: CHỈ nhấn khi HP dưới ``heal_hp_percent``.
    Toạ độ tâm 8 ô nằm trong coordinates.yaml (``farm.skill_slots``).
    """

    enabled: bool = False
    # Các ô chiêu nhấn xoay vòng, số 1-8 theo vị trí trên vòng chiêu
    slots: list[int] = []
    # Giãn cách tối thiểu giữa 2 lần nhấn chiêu (giây). Sàn THỰC TẾ do
    # safety.action_delay + max_actions_per_minute quyết định — đặt nhỏ hơn
    # cũng không nhanh hơn được
    cast_interval: float = Field(gt=0, default=1.0)
    # Ô vật phẩm hồi máu (1-8); 0 = không dùng
    heal_slot: int = Field(ge=0, le=8, default=0)
    # Dùng vật phẩm hồi máu khi HP dưới ngưỡng này (%)
    heal_hp_percent: int = Field(ge=1, le=99, default=60)
    # Giãn cách tối thiểu giữa 2 lần dùng vật phẩm hồi máu (giây) — thuốc
    # có thời gian hồi riêng, spam tap chỉ đốt quota hành động
    heal_cooldown: float = Field(ge=0, default=5.0)

    @model_validator(mode="after")
    def _slots_valid(self) -> FarmSkillSettings:
        for s in self.slots:
            if not 1 <= s <= 8:
                raise ValueError(f"skill slot must be 1..8, got {s}")
        if len(set(self.slots)) != len(self.slots):
            raise ValueError(f"duplicate skill slots: {self.slots}")
        if self.heal_slot and self.heal_slot in self.slots:
            raise ValueError(
                f"heal_slot {self.heal_slot} must not also be in the rotation slots "
                "(thuốc hồi máu chỉ dùng theo ngưỡng HP, không xoay vòng)"
            )
        return self


class FarmSettings(_StrictModel):
    """Knobs of the farm/retreat tasks (R3) — shared by every account."""

    # Chu kỳ MONITOR: bao lâu chụp màn kiểm tra một lần (giây)
    monitor_interval: float = Field(gt=0)
    # HP dưới ngưỡng liên TIẾP bao nhiêu chu kỳ mới phát HP_LOW
    # (auto in-game tự uống thuốc — một chu kỳ tụt máu chưa phải khẩn cấp)
    hp_low_cycles: int = Field(gt=0)
    # HP dưới mức này (%) thì phát HP_LOW NGAY, không chờ đủ chu kỳ — chống
    # quái đánh dồn giết nhân vật nhanh hơn chu kỳ giám sát
    hp_emergency_percent: int = Field(ge=1, le=99)
    # Số chu kỳ MONITOR liên tiếp không đọc được màn hình trước khi RECOVER
    lost_cycles: int = Field(gt=0)
    # Số lần bật lại auto tối đa trong một phiên MONITOR trước khi RECOVER
    auto_reenable_limit: int = Field(ge=0)
    # Nút auto ON nhưng nhân vật đứng ngây N chu kỳ MONITOR liền -> tắt/bật
    # lại auto để đánh thức AI của game (hay "ngủ gật" sau 1-2 phát đánh)
    monitor_idle_cycles: int = Field(gt=0, default=4)
    # Chờ các thao tác trên màn bản đồ (mở/đóng/chuyển tab)
    map_timeout: float = Field(gt=0)
    # Số lần tap điểm đến trên ảnh bản đồ trước khi coi là thất bại — game
    # hay nuốt tap đầu ngay sau khi preview map vừa đổi (quan sát AVD 26/10)
    map_spot_tap_attempts: int = Field(gt=0, default=3)
    # Chọn map qua KHO data/maps (map chưa có template row_/hud_name_):
    # ngưỡng match mặt nạ chữ của dòng tên (đo 28/10 ngưỡng nhị phân 160:
    # tên ngắn chữ mảnh như "Võ Đang" ~0.87 có mặt, vắng mặt ~0.3-0.6; tap
    # nhầm dòng tên gần giống đã có vòng xác minh tiêu đề chặn lại)
    map_row_match_threshold: float = Field(gt=0.0, le=1.0, default=0.80)
    # Ngưỡng match crop thanh tiêu đề màn bản đồ (xác minh map hiện tại/preview)
    map_title_match_threshold: float = Field(gt=0.0, le=1.0, default=0.85)
    # Số bước cuộn tối đa khi dò danh sách "Tìm map" (mỗi bước NỬA trang để
    # dòng nào cũng có ít nhất một lần hiện nguyên vẹn giữa vùng danh sách)
    map_list_max_pages: int = Field(gt=0, default=24)
    # Sau khi map đóng: chờ tối đa bao lâu để thấy tìm đường THỰC SỰ bắt đầu
    # (dòng "Đi: ..." hoặc toạ độ HUD đổi) — map đóng không đồng nghĩa đã đi.
    # Đo trên AVD 26/10: có lúc trễ hơn 5s mới nhúc nhích; đặt quá ngắn thì
    # task mở lại map và TỰ HUỶ chuyến đi đang khởi động
    travel_start_timeout: float = Field(gt=0, default=20.0)
    # Số lần làm lại cả chuỗi mở map -> chọn map -> tap điểm khi tìm đường
    # không chịu bắt đầu (hay gặp ngay sau login)
    nav_retries: int = Field(ge=0, default=2)
    # Đứng yên liên tiếp N chu kỳ (không dòng "Đi:", chưa tới map đích) thì
    # coi là KẸT giữa đường — game hay dừng ở cổng map thay vì vượt sang
    travel_stall_cycles: int = Field(gt=0, default=8)
    # Gỡ kẹt: đứng ngay cổng map game từ chối lệnh đi xuyên map nhưng lệnh
    # đi tới NPC vẫn ăn — route tới NPC này vài giây để rời chỗ kẹt rồi
    # điều hướng lại; None = không gỡ, abort như thường
    unstick_npc_row: str | None = None
    unstick_walk_seconds: float = Field(ge=0, default=8.0)
    # Trần thời gian auto tìm đường tới điểm farm (giây)
    travel_timeout: float = Field(gt=0)
    # Phát hiện "đã tới nơi": vùng toạ độ HUD đứng yên N chu kỳ liên tiếp
    arrive_check_interval: float = Field(gt=0)
    arrive_stable_cycles: int = Field(gt=0)
    # Tỉ lệ pixel khác nhau tối đa giữa 2 khung vùng toạ độ để coi là "đứng yên"
    arrive_diff_threshold: float = Field(ge=0.0, le=1.0)
    # Trần thời gian đứng nghỉ hồi máu trong thành (giây)
    rest_timeout: float = Field(gt=0)
    # Hai template nút auto on/off gần giống nhau — chênh lệch điểm match giữa
    # chúng phải vượt biên này mới dám kết luận trạng thái, không thì coi là
    # "không rõ" (tránh tap nhầm làm auto bật/tắt loạn)
    auto_state_margin: float = Field(ge=0.0, le=1.0)
    # Chờ nhân vật dừng hẳn sau tìm đường rồi mới bật auto (giây) — bật ngay
    # khoảnh khắc vừa tới nơi thì AI auto của game hay không kích hoạt
    engage_settle_seconds: float = Field(ge=0)
    # Sau khi bật auto: chờ nhân vật di chuyển (đi tìm quái) tối đa bao lâu
    engage_timeout: float = Field(gt=0)
    # Đứng im quá engage_timeout thì tắt/bật lại auto, tối đa N lần
    engage_retries: int = Field(ge=0)
    # Game lỗi: bật auto lần đầu nhân vật chỉ đánh 1 phát rồi đứng ngây —
    # LUÔN tắt/bật lại auto sau khi bật, chờ N giây giữa hai nhịp; 0 = tắt
    # chu trình đánh thức chủ động này
    engage_warmup_toggle_seconds: float = Field(ge=0, default=2.0)
    # Dải màu HSV của phần MÁU trên thanh HP (đọc % máu bằng màu, không OCR)
    hp_bar_color: HSVRange
    # Phát hiện túi đầy ("Hành trang quá nặng"): icon Túi đồ trên HUD chuyển
    # thành vòng đỏ gạch chéo chữ "Đầy". Xác nhận KÉP mới tin — template
    # common/bag_full_icon khớp VÀ tỉ lệ pixel đỏ trong ô icon vượt ngưỡng
    # (icon thường cùng khối tròn, match xám/màu một mình lên tới 0.806 trên
    # frame âm tính — đo 2026-10-27)
    bag_full_color: HSVRange = HSVRange(h=(172, 8), s=(120, 255), v=(80, 255))
    bag_full_min_red_ratio: float = Field(ge=0.0, le=1.0, default=0.12)
    # Túi đầy liên tiếp N chu kỳ MONITOR mới phát BAG_FULL (chống nhiễu 1 khung)
    bag_full_cycles: int = Field(gt=0, default=2)
    # Auto nhấn chiêu + vật phẩm hồi máu trong vòng MONITOR
    skills: FarmSkillSettings = FarmSkillSettings()


class LoginSettings(_StrictModel):
    """Timeouts/knobs of the login task (R1)."""

    boot_timeout: float = Field(gt=0)
    flow_timeout: float = Field(gt=0)
    ingame_timeout: float = Field(gt=0)
    clear_chars: int = Field(gt=0)


class OrchestratorSettings(_StrictModel):
    """Multi-instance orchestration (W2): worker processes + LDPlayer lifecycle.

    Mỗi worker là MỘT TIẾN TRÌNH riêng (``python -m vhtk.worker``) — crash
    một acc không kéo các acc khác; restart đúng worker = kill + spawn lại
    đúng PID trong registry. Mac dev (AVD, 1 thiết bị) đặt
    ``manage_instances: false`` — orchestrator chỉ quản lý tiến trình, không
    đụng ldconsole; trên server Windows bật true. Code không if/else theo
    môi trường — chỉ khác giá trị config này.
    """

    # Có điều khiển vòng đời instance LDPlayer qua ldconsole hay không
    # (false = thiết bị đã chạy sẵn, vd AVD trên Mac dev)
    manage_instances: bool = False
    # Index instance MẪU (đã cài game, 960x540/DPI 160) để `ldconsole copy`
    # khi thêm tài khoản mới; None = không cho provision
    template_instance_index: int | None = None
    # Số instance chạy đồng thời tối đa (farm + main); mở main khi kín slot
    # thì scheduler yêu cầu tạm dừng 1 worker farm trước
    max_concurrent_instances: int = Field(gt=0, default=5)
    # Chờ Android boot xong sau `ldconsole launch` (giây)
    boot_timeout: float = Field(gt=0, default=180.0)
    # Chờ worker dừng sạch qua kill-switch trước khi buộc dừng tiến trình (giây)
    stop_timeout: float = Field(gt=0, default=120.0)
    # Thư mục registry PID + file trạng thái từng worker
    workers_dir: Path = Path("logs/workers")
    # Gom yêu cầu chuyển vàng: đợi tối đa N giây kể từ yêu cầu đầu tiên để
    # nhiều acc đạt ngưỡng gần nhau đi chung MỘT lần mở main (0 = mở ngay)
    main_batch_window_seconds: float = Field(ge=0, default=300.0)


class TelegramSettings(_StrictModel):
    enabled: bool
    bot_token: str = ""
    chat_id: str = ""


class LoggingSettings(_StrictModel):
    dir: Path
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"]


class PathSettings(_StrictModel):
    accounts_dir: Path
    coordinates_file: Path


class Settings(_StrictModel):
    """Top-level schema of ``config/settings.yaml``."""

    ldplayer: LDPlayerSettings
    game: GameSettings
    templates: TemplateSettings
    vision: VisionSettings
    ocr: OcrSettings
    safety: SafetySettings
    sell: SellSettings
    quality: QualitySettings
    farm: FarmSettings
    login: LoginSettings
    # Mặc định đầy đủ để settings.yaml cũ (chưa có mục này) vẫn load được
    orchestrator: OrchestratorSettings = OrchestratorSettings()
    telegram: TelegramSettings
    logging: LoggingSettings
    paths: PathSettings


class AccountLDPlayer(_StrictModel):
    index: int = Field(ge=0)
    adb_serial: str


class AccountLogin(_StrictModel):
    server: str
    username: str
    password: str
    # Slot nhân vật trên màn "Chọn nhân vật" (1 = trên cùng)
    character_slot: int = Field(default=1, ge=1)


class AccountFarm(_StrictModel):
    # Tên map farm, đúng như hiển thị trong game. Tên này sinh ra tên template:
    # map/row_<slug> (dòng trong tab "Tìm map") và map/hud_name_<slug> (tên map
    # trên HUD) — xem map_slug() trong tasks/navigate.py.
    map: str
    # Điểm farm: PIXEL trên ẢNH bản đồ của màn bản đồ (màn 960x540) — tap vào
    # đó là game tự tìm đường (kể cả xuyên map). Người vận hành chọn điểm bằng
    # mắt từ ảnh chụp màn bản đồ. (0, 0) = chưa cấu hình — farm task từ chối chạy.
    spot: tuple[int, int] = (0, 0)
    # HP dưới ngưỡng này (%) liên tiếp nhiều chu kỳ → tín hiệu HP_LOW, rút về
    hp_retreat_percent: int = Field(default=30, ge=1, le=99)
    # Nghỉ trong thành tới khi HP hồi lại >= ngưỡng này (%) mới quay lại farm
    hp_resume_percent: int = Field(default=90, ge=1, le=100)
    # Map + điểm an toàn để rút về khi HP thấp (thường là trong thành);
    # retreat_spot cùng hệ quy chiếu với spot (pixel trên ảnh bản đồ)
    retreat_map: str = ""
    retreat_spot: tuple[int, int] = (0, 0)
    gold_transfer_threshold: int = Field(ge=0)

    @model_validator(mode="after")
    def _resume_above_retreat(self) -> AccountFarm:
        if self.hp_resume_percent <= self.hp_retreat_percent:
            raise ValueError(
                "hp_resume_percent must be greater than hp_retreat_percent "
                f"({self.hp_resume_percent} <= {self.hp_retreat_percent})"
            )
        return self


class AccountConfig(_StrictModel):
    """Schema of one file in ``config/accounts/``."""

    name: str
    role: Literal["main", "farm"]
    ldplayer: AccountLDPlayer
    login: AccountLogin
    farm: AccountFarm


class LoginCoordinates(_StrictModel):
    """Fallback coordinates of the login flow (SDK form has no templates)."""

    username_field: tuple[int, int]
    password_field: tuple[int, int]
    char_slots: list[tuple[int, int]]


class MapListGeometry(_StrictModel):
    """Geometry of the map screen's left list ("Tìm map") — dynamic rows,
    untemplatable; the harvest tool taps rows by position (đo trên AVD 27/10)."""

    # Vùng danh sách (x, y, w, h) — dùng làm vùng vuốt khi cuộn
    rect: tuple[int, int, int, int]
    # Tâm X của mọi dòng; Y dòng đầu; bước Y giữa 2 dòng; số dòng nhìn thấy
    row_x: int
    first_row_y: int
    row_pitch: int = Field(gt=0)
    visible_rows: int = Field(gt=0)


class FarmCoordinates(_StrictModel):
    """Fixed UI positions of the farm flow (no templates possible there)."""

    # Điểm tap mở màn bản đồ (minimap góc phải trên HUD)
    minimap: tuple[int, int]
    # ROI thanh HP trên HUD (x, y, w, h) — đọc % máu bằng màu
    hp_bar_rect: tuple[int, int, int, int]
    # ROI dòng toạ độ hiện tại trên HUD — pixel-diff phát hiện đứng yên
    hud_coords_rect: tuple[int, int, int, int]
    # Vùng ẢNH BẢN ĐỒ trên màn bản đồ (x, y, w, h) — spot 0/0 nghĩa là
    # "chưa cấu hình, tap vào TÂM vùng này" (trung tâm map đích)
    map_image_rect: tuple[int, int, int, int]
    # Tâm 8 ô trên vòng chiêu HUD (ô 1-8). (0, 0) = chưa đo — farm task từ
    # chối bật auto chiêu với ô chưa đo toạ độ
    skill_slots: list[tuple[int, int]] = []
    # Ô icon Túi đồ trên HUD (x, y, w, h) — kiểm tỉ lệ pixel đỏ khi icon
    # chuyển trạng thái "Đầy"; None = tắt phát hiện túi đầy
    bag_full_icon_rect: tuple[int, int, int, int] | None = None
    # Danh sách map bên trái màn bản đồ — cho tools/harvest_maps.py
    map_list: MapListGeometry | None = None
    # Thanh tiêu đề màn bản đồ (tên map đang preview) — crop cho người đọc
    map_title_rect: tuple[int, int, int, int] | None = None
    # Vùng dòng "Đi: <Map>(x, y)" game in khi bắt đầu tìm đường — nguồn cặp
    # (pixel, toạ độ) cho tools/calibrate_map.py
    travel_text_rect: tuple[int, int, int, int] | None = None

    @model_validator(mode="after")
    def _skill_slots_len(self) -> FarmCoordinates:
        if self.skill_slots and len(self.skill_slots) != 8:
            raise ValueError(
                f"skill_slots must have exactly 8 entries, got {len(self.skill_slots)}"
            )
        return self


class BagGrid(_StrictModel):
    """Geometry of the bag's item grid on the shop screen (measured on AVD)."""

    # Góc trái-trên của Ô ĐẦU TIÊN (x, y)
    origin: tuple[int, int]
    # Kích thước một ô (w, h)
    cell: tuple[int, int]
    # Bước giữa hai ô liền kề (x, y)
    pitch: tuple[int, int]
    cols: int = Field(gt=0)
    rows: int = Field(gt=0)


class SellCoordinates(_StrictModel):
    """Fixed UI geometry of the sell flow (bag grid, money row, page tabs)."""

    bag_grid: BagGrid
    # Vùng hiển thị tiền (vàng/bạc/đồng) dưới túi — so trước/sau khi bán
    money_rect: tuple[int, int, int, int]
    # Nút chuyển trang túi (1, 2, 3) bên phải lưới
    page_buttons: list[tuple[int, int]]


class Coordinates(_StrictModel):
    """Schema of ``config/coordinates.yaml`` — fallback UI coordinates.

    Rule #1 of CLAUDE.md: every use of these must emit a warning log.
    """

    login: LoginCoordinates
    farm: FarmCoordinates
    sell: SellCoordinates


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ConfigError(f"config file not found: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"config file {path} must contain a YAML mapping at top level")
    return data


def _format_validation_error(path: Path, exc: ValidationError) -> str:
    lines = [f"invalid config in {path}:"]
    for err in exc.errors():
        loc = ".".join(str(part) for part in err["loc"]) or "<root>"
        lines.append(f"  - {loc}: {err['msg']}")
    return "\n".join(lines)


def load_settings(path: Path = DEFAULT_SETTINGS_PATH) -> Settings:
    """Load and validate the main settings file.

    Raises:
        ConfigError: if the file is missing, is not valid YAML, or fails
            schema validation (missing key, wrong type, unknown key).
    """
    data = _read_yaml(path)
    try:
        return Settings.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(_format_validation_error(path, exc)) from exc


def load_account(path: Path) -> AccountConfig:
    """Load and validate a single account config file.

    Raises:
        ConfigError: same conditions as :func:`load_settings`.
    """
    data = _read_yaml(path)
    try:
        return AccountConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(_format_validation_error(path, exc)) from exc


def load_coordinates(path: Path) -> Coordinates:
    """Load and validate the fallback-coordinates file.

    Raises:
        ConfigError: same conditions as :func:`load_settings`.
    """
    data = _read_yaml(path)
    try:
        return Coordinates.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(_format_validation_error(path, exc)) from exc


def load_all_accounts(accounts_dir: Path) -> list[AccountConfig]:
    """Load every account YAML in ``accounts_dir``, skipping ``example.yaml``."""
    if not accounts_dir.is_dir():
        raise ConfigError(f"accounts directory not found: {accounts_dir}")
    accounts = [
        load_account(f) for f in sorted(accounts_dir.glob("*.yaml")) if f.name != "example.yaml"
    ]
    if not accounts:
        raise ConfigError(
            f"no account config found in {accounts_dir} "
            "(copy example.yaml to e.g. main.yaml and edit it)"
        )
    return accounts
