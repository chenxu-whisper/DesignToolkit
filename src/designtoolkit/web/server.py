from __future__ import annotations

import uvicorn


def run_web_server(host: str = "127.0.0.1", port: int = 8765) -> int:
    uvicorn.run(
        "designtoolkit.web.app:create_app",
        factory=True,
        host=host,
        port=port,
        log_level="info",
    )
    return 0
