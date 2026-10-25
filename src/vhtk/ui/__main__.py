"""Entry point: run the management app.

    python -m vhtk.ui              # cửa sổ desktop (pywebview); thiếu thì mở trình duyệt
    python -m vhtk.ui --browser    # luôn mở bằng trình duyệt mặc định

Binds to 127.0.0.1 only.
"""

from __future__ import annotations

import argparse
import threading
import webbrowser
from pathlib import Path

import uvicorn

from vhtk.ui.server import create_app


def _serve(app_path_settings: Path, host: str, port: int) -> uvicorn.Server:
    config = uvicorn.Config(
        create_app(app_path_settings), host=host, port=port, log_level="warning"
    )
    return uvicorn.Server(config)


def main() -> int:
    parser = argparse.ArgumentParser(description="VHTK2 management app")
    parser.add_argument("--settings", default="config/settings.yaml")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--browser", action="store_true", help="open in browser, no app window")
    args = parser.parse_args()

    host = "127.0.0.1"
    url = f"http://{host}:{args.port}"
    server = _serve(Path(args.settings), host, args.port)

    window = None
    if not args.browser:
        try:
            import webview  # pip install -e ".[desktop]"

            window = webview
        except ImportError:
            print("pywebview chua cai (pip install -e '.[desktop]') -> mo bang trinh duyet")

    if window is None:
        threading.Timer(0.8, webbrowser.open, args=(url,)).start()
        server.run()
        return 0

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    window.create_window("VHTK2 Quản Lý", url, width=1100, height=760)
    window.start()
    server.should_exit = True
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
