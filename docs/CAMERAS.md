# Camera compatibility

pawcorder talks to any camera that speaks plain RTSP. The list below is
what we've tested. New brands get added when a user reports them
working — please open a PR if you have one to share.

| Brand                  | Talk-back | Default user | Notes                                                                                                                |
| ---                    | ---       | ---          | ---                                                                                                                  |
| Reolink (E-series)     | yes       | `admin`      | First-class. Set encoding to H.264 in the Reolink app — H.265 confuses Frigate's hardware decoder on some hosts.    |
| TP-Link Tapo C2/C3xx   | yes       | (see notes)  | Make a separate "camera account" in the Tapo app first — your cloud login won't work for RTSP.                       |
| Hikvision              | no        | `admin`      | Talk-back over RTSP is unreliable. iSAPI integration would be more stable, out of scope.                             |
| Dahua                  | no        | `admin`      | Standard `realmonitor` URLs. Disable audio in stream settings if you hear glitching.                                  |
| Amcrest                | yes       | `admin`      | Dahua OEM. Most models support backchannel.                                                                          |
| Imou                   | no        | `admin`      | Dahua OEM. Enable RTSP in the Imou Life app first (Settings → Local config → RTSP).                                  |
| Ubiquiti UniFi Protect | no        | `ubnt`       | Enable RTSP per-camera in the Protect web UI (Cameras → camera → Settings → RTSP). Port `7447`, not `554`.           |
| Axis                   | no        | `root`       | Industrial-grade — reliable but expensive.                                                                            |
| Foscam                 | no        | `admin`      | Older models work; the newer "Foscam app only" models don't expose RTSP.                                              |
| Wyze v2 / v3           | no        | `admin`      | Stock firmware doesn't speak RTSP. You need the unofficial RTSP firmware (ask Wyze support, they ship it on request) or run docker-wyze-bridge in front. |
| Other RTSP cameras     | depends   | varies       | Anything ONVIF / RTSP works if you can find the stream URLs. Use an ONVIF discovery tool to enumerate them.         |

## Two-way audio

Talk-back is opt-in per camera (Cameras page → Edit → "Enable two-way
audio"). When you enable it, pawcorder asks go2rtc to open the camera's
RTSP backchannel; the Frigate web UI then shows a "talk" button on the
live view.

Talk-back depends on three things:

1. The camera supports it (see the table — even within "Reolink", PTZ
   models like the RLC-823A support it; bullet models like the RLC-510A
   often don't).
2. Frigate is up-to-date (≥ 0.14 ships go2rtc 1.9, which speaks the
   common backchannel codecs).
3. Your browser has microphone permission for the Frigate UI origin.

If "talk" doesn't work, the troubleshooting order is: confirm the
camera lists "two-way audio" or "talk-back" in its spec sheet → try the
camera vendor's own app to confirm it works at all → test with VLC
(`rtsp://USER:PASS@IP:554/...#backchannel=1`) → only then bring it back
to pawcorder.

## When you don't know your camera's stream URL

Run a quick scan from the admin panel: Cameras → Add camera → Scan for
cameras (optional). It probes RTSP port 554 across your subnet and
shows hosts that respond. From there, the camera vendor's own app
usually shows the stream URL in advanced / developer settings. As a
last resort, [iSpyConnect's camera DB](https://www.ispyconnect.com/cameras)
has user-reported URLs for thousands of models.

## What "supported" means

A brand in this list means: at least one user reported it works for
record + detect + (where listed) talk-back. It does **not** mean
pawcorder vendors-in any code specific to that brand. The admin panel
just produces the right RTSP URLs — the rest is plain Frigate.
