"""Load and validate YAML configuration with pydantic v2.

All runtime values (paths, thresholds, timeouts, blacklists) live in YAML;
code contains logic only. Validation errors are re-raised as ``ConfigError``
with a human-readable message pointing at the exact key and problem.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, cast

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
    # Chờ sau mỗi lần tap bán, trước khi chụp lại màn hình (giây)
    tap_settle_seconds: float = Field(default=0.15, ge=0.0)


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
    # Chờ map đóng sau mỗi tap điểm đến TRỪ lần cuối (lần cuối chờ đủ
    # map_timeout): tap ăn thì map đóng sau ~2s, chờ đủ 10s cho tap bị nuốt
    # là phí — đo AVD 07/11
    map_spot_retap_seconds: float = Field(gt=0, default=3.5)
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
    # Tầng cảnh báo sớm: dòng chat đỏ "Hành trang mang theo quá nhiều vật
    # phẩm..." cũng tính là túi đầy. MẶC ĐỊNH TẮT (sự cố 2026-11-04): game
    # phát dòng này từ ~75% cân nặng — chưa khoá võ công, farm vẫn bình
    # thường — và tab chat "Hệ thống" giữ dòng cũ mãi không trôi, nên tool
    # cứ thấy là đòi về bán -> vòng lặp về thành/cửa hàng/ra farm vô tận
    # (nhất là khi dry_run không bán được gì). Chỉ bật khi muốn đi bán sớm
    # và đã chắc chắn bán được.
    bag_heavy_notice: bool = False
    # Auto nhấn chiêu + vật phẩm hồi máu trong vòng MONITOR
    skills: FarmSkillSettings = FarmSkillSettings()


class LoginSettings(_StrictModel):
    """Timeouts/knobs of the login task (R1)."""

    boot_timeout: float = Field(gt=0)
    flow_timeout: float = Field(gt=0)
    ingame_timeout: float = Field(gt=0)
    clear_chars: int = Field(gt=0)


class WatchdogSettings(_StrictModel):
    """Tự phục hồi worker hỏng (orchestrator, chạy nền trong GUI server).

    Chỉ áp dụng cho acc đang ở trạng thái "muốn chạy" (bấm Chạy / Chạy tất cả);
    bấm Dừng là watchdog thôi can thiệp. Lỗi cần người (thiếu template, sai
    server, vượt giới hạn an toàn...) không bao giờ tự chạy lại.
    """

    enabled: bool = True
    # Giãn cách trước lần chạy lại thứ 1, 2, 3+ (giây). Từ lần hỏng thứ 2 liên
    # tiếp: tắt/bật lại cả instance LDPlayer chứ không chỉ worker
    backoff_seconds: list[float] = Field(default=[60.0, 180.0, 600.0], min_length=1)
    # Quá số lần hỏng này trong 1 giờ thì thôi, báo "cần người xử lý"
    max_restarts_per_hour: int = Field(gt=0, default=5)
    # Worker còn sống nhưng không chụp màn hình quá N giây = treo
    hang_timeout: float = Field(gt=0, default=300.0)
    # Chạy ổn liên tục N giây thì bộ đếm hỏng liên tiếp về 0
    healthy_after: float = Field(gt=0, default=600.0)
    # Chu kỳ kiểm tra (giây)
    poll_interval: float = Field(gt=0, default=10.0)


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
    # Dung lượng ổ đĩa (GB) phải còn trống SAU khi clone; không đủ thì từ chối
    # provision — ổ đầy làm hỏng đĩa ảo của các máy đang chạy
    clone_headroom_gb: float = Field(ge=0, default=5.0)
    # Giới hạn thời gian `ldconsole copy` (giây): lệnh copy cả đĩa máy mẫu xong
    # mới trả về; quá hạn thì tool dừng lệnh -> để lại máy copy dở
    clone_timeout: float = Field(gt=0, default=600.0)
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
    # Tự phục hồi worker hỏng/treo (mặc định đầy đủ — settings cũ vẫn load)
    watchdog: WatchdogSettings = WatchdogSettings()


Currency = Literal["vang", "bac", "dong"]


class TransferSettings(_StrictModel):
    """Gold transfer to the main account (W3/R4): rendezvous + trade knobs.

    Giao dịch trực tiếp giữa 2 nhân vật đứng cạnh nhau tại ĐIỂM HẸN cố định
    trong thành. Acc farm tới trước chờ, acc main (on-demand) tới sau, hai
    bên đồng bộ qua file hàng rào trong ``orchestrator.workers_dir``.
    """

    # Bật vòng điều phối chuyển vàng (GUI server chạy nền); false = farm chỉ
    # ghi cờ gold_request + báo người như trước
    enabled: bool = False
    # Loại tiền dùng làm "vàng" của ngưỡng/giao dịch: vang | bac | dong
    # (3 cột dưới túi). Test lúc ít vàng: đổi sang "bac"
    currency: Currency = "vang"
    # Tên file acc main (không đuôi .yaml) trong paths.accounts_dir
    main_stem: str = "main"
    # Điểm hẹn: map + pixel trên ảnh bản đồ (cùng hệ quy chiếu farm.spot)
    rendezvous_map: str = ""
    rendezvous_spot: tuple[int, int] = (0, 0)
    # Chờ bên kia báo "đã tới điểm hẹn" (giây) — quá hạn thì huỷ sạch về farm
    partner_wait_timeout: float = Field(gt=0, default=600.0)
    # Chờ từng bước trong khung giao dịch (mời/chấp nhận/khoá/xác nhận)
    trade_step_timeout: float = Field(gt=0, default=30.0)
    # Dung sai khi đối chiếu tiền trước/sau giao dịch (phí/làm tròn OCR)
    verify_tolerance: int = Field(ge=0, default=0)
    # Chu kỳ vòng điều phối đọc state file các worker (giây)
    poll_interval: float = Field(gt=0, default=10.0)
    # Sau một chuyến thất bại, chờ bao lâu mới thử lại acc đó (giây)
    retry_delay_seconds: float = Field(ge=0, default=900.0)


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

    # Mặc định chung cho mọi acc (D1b): file acc chỉ ghi phần KHÁC. None =
    # settings.yaml cũ chưa có mục này -> file acc phải ghi đủ như trước
    account_defaults: AccountDefaults | None = None

    @property
    def farm_defaults(self) -> AccountFarm | None:
        """Shared ``farm`` defaults for :func:`load_account` (None = legacy)."""
        return self.account_defaults.farm if self.account_defaults is not None else None

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
    transfer: TransferSettings = TransferSettings()
    telegram: TelegramSettings
    logging: LoggingSettings
    paths: PathSettings


class AccountLDPlayer(_StrictModel):
    # None = acc chưa được cấp máy -> chỉ "Cấp máy mới" (provision) mới gán index;
    # tránh clone nhầm cho acc đã có instance (lỗi thực tế 28/10)
    index: int | None = Field(default=None, ge=0)
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
    # Số tiền GIỮ LẠI trên acc farm sau mỗi lần chuyển (mua thuốc, sửa đồ)
    gold_keep: int = Field(ge=0, default=0)

    @model_validator(mode="after")
    def _resume_above_retreat(self) -> AccountFarm:
        if self.hp_resume_percent <= self.hp_retreat_percent:
            raise ValueError(
                "hp_resume_percent must be greater than hp_retreat_percent "
                f"({self.hp_resume_percent} <= {self.hp_retreat_percent})"
            )
        return self


ACCOUNT_FARM_FIELDS: tuple[str, ...] = tuple(AccountFarm.model_fields)


class AccountFarmOverride(_StrictModel):
    """``farm:`` as WRITTEN in an account file: every field optional.

    None/absent = "dùng cài đặt chung" (``account_defaults.farm`` in
    settings.yaml). Same field names as :class:`AccountFarm`, unknown keys
    still rejected. Range checks run after merging, on :class:`AccountFarm`.
    """

    map: str | None = None
    spot: tuple[int, int] | None = None
    hp_retreat_percent: int | None = None
    hp_resume_percent: int | None = None
    retreat_map: str | None = None
    retreat_spot: tuple[int, int] | None = None
    gold_transfer_threshold: int | None = None
    gold_keep: int | None = None

    def overrides(self) -> dict[str, Any]:
        """Only the fields the account actually sets."""
        return {k: v for k, v in self.model_dump(mode="json").items() if v is not None}


class AccountDefaults(_StrictModel):
    """``account_defaults`` of settings.yaml — shared by every account."""

    farm: AccountFarm


class AccountConfig(_StrictModel):
    """Schema of one file in ``config/accounts/`` AFTER merging defaults."""

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
    # Ô SỐ của từng loại tiền trên dòng đó (OCR đọc số) — None = chưa đo,
    # tool không đọc được tiền (không phát tín hiệu chuyển vàng)
    money_cells: MoneyCells | None = None
    # Nút chuyển trang túi (1, 2, 3) bên phải lưới
    page_buttons: list[tuple[int, int]]


class MoneyCells(_StrictModel):
    """OCR rectangles (x, y, w, h) of the three money figures under the bag."""

    vang: tuple[int, int, int, int]
    bac: tuple[int, int, int, int]
    dong: tuple[int, int, int, int]

    def rect(self, currency: Currency) -> tuple[int, int, int, int]:
        """Rectangle of one currency's figure."""
        return cast(tuple[int, int, int, int], getattr(self, currency))


class TradeCoordinates(_StrictModel):
    """Fixed geometry of the trade window (chụp/đo khi có 2 acc trên LDPlayer).

    Mọi rect (0, 0, 0, 0) / điểm (0, 0) = CHƯA ĐO — task giao dịch từ chối
    chạy thay vì tap mù.
    """

    # Ô nhập số tiền trong khung giao dịch (tap để gõ số)
    money_input: tuple[int, int] = (0, 0)
    # Ô số tiền MÌNH đã đặt / bên kia đã đặt trong khung giao dịch (OCR)
    own_offer_rect: tuple[int, int, int, int] = (0, 0, 0, 0)
    partner_offer_rect: tuple[int, int, int, int] = (0, 0, 0, 0)
    # Điểm tap lên nhân vật bên kia khi 2 bên đứng đúng điểm hẹn (chọn mục tiêu)
    partner_tap: tuple[int, int] = (0, 0)
    # Tên trên BẢNG MỤC TIÊU (đỉnh màn) sau khi chọn người chơi — tap vào đó
    # mới ra menu người chơi (Tán gẫu/Hảo hữu/Giao dịch/...)
    plate_name_tap: tuple[int, int] = (0, 0)

    def unmeasured(self) -> list[str]:
        """Names of fields still at their placeholder value."""
        out: list[str] = []
        for name in ("money_input", "partner_tap", "plate_name_tap"):
            if getattr(self, name) == (0, 0):
                out.append(name)
        for name in ("own_offer_rect", "partner_offer_rect"):
            if getattr(self, name) == (0, 0, 0, 0):
                out.append(name)
        return out


class Coordinates(_StrictModel):
    """Schema of ``config/coordinates.yaml`` — fallback UI coordinates.

    Rule #1 of CLAUDE.md: every use of these must emit a warning log.
    """

    login: LoginCoordinates
    farm: FarmCoordinates
    sell: SellCoordinates
    trade: TradeCoordinates = TradeCoordinates()


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


def local_settings_path(path: Path) -> Path:
    """Per-machine override file next to ``path`` (``settings.local.yaml``)."""
    return path.with_name(f"{path.stem}.local{path.suffix}")


def read_local_overrides(path: Path) -> dict[str, Any]:
    """Overrides from the local file of ``path``; ``{}`` when absent or empty.

    Raises:
        ConfigError: the local file is not valid YAML or not a mapping.
    """
    local = local_settings_path(path)
    if not local.is_file():
        return {}
    try:
        data = yaml.safe_load(local.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {local}: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"config file {local} must contain a YAML mapping at top level")
    return data


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Return ``base`` with ``override`` applied key by key (lists are replaced)."""
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_settings(path: Path = DEFAULT_SETTINGS_PATH) -> Settings:
    """Load and validate the main settings file plus its local overrides.

    ``settings.local.yaml`` next to ``path`` (git-ignored) holds the keys that
    differ per machine (Mac/AVD dev vs Windows/LDPlayer); it is deep-merged
    over ``path`` before validation, so a typo there is rejected the same way.

    Raises:
        ConfigError: if the file is missing, is not valid YAML, or fails
            schema validation (missing key, wrong type, unknown key).
    """
    data = _read_yaml(path)
    overrides = read_local_overrides(path)
    try:
        return Settings.model_validate(deep_merge(data, overrides))
    except ValidationError as exc:
        source = Path(f"{path} + {local_settings_path(path).name}") if overrides else path
        raise ConfigError(_format_validation_error(source, exc)) from exc


def merge_account_farm(
    raw_farm: Any, defaults: AccountFarm | None, path: Path
) -> dict[str, Any]:
    """``farm:`` of an account file merged over the shared defaults.

    Each field the file sets wins; the rest come from ``defaults``. With no
    defaults (old settings.yaml) the file must be complete — missing keys are
    then reported by :class:`AccountFarm` validation as before.

    Raises:
        ConfigError: unknown key / wrong type inside ``farm``.
    """
    try:
        override = AccountFarmOverride.model_validate(raw_farm or {})
    except ValidationError as exc:
        raise ConfigError(_format_validation_error(path, exc)) from exc
    base = defaults.model_dump(mode="json") if defaults is not None else {}
    return {**base, **override.overrides()}


def account_farm_overrides(path: Path) -> list[str]:
    """Names of the ``farm`` fields an account file sets itself (GUI)."""
    raw = _read_yaml(path).get("farm") or {}
    return [k for k in ACCOUNT_FARM_FIELDS if isinstance(raw, dict) and raw.get(k) is not None]


def load_account(path: Path, defaults: AccountFarm | None = None) -> AccountConfig:
    """Load and validate a single account config file.

    Args:
        path: the account YAML.
        defaults: ``settings.account_defaults.farm`` — fields the file does
            not set are taken from here. Cross-field rules (resume > retreat)
            are checked on the MERGED values.

    Raises:
        ConfigError: same conditions as :func:`load_settings`.
    """
    data = _read_yaml(path)
    data = {**data, "farm": merge_account_farm(data.get("farm"), defaults, path)}
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


def load_all_accounts(
    accounts_dir: Path, defaults: AccountFarm | None = None
) -> list[AccountConfig]:
    """Load every account YAML in ``accounts_dir``, skipping ``example.yaml``."""
    if not accounts_dir.is_dir():
        raise ConfigError(f"accounts directory not found: {accounts_dir}")
    accounts = [
        load_account(f, defaults)
        for f in sorted(accounts_dir.glob("*.yaml"))
        if f.name != "example.yaml"
    ]
    if not accounts:
        raise ConfigError(
            f"no account config found in {accounts_dir} "
            "(copy example.yaml to e.g. main.yaml and edit it)"
        )
    return accounts
