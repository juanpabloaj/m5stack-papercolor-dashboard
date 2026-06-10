# M5Stack PaperColor EzData Notes

<img width="600" height="867" alt="colorpaper" src="https://github.com/user-attachments/assets/927d1c29-6ee9-475f-82de-3cc41957a786" />


Unofficial project, not affiliated with M5Stack.

This repo contains notes and local test assets for pushing images to an M5Stack PaperColor device through M5Stack EzData2.

## Device Context

- Device: M5Stack PaperColor / M5Paper Color, SKU `C151`
- Display: 4-inch color e-paper
- Native image size: `400x600`
- The factory firmware supports remote image updates through EzData Mode.

## EzData Image Upload

The EzData2 HTTP file upload endpoint is:

```sh
https://ezdata2.m5stack.com/api/v2/device/uploadDeviceFile
```

The request must be `multipart/form-data`.

Required fields:

- `deviceToken`: the PaperColor EzData token
- `name`: must be `image` for the PaperColor factory/user demo firmware
- `file`: the image file

The token is stored locally in `.env`:

```sh
TOKEN=...
DEVICE_ID=...
WEATHER_LAT=...
WEATHER_LON=...
WEATHER_LOCATION=...
WEATHER_TIMEZONE=...
TODOIST_API_TOKEN=...
TODOIST_QUERY="overdue | today"
TODOIST_TASK_LIMIT=50
PAPERCOLOR_BASE_URL=http://192.168.4.1
BATTERY_LOG=battery_log.csv
BATTERY_CONNECT_TIMEOUT_SECONDS=3
BATTERY_MAX_TIME_SECONDS=8
HTTP_RETRIES=3
HTTP_RETRY_DELAY_SECONDS=5
HTTP_TIMEOUT_SECONDS=60
```

Do not commit `.env`.

## Upload Command

Generate or prepare a `400x600` PNG/JPG/BMP, then upload it like this:

```sh
set -a
source .env
set +a

curl -sS -X POST 'https://ezdata2.m5stack.com/api/v2/device/uploadDeviceFile' \
  -F "deviceToken=${TOKEN}" \
  -F "name=image" \
  -F "file=@papercolor_ezdata_image_field.png;type=image/png" \
  -w '\nHTTP_STATUS:%{http_code}\n'
```

The included `generate_dashboard.py` script creates a `400x600` dashboard PNG.
It reads weather location settings from `.env`, fetches current weather plus
today/tomorrow forecast from Open-Meteo, fetches current PM2.5 and 24-hour US AQI
air-quality data plus hourly PM2.5 forecasts from Open-Meteo Air Quality, queries
Todoist with `TODOIST_QUERY`, and renders active tasks due today or overdue.
Tasks are sorted by due time/date, overdue tasks are marked in red with `!`, and
hidden overflow is summarized as `+N more`.

```sh
uv run python generate_dashboard.py
```

To generate and upload the image to EzData in one step:

```sh
uv run python generate_dashboard.py --upload
```

A successful response looks like:

```json
{
  "code": 200,
  "msg": "OK",
  "data": {
    "deviceToken": "...",
    "value": "https://ezdata2-oss-dev.m5stack.com/.../image/image_....png",
    "name": "image"
  }
}
```

## Important Finding

The public EzData API docs show the file upload API and examples using
`deviceFile`, but the PaperColor firmware in `m5stack/M5PaperColor-UserDemo`
tracks the active image field as `image`.

Using `name=deviceFile` uploads successfully, but the PaperColor firmware may
not treat it as the current display image. Use:

```sh
-F "name=image"
```

## Polling Behavior

From `m5stack/M5PaperColor-UserDemo`:

- While the device is awake and connected, the EzData daemon queries EzData
  every `5` seconds.
- The UI setting named `Interval` is stored as minutes. The default is `60`.
- In low-power mode, the device can sleep between refreshes and wake according
  to the configured interval.

In normal EzData Mode, a newly uploaded `image` field should appear after the
device's next polling/update cycle.

## Battery Logging

The factory firmware exposes battery voltage on the local API:

```sh
/api/battery
```

Use `log_battery.sh` to append one battery sample to a CSV file:

```sh
./log_battery.sh
```

Default output:

```sh
battery_log.csv
```

Each row contains:

```csv
timestamp,status,voltage_mv,percent,error
```

`status=ok` means a battery sample was captured. `status=unreachable` usually
means the device is asleep, powered off, or unavailable on the local network.
This is expected when Low Power Mode is working.

To sample every 5 minutes for a few hours, leave this running:

```sh
while true; do ./log_battery.sh; sleep 300; done
```

The percentage is an approximation derived from the voltage curve used by the
factory web UI. For the local API to work, this machine must be able to reach
the PaperColor local web server, usually `http://192.168.4.1` when connected to
the device AP.

## Extending Battery Life

With the official/factory firmware, battery life depends mostly on whether the
ESP32-S3 stays awake with Wi-Fi active or sleeps between refreshes.

Use the web UI settings:

- Enable `Low Power Mode`.
- Enable `Auto Slideshow`.
- Set `Interval` to the desired wake cadence, for example `60` minutes.
- Disconnect clients from the PaperColor AP or close the local web UI. The
  firmware keeps the device awake while AP clients are connected.

In this mode the firmware should wake on schedule, refresh the image, then power
off/sleep again. Avoid manual double-press shutdown because the UI notes that it
disables auto wake.

Observed active-mode discharge can be on the order of hours because the device
keeps Wi-Fi, EzData/MQTT, and the web server active. Low Power Mode should reduce
the average current by making the device unavailable between wake windows; in the
battery log this appears as `status=unreachable` rows while the device sleeps.

## Future Improvements

- Include battery status in generated images. The factory firmware exposes
  battery voltage through its local web API at `/api/battery`, and the web UI
  converts that voltage to an approximate percentage. Without firmware changes,
  battery status would need to be fetched externally, rendered into the image,
  and uploaded again through EzData.

## Useful Sources

- PaperColor docs: https://docs.m5stack.com/en/core/PaperColor
- Factory firmware guide: https://docs.m5stack.com/en/guide/display_device/papercolor/usage
- EzData2 API docs: https://docs.m5stack.com/en/guide/ezdata/ezdata_v2_protocol
- Firmware/demo repo: https://github.com/m5stack/M5PaperColor-UserDemo
- Open-Meteo forecast API: https://open-meteo.com/en/docs
- Open-Meteo air quality API: https://open-meteo.com/en/docs/air-quality-api
- Todoist API docs: https://developer.todoist.com/api/v1/
