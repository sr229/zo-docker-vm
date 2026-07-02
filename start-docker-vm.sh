#!/bin/bash
# Docker ARM64 VM Startup Script for Zo Computer

cd /home/workspace

# Load configuration from JSON
CONFIG_FILE=".docker-vm.json"
if [ ! -f "$CONFIG_FILE" ]; then
    echo "Error: Configuration file $CONFIG_FILE not found. Please run 'docker-vm start' first."
    exit 1
fi

# Extract values from JSON using grep/sed to avoid dependencies
IMAGE_PATH=$(grep -oP '"image_path":\s*"\K[^"]+' "$CONFIG_FILE")
LOG_FILE=$(grep -oP '"log_file":\s*"\K[^"]+' "$CONFIG_FILE")

# Build hostfwd arguments dynamically from port_forwards
HOSTFWD_ARGS=""
PF_BLOCK=$(grep -A 20 '"port_forwards"' "$CONFIG_FILE" | tail -n +2)
if [ -n "$PF_BLOCK" ]; then
  echo "$PF_BLOCK" | grep -oP '"[0-9]+":\s*"[0-9]+"' | while IFS= read -r line; do
    GUEST=$(echo "$line" | grep -oP '"\K[0-9]+(?=":)')
    HOST=$(echo "$line" | grep -oP ':\s*"\K[0-9]+(?=")')
    if [ -n "$GUEST" ] && [ -n "$HOST" ]; then
      HOSTFWD_ARGS="${HOSTFWD_ARGS},hostfwd=tcp::${HOST}-:${GUEST}"
      echo "Forwarding host $HOST -> guest $GUEST"
    fi
  done
fi

# Write QEMU logs to a file (-D is the global QEMU debug log)
echo "Starting QEMU..."
exec qemu-system-aarch64 \
  -machine virt \
  -cpu cortex-a57 \
  -smp 4 \
  -m 4G \
  -drive if=pflash,format=raw,readonly=on,file=/home/workspace/efi-pflash.raw \
  -drive if=virtio,format=qcow2,file="$IMAGE_PATH" \
  -drive if=virtio,format=raw,file=/home/workspace/cloud-init.iso \
  -netdev user,id=net0${HOSTFWD_ARGS} \
  -device virtio-net-device,netdev=net0 \
  -nographic -serial mon:stdio 2>&1
