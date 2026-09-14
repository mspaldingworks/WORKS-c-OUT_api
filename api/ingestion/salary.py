"""
Turn a scraped posting's salary block into comparable annual numbers.

The payload's `salary` is inconsistent across boards: sometimes it carries numeric
`salaryMin`/`salaryMax`, sometimes only free text ("$25 an hour", "$80,000 -
$100,000 a year"), and the pay period shows up in a dozen different ways. To filter
by a salary *range* we need one comparable figure, so everything is normalized to
an annual amount (hourly × 2080, weekly × 52, …). Anything we can't read stays
None rather than being guessed at — the feed treats null bounds as "pay not listed".

`details._salary` still owns the human-readable display string; this owns the numbers.
"""

import re

# 40 hrs/wk × 52 wk — the same figure job boards use to annualize an hourly rate.
HOURS_PER_YEAR = 2080

_PERIOD_MULTIPLIERS = {
    "hour": HOURS_PER_YEAR, "hourly": HOURS_PER_YEAR, "hr": HOURS_PER_YEAR,
    "day": 260, "daily": 260,          # 5 days × 52 weeks
    "week": 52, "weekly": 52,
    "biweekly": 26, "fortnight": 26, "fortnightly": 26,
    "month": 12, "monthly": 12,
    "year": 1, "yearly": 1, "annual": 1, "annually": 1, "annum": 1,
}

# Rates below this almost certainly arrived without a period and are hourly, not a
# $500/yr job — used only when no period is stated anywhere.
_LIKELY_HOURLY_CEILING = 2000

_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _to_number(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        match = _NUMBER.search(value)
        if match:
            try:
                return float(match.group().replace(",", ""))
            except ValueError:
                return None
    return None


def _multiplier(period):
    if not isinstance(period, str):
        return None
    return _PERIOD_MULTIPLIERS.get(period.strip().lower())


def _period_from_text(text):
    lowered = text.lower()
    if "hour" in lowered or "/hr" in lowered or " hr" in lowered or "per hr" in lowered:
        return "hour"
    if "year" in lowered or "annum" in lowered or "annual" in lowered or "/yr" in lowered:
        return "year"
    if "month" in lowered:
        return "month"
    if "week" in lowered:
        return "week"
    if "day" in lowered:
        return "day"
    return None


def _numbers_from_text(text):
    numbers = []
    for token in _NUMBER.findall(text):
        try:
            numbers.append(float(token.replace(",", "")))
        except ValueError:
            continue
    return numbers


def parse_salary(payload):
    """
    payload -> {"salary_min_annual": int|None, "salary_max_annual": int|None}

    Annualizes whatever the payload's `salary` block exposes. A single figure
    becomes both bounds; nothing usable leaves both None.
    """
    empty = {"salary_min_annual": None, "salary_max_annual": None}
    salary = (payload or {}).get("salary")
    if not isinstance(salary, dict):
        return empty

    low = _to_number(salary.get("salaryMin"))
    high = _to_number(salary.get("salaryMax"))

    text = ""
    for key in ("salaryText", "text"):
        value = salary.get(key)
        if isinstance(value, str) and value.strip():
            text = value
            break

    # Period: an explicit field wins; otherwise sniff the display text.
    period = None
    for key in ("salaryType", "period", "unit", "payPeriod"):
        if _multiplier(salary.get(key)) is not None:
            period = salary.get(key)
            break
    if period is None and text:
        period = _period_from_text(text)

    # No numeric bounds but there is text — pull the numbers out of it.
    if low is None and high is None and text:
        numbers = _numbers_from_text(text)
        if numbers:
            low, high = min(numbers), max(numbers)

    if low is None and high is None:
        return empty

    multiplier = _multiplier(period)
    if multiplier is None:
        sample = high if high is not None else low
        multiplier = HOURS_PER_YEAR if sample is not None and sample < _LIKELY_HOURLY_CEILING else 1

    def annualize(value):
        return int(round(value * multiplier)) if value is not None else None

    min_annual = annualize(low if low is not None else high)
    max_annual = annualize(high if high is not None else low)
    # A malformed payload can hand back min > max — normalize the ordering.
    if min_annual is not None and max_annual is not None and min_annual > max_annual:
        min_annual, max_annual = max_annual, min_annual
    return {"salary_min_annual": min_annual, "salary_max_annual": max_annual}
