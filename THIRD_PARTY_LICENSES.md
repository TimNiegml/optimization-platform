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
| sqlite3 | Python stdlib (PSF) | permissive |

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
