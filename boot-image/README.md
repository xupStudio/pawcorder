# pawcorder boot image

Builds a customised Ubuntu Server 24.04 LTS ISO that installs Ubuntu
**unattended** and runs `pawcorder/install.sh` automatically on first
boot. The end-user experience is:

1. Flash the ISO onto a USB stick (Etcher / Rufus / `dd`).
2. Boot any x86_64 box from USB — Intel N100 mini PC, NUC, an old
   laptop, even a desktop. Anything Ubuntu Server 24.04 supports.
3. Wait ~10 minutes. Ubuntu installs itself, configures Docker, pulls
   the pawcorder stack, and starts the admin panel.
4. The host announces itself on the LAN as `pawcorder.local` via Avahi
   (mDNS). Browse to `http://pawcorder.local:8080` from any device on
   the same Wi-Fi.
5. Log in with the random admin password printed on the host's TTY
   (visible if you have a monitor attached) or recover from `/etc/pawcorder.password`
   via SSH (`pawcorder@pawcorder.local`, default password `pawcorder`,
   change immediately).

## Tools used (all permissive licenses)

- [HashiCorp Packer](https://www.packer.io/) — MPL 2.0 — orchestrates the build
- Ubuntu autoinstall (cloud-init flavour) — built into Ubuntu Server installer
- Avahi — LGPL — mDNS daemon, included in the Ubuntu base
- QEMU/KVM — GPL (only used for **building** the ISO, not redistributed)

## Building the ISO

```sh
cd boot-image/
./build.sh
# → produces output/pawcorder-ubuntu-24.04.iso
```

Build needs:
- Linux or macOS host with Packer ≥ 1.10
- ~10 GB free disk
- ~15 minutes on a modern laptop

CI builds the ISO on every push to `main` (see `.github/workflows/iso.yml`).

## What the autoinstall does

`http/user-data` is the cloud-init seed Ubuntu's installer reads. Key
behaviours:

- Creates a `pawcorder` user with passwordless sudo
- Enables OpenSSH so the user can log in remotely
- Sets the hostname to `pawcorder` (resolves via mDNS as
  `pawcorder.local` on most home networks)
- On first boot after install:
  1. Installs Avahi (mDNS) + Docker
  2. Clones the pawcorder repo to `/opt/pawcorder`
  3. Runs `install.sh` non-interactively
  4. Writes the admin password to `/etc/pawcorder.password`

## Notes / caveats

- Currently targets x86_64 (any Intel/AMD-64 box: N100, NUC, mini PCs,
  laptops, NAS x86 hosts). An ARM64 image (Pi 5, Orange Pi, etc.) is
  straightforward to add — same flow but `--server-image=...arm64...`
  ISO base.
- For users who'd rather skip the USB image, the regular path
  (`apt install ubuntu-server` then `git clone && ./install.sh`) still
  works.
- Re-running `./install.sh` is idempotent, so a half-installed host can
  be recovered just by running it again.
