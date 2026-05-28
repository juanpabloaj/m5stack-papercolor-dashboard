# M5Stack PaperColor EzData Notes

This repo contains notes and local test assets for pushing images to an M5Stack
PaperColor device through M5Stack EzData2.

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
