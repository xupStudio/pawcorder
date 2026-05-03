"""Wi-Fi onboarding for cameras that aren't yet on the network.

The package handles the "I just plugged it in" path — discover cameras
broadcasting on BLE / SoftAP / via QR-receive, push the user's home Wi-Fi
credentials, then watch the LAN for the camera's first DHCP lease and
hand off to ``camera_setup.auto_configure_for_brand``.

Module map:

  base.py              Shared dataclasses (DiscoveredDevice,
                       ProvisionerResult) and the BaseProvisioner ABC.
  fingerprints.py      Per-vendor identification: BLE service UUIDs,
                       manufacturer-data prefixes, SoftAP SSID patterns,
                       MAC OUI ranges. Single source of truth used by
                       every scanner.
  ble_scanner.py       Periodically scans BLE advertisements; emits
                       DiscoveredDevice records for matched fingerprints.
  ble_homekit.py       HomeKit Accessory Protocol BLE detection.
  ble_matter.py        Matter (CSA) BLE commissioning detection.
  ble_proprietary.py   Detection-only flows for Tapo / Wyze / Eufy /
                       Ring / Nest / Tuya — return a "use vendor app"
                       handoff plus the MAC for arrival_watcher.
  softap_scanner.py    nmcli/iw-based scan for known SoftAP SSIDs.
  softap_foscam.py     Foscam SoftAP CGI cred push.
  softap_dahua.py      Dahua / Amcrest SoftAP cred push.
  softap_hnap.py       Older D-Link / TP-Link HNAP cred push.
  softap_espressif.py  Generic ESP32 / ESP-IDF SoftAP protobuf cred push.
  qr_generic.py        Standard ``WIFI:S:...;`` Wi-Fi QR code generator.
  qr_reolink.py        Reolink XML-tagged QR variant.
  esptouch_v2.py       Espressif EspTouch v2 broadcast provisioner.
  wps_pbc.py           wpa_supplicant WPS Push-Button-Config wrapper.
  arrival_watcher.py   ARP / DHCP-lease monitor; matches MACs against
                       in-flight provisioning attempts and notifies the
                       orchestrator when the camera shows up on the LAN.
  orchestrator.py      Top-level state machine consumed by the admin
                       SSE endpoint.

This package is import-light. The admin process imports only what its
active route needs; ``bleak`` etc. are lazy-imported inside the modules
that actually use them so headless installs without those libraries
still serve the rest of the admin UI.
"""
