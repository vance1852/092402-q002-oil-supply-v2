"""可注入时间源。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


@dataclass
class FrozenClock:
    current: datetime

    def now(self) -> datetime:
        if self.current.tzinfo is None:
            raise ValueError("冻结时钟必须带时区")
        return self.current

    def advance(self, **kwargs: float) -> None:
        self.current += timedelta(**kwargs)


def isoformat(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("时间必须带时区")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_instant(value: Any, field: str = "时间") -> datetime:
    """严格解析带时区的 ISO 8601 时刻；拒绝日期、无时区时间和非字符串输入。"""

    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} 必须是带时区的 ISO 8601 时刻")
    text = value.strip()
    if "T" not in text:
        raise ValueError(f"{field} 必须是带时区的 ISO 8601 时刻")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} 必须是带时区的 ISO 8601 时刻") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} 必须包含时区")
    return parsed.astimezone(timezone.utc)


def instant_text(value: Any, field: str = "时间") -> str:
    """解析时刻并规范化为 UTC、以 Z 结尾的稳定文本。"""

    return isoformat(parse_instant(value, field))
