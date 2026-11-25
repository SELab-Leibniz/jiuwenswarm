"""Loopback-only standard-library server for the read-only Runtime Dashboard."""

from __future__ import annotations

import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ..artifacts import read_json
from .projection import (
    build_dashboard_projection,
    empty_dashboard_projection,
)
from .schemas import LATEST_CAMPAIGN_SCHEMA
from .store import CampaignReader

_ASSET_ROOT = Path(__file__).with_name("dashboard_assets")
_ASSETS = {
    "/": (_ASSET_ROOT / "index.html", "text/html; charset=utf-8"),
    "/assets/style.css": (_ASSET_ROOT / "style.css", "text/css; charset=utf-8"),
    "/assets/app.js": (_ASSET_ROOT / "app.js", "text/javascript; charset=utf-8"),
}


class RuntimeDashboardServer(ThreadingHTTPServer):
    """HTTP server carrying only read-side selection configuration."""

    daemon_threads = True

    def __init__(
        self,
        workspace: Path,
        campaign: str,
        port: int,
    ) -> None:
        self.workspace = workspace.expanduser().resolve()
        self.campaign = campaign
        super().__init__(("127.0.0.1", port), RuntimeDashboardHandler)


class RuntimeDashboardHandler(BaseHTTPRequestHandler):
    server: RuntimeDashboardServer

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler contract
        route = self.path.split("?", 1)[0]
        if route == "/api/projection":
            self._send_json(load_dashboard_projection(self.server.workspace, self.server.campaign))
            return
        asset = _ASSETS.get(route)
        if asset is None:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        path, content_type = asset
        try:
            body = path.read_bytes()
        except OSError:
            self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler contract
        self.send_error(HTTPStatus.METHOD_NOT_ALLOWED)

    def log_message(self, format: str, *args: object) -> None:
        """Keep polling requests out of the operator terminal."""

    def _send_json(self, payload: dict[str, object]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def load_dashboard_projection(workspace: Path, campaign: str) -> dict[str, object]:
    """Rebuild a current read-only view, without creating or repairing any artifact."""
    resolved_campaign = campaign
    if campaign == "latest":
        latest = workspace.expanduser().resolve() / ".pact" / "runtime" / "latest.json"
        if not latest.is_file():
            return empty_dashboard_projection()
        try:
            latest_payload = read_json(latest)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
            return empty_dashboard_projection(availability="corrupt")
        if latest_payload.get("schema") != LATEST_CAMPAIGN_SCHEMA:
            return empty_dashboard_projection(availability="corrupt")
        value = latest_payload.get("campaign_id")
        if not isinstance(value, str) or not value:
            return empty_dashboard_projection(availability="corrupt")
        resolved_campaign = value
    try:
        reader = CampaignReader(workspace, resolved_campaign)
    except RuntimeError:
        return empty_dashboard_projection(
            availability="corrupt", campaign_id=resolved_campaign
        )
    if not reader.paths.current_state.is_file():
        return empty_dashboard_projection(availability="missing", campaign_id=resolved_campaign)
    try:
        return build_dashboard_projection(reader)
    except (OSError, ValueError, RuntimeError, KeyError, TypeError):
        return empty_dashboard_projection(availability="corrupt", campaign_id=resolved_campaign)


def create_dashboard_server(
    workspace: Path,
    *,
    campaign: str = "latest",
    port: int = 8765,
) -> RuntimeDashboardServer:
    """Create, but do not start, a loopback-only Dashboard server."""
    if port < 0 or port > 65535:
        raise ValueError("dashboard port must be between 0 and 65535")
    return RuntimeDashboardServer(workspace, campaign, port)


def serve_dashboard(
    workspace: Path,
    *,
    campaign: str = "latest",
    port: int = 8765,
) -> None:
    """Serve until interrupted; the server owns no Runtime write capability."""
    server = create_dashboard_server(workspace, campaign=campaign, port=port)
    host, bound_port = server.server_address[:2]
    host_text = host.decode() if isinstance(host, bytes) else str(host)
    print(f"PACT Runtime Dashboard: http://{host_text}:{bound_port}")
    print(f"workspace: {workspace.expanduser().resolve()}")
    print(f"campaign:  {campaign}")
    print("mode:      read-only projection")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


__all__ = [
    "RuntimeDashboardServer",
    "create_dashboard_server",
    "load_dashboard_projection",
    "serve_dashboard",
]
