"""
forecast.py

This module provides two separate responsibilities:

1. Forecast normalization for PumpSteer utility/control support
2. Thermal outlook analysis for diagnostics and observability

CURRENT 2.2.x BEHAVIOR:
ThermalOutlook.preheat_worthwhile and preheat_strength are used by the
main control loop to gate and scale preheat boost. Other outlook fields
remain primarily diagnostic. The simpler _forecast_is_cold() path is
used only when ThermalOutlook cannot be built.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Iterable, Mapping
from itertools import islice
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


@dataclass
class ForecastPoint:
    """Normalized future data for one planning step."""

    timestamp: datetime
    price: Optional[float]
    outdoor_temp: Optional[float]
    wind_speed: Optional[float]
    wind_gust_speed: Optional[float]
    cloud_coverage: Optional[float] = None
    precipitation: Optional[float] = None
    humidity: Optional[float] = None
    source_price: str = "unknown"
    source_weather: str = "unknown"


# Snapshot bounds are independent of the legacy control-facing hourly normalizer.
_THERMAL_MAX_ROWS = 512
_THERMAL_RECEIPT_TTL = timedelta(minutes=90)
_THERMAL_WEATHER_TTL = timedelta(hours=6)
_THERMAL_PRICE_TTL = timedelta(hours=36)
_THERMAL_MAX_SLOT = timedelta(hours=1)


def _thermal_datetime(value: Any) -> datetime | None:
    """Parse genuine aware source timestamps, without assuming a local timezone."""
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def _thermal_number(value: Any) -> float | None:
    try:
        number = _as_float(value)
    except OverflowError:
        return None
    return (
        number
        if not isinstance(value, bool) and number is not None and math.isfinite(number)
        else None
    )


@dataclass(frozen=True)
class WeatherSlot:
    """An actual forecast temperature valid for at most one hour."""

    start: datetime
    end: datetime
    temperature_c: float


@dataclass(frozen=True)
class PriceSlot:
    """A price interval retaining the source's original resolution."""

    start: datetime
    end: datetime
    price: float


@dataclass(frozen=True)
class ThermalForecast:
    """Immutable as-of inputs; absence and unknown issue times stay explicit.

    Freshness is assessed at the requested start, so a fresh snapshot may describe
    a longer future horizon. Receipt is never proof of weather issue time or of
    the time Home Assistant last reported a price.
    """

    received_at: datetime
    weather_source: str = ""
    price_source: str = ""
    weather_issued_at: datetime | None = None
    weather: tuple[WeatherSlot, ...] = ()
    prices: tuple[PriceSlot, ...] = ()
    price_reported_at: datetime | None = None
    price_unit: str | None = None

    def __post_init__(self) -> None:
        received = _thermal_datetime(self.received_at)
        if received is None:
            raise ValueError("received_at must be an aware datetime")
        object.__setattr__(self, "received_at", received)
        object.__setattr__(self, "weather", tuple(self.weather))
        object.__setattr__(self, "prices", tuple(self.prices))

    def _window(
        self, start: datetime, end: datetime
    ) -> tuple[datetime, datetime] | None:
        start_utc, end_utc = _thermal_datetime(start), _thermal_datetime(end)
        if start_utc is None or end_utc is None or end_utc <= start_utc:
            return None
        if not timedelta(0) <= start_utc - self.received_at <= _THERMAL_RECEIPT_TTL:
            return None
        return start_utc, end_utc

    def weather_path(
        self, start: datetime, end: datetime, current_outdoor: float
    ) -> tuple[tuple[datetime, float], ...] | None:
        """Return contiguous step ends/temperatures, or no usable forecast.

        The current observation can bridge only to the first future forecast
        point, for at most one hour. No weather value fills a forecast gap.
        """
        window = self._window(start, end)
        if window is None or not self.weather:
            return None
        start, end = window
        if self.weather_issued_at is not None:
            if not timedelta(0) <= self.received_at - self.weather_issued_at:
                return None
            if (
                not timedelta(0)
                <= start - self.weather_issued_at
                <= _THERMAL_WEATHER_TTL
            ):
                return None
        cursor = start
        path: list[tuple[datetime, float]] = []
        first = self.weather[0]
        if cursor < first.start:
            outdoor = _thermal_number(current_outdoor)
            if outdoor is None or first.start - cursor > _THERMAL_MAX_SLOT:
                return None
            cursor = min(first.start, end)
            path.append((cursor, outdoor))
        for slot in self.weather:
            if cursor >= end:
                break
            if slot.end <= cursor:
                continue
            if slot.start > cursor:
                return None
            cursor = min(slot.end, end)
            path.append((cursor, slot.temperature_c))
        return tuple(path) if cursor == end else None

    def prices_between(
        self, start: datetime, end: datetime
    ) -> tuple[PriceSlot, ...] | None:
        """Return clipped real price intervals only with full fresh coverage."""
        window = self._window(start, end)
        if window is None or not self.prices or self.price_reported_at is None:
            return None
        start, end = window
        if not timedelta(0) <= self.received_at - self.price_reported_at:
            return None
        if not timedelta(0) <= start - self.price_reported_at <= _THERMAL_PRICE_TTL:
            return None
        cursor = start
        result: list[PriceSlot] = []
        for slot in self.prices:
            if cursor >= end:
                break
            if slot.end <= cursor:
                continue
            if slot.start > cursor:
                return None
            stop = min(slot.end, end)
            result.append(PriceSlot(cursor, stop, slot.price))
            cursor = stop
        return tuple(result) if cursor == end else None


def _thermal_weather(
    points: Iterable[ForecastPoint], unit: str
) -> tuple[WeatherSlot, ...]:
    rows = list(islice(points, _THERMAL_MAX_ROWS + 1))
    if len(rows) > _THERMAL_MAX_ROWS or unit not in (
        "°C",
        "C",
        "celsius",
        "°F",
        "F",
        "fahrenheit",
    ):
        return ()
    temperatures: dict[datetime, float] = {}
    for point in rows:
        # Price-only ForecastPoints have no weather observation.
        if point.outdoor_temp is None:
            continue
        at = _thermal_datetime(point.timestamp)
        temperature = _thermal_number(point.outdoor_temp)
        if at is None or temperature is None or at in temperatures:
            return ()
        if unit in ("°F", "F", "fahrenheit"):
            temperature = (temperature - 32.0) / 9.0 * 5.0
        temperatures[at] = temperature
    starts = sorted(temperatures)
    slots: list[WeatherSlot] = []
    for i, at in enumerate(starts):
        try:
            end = at + _THERMAL_MAX_SLOT
        except OverflowError:
            return ()
        if i + 1 < len(starts):
            end = min(end, starts[i + 1])
        slots.append(WeatherSlot(at, end, temperatures[at]))
    return tuple(slots)


def _thermal_prices(
    *states: Any, received_at: datetime
) -> tuple[tuple[PriceSlot, ...], datetime | None]:
    rows: list[dict[str, Any]] = []
    reported_times: list[datetime] = []
    for state in states:
        if state is None:
            continue
        if getattr(state, "state", None) in ("unknown", "unavailable"):
            return (), None
        attrs = getattr(state, "attributes", None)
        if not isinstance(attrs, Mapping):
            return (), None
        state_rows: list[dict[str, Any]] = []
        for name in ("raw_today", "raw_tomorrow", "prices", "forecast"):
            source = attrs.get(name)
            if source is None:
                continue
            if (
                not isinstance(source, (list, tuple))
                or len(source) + len(rows) + len(state_rows) > _THERMAL_MAX_ROWS
            ):
                return (), None
            if any(not isinstance(row, dict) for row in source):
                return (), None
            state_rows.extend(source)
        if not state_rows:
            continue
        # Both fields are genuine HA state provenance; receipt is never a fallback.
        report_value = getattr(state, "last_reported", None)
        if report_value is None:
            report_value = getattr(state, "last_updated", None)
        reported = _thermal_datetime(report_value)
        if reported is None or reported > received_at:
            return (), None
        reported_times.append(reported)
        rows.extend(state_rows)
    parsed: dict[datetime, tuple[datetime | None, float]] = {}
    for row in rows:
        start = next(
            (
                _thermal_datetime(row[key])
                for key in ("start", "startsAt", "datetime", "time", "hour")
                if key in row
            ),
            None,
        )
        end = next(
            (
                _thermal_datetime(row[key])
                for key in ("end", "endsAt", "end_time")
                if key in row
            ),
            None,
        )
        try:
            price = _thermal_number(_extract_price_value(row))
            raw_value = next(
                (
                    row[key]
                    for key in ("value", "price", "total", "spot", "cost")
                    if key in row and _as_float(row[key]) is not None
                ),
                None,
            )
        except OverflowError:
            return (), None
        if isinstance(raw_value, bool):
            price = None
        explicit_end = any(key in row for key in ("end", "endsAt", "end_time"))
        if (
            start is None
            or price is None
            or start in parsed
            or (explicit_end and end is None)
        ):
            return (), None
        parsed[start] = (end, price)
    starts = sorted(parsed)
    result: list[PriceSlot] = []
    for i, start in enumerate(starts):
        end, price = parsed[start]
        next_start = starts[i + 1] if i + 1 < len(starts) else None
        if end is None:
            # Only a following real timestamp establishes an otherwise absent end.
            if next_start is None:
                continue
            end = next_start
        if not timedelta(0) < end - start <= _THERMAL_MAX_SLOT:
            return (), None
        if next_start is not None and end > next_start:
            return (), None
        result.append(PriceSlot(start, end, price))
    return tuple(result), min(reported_times) if reported_times else None


def make_thermal_forecast(
    points: Iterable[ForecastPoint],
    *,
    received_at: datetime,
    weather_source: str = "",
    price_source: str = "",
    price_state: Any = None,
    tomorrow_state: Any = None,
    weather_issued_at: datetime | None = None,
    weather_unit: str = "°C",
) -> ThermalForecast:
    """Copy bounded source inputs for thermal diagnostics without HA imports.

    Invalid channels become unavailable, independently. Naive timestamps never
    receive an invented timezone. Numeric-only price arrays have no real slot
    times and are deliberately unavailable; a final row without an explicit end
    remains uncovered. No receipt timestamp is substituted for source provenance.
    """
    received = _thermal_datetime(received_at)
    if received is None:
        raise ValueError("received_at must be an aware datetime")
    issued = _thermal_datetime(weather_issued_at)
    weather = _thermal_weather(points, weather_unit)
    if weather_issued_at is not None and (
        issued is None or not timedelta(0) <= received - issued <= _THERMAL_WEATHER_TTL
    ):
        weather = ()
    prices, reported = _thermal_prices(
        price_state, tomorrow_state, received_at=received
    )
    if (
        reported is not None
        and not timedelta(0) <= received - reported <= _THERMAL_PRICE_TTL
    ):
        prices = ()
    units={getattr(state,'attributes',{}).get('unit_of_measurement')
           for state in (price_state,tomorrow_state) if state is not None}
    unit=next(iter(units)) if len(units)==1 and isinstance(next(iter(units)),str) else None
    if len(units)>1:
        prices=()
    return ThermalForecast(
        received, weather_source, price_source, issued, weather, prices, reported,unit
    )


@dataclass
class ThermalOutlook:
    """
    Summarized thermal analysis based on upcoming weather and prices.

    This structure provides diagnostic insight into upcoming thermal conditions,
    such as cold periods, warming trends, and precool risk.

    In PumpSteer 2.2.x, preheat_worthwhile and preheat_strength are used
    in block 5b of sensor.py to gate and scale the preheat boost.
    Other fields remain primarily diagnostic.

    The simpler _forecast_is_cold() path is retained only as a fallback
    when ThermalOutlook is unavailable.
    """

    # Raw values
    night_min_temp: Optional[float]  # Lowest temperature in 22:00–06:00 window
    day_max_temp: Optional[float]  # Highest temperature in 06:00–22:00 window
    hours_below_threshold: int  # Hours below summer_threshold in analysis window

    effective_temp_now: Optional[
        float
    ]  # Wind-chill-adjusted temperature for nearest point

    # Trends
    warming_trend: bool  # Temperature rising in the near horizon
    cooling_trend: bool  # Temperature falling in the near horizon

    # Risk signal
    precool_risk: bool  # Warm period ahead that may reduce preheat value

    # Diagnostic summary outputs
    preheat_worthwhile: bool  # Gates preheat boost in block 5b
    preheat_strength: float  # Scales preheat boost magnitude (0.0–1.0)


def _as_float(value: Any) -> Optional[float]:
    """Convert a value to float when possible."""
    try:
        if value in (None, "", "unknown", "unavailable", "None"):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_datetime(value: Any) -> Optional[datetime]:
    """Parse datetime and normalize to aware UTC datetime."""
    import homeassistant.util.dt as dt_util

    if not value:
        return None

    if isinstance(value, datetime):
        dt_value = value
    else:
        dt_value = dt_util.parse_datetime(str(value))

    if dt_value is None:
        return None

    if dt_value.tzinfo is None:
        dt_value = dt_util.as_utc(dt_value.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE))
    else:
        dt_value = dt_util.as_utc(dt_value)

    return dt_value


def _round_to_hour(dt_value: datetime) -> datetime:
    """Round down datetime to whole hour in UTC."""
    return dt_value.replace(minute=0, second=0, microsecond=0)


def _wind_chill(temp_c: float, wind_ms: float) -> float:
    """
    Calculate a simplified wind chill estimate.

    Valid below approximately 10°C and for wind speeds above 1.3 m/s.
    Outside these bounds, the raw outdoor temperature is returned unchanged.
    """
    if temp_c >= 10.0 or wind_ms < 1.3:
        return temp_c
    v016 = wind_ms**0.16
    return 13.12 + 0.6215 * temp_c - 11.37 * v016 + 0.3965 * temp_c * v016


async def _async_extract_weather_points(
    hass: HomeAssistant,
    weather_entity_id: str,
    horizon_hours: int,
) -> dict[datetime, ForecastPoint]:
    """Read hourly weather forecast via weather.get_forecasts service call."""

    try:
        response = await hass.services.async_call(
            "weather",
            "get_forecasts",
            {"type": "hourly"},
            target={"entity_id": weather_entity_id},
            blocking=True,
            return_response=True,
        )
    except Exception as err:
        _LOGGER.debug("weather.get_forecasts failed for %s: %s", weather_entity_id, err)
        return {}

    if not isinstance(response, dict):
        _LOGGER.debug(
            "weather.get_forecasts returned unexpected type for %s: %s",
            weather_entity_id,
            type(response),
        )
        return {}

    entity_data = response.get(weather_entity_id, {})
    forecast = entity_data.get("forecast") if isinstance(entity_data, dict) else None

    if not isinstance(forecast, list):
        _LOGGER.debug(
            "No forecast list in weather.get_forecasts response for %s",
            weather_entity_id,
        )
        return {}

    import homeassistant.util.dt as dt_util

    now_utc = dt_util.utcnow()
    current_hour = now_utc.replace(minute=0, second=0, microsecond=0)
    end_utc = now_utc + timedelta(hours=horizon_hours)
    result: dict[datetime, ForecastPoint] = {}

    for entry in forecast:
        if not isinstance(entry, dict):
            continue

        ts = _parse_datetime(entry.get("datetime"))
        if ts is None:
            continue

        ts = _round_to_hour(ts)

        if ts < current_hour:
            continue
        if ts > end_utc:
            continue

        result[ts] = ForecastPoint(
            timestamp=ts,
            price=None,
            outdoor_temp=_as_float(entry.get("temperature")),
            wind_speed=_as_float(entry.get("wind_speed")),
            wind_gust_speed=_as_float(entry.get("wind_gust_speed")),
            cloud_coverage=_as_float(entry.get("cloud_coverage")),
            precipitation=_as_float(entry.get("precipitation")),
            humidity=_as_float(entry.get("humidity")),
            source_weather=weather_entity_id,
        )

    return result


def _extract_price_value(entry: dict[str, Any]) -> Optional[float]:
    """Extract numeric price from a raw price entry."""
    for key in ("value", "price", "total", "spot", "cost"):
        if key in entry:
            value = _as_float(entry.get(key))
            if value is not None:
                return value
    return None


def _extract_price_timestamp(entry: dict[str, Any]) -> Optional[datetime]:
    """Extract timestamp from a raw price entry."""
    for key in ("start", "startsAt", "datetime", "time", "hour"):
        if key in entry:
            ts = _parse_datetime(entry.get(key))
            if ts is not None:
                return _round_to_hour(ts)
    return None


def _extract_price_points(
    hass: HomeAssistant,
    price_entity_id: str,
    horizon_hours: int,
) -> dict[datetime, float]:
    """
    Read future hourly prices from a Home Assistant price entity.

    Expected sources include:
    - raw_today / raw_tomorrow style lists
    - forecast-like price lists in entity attributes
    """
    state = hass.states.get(price_entity_id)
    if state is None:
        _LOGGER.debug("Price entity not found: %s", price_entity_id)
        return {}

    import homeassistant.util.dt as dt_util

    now_utc = dt_util.utcnow()
    current_hour = now_utc.replace(minute=0, second=0, microsecond=0)
    end_utc = now_utc + timedelta(hours=horizon_hours)

    candidates: list[dict[str, Any]] = []

    for attr_name in ("raw_today", "raw_tomorrow", "prices", "forecast"):
        attr = state.attributes.get(attr_name)
        if isinstance(attr, list):
            candidates.extend(item for item in attr if isinstance(item, dict))

    result: dict[datetime, float] = {}

    for entry in candidates:
        ts = _extract_price_timestamp(entry)
        if ts is None:
            continue

        if ts < current_hour:
            continue
        if ts > end_utc:
            continue

        value = _extract_price_value(entry)
        if value is None:
            continue

        result[ts] = value

    return result


async def async_build_forecast(
    hass: HomeAssistant,
    *,
    price_entity_id: str,
    weather_entity_id: str,
    horizon_hours: int = 24,
) -> list[ForecastPoint]:
    """
    Build a normalized hourly forecast for PumpSteer utility and diagnostics.

    This function merges:
    - future weather forecast from a weather entity
    - future electricity prices from a price entity

    This supports both:
    - control-facing ThermalOutlook analysis for bounded preheat
    - diagnostic forecast observability
    """
    weather_points = await _async_extract_weather_points(
        hass, weather_entity_id, horizon_hours
    )
    price_points = _extract_price_points(hass, price_entity_id, horizon_hours)

    all_hours = sorted(set(weather_points.keys()) | set(price_points.keys()))
    merged: list[ForecastPoint] = []

    for ts in all_hours:
        weather_point = weather_points.get(ts)

        if weather_point is not None:
            point = ForecastPoint(
                timestamp=ts,
                price=price_points.get(ts),
                outdoor_temp=weather_point.outdoor_temp,
                wind_speed=weather_point.wind_speed,
                wind_gust_speed=weather_point.wind_gust_speed,
                cloud_coverage=weather_point.cloud_coverage,
                precipitation=weather_point.precipitation,
                humidity=weather_point.humidity,
                source_price=price_entity_id if ts in price_points else "missing",
                source_weather=weather_entity_id,
            )
        else:
            point = ForecastPoint(
                timestamp=ts,
                price=price_points.get(ts),
                outdoor_temp=None,
                wind_speed=None,
                wind_gust_speed=None,
                cloud_coverage=None,
                precipitation=None,
                humidity=None,
                source_price=price_entity_id if ts in price_points else "missing",
                source_weather="missing",
            )

        merged.append(point)

    return merged


def analyze_thermal_outlook(
    points: list[ForecastPoint],
    summer_threshold: float,
    now_hour: int = 0,  # accepted for API compatibility, not used in logic
    precool_margin: float = 3.0,
    trend_hours: int = 6,
) -> ThermalOutlook:
    """
    Analyze a ForecastPoint list and return a ThermalOutlook.

    This function produces a richer thermal interpretation of the forecast,
    including trends, cold duration, effective temperature, and precool risk.

    In PumpSteer 2.1.x, preheat_worthwhile and preheat_strength from this
    function are used in block 5b to gate and scale the preheat boost.
    All other outputs remain diagnostic only.

    The main control loop still relies on simpler forecast signals.
    """
    if not points:
        return ThermalOutlook(
            night_min_temp=None,
            day_max_temp=None,
            hours_below_threshold=0,
            effective_temp_now=None,
            warming_trend=False,
            cooling_trend=False,
            precool_risk=False,
            preheat_worthwhile=False,
            preheat_strength=0.0,
        )

    # Split into night (22–06) and day (06–22) based on forecast timestamp hour.
    night_temps: list[float] = []
    day_temps: list[float] = []
    for point in points:
        if point.outdoor_temp is None:
            continue
        hour = point.timestamp.hour
        if hour >= 22 or hour < 6:
            night_temps.append(point.outdoor_temp)
        else:
            day_temps.append(point.outdoor_temp)

    night_min = min(night_temps) if night_temps else None
    day_max = max(day_temps) if day_temps else None

    # Count hours below the threshold in the provided analysis window.
    hours_cold = sum(
        1
        for point in points
        if point.outdoor_temp is not None and point.outdoor_temp < summer_threshold
    )

    # Wind-chill-adjusted temperature for the nearest forecast point.
    eff_temp: Optional[float] = None
    if points[0].outdoor_temp is not None:
        wind = points[0].wind_speed or 0.0
        eff_temp = _wind_chill(points[0].outdoor_temp, wind)

    # Trend analysis based on the first trend_hours forecast points.
    trend_temps = [
        point.outdoor_temp
        for point in points[:trend_hours]
        if point.outdoor_temp is not None
    ]
    warming = False
    cooling = False
    if len(trend_temps) >= 4:
        mid = len(trend_temps) // 2
        early_avg = sum(trend_temps[:mid]) / mid
        late_avg = sum(trend_temps[mid:]) / (len(trend_temps) - mid)
        warming = late_avg > early_avg + 1.0
        cooling = late_avg < early_avg - 1.0

    # Warm daytime period ahead may reduce the value of preheating.
    precool_risk = day_max is not None and day_max >= summer_threshold + precool_margin

    # Diagnostic estimate only in 2.1.0.
    preheat_worthwhile = (
        hours_cold >= 3
        and not warming
        and not precool_risk
        and (day_max is None or day_max < summer_threshold - 2.0)
    )

    # Diagnostic estimate only in 2.1.0.
    preheat_strength = 0.0
    if preheat_worthwhile and night_min is not None:
        delta = summer_threshold - night_min
        preheat_strength = min(1.0, max(0.0, delta / 15.0))

    # NOTE:
    # preheat_worthwhile and preheat_strength feed into block 5b in sensor.py.
    # All other ThermalOutlook fields are diagnostic only.
    # Pre-brake (block 5a) is never forecast-gated.
    return ThermalOutlook(
        night_min_temp=night_min,
        day_max_temp=day_max,
        hours_below_threshold=hours_cold,
        effective_temp_now=eff_temp,
        warming_trend=warming,
        cooling_trend=cooling,
        precool_risk=precool_risk,
        preheat_worthwhile=preheat_worthwhile,
        preheat_strength=preheat_strength,
    )
