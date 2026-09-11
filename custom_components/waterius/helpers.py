from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


def extract_source_id(channel_raw: Dict[str, Any]) -> Optional[int]:
    v = channel_raw.get("source")
    if isinstance(v, int):
        return v
    if isinstance(v, str) and v.strip().isdigit():
        return int(v.strip())
    return None


def extract_export_id(channel_raw: Dict[str, Any]) -> Optional[int]:
    v = channel_raw.get("export")
    if isinstance(v, int):
        return v
    if isinstance(v, str) and v.strip().isdigit():
        return int(v.strip())
    return None


def _get(raw: Dict[str, Any], *keys: str, default: Any = None) -> Any:
    for k in keys:
        if k in raw:
            return raw.get(k)
    return default


def extract_uk_period_values(reports: List[Dict[str, Any]]) -> Dict[str, Any]:
    for r in reports:
        st = (r.get("status_text") or "").strip().lower()
        if st == "ошибка ук":
            continue
        if st == "отправлено":
            return {
                "prev_period_value": r.get("uk_read_value"),
                "curr_period_value": r.get("uk_send_value"),
                "timestamp": r.get("timestamp"),
            }

    ts = None
    if reports and isinstance(reports[0], dict):
        ts = reports[0].get("timestamp")

    return {
        "prev_period_value": "ошибка УК",
        "curr_period_value": "ошибка УК",
        "timestamp": ts,
    }


def build_channel_attrs(ch_raw: Dict[str, Any], uk_vals: Dict[str, Any]) -> Dict[str, Any]:
    attrs: Dict[str, Any] = {
        "Серийный номер": _get(ch_raw, "serial"),
        "Статус отчёта": _get(ch_raw, "report_status", "reportStatus"),
        "Дата поверки": _get(ch_raw, "service_date", "serviceDate"),
    }
    warnings = _get(ch_raw, "warnings")
    if warnings:
        attrs["Предупреждения"] = warnings

    attrs.update(
        {
            "Значение в предыдущем периоде": uk_vals.get("prev_period_value", "ошибка УК"),
            "Значение в текущем периоде": uk_vals.get("curr_period_value", "ошибка УК"),
            "Передача в УК": uk_vals.get("timestamp"),
        }
    )
    return attrs


def normalize_tarif_ended(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        return ""
    if "T" in raw:
        return raw
    try:
        dt = datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return dt.isoformat()
    except Exception:
        return raw


def compute_days_left(raw: str) -> Optional[int]:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        if "T" in raw:
            dt_due = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        else:
            dt_due = datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        now_utc = datetime.now(timezone.utc)
        return (dt_due.date() - now_utc.date()).days
    except Exception:
        return None


def parse_personal_account(title4: Any) -> str:
    s = str(title4 or "").strip()
    return s.replace("Лицевой счёт:", "").strip()


def normalize_ha_meter_value(state: Any, data_type: int) -> tuple[float, str, bool]:
    """Normalize a Home Assistant total sensor to Waterius' expected unit.

    Returns (value, normalized_unit, converted). Raises ValueError for obvious
    instantaneous-rate/power sensors. Unknown or empty units are left unchanged.
    """
    try:
        value = float(state.state)
    except (TypeError, ValueError) as err:
        raise ValueError(f"state is not numeric: {getattr(state, 'state', None)}") from err

    attrs = getattr(state, "attributes", {}) or {}
    unit_raw = str(attrs.get("unit_of_measurement") or "").strip()
    unit = unit_raw.lower().replace(" ", "")

    # Water / gas / drinking water: Universal Cloud expects cubic metres.
    if data_type in (0, 1, 3, 9):
        if "/" in unit or unit in ("l/min", "l/h", "m³/h", "m3/h"):
            raise ValueError(f"instantaneous flow unit is not a meter total: {unit_raw}")
        if unit in ("l", "liter", "litre", "литр", "литры", "л"):
            return value / 1000.0, "m³", True
        if unit in ("m³", "m3", ""):
            return value, "m³" if unit else "", False
        return value, unit_raw, False

    # Electricity tariff values are energy totals in kWh, not power.
    if data_type in (2, 5, 6, 7, 8):
        if unit in ("w", "kw", "mw") or "/" in unit:
            raise ValueError(f"power/rate unit is not an energy total: {unit_raw}")
        if unit == "wh":
            return value / 1000.0, "kWh", True
        if unit == "mwh":
            return value * 1000.0, "kWh", True
        if unit in ("kwh", ""):
            return value, "kWh" if unit else "", False
        return value, unit_raw, False

    # Heat and custom counters may use provider-specific units. Preserve them.
    if "/" in unit:
        raise ValueError(f"rate unit is not a cumulative meter total: {unit_raw}")
    return value, unit_raw, False
