#!/bin/bash
# Docker ARM64 VM Startup Script for Zo Computer

cd /home/workspace

# Load configuration from JSON
CONFIG_FILE=".docker-vm.json"
if [ ! -f "$CONFIG_FILE" ]; then
    echo "Error: Configuration file $CONFIG_FILE not found!"
    exit 1
fi

IMAGE=$(grep -oP '"image_path":\s*"\K[^"]+' "$CONFIG_FILE")
BIOS="/usr/share/qemu-efi-aarch64/QEMU_EFI.fd"

if [ ! -f "$IMAGE" ]; then
    echo "Error: Disk image $IMAGE not found!"
    exit 1
fi

# Build the hostfwd string from the JSON port_forwards object
# We use jq if available, but to keep it zero-dep we'll use a small python snippet
HOSTFWD=$(python3 -c "
import json
with open('$CONFIG_FILE') as f:
    config = json.load(f)
    pf = config.get('port_forwards', {})
    print(','.join([f'tcp::{host}-:{guest}' for guest, host in pf.items()]))
")

exec qemu-system-aarch64 \
  -machine virt \
  -cpu cortex-a57 \
  -m 2G \
  -bios "$BIOS" \
  -drive if=none,file="$IMAGE",id=hd0 \
  -device virtio-blk-device,drive=hd0 \
  -drive if=none,file=cloud-init.iso,id=cd0 \
  -device virtio-scsi-device \
  -device scsi-cd,drive=cd0 \
  -netdev user,id=net0,hostfwd=$HOSTFWD \
  -device virtio-net-device,netdev=net0 \
  -nographic
