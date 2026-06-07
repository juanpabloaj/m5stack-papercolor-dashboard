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
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont

WIDTH = 400
HEIGHT = 600
OUT = Path("papercolor_dashboard_test.png")

logger = logging.getLogger("papercolor_dashboard")


@dataclass(frozen=True)
class Weather:
    location: str
    current_c: int
    condition: str
    feels_c: int
    humidity: int
    wind_kmh: int
    today_low: int
    today_high: int
    today_condition: str
    today_rain_prob: int | None
    today_rain_hour: str | None
    tomorrow_low: int
    tomorrow_high: int
    tomorrow_condition: str
    tomorrow_rain_prob: int | None
    tomorrow_rain_hour: str | None
    next_solar_time: str | None
    next_solar_label: str | None


@dataclass(frozen=True)
class AirQuality:
    aqi: int | None
    label: str
    pm25: float | None
    today_min: int | None = None
    today_max: int | None = None
    today_peak_hour: str | None = None
    tomorrow_min: int | None = None
    tomorrow_max: int | None = None
    tomorrow_peak_hour: str | None = None


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
        raise SystemExit(f"Missing required environment variable: {name}")
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


def rain_probability_summary(
    hourly: dict, now: datetime
) -> dict[date, tuple[int, str | None]]:
    times = hourly.get("time") or []
    values = hourly.get("precipitation_probability") or []
    if not times or not values:
        return {}

    today = now.date()
    tomorrow = date.fromordinal(today.toordinal() + 1)
    points: dict[date, list[tuple[datetime, int]]] = {
        today: [],
        tomorrow: [],
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
        if parsed.date() not in points:
            continue

        points[parsed.date()].append((parsed, round(raw_value)))

    summary: dict[date, tuple[int, str | None]] = {}
    for day, day_points in points.items():
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
    load_dotenv()
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
                "apparent_temperature",
                "relative_humidity_2m",
                "weather_code",
                "wind_speed_10m",
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
        "forecast_days": "2",
    }
    data = request_json(
        "https://api.open-meteo.com/v1/forecast", params=params
    )

    current = data["current"]
    daily = data["daily"]
    rain_summary = rain_probability_summary(data.get("hourly", {}), now)
    today_rain_prob, today_rain_hour = rain_summary.get(
        now.date(),
        (daily_rain_probability(daily, 0), None),
    )
    tomorrow = date.fromordinal(now.date().toordinal() + 1)
    tomorrow_rain_prob, tomorrow_rain_hour = rain_summary.get(
        tomorrow,
        (daily_rain_probability(daily, 1), None),
    )
    next_solar_time, next_solar_label = next_solar_event(daily, now)
    return Weather(
        location=location,
        current_c=round(current["temperature_2m"]),
        condition=weather_label(int(current["weather_code"])),
        feels_c=round(current["apparent_temperature"]),
        humidity=round(current["relative_humidity_2m"]),
        wind_kmh=round(current["wind_speed_10m"]),
        today_low=round(daily["temperature_2m_min"][0]),
        today_high=round(daily["temperature_2m_max"][0]),
        today_condition=weather_label(int(daily["weather_code"][0])),
        today_rain_prob=today_rain_prob,
        today_rain_hour=today_rain_hour,
        tomorrow_low=round(daily["temperature_2m_min"][1]),
        tomorrow_high=round(daily["temperature_2m_max"][1]),
        tomorrow_condition=weather_label(int(daily["weather_code"][1])),
        tomorrow_rain_prob=tomorrow_rain_prob,
        tomorrow_rain_hour=tomorrow_rain_hour,
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


def air_quality_label(aqi: int | None) -> str:
    if aqi is None:
        return "--"
    if aqi <= 50:
        return "Good"
    if aqi <= 100:
        return "Moderate"
    if aqi <= 150:
        return "USG"
    if aqi <= 200:
        return "Unhealthy"
    if aqi <= 300:
        return "Very bad"
    return "Hazardous"


def air_quality_style(aqi: int | None) -> tuple[str, str, str]:
    if aqi is None:
        return "#ffffff", "#245aa6", "#5c6270"
    if aqi <= 50:
        return "#ffffff", "#245aa6", "#5c6270"
    if aqi <= 100:
        return "#f4efe3", "#121417", "#5c6270"
    if aqi <= 150:
        return "#e6b53b", "#121417", "#3d3420"
    return "#ce3328", "#ffffff", "#ffffff"


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
            "current": "us_aqi,pm2_5,pm10",
            "hourly": "us_aqi",
            "forecast_days": "2",
            "timezone": timezone,
        },
    )
    current = data.get("current", {})
    aqi = current.get("us_aqi")
    rounded_aqi = round(aqi) if aqi is not None else None
    pm25 = current.get("pm2_5")
    hourly = data.get("hourly", {})
    daily_aqi = daily_air_quality_summary(hourly, now)
    return AirQuality(
        aqi=rounded_aqi,
        label=air_quality_label(rounded_aqi),
        pm25=round(pm25, 1) if pm25 is not None else None,
        today_min=daily_aqi.get("today_min"),
        today_max=daily_aqi.get("today_max"),
        today_peak_hour=daily_aqi.get("today_peak_hour"),
        tomorrow_min=daily_aqi.get("tomorrow_min"),
        tomorrow_max=daily_aqi.get("tomorrow_max"),
        tomorrow_peak_hour=daily_aqi.get("tomorrow_peak_hour"),
    )


def summarize_aqi_points(
    points: list[tuple[datetime, int]],
) -> dict[str, int | str] | None:
    if not points:
        return None
    min_value = min(value for _, value in points)
    peak_time, max_value = max(points, key=lambda item: item[1])
    return {
        "min": min_value,
        "max": max_value,
        "peak_hour": peak_time.strftime("%H:%M"),
    }


def daily_air_quality_summary(
    hourly: dict, now: datetime
) -> dict[str, int | str]:
    times = hourly.get("time") or []
    values = hourly.get("us_aqi") or []
    today = now.date()
    tomorrow = date.fromordinal(today.toordinal() + 1)
    points: dict[date, list[tuple[datetime, int]]] = {
        today: [],
        tomorrow: [],
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
        if parsed.date() not in points:
            continue

        points[parsed.date()].append((parsed, round(raw_value)))

    summary: dict[str, int | str] = {}
    for label, day in (("today", today), ("tomorrow", tomorrow)):
        day_points = points[day]
        day_summary = summarize_aqi_points(day_points)
        if day_summary is None:
            continue
        summary[f"{label}_min"] = day_summary["min"]
        summary[f"{label}_max"] = day_summary["max"]
        summary[f"{label}_peak_hour"] = day_summary["peak_hour"]
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


def font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def draw_text(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    text: str,
    size: int,
    color: str = "#111111",
    bold: bool = False,
) -> None:
    draw.text(xy, text, fill=color, font=font(size, bold))
    if bold:
        draw.text((xy[0] + 1, xy[1]), text, fill=color, font=font(size, bold))


def centered_text(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    text: str,
    size: int,
    color: str,
    bold: bool = False,
) -> None:
    f = font(size, bold)
    bbox = draw.textbbox((0, 0), text, font=f)
    x = box[0] + (box[2] - box[0] - (bbox[2] - bbox[0])) // 2
    y = box[1] + (box[3] - box[1] - (bbox[3] - bbox[1])) // 2
    draw_text(draw, (x, y), text, size, color, bold)


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
    f = font(size, bold)
    bbox = draw.textbbox((0, 0), text, font=f)
    x = x0 + (x1 - x0 - (bbox[2] - bbox[0])) // 2
    draw_text(draw, (x, y), text, size, color, bold)


def draw_inline_centered(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    segments: list[tuple[str, int, str, bool]],
    gap: int,
) -> None:
    x0, y0, x1, y1 = box
    measured = []
    total_width = 0
    max_height = 0
    for text, size, color, bold in segments:
        f = font(size, bold)
        bbox = draw.textbbox((0, 0), text, font=f)
        width = bbox[2] - bbox[0]
        height = bbox[3] - bbox[1]
        measured.append((text, size, color, bold, bbox, width, height))
        total_width += width
        max_height = max(max_height, height)
    total_width += gap * max(0, len(measured) - 1)

    x = x0 + (x1 - x0 - total_width) // 2
    y = y0 + (y1 - y0 - max_height) // 2
    for text, size, color, bold, bbox, width, height in measured:
        segment_y = y + (max_height - height) // 2
        draw_text(
            draw, (x - bbox[0], segment_y - bbox[1]), text, size, color, bold
        )
        x += width + gap


def draw_centered_stack(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    lines: list[tuple[str, int, str, bool]],
    gap: int = 4,
) -> None:
    measured = []
    total_height = 0
    for text, size, color, bold in lines:
        f = font(size, bold)
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
    fill: str = "#ffffff",
    outline: str = "#d7d2c4",
) -> None:
    draw.rounded_rectangle(box, radius=8, fill=fill, outline=outline, width=2)


def draw_forecast_card(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    title: str,
    title_color: str,
    value: str,
    subtitle: str,
    rain_prob: int | None,
    fill: str,
    outline: str,
    ink: str,
    muted: str,
    rain_hour: str | None = None,
    aqi_min: int | None = None,
    aqi_max: int | None = None,
    peak_hour: str | None = None,
) -> None:
    card(draw, box, fill, outline)
    aqi_line = (
        f"AQI {aqi_min}-{aqi_max}  {peak_hour[:2]}h"
        if aqi_min is not None and aqi_max is not None and peak_hour
        else "AQI n/a"
    )
    x0, y0, x1, _ = box
    draw_inline_centered(
        draw,
        (x0 + 10, y0 + 12, x1 - 10, y0 + 28),
        [(title, 14, title_color, True), (subtitle, 14, muted, False)],
        gap=6,
    )

    rain_segments: list[tuple[str, int, str, bool]] = [
        (value.replace(" / ", "/"), 19, ink, True)
    ]
    rain_text = f"{rain_prob}%" if rain_prob is not None else "--"
    rain_segments.append((rain_text, 19, muted, False))
    if rain_prob and rain_hour:
        rain_segments.append((rain_hour, 19, muted, rain_prob > 50))
    draw_inline_centered(
        draw,
        (x0 + 10, y0 + 35, x1 - 10, y0 + 59),
        rain_segments,
        gap=7,
    )
    centered_text(
        draw, (x0 + 8, y0 + 60, x1 - 8, y0 + 82), aqi_line, 18, muted
    )


def draw_section(
    draw: ImageDraw.ImageDraw, y: int, title: str, color: str
) -> int:
    draw.rounded_rectangle((18, y, WIDTH - 18, y + 30), radius=6, fill=color)
    draw_text(draw, (30, y + 5), title.upper(), 16, "#ffffff", True)
    return y + 42


def clipped_task(text: str, width: int = 26) -> list[str]:
    return textwrap.wrap(
        text,
        width=width,
        break_long_words=False,
        max_lines=2,
        placeholder="...",
    ) or [""]


def measure_task_height(task: TodoistTask) -> int:
    return 44 if len(clipped_task(task.content)) > 1 else 29


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
        air = AirQuality(aqi=None, label="--", pm25=None)

    try:
        tasks: list[TodoistTask] | None = fetch_todoist_tasks(now)
    except Exception as exc:
        logger.warning("Could not fetch Todoist tasks: %s", exc)
        tasks = None

    img = Image.new("RGB", (WIDTH, HEIGHT), "#fbfaf4")
    draw = ImageDraw.Draw(img)

    ink = "#121417"
    muted = "#5c6270"
    red = "#ce3328"
    green = "#2f7d4f"
    blue = "#245aa6"
    yellow = "#e6b53b"
    cream = "#f4efe3"
    line = "#d8d0bf"
    air_fill, air_text, air_subtext = air_quality_style(air.aqi)

    draw.rectangle((0, 0, WIDTH, HEIGHT), fill="#fbfaf4")
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
    draw_text(draw, (22, 16), display_date(now), 32, "#ffffff", True)
    draw_text(draw, (24, 54), f"{location}  |  {solar_text}", 17, "#ffffff")
    centered_text_at_y(draw, 292, 382, 16, f"{now:%H:%M}", 32, "#ffffff", True)
    centered_text_at_y(draw, 294, 382, 54, "updated", 17, "#ffffff")

    current_card = (18, 108, 191, 198)
    air_card = (209, 108, 382, 198)
    today_card = (18, 214, 191, 304)
    tomorrow_card = (209, 214, 382, 304)

    card(draw, current_card, "#ffffff", line)
    if weather is not None:
        current_lines = [
            (f"{weather.current_c}°", 42, ink, True),
            (weather.condition, 18, muted, False),
        ]
    else:
        current_lines = [
            ("--°", 42, ink, True),
            ("weather n/a", 18, muted, False),
        ]
    draw_centered_stack(draw, current_card, current_lines, gap=4)

    card(draw, air_card, air_fill, line)
    draw_centered_stack(
        draw,
        air_card,
        [
            ("US AQI", 14, air_text, True),
            (
                str(air.aqi if air.aqi is not None else "--"),
                31,
                air_text,
                True,
            ),
            (air.label, 14, air_subtext, False),
        ],
        gap=4,
    )

    draw_forecast_card(
        draw,
        today_card,
        "TODAY",
        red,
        f"{weather.today_low}° / {weather.today_high}°" if weather else "n/a",
        weather.today_condition if weather else "weather n/a",
        weather.today_rain_prob if weather else None,
        cream,
        line,
        ink,
        muted,
        weather.today_rain_hour if weather else None,
        air.today_min,
        air.today_max,
        air.today_peak_hour,
    )
    draw_forecast_card(
        draw,
        tomorrow_card,
        "TOMORROW",
        green,
        (
            f"{weather.tomorrow_low}° / {weather.tomorrow_high}°"
            if weather
            else "n/a"
        ),
        weather.tomorrow_condition if weather else "weather n/a",
        weather.tomorrow_rain_prob if weather else None,
        cream,
        line,
        ink,
        muted,
        weather.tomorrow_rain_hour if weather else None,
        air.tomorrow_min,
        air.tomorrow_max,
        air.tomorrow_peak_hour,
    )

    y = 330
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
            task_height = measure_task_height(task)
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
            lines = clipped_task(task.content)
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

    image_path = generate_dashboard()
    logger.info("Dashboard generated: %s", image_path)
    if args.upload:
        result = upload_to_ezdata(image_path)
        code = result.get("code")
        msg = result.get("msg")
        if code == 200:
            logger.info("Image uploaded to EzData (code=%s msg=%s)", code, msg)
        else:
            logger.error(
                "EzData rejected the upload (code=%s msg=%s)", code, msg
            )


if __name__ == "__main__":
    main()
