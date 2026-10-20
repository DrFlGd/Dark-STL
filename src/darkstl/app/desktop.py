"""Start Dark STL: the server, then a window onto it.

The window is the system's own web view through pywebview (Edge WebView2 on
Windows, WebKit on macOS and Linux), with native folder and file dialogs.
Without pywebview, or with ``--browser``, the default browser opens the page
instead and folders are typed or pasted in.
"""

from __future__ import annotations

import sys
import threading
import time
import webbrowser

from .config import Config, cache_dir
from .server import AppServer
from .service import Host, Service


class WebviewHost(Host):
    dialogs = True

    def __init__(self, window):
        self.window = window

    def pick_folder(self, title: str, start: str | None = None) -> str | None:
        import webview

        picked = self.window.create_file_dialog(webview.FOLDER_DIALOG, directory=start or "")
        return str(picked[0]) if picked else None

    def pick_file(self, title: str, patterns: list[str], start: str | None = None) -> str | None:
        import webview

        types = tuple(patterns) or ("All files (*.*)",)
        picked = self.window.create_file_dialog(webview.OPEN_DIALOG, directory=start or "", file_types=types)
        return str(picked[0]) if picked else None


def start(folders: list[str] | None = None, port: int = 0) -> tuple[Service, AppServer]:
    service = Service(Config.load(), cache_dir())
    service.load_folders()
    for folder in folders or []:
        try:
            service.call("folder_add", {"path": folder})
        except Exception as exc:  # noqa: BLE001 - reported, the app still starts
            print(f"Dark STL: can't add {folder}: {exc}", file=sys.stderr)
    return service, AppServer(service, port).start()


def run(folders: list[str] | None = None, browser: bool = False, port: int = 0) -> int:
    service, server = start(folders, port)
    if not browser:
        try:
            import webview
        except ImportError:
            print("Dark STL: pywebview isn't installed, so the app opens in your browser "
                  "(pip install pywebview for its own window).", file=sys.stderr)
        else:
            window = webview.create_window("Dark STL", server.url, width=1440, height=920, min_size=(900, 600),
                                           background_color="#1c2128")
            service.host = WebviewHost(window)
            try:
                webview.start()
                return 0
            except Exception as exc:  # noqa: BLE001 - no usable web view here: use the browser
                print(f"Dark STL: no app window ({exc}); opening your browser instead.", file=sys.stderr)
            finally:
                server.shutdown()
            service, server = start([], port)
    print(f"Dark STL is running at {server.url}  (Ctrl+C to stop)")
    threading.Timer(0.3, lambda: webbrowser.open(server.url)).start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        server.shutdown()
    return 0


def main() -> int:
    import argparse

    p = argparse.ArgumentParser(prog="darkstl-gui", description="Open the Dark STL explorer")
    p.add_argument("folders", nargs="*", help="game folders to add")
    p.add_argument("--browser", action="store_true", help="open in the web browser instead of an app window")
    p.add_argument("--port", type=int, default=0, help="port for the local server (default: any free port)")
    args = p.parse_args()
    return run(args.folders, args.browser, args.port)


if __name__ == "__main__":
    sys.exit(main())
