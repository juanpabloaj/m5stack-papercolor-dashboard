from __future__ import annotations

import argparse
import json
import logging
import os
import textwrap
import time as time_module
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, time
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont

WIDTH = 400
HEIGHT = 600
OUT = Path("papercolor_dashboard_test.png")
FORECAST_DAYS = 3

# E Ink Spectra 6 palette for the PaperColor panel (ED2208). The device
# quantizes every uploaded image to 6 inks and diffuses the error (the grainy
# "dirty color" look); a pixel already equal to an ink anchor has zero error, so
# flat fills stay solid. Black/white/yellow/red/green are the M5GFX
# Panel_ED2208.cpp epd_palette[] values. BLUE is the exception: the M5GFX value
# (100,64,255, a violet-blue) visibly dithered on the real EzData render path, so
# its quantizer's blue anchor differs from M5GFX's. An on-device swatch sweep
# showed pure blue (0,0,255) renders cleanest, so we use that. No gray/cream.
EPD_BLACK = "#000000"   # 0, 0, 0
EPD_WHITE = "#ffffff"   # 255, 255, 255
EPD_YELLOW = "#fff338"  # 255, 243, 56
EPD_RED = "#bf0000"     # 191, 0, 0
EPD_BLUE = "#0000ff"    # 0, 0, 255 — tuned on-device (M5GFX 100,64,255 dithered)
EPD_GREEN = "#438a1c"   # 67, 138, 28

logger = logging.getLogger("papercolor_dashboard")


class MissingConfig(Exception):
    """Raised when a required environment variable is absent."""


@dataclass(frozen=True)
class DayWeather:
    day: date
    condition: str
    low: int
    high: int
    rain_prob: int | None
    rain_hour: str | None


@dataclass(frozen=True)
class Weather:
    location: str
    current_c: int
    condition: str
    days: tuple[DayWeather, ...]
    next_solar_time: str | None
    next_solar_label: str | None


@dataclass(frozen=True)
class DayAir:
    pm25_min: int | None
    pm25_max: int | None
    pm25_peak_hour: str | None


@dataclass(frozen=True)
class AirQuality:
    aqi: int | None
    label: str
    pm25: float | None
    days: dict[date, DayAir]


@dataclass(frozen=True)
class TodoistTask:
    content: str
    due_label: str
    sort_at: datetime
    overdue: bool
    priority: int


def load_dotenv(path: Path = Path(".env")) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise MissingConfig(f"Missing required environment variable: {name}")
    return value


def http_retries() -> int:
    return int(os.environ.get("HTTP_RETRIES", "3"))


def http_retry_delay_seconds() -> float:
    return float(os.environ.get("HTTP_RETRY_DELAY_SECONDS", "5"))


def http_timeout_seconds(default: int = 20) -> int:
    return int(os.environ.get("HTTP_TIMEOUT_SECONDS", str(default)))


def display_safe(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    return " ".join(ascii_text.split())


def weather_label(code: int) -> str:
    labels = {
        0: "Clear",
        1: "Mostly clear",
        2: "Partly cloudy",
        3: "Cloudy",
        45: "Fog",
        48: "Rime fog",
        51: "Drizzle",
        53: "Drizzle",
        55: "Drizzle",
        61: "Rain",
        63: "Rain",
        65: "Heavy rain",
        71: "Snow",
        73: "Snow",
        75: "Heavy snow",
        80: "Showers",
        81: "Showers",
        82: "Heavy showers",
        95: "Storm",
        96: "Storm",
        99: "Heavy storm",
    }
    return labels.get(code, "Variable")


def next_solar_event(
    daily: dict, now: datetime
) -> tuple[str | None, str | None]:
    events: list[tuple[datetime, str]] = []
    for raw_time in daily.get("sunrise") or []:
        parsed = datetime.fromisoformat(raw_time)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=now.tzinfo)
        events.append((parsed, "sunrise"))
    for raw_time in daily.get("sunset") or []:
        parsed = datetime.fromisoformat(raw_time)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=now.tzinfo)
        events.append((parsed, "sunset"))

    future_events = sorted(event for event in events if event[0] > now)
    if not future_events:
        return None, None
    event_time, label = future_events[0]
    return event_time.strftime("%H:%M"), label


def _bucket_hourly_by_day(
    hourly: dict, value_key: str, now: datetime, num_days: int = FORECAST_DAYS
) -> dict[date, list[tuple[datetime, float]]]:
    """Group Open-Meteo hourly samples into per-day buckets (today onward).

    Drops missing values and any of today's samples that are already in the
    past, normalizing every timestamp to ``now``'s timezone.
    """
    times = hourly.get("time") or []
    values = hourly.get(value_key) or []
    today = now.date()
    buckets: dict[date, list[tuple[datetime, float]]] = {
        date.fromordinal(today.toordinal() + offset): []
        for offset in range(num_days)
    }

    for raw_time, raw_value in zip(times, values):
        if raw_value is None:
            continue

        parsed = datetime.fromisoformat(raw_time)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=now.tzinfo)
        parsed = parsed.astimezone(now.tzinfo)
        if parsed.date() == today and parsed < now:
            continue
        if parsed.date() not in buckets:
            continue

        buckets[parsed.date()].append((parsed, raw_value))

    return buckets


def rain_probability_summary(
    hourly: dict, now: datetime
) -> dict[date, tuple[int, str | None]]:
    if not (hourly.get("time") and hourly.get("precipitation_probability")):
        return {}

    buckets = _bucket_hourly_by_day(hourly, "precipitation_probability", now)

    summary: dict[date, tuple[int, str | None]] = {}
    for day, raw_points in buckets.items():
        day_points = [(dt, round(value)) for dt, value in raw_points]
        if not day_points:
            summary[day] = (0, None)
            continue

        max_probability = max(value for _, value in day_points)
        first_rain = next(
            (dt for dt, value in day_points if value > 0),
            None,
        )
        summary[day] = (
            max_probability,
            first_rain.strftime("%Hh") if first_rain else None,
        )

    return summary


def daily_rain_probability(daily: dict, index: int) -> int | None:
    values = daily.get("precipitation_probability_max")
    if not values or len(values) <= index or values[index] is None:
        return None
    return round(values[index])


def fetch_weather(now: datetime | None = None) -> Weather:
    lat = required_env("WEATHER_LAT")
    lon = required_env("WEATHER_LON")
    location = required_env("WEATHER_LOCATION")
    timezone = required_env("WEATHER_TIMEZONE")
    now = now or datetime.now(ZoneInfo(timezone))

    params = {
        "latitude": lat,
        "longitude": lon,
        "current": ",".join(
            [
                "temperature_2m",
                "weather_code",
            ]
        ),
        "daily": ",".join(
            [
                "weather_code",
                "temperature_2m_max",
                "temperature_2m_min",
                "precipitation_probability_max",
                "sunrise",
                "sunset",
            ]
        ),
        "hourly": "precipitation_probability",
        "timezone": timezone,
        "forecast_days": str(FORECAST_DAYS),
    }
    data = request_json(
        "https://api.open-meteo.com/v1/forecast", params=params
    )

    current = data["current"]
    daily = data["daily"]
    rain_summary = rain_probability_summary(data.get("hourly", {}), now)
    daily_dates = daily.get("time") or []
    days: list[DayWeather] = []
    for index in range(min(FORECAST_DAYS, len(daily_dates))):
        day = date.fromisoformat(daily_dates[index])
        rain_prob, rain_hour = rain_summary.get(
            day, (daily_rain_probability(daily, index), None)
        )
        days.append(
            DayWeather(
                day=day,
                condition=weather_label(int(daily["weather_code"][index])),
                low=round(daily["temperature_2m_min"][index]),
                high=round(daily["temperature_2m_max"][index]),
                rain_prob=rain_prob,
                rain_hour=rain_hour,
            )
        )
    next_solar_time, next_solar_label = next_solar_event(daily, now)
    return Weather(
        location=location,
        current_c=round(current["temperature_2m"]),
        condition=weather_label(int(current["weather_code"])),
        days=tuple(days),
        next_solar_time=next_solar_time,
        next_solar_label=next_solar_label,
    )


def urlopen_with_retries(
    req: urllib.request.Request, *, timeout: int
) -> bytes:
    retryable_statuses = {429, 500, 502, 503, 504}
    attempts = max(1, http_retries())
    delay = max(0.0, http_retry_delay_seconds())
    last_error: Exception | None = None

    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            last_error = exc
            retryable = exc.code in retryable_statuses
            if not retryable or attempt == attempts:
                raise
        except (urllib.error.URLError, TimeoutError) as exc:
            last_error = exc
            if attempt == attempts:
                raise

        logger.warning(
            "HTTP retry %d/%d for %s: %s",
            attempt,
            attempts,
            req.full_url,
            last_error,
        )
        if delay > 0:
            time_module.sleep(delay)

    raise RuntimeError(
        f"HTTP request failed after {attempts} attempts: {last_error}"
    )


def request_json(
    url: str,
    *,
    token: str | None = None,
    method: str = "GET",
    params: dict[str, str] | None = None,
    form: dict[str, str] | None = None,
) -> dict:
    if params:
        url += "?" + urllib.parse.urlencode(params)

    headers = {"User-Agent": "papercolor-dashboard/0.1"}
    data = None
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if form is not None:
        data = urllib.parse.urlencode(form).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"

    req = urllib.request.Request(
        url, data=data, headers=headers, method=method
    )
    return json.loads(
        urlopen_with_retries(req, timeout=http_timeout_seconds()).decode(
            "utf-8"
        )
    )


def pm25_label(pm25: float | None) -> str:
    if pm25 is None:
        return "--"
    if pm25 <= 12:
        return "Good"
    if pm25 <= 35.4:
        return "Moderate"
    if pm25 <= 55.4:
        return "U. sensitive groups"
    if pm25 <= 150.4:
        return "Unhealthy"
    if pm25 <= 250.4:
        return "Very unhealthy"
    return "Hazardous"


def pm25_style(pm25: float | None) -> tuple[str, str, str]:
    if pm25 is None or pm25 <= 12:
        return EPD_WHITE, EPD_BLUE, EPD_BLACK
    if pm25 <= 35.4:
        return EPD_WHITE, EPD_BLACK, EPD_BLACK
    if pm25 <= 55.4:
        return EPD_YELLOW, EPD_BLACK, EPD_BLACK
    return EPD_RED, EPD_WHITE, EPD_WHITE


RAIN_CHIP_THRESHOLD = 50


def temp_chip_style(temp_c: int | None) -> tuple[str, str | None]:
    """Return (text_color, fill) for a temperature value, by band.

    A fill of None means the value sits plainly on the page (black on white),
    so mild days stay clean and only extremes light up. The four bands map to
    the panel's native inks, so fills stay solid instead of grainy:
    blue <10, none 10-19, yellow 20-29, red >=30.
    """
    if temp_c is None:
        return EPD_BLACK, None
    if temp_c < 10:
        return EPD_WHITE, EPD_BLUE
    if temp_c < 20:
        return EPD_BLACK, None
    if temp_c < 30:
        return EPD_BLACK, EPD_YELLOW
    return EPD_WHITE, EPD_RED


def temp_style(temp_c: int | None) -> tuple[str, str]:
    """(card_fill, text_color) for the big current-temperature card."""
    fg, bg = temp_chip_style(temp_c)
    return (bg if bg is not None else EPD_WHITE), fg


def pm_chip_style(value: float | None) -> tuple[str, str | None]:
    """Return (text_color, fill) for a PM2.5 value drawn as an inline chip.

    Mirrors pm25_style's health bands but only highlights the unhealthy end:
    no fill <=35.4, yellow <=55.4, red above.
    """
    if value is None or value <= 35.4:
        return EPD_BLACK, None
    if value <= 55.4:
        return EPD_BLACK, EPD_YELLOW
    return EPD_WHITE, EPD_RED


def fetch_air_quality(now: datetime | None = None) -> AirQuality:
    lat = required_env("WEATHER_LAT")
    lon = required_env("WEATHER_LON")
    timezone = required_env("WEATHER_TIMEZONE")
    now = now or datetime.now(ZoneInfo(timezone))
    data = request_json(
        "https://air-quality-api.open-meteo.com/v1/air-quality",
        params={
            "latitude": lat,
            "longitude": lon,
            "current": "us_aqi,pm2_5",
            "hourly": "pm2_5",
            "forecast_days": str(FORECAST_DAYS),
            "timezone": timezone,
        },
    )
    current = data.get("current", {})
    aqi = current.get("us_aqi")
    rounded_aqi = round(aqi) if aqi is not None else None
    pm25 = current.get("pm2_5")
    hourly = data.get("hourly", {})
    days = {
        day: DayAir(
            pm25_min=summary["min"],
            pm25_max=summary["max"],
            pm25_peak_hour=summary["peak_hour"],
        )
        for day, summary in daily_pm25_summary(hourly, now).items()
    }
    return AirQuality(
        aqi=rounded_aqi,
        label=pm25_label(pm25),
        pm25=pm25,
        days=days,
    )


def summarize_pm25_points(
    points: list[tuple[datetime, float]],
) -> dict[str, int | str] | None:
    if not points:
        return None
    min_value = round(min(value for _, value in points))
    peak_time, max_value = max(points, key=lambda item: item[1])
    return {
        "min": min_value,
        "max": round(max_value),
        "peak_hour": peak_time.strftime("%H:%M"),
    }


def daily_pm25_summary(
    hourly: dict, now: datetime
) -> dict[date, dict[str, int | str]]:
    buckets = _bucket_hourly_by_day(hourly, "pm2_5", now)
    summary: dict[date, dict[str, int | str]] = {}
    for day, raw_points in buckets.items():
        day_points = [(dt, float(value)) for dt, value in raw_points]
        day_summary = summarize_pm25_points(day_points)
        if day_summary is not None:
            summary[day] = day_summary
    return summary


def parse_todoist_due(
    due: dict, now: datetime
) -> tuple[datetime, str, bool] | None:
    yesterday = date.fromordinal(now.date().toordinal() - 1)

    due_datetime = due.get("datetime")
    due_date = due.get("date")
    if due_datetime:
        parsed = datetime.fromisoformat(due_datetime.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=now.tzinfo)
        parsed = parsed.astimezone(now.tzinfo)
        label = "yesterday" if parsed.date() == yesterday else "overdue"
        if parsed.date() == now.date():
            label = parsed.strftime("%H:%M")
        return parsed, label, parsed < now

    if due_date:
        if "T" in due_date:
            parsed = datetime.fromisoformat(due_date)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=now.tzinfo)
            parsed = parsed.astimezone(now.tzinfo)
            label = "yesterday" if parsed.date() == yesterday else "overdue"
            if parsed.date() == now.date():
                label = parsed.strftime("%H:%M")
            return parsed, label, parsed < now

        parsed_date = date.fromisoformat(due_date)
        parsed = datetime.combine(parsed_date, time.min, tzinfo=now.tzinfo)
        if parsed_date < now.date():
            label = (
                "yesterday"
                if parsed_date == yesterday
                else parsed_date.strftime("%d/%m")
            )
            return parsed, label, True
        if parsed_date == now.date():
            return parsed, "today", False
        return parsed, parsed_date.strftime("%d/%m"), False

    return None


def fetch_todoist_tasks(now: datetime) -> list[TodoistTask]:
    token = required_env("TODOIST_API_TOKEN")
    query = os.environ.get("TODOIST_QUERY", "overdue | today")
    limit = os.environ.get("TODOIST_TASK_LIMIT", "50")
    data = request_json(
        "https://api.todoist.com/api/v1/tasks/filter",
        token=token,
        params={"query": query, "lang": "en", "limit": limit},
    )

    tasks: list[TodoistTask] = []
    for item in data.get("results", []):
        due = item.get("due") or {}
        parsed_due = parse_todoist_due(due, now)
        if parsed_due is None:
            continue
        sort_at, due_label, overdue = parsed_due
        if sort_at.date() > now.date():
            continue
        tasks.append(
            TodoistTask(
                content=display_safe(item.get("content", "").strip()),
                due_label=due_label,
                sort_at=sort_at,
                overdue=overdue,
                priority=int(item.get("priority", 1)),
            )
        )
    return sorted(tasks, key=lambda task: task.sort_at)


def upload_to_ezdata(image_path: Path) -> dict:
    token = required_env("TOKEN")
    boundary = "----papercolor-dashboard-boundary"
    chunks: list[bytes] = []

    def add_field(name: str, value: str) -> None:
        chunks.extend(
            [
                f"--{boundary}\r\n".encode("utf-8"),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(
                    "utf-8"
                ),
                value.encode("utf-8"),
                b"\r\n",
            ]
        )

    add_field("deviceToken", token)
    add_field("name", "image")
    chunks.extend(
        [
            f"--{boundary}\r\n".encode("utf-8"),
            (
                'Content-Disposition: form-data; name="file"; '
                f'filename="{image_path.name}"\r\n'
                "Content-Type: image/png\r\n\r\n"
            ).encode("utf-8"),
            image_path.read_bytes(),
            b"\r\n",
            f"--{boundary}--\r\n".encode("utf-8"),
        ]
    )

    req = urllib.request.Request(
        "https://ezdata2.m5stack.com/api/v2/device/uploadDeviceFile",
        data=b"".join(chunks),
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "User-Agent": "papercolor-dashboard/0.1",
        },
        method="POST",
    )
    return json.loads(
        urlopen_with_retries(
            req, timeout=http_timeout_seconds(default=30)
        ).decode("utf-8")
    )


@lru_cache(maxsize=None)
def font(size: int) -> ImageFont.ImageFont:
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def draw_text(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    text: str,
    size: int,
    color: str = EPD_BLACK,
    bold: bool = False,
) -> None:
    draw.text(xy, text, fill=color, font=font(size))
    if bold:
        draw.text((xy[0] + 1, xy[1]), text, fill=color, font=font(size))


def fitted_text_size(
    draw: ImageDraw.ImageDraw,
    text: str,
    *,
    max_width: int,
    preferred_size: int,
    min_size: int,
) -> int:
    for size in range(preferred_size, min_size - 1, -1):
        bbox = draw.textbbox((0, 0), text, font=font(size))
        if bbox[2] - bbox[0] <= max_width:
            return size
    return min_size


def draw_text_at_visible_top(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    text: str,
    size: int,
    color: str,
    bold: bool = False,
) -> None:
    bbox = draw.textbbox((0, 0), text, font=font(size))
    draw_text(draw, (xy[0] - bbox[0], xy[1] - bbox[1]), text, size, color, bold)


def centered_text_at_y(
    draw: ImageDraw.ImageDraw,
    x0: int,
    x1: int,
    y: int,
    text: str,
    size: int,
    color: str,
    bold: bool = False,
) -> None:
    f = font(size)
    bbox = draw.textbbox((0, 0), text, font=f)
    x = x0 + (x1 - x0 - (bbox[2] - bbox[0])) // 2
    draw_text(draw, (x, y), text, size, color, bold)


def draw_centered_stack(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    lines: list[tuple[str, int, str, bool]],
    gap: int = 4,
) -> None:
    measured = []
    total_height = 0
    for text, size, color, bold in lines:
        f = font(size)
        bbox = draw.textbbox((0, 0), text, font=f)
        width = bbox[2] - bbox[0]
        height = bbox[3] - bbox[1]
        measured.append((text, size, color, bold, bbox, width, height))
        total_height += height

    total_height += gap * max(0, len(measured) - 1)
    y = box[1] + (box[3] - box[1] - total_height) // 2
    for text, size, color, bold, bbox, width, height in measured:
        x = box[0] + (box[2] - box[0] - width) // 2
        draw_text(draw, (x - bbox[0], y - bbox[1]), text, size, color, bold)
        y += height + gap


def card(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    fill: str = EPD_WHITE,
    outline: str = EPD_BLACK,
) -> None:
    draw.rounded_rectangle(box, radius=8, fill=fill, outline=outline, width=2)


# --- Forecast rows ----------------------------------------------------------
# Each day is one full-width row laid out as a column grid. Numeric data is
# drawn as "chips": a token sitting on a rounded native-color fill (white/black
# text for contrast), so temperature/rain/air severity reads at a glance.
# A token is (text, text_color, fill); a fill of None means plain text.

FORECAST_SIZE = 16
CHIP_PADX = 4      # chip horizontal padding
CHIP_H = 21        # uniform chip height
CHIP_R = 7         # chip corner radius (rounded, not a full pill)
CHIP_GAP = 0       # gap between tokens within one field

WEEKDAY_ABBR = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]

SHORT_COND = {
    "Mostly clear": "Clear",
    "Partly cloudy": "Cloudy",
    "Heavy showers": "Showers",
    "Heavy rain": "Rain",
    "Heavy snow": "Snow",
    "Heavy storm": "Storm",
    "Rime fog": "Fog",
}


def cond_tokens(condition: str) -> list[tuple[str, str, str | None]]:
    return [(SHORT_COND.get(condition, condition), EPD_BLACK, None)]


def temp_tokens(low: int, high: int) -> list[tuple[str, str, str | None]]:
    low_fg, low_bg = temp_chip_style(low)
    high_fg, high_bg = temp_chip_style(high)
    return [
        (f"{low}°", low_fg, low_bg),
        ("/", EPD_BLACK, None),
        (f"{high}°", high_fg, high_bg),
    ]


def rain_tokens(
    prob: int | None, hour: str | None
) -> list[tuple[str, str, str | None]]:
    if prob is None:
        return [("--", EPD_BLACK, None)]
    text = f"{prob}% {hour}" if hour else f"{prob}%"
    if prob >= RAIN_CHIP_THRESHOLD:
        return [(text, EPD_WHITE, EPD_BLUE)]
    return [(text, EPD_BLACK, None)]


def pm_tokens(
    low: int | None, high: int | None, peak_hour: str | None
) -> list[tuple[str, str, str | None]]:
    if low is None or high is None:
        return [("PM2.5 n/a", EPD_BLACK, None)]
    low_fg, low_bg = pm_chip_style(low)
    high_fg, high_bg = pm_chip_style(high)
    # The peak hour is *when* the max happens, so it shares the max's chip.
    high_text = f"{high} {peak_hour[:2]}h" if peak_hour else f"{high}"
    return [
        (f"{low}", low_fg, low_bg),
        ("-", EPD_BLACK, None),
        (high_text, high_fg, high_bg),
    ]


def token_width(
    draw: ImageDraw.ImageDraw, token: tuple[str, str, str | None]
) -> int:
    text, _, fill = token
    bbox = draw.textbbox((0, 0), text, font=font(FORECAST_SIZE))
    return (bbox[2] - bbox[0]) + (2 * CHIP_PADX if fill is not None else 0)


def field_width(
    draw: ImageDraw.ImageDraw, tokens: list[tuple[str, str, str | None]]
) -> int:
    return sum(token_width(draw, t) for t in tokens) + CHIP_GAP * (
        len(tokens) - 1
    )


def draw_chip_token(
    draw: ImageDraw.ImageDraw,
    x: float,
    ym: float,
    token: tuple[str, str, str | None],
) -> float:
    """Draw one token, left ink edge at x, vertically centered on mid-line ym.

    Vertical centering uses anchor="?m" (font metrics), so a descender such as
    the 'y' in Cloudy never shifts a token relative to an all-digit one.
    Returns the advance width.
    """
    text, fg, fill = token
    bbox = draw.textbbox((0, 0), text, font=font(FORECAST_SIZE))
    width = bbox[2] - bbox[0]
    if fill is not None:
        draw.rounded_rectangle(
            (x, ym - CHIP_H / 2, x + width + 2 * CHIP_PADX, ym + CHIP_H / 2),
            radius=CHIP_R,
            fill=fill,
        )
        draw.text(
            (x + CHIP_PADX, ym),
            text,
            fill=fg,
            font=font(FORECAST_SIZE),
            anchor="lm",
        )
        return width + 2 * CHIP_PADX
    draw.text((x, ym), text, fill=fg, font=font(FORECAST_SIZE), anchor="lm")
    return width


def draw_field(
    draw: ImageDraw.ImageDraw,
    column: tuple[float, float],
    ym: float,
    tokens: list[tuple[str, str, str | None]],
    align: str = "center",
) -> None:
    x0, x1 = column
    width = field_width(draw, tokens)
    x = x0 if align == "left" else x0 + (x1 - x0 - width) / 2
    for token in tokens:
        x += draw_chip_token(draw, x, ym, token) + CHIP_GAP


def draw_day_label(
    draw: ImageDraw.ImageDraw, x: int, ym: float, day: str
) -> None:
    for dx in (0, 1):  # faux-bold; day label anchors the row
        draw.text(
            (x + dx, ym),
            day,
            fill=EPD_BLACK,
            font=font(FORECAST_SIZE),
            anchor="lm",
        )


def day_label(day: date) -> str:
    """Compact forecast label: weekday then day-of-month, e.g. "THU 18"."""
    return f"{WEEKDAY_ABBR[day.weekday()]} {day.day}"


def draw_forecast_rows(
    draw: ImageDraw.ImageDraw,
    top: int,
    weather: Weather | None,
    air: AirQuality,
    row_height: int = 30,
) -> int:
    """Render the multi-day forecast as a chip grid, one row per day.

    Columns are sized to the widest content across all rows so the rows line
    up. Day labels and the condition column are left-aligned; numeric data
    columns are centered. Returns the bottom y.
    """
    if weather is None or not weather.days:
        draw_text(draw, (20, top + 8), "Forecast n/a", 18, EPD_BLACK, True)
        return top + row_height

    fields = []
    for day_weather in weather.days:
        day_air = air.days.get(day_weather.day)
        fields.append(
            {
                "cond": cond_tokens(day_weather.condition),
                "temp": temp_tokens(day_weather.low, day_weather.high),
                "rain": rain_tokens(
                    day_weather.rain_prob, day_weather.rain_hour
                ),
                "pm": pm_tokens(
                    day_air.pm25_min if day_air else None,
                    day_air.pm25_max if day_air else None,
                    day_air.pm25_peak_hour if day_air else None,
                ),
            }
        )

    data_x0, data_x1 = 80, WIDTH - 18
    order = ["cond", "temp", "rain", "pm"]
    maxw = {key: max(field_width(draw, f[key]) for f in fields)
            for key in order}
    # Graceful degradation: on an extreme day (all chips, signed temps, 3-digit
    # air) drop the least essential column — the condition — before overflowing.
    if sum(maxw.values()) > data_x1 - data_x0:
        order = ["temp", "rain", "pm"]
        maxw = {key: maxw[key] for key in order}

    pad = max(0.0, (data_x1 - data_x0 - sum(maxw.values())) / len(order))
    columns: dict[str, tuple[float, float]] = {}
    cursor = float(data_x0)
    for key in order:
        columns[key] = (cursor, cursor + maxw[key] + pad)
        cursor += maxw[key] + pad

    for index, (day_weather, field) in enumerate(zip(weather.days, fields)):
        ym = top + row_height / 2 + index * row_height
        label = "TODAY" if index == 0 else day_label(day_weather.day)
        draw_day_label(draw, 20, ym, label)
        for key in order:
            draw_field(
                draw,
                columns[key],
                ym,
                field[key],
                align="left" if key == "cond" else "center",
            )
    return top + row_height * len(weather.days)


def draw_section(
    draw: ImageDraw.ImageDraw, y: int, title: str, color: str
) -> None:
    draw.rounded_rectangle((18, y, WIDTH - 18, y + 30), radius=6, fill=color)
    draw_text(draw, (30, y + 5), title.upper(), 16, EPD_WHITE, True)


def clipped_task(text: str, width: int = 26) -> list[str]:
    return textwrap.wrap(
        text,
        width=width,
        break_long_words=False,
        max_lines=2,
        placeholder="...",
    ) or [""]


def display_date(now: datetime) -> str:
    weekdays = [
        "Monday",
        "Tuesday",
        "Wednesday",
        "Thursday",
        "Friday",
        "Saturday",
        "Sunday",
    ]
    months = [
        "January",
        "February",
        "March",
        "April",
        "May",
        "June",
        "July",
        "August",
        "September",
        "October",
        "November",
        "December",
    ]
    return f"{weekdays[now.weekday()]} {now.day} {months[now.month - 1]}"


def generate_dashboard() -> Path:
    load_dotenv()
    timezone = required_env("WEATHER_TIMEZONE")
    now = datetime.now(ZoneInfo(timezone))

    try:
        weather = fetch_weather(now)
    except Exception as exc:
        logger.warning("Could not fetch weather: %s", exc)
        weather = None

    try:
        air = fetch_air_quality(now)
    except Exception as exc:
        logger.warning("Could not fetch air quality: %s", exc)
        air = AirQuality(aqi=None, label="--", pm25=None, days={})

    try:
        tasks: list[TodoistTask] | None = fetch_todoist_tasks(now)
    except Exception as exc:
        logger.warning("Could not fetch Todoist tasks: %s", exc)
        tasks = None

    img = Image.new("RGB", (WIDTH, HEIGHT), EPD_WHITE)
    draw = ImageDraw.Draw(img)

    ink = EPD_BLACK
    muted = EPD_BLACK  # no native gray; hierarchy comes from size/weight
    red = EPD_RED
    green = EPD_GREEN
    blue = EPD_BLUE
    yellow = EPD_YELLOW
    line = EPD_BLACK   # thin black card borders read crisp on the panel
    air_fill, air_text, air_subtext = pm25_style(air.pm25)

    draw.rectangle((0, 0, WIDTH, HEIGHT), fill=EPD_WHITE)
    draw.rectangle((0, 0, WIDTH, 82), fill=ink)
    draw.rectangle((0, 82, WIDTH, 94), fill=yellow)

    location = (
        weather.location if weather else os.environ.get("WEATHER_LOCATION", "")
    )
    solar_text = (
        f"{weather.next_solar_label} {weather.next_solar_time}"
        if weather and weather.next_solar_time and weather.next_solar_label
        else f"updated {now:%H:%M}"
    )
    date_text = display_date(now)
    date_size = fitted_text_size(
        draw,
        date_text,
        max_width=258,
        preferred_size=32,
        min_size=25,
    )
    header_text_top = 23
    draw_text_at_visible_top(
        draw, (22, header_text_top), date_text, date_size, EPD_WHITE, True
    )
    draw_text(draw, (24, 54), f"{location}  |  {solar_text}", 17, EPD_WHITE)
    time_text = f"{now:%H:%M}"
    time_font = font(32)
    time_bbox = draw.textbbox((0, 0), time_text, font=time_font)
    time_width = time_bbox[2] - time_bbox[0]
    time_x = 292 + (382 - 292 - time_width) // 2
    draw_text_at_visible_top(
        draw,
        (time_x, header_text_top),
        time_text,
        32,
        EPD_WHITE,
        True,
    )
    centered_text_at_y(draw, 294, 382, 54, "updated", 17, EPD_WHITE)

    current_card = (18, 108, 191, 198)
    air_card = (209, 108, 382, 198)

    temp_fill, temp_ink = temp_style(
        weather.current_c if weather is not None else None
    )
    card(draw, current_card, temp_fill, line)
    if weather is not None:
        current_lines = [
            (f"{weather.current_c}°", 42, temp_ink, True),
            (weather.condition, 18, temp_ink, False),
        ]
    else:
        current_lines = [
            ("--°", 42, temp_ink, True),
            ("weather n/a", 18, temp_ink, False),
        ]
    draw_centered_stack(draw, current_card, current_lines, gap=4)

    card(draw, air_card, air_fill, line)
    draw_centered_stack(
        draw,
        (air_card[0] + 8, air_card[1] + 8, air_card[2] - 8, air_card[3] - 8),
        [
            (
                f"AQI {air.aqi}" if air.aqi is not None else "AQI --",
                14,
                air_text,
                True,
            ),
            (
                (
                    f"PM2.5 {round(air.pm25)}"
                    if air.pm25 is not None
                    else "PM2.5 --"
                ),
                24,
                air_text,
                True,
            ),
            (air.label, 14, air_subtext, False),
        ],
        gap=7,
    )

    draw_forecast_rows(draw, 202, weather, air)

    y = 300
    draw_section(draw, y, "Tasks", red if tasks else blue)
    if tasks is None:
        draw_text(draw, (28, y + 44), "Tasks n/a", 20, muted, True)
    elif not tasks:
        draw_text(draw, (28, y + 44), "No pending tasks", 20, green, True)
    else:
        task_y = y + 42
        shown_count = 0
        hidden_count = 0
        for task_index, task in enumerate(tasks):
            lines = clipped_task(task.content)
            task_height = 44 if len(lines) > 1 else 29
            remaining_after_task = len(tasks) - task_index - 1
            needs_more_line = remaining_after_task > 0
            reserved_more = 28 if needs_more_line else 0
            if task_y + task_height + reserved_more > 584:
                hidden_count = len(tasks) - shown_count
                break

            priority_color = red if task.overdue or task.priority >= 4 else ink
            marker = "!" if task.overdue else "-"
            marker_color = red if task.overdue else muted
            draw_text(draw, (28, task_y), marker, 20, marker_color, True)
            draw_text(
                draw,
                (56, task_y),
                lines[0],
                20,
                priority_color,
                task.priority >= 4,
            )
            label_color = red if task.overdue else muted
            draw_text(
                draw,
                (306, task_y + 2),
                task.due_label,
                15,
                label_color,
                task.overdue,
            )
            if len(lines) > 1:
                draw_text(draw, (56, task_y + 22), lines[1], 17, muted)
            task_y += task_height
            shown_count += 1

        if hidden_count > 0 and task_y <= 574:
            draw_text(
                draw,
                (56, task_y + 2),
                f"+{hidden_count} more",
                17,
                muted,
                True,
            )

    img.save(OUT)
    return OUT.resolve()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate a PaperColor dashboard image."
    )
    parser.add_argument(
        "--upload",
        action="store_true",
        help="Upload the generated image to EzData.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        image_path = generate_dashboard()
        logger.info("Dashboard generated: %s", image_path)
        if args.upload:
            result = upload_to_ezdata(image_path)
            code = result.get("code")
            msg = result.get("msg")
            if code == 200:
                logger.info(
                    "Image uploaded to EzData (code=%s msg=%s)", code, msg
                )
            else:
                logger.error(
                    "EzData rejected the upload (code=%s msg=%s)", code, msg
                )
    except MissingConfig as exc:
        raise SystemExit(str(exc))


if __name__ == "__main__":
    main()
