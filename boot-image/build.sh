#!/usr/bin/env bash
# Wrapper around `packer build`. Validates that prerequisites are
# installed, refuses to overwrite output without --force, prints a
# friendly summary of where the ISO ended up.

set -euo pipefail

cd "$(dirname "$0")"

if ! command -v packer >/dev/null 2>&1; then
  echo "packer not installed. https://developer.hashicorp.com/packer/install"
  exit 1
fi

if ! command -v qemu-img >/dev/null 2>&1; then
  echo "qemu not installed. brew install qemu  (or: apt install qemu-system-x86)"
  exit 1
fi

FORCE=""
if [[ "${1:-}" == "--force" ]]; then
  FORCE="-force"
  shift
fi

if [[ -d output ]] && [[ -z "$FORCE" ]]; then
  echo "output/ already exists. Pass --force to overwrite."
  exit 1
fi

packer init pawcorder.pkr.hcl
packer build $FORCE pawcorder.pkr.hcl

echo
echo "Built ISO:"
ls -lh output/pawcorder-ubuntu-24.04.iso 2>/dev/null || true
echo
echo "Flash to USB with one of:"
echo "  Linux:  sudo dd if=output/pawcorder-ubuntu-24.04.iso of=/dev/sdX bs=4M status=progress conv=fsync"
echo "  macOS:  use balenaEtcher (https://etcher.balena.io/)"
echo "  Win:    use Rufus (https://rufus.ie/) — leave as DD image"
