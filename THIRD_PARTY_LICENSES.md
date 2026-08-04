# Third-Party Licenses

Every runtime dependency is **permissive** (MIT / BSD / Apache-2.0). None are
copyleft (GPL/LGPL/AGPL), so this project can be kept **closed-source** and
redistributed to customers without license contamination.

| Package | License | Type |
|---------|---------|------|
| pydantic | MIT | permissive |
| numpy | BSD-3-Clause | permissive |
| scipy | BSD-3-Clause | permissive |
| lmfit | BSD-3-Clause | permissive |
| asteval | MIT | permissive |
| pyyaml | MIT | permissive |
| streamlit | Apache-2.0 | permissive |
| pandas | BSD-3-Clause | permissive |
| optuna | MIT | permissive |
| fastapi | MIT | permissive |
| uvicorn | BSD-3-Clause | permissive |
| httpx | BSD-3-Clause | permissive |
| sqlite3 | Python stdlib (PSF) | permissive |

The drag-drop canvas (`web/index.html`) is dependency-free vanilla JS + SVG —
no CDN, no bundler, works offline (suits closed-source / air-gapped use).

## External processes (not linked, not redistributed)

| Component | License | How it is used |
|-----------|---------|----------------|
| [OpticStudioMCPServer](https://github.com/zym1998year/OpticStudioMCPServer) | MIT | Zemax backend. A **separate .NET process** the platform talks to over MCP stdio (`optplat/zemax.py`). No code is linked or vendored; the customer installs it alongside OpticStudio. |
| Zemax OpticStudio + ZOS-API | commercial (Ansys) | The customer's own licensed installation. |

The MCP client (`optplat/mcp_client.py`) is **stdlib-only** — talking MCP added
no third-party dependency.

## Deliberately NOT used

| Package | License | Why avoided |
|---------|---------|-------------|
| **Badger** (xopt-org) | **GPL-3.0** | Copyleft — would force this project open once distributed to customers. |

## Safe future upgrades (when the MVP is outgrown)

All permissive, drop-in when needed:

| Need | Package | License |
|------|---------|---------|
| Real Bayesian / multi-objective / NSGA-II generators (ask/tell compatible) | **Xopt** | Apache-2.0 |
| Bayesian optimization with a scipy-like API | scikit-optimize | BSD-3 |
| Drag-drop node canvas (frontend) | React Flow | MIT |
| Instrument I/O (motor stage, power meter) | PyVISA / PyMeasure | MIT |
