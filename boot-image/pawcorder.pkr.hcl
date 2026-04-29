# Packer template that builds an unattended Ubuntu Server 24.04 ISO
# pre-baked to install pawcorder on first boot.
#
# Build:    packer init . && packer build pawcorder.pkr.hcl
# Output:   output/pawcorder-ubuntu-24.04.iso
#
# Packer's qemu builder serves http/user-data + http/meta-data over an
# HTTP server during install; Ubuntu's autoinstall fetches them via
# the kernel cmdline `autoinstall ds=nocloud-net;s=...`.

packer {
  required_plugins {
    qemu = {
      source  = "github.com/hashicorp/qemu"
      version = "~> 1"
    }
  }
}

variable "iso_url" {
  type    = string
  default = "https://releases.ubuntu.com/24.04/ubuntu-24.04.1-live-server-amd64.iso"
}

variable "iso_checksum" {
  type    = string
  # Placeholder so `packer validate` passes in CI without fetching the
  # SHA256SUMS manifest (Ubuntu drops old point releases from the
  # manifest over time, which broke the previous `file:` form). Real
  # builds must override with the real ISO's SHA256, e.g.:
  #   packer build -var iso_checksum=sha256:<real> pawcorder.pkr.hcl
  default = "sha256:0000000000000000000000000000000000000000000000000000000000000000"
}

source "qemu" "pawcorder" {
  iso_url            = var.iso_url
  iso_checksum       = var.iso_checksum
  output_directory   = "output"
  shutdown_command   = "sudo -S poweroff"
  disk_size          = "20G"
  format             = "raw"
  accelerator        = "kvm"
  cpus               = 4
  memory             = 4096
  net_device         = "virtio-net"
  disk_interface     = "virtio"
  boot_wait          = "5s"
  boot_command = [
    "<wait>c<wait>",
    "linux /casper/vmlinuz autoinstall ds=nocloud-net;s=http://{{ .HTTPIP }}:{{ .HTTPPort }}/ ---",
    "<enter>",
    "initrd /casper/initrd",
    "<enter>",
    "boot",
    "<enter>",
  ]
  http_directory     = "http"
  ssh_username       = "pawcorder"
  ssh_password       = "pawcorder"
  ssh_timeout        = "30m"
  vm_name            = "pawcorder-ubuntu-24.04.iso"
  headless           = true
}

build {
  sources = ["source.qemu.pawcorder"]
}
