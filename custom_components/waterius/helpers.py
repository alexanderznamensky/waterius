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
