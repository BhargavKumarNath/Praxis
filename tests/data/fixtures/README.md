# Test fixtures

| File | Origin |
| --- | --- |
| `open_meteo_london_2026-01-05.json` | REAL response captured from the Open-Meteo archive API (one request, 2026-10-03). |
| `carbon_gb_2026-01-05.json` | REAL response captured from the NESO Carbon Intensity API (one request, 2026-10-03). |
| `fred_cpiaucsl.json` | HAND-WRITTEN to the documented FRED `series/observations` shape. Values are illustrative, NOT real CPI. No FRED key was available. |
| `eia_ny_demand.json` | HAND-WRITTEN to the documented EIA v2 `region-data` shape. Values are illustrative, NOT real demand. No EIA key was available. |

Tests never call a live API.
