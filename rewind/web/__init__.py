"""Start the loopback UI or export self-contained browser replays."""

from html import escape
from pathlib import Path
from secrets import token_urlsafe
from tempfile import NamedTemporaryFile

from rewind.config import RewindConfig


UI_PORT = 8765


def serve(root: Path, repository: Path, *, config: RewindConfig | None = None, case: str = "a") -> None:
    import uvicorn
    from rewind.web.server import create_app

    token = token_urlsafe(32)
    app = create_app(root, repository, token, UI_PORT, config=config, replay_case=case)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=UI_PORT, access_log=False, proxy_headers=False))
    directory = root / ".artifacts"
    directory.mkdir(exist_ok=True)
    url = escape(f"http://127.0.0.1:{UI_PORT}/open/{token}", quote=True)
    with NamedTemporaryFile(mode="w", prefix="open-rewind-", suffix=".html", dir=directory,
                            encoding="utf-8", delete_on_close=False) as launcher:
        launcher.write(f'<!doctype html><meta name="referrer" content="no-referrer"><title>Open Rewind</title>'
                       f'<meta http-equiv="refresh" content="0;url={url}"><a href="{url}">Open Rewind</a>')
        launcher.flush()
        print(f"Open this local file in your browser while Rewind is running: {launcher.name}", flush=True)
        server.run()
