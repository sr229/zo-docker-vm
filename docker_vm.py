#!/usr/bin/env python3
"""
Docker ARM64 VM Manager for Zo Computer.

Consolidates the QEMU launch logic, configuration parsing, and subcommand
handling into a single Python entry point.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

WORKSPACE = Path("/home/workspace")
CONFIG_FILE = WORKSPACE / ".docker-vm.json"
PID_FILE = Path("/var/run/docker-vm.pid")
LOG_FILE = Path("/dev/shm/docker-vm.log")
QEMU_BIN = "qemu-system-aarch64"
EFI_PFLASH = WORKSPACE / "efi-pflash.raw"
CLOUD_INIT_ISO = WORKSPACE / "cloud-init.iso"
SSH_TUNNEL_PORT = 2222
SSH_GUEST_PORT = 22

DEFAULT_CONFIG = {
    "image_path": str(WORKSPACE / ".docker-vm" / "image.qcow2"),
    "log_file": str(LOG_FILE),
    "port_forwards": {
        str(SSH_TUNNEL_PORT): str(SSH_GUEST_PORT),
    },
}


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        return DEFAULT_CONFIG.copy()
    try:
        with CONFIG_FILE.open("r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"Error reading {CONFIG_FILE}: {e}", file=sys.stderr)
        sys.exit(1)


def save_config(config: dict) -> None:
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with CONFIG_FILE.open("w") as f:
        json.dump(config, f, indent=2)


def ensure_config() -> dict:
    config = load_config()
    if not CONFIG_FILE.exists():
        save_config(config)
        print(f"Created default configuration at {CONFIG_FILE}")
    return config


def read_pid() -> int | None:
    if not PID_FILE.exists():
        return None
    try:
        return int(PID_FILE.read_text().strip())
    except (ValueError, OSError):
        return None


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def get_qemu_pid() -> int | None:
    pid = read_pid()
    if pid and pid_alive(pid):
        return pid
    try:
        result = subprocess.run(
            ["pgrep", "-f", "qemu-system-aarch64"],
            capture_output=True,
            text=True,
            check=False,
        )
        pids = [int(p) for p in result.stdout.split() if p.strip()]
        return pids[0] if pids else None
    except (ValueError, OSError):
        return None


def build_qemu_command(config: dict) -> list[str]:
    image_path = Path(config["image_path"])
    log_file = Path(config["log_file"])
    port_forwards = config.get("port_forwards", {})

    hostfwd_args = ""
    for host_port, guest_port in port_forwards.items():
        hostfwd_args += f",hostfwd=tcp::{host_port}-:{guest_port}"
        print(f"Forwarding host {host_port} -> guest {guest_port}")

    log_file.parent.mkdir(parents=True, exist_ok=True)

    return [
        QEMU_BIN,
        "-machine", "virt",
        "-cpu", "cortex-a57",
        "-smp", "4",
        "-m", "4G",
        "-drive", f"if=pflash,format=raw,readonly=on,file={EFI_PFLASH}",
        "-drive", f"if=virtio,format=qcow2,file={image_path}",
        "-drive", f"if=virtio,format=raw,file={CLOUD_INIT_ISO}",
        f"-netdev user,id=net0{hostfwd_args}",
        "-device", "virtio-net-device,netdev=net0",
        "-nographic",
        "-serial", "mon:stdio",
        "-D", str(log_file),
    ]


def cmd_start(args) -> int:
    config = ensure_config()
    pid = get_qemu_pid()
    if pid:
        print(f"Docker VM is already running (PID {pid}).")
        return 0

    image_path = Path(config["image_path"])
    if not image_path.exists():
        print(f"Error: VM image not found at {image_path}. Run 'docker-vm destroy' to set up a fresh VM.")
        return 1

    cmd = build_qemu_command(config)
    print("Starting QEMU...")
    log_path = Path(config["log_file"])
    log_handle = log_path.open("ab")
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
        )
    finally:
        log_handle.close()

    try:
        PID_FILE.parent.mkdir(parents=True, exist_ok=True)
        PID_FILE.write_text(str(proc.pid))
    except OSError as e:
        print(f"Warning: could not write PID file: {e}", file=sys.stderr)

    print(f"QEMU started with PID {proc.pid}. Logs: {config['log_file']}")
    return 0


def cmd_stop(args) -> int:
    pid = get_qemu_pid()
    if not pid:
        print("Docker VM is not running.")
        if PID_FILE.exists():
            PID_FILE.unlink()
        return 0

    print(f"Stopping QEMU (PID {pid})...")
    try:
        os.kill(pid, 15)  # SIGTERM
    except OSError as e:
        print(f"Error sending SIGTERM: {e}", file=sys.stderr)

    for _ in range(20):
        if not pid_alive(pid):
            break
        time.sleep(0.5)
    else:
        try:
            os.kill(pid, 9)
        except OSError:
            pass

    if PID_FILE.exists():
        PID_FILE.unlink()
    print("Docker VM stopped.")
    return 0


def cmd_restart(args) -> int:
    cmd_stop(args)
    time.sleep(1)
    return cmd_start(args)


def cmd_status(args) -> int:
    pid = get_qemu_pid()
    if not pid:
        print("Docker VM is not running.")
        return 1

    print(f"Docker VM is running (PID {pid}).")
    try:
        result = subprocess.run(
            ["docker", "-H", f"ssh://debian@localhost:{SSH_TUNNEL_PORT}", "info"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            print("Docker daemon: healthy")
            return 0
        print("Docker daemon: not yet responding")
        return 1
    except (subprocess.TimeoutExpired, FileNotFoundError):
        print("Docker daemon: not yet responding")
        return 1


def cmd_shell(args) -> int:
    cmd = [
        "ssh",
        "-p", str(SSH_TUNNEL_PORT),
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "debian@localhost",
    ]
    os.execvp("ssh", cmd)
    return 0


def cmd_docker(args) -> int:
    docker_args = args.docker_args or []
    full_cmd = ["docker", "-H", f"ssh://debian@localhost:{SSH_TUNNEL_PORT}", *docker_args]
    return subprocess.call(full_cmd)


def cmd_pf_add(args) -> int:
    config = ensure_config()
    forwards = config.setdefault("port_forwards", {})

    host_port = str(args.host)
    guest_port = str(args.guest)

    if host_port in forwards:
        print(f"Host port {host_port} is already forwarded to guest {forwards[host_port]}.")
        return 0

    forwards[host_port] = guest_port
    save_config(config)
    print(f"Added forward: host {host_port} -> guest {guest_port}")
    print("Run 'docker-vm restart' to apply the change.")
    return 0


def cmd_pf_rm(args) -> int:
    config = ensure_config()
    forwards = config.get("port_forwards", {})

    host_port = str(args.host)
    if host_port not in forwards:
        print(f"Host port {host_port} is not currently forwarded.")
        return 0

    if int(host_port) == SSH_TUNNEL_PORT:
        print(f"Port {SSH_TUNNEL_PORT} is reserved for the SSH tunnel and cannot be removed.")
        return 1

    del forwards[host_port]
    save_config(config)
    print(f"Removed forward for host port {host_port}.")
    print("Run 'docker-vm restart' to apply the change.")
    return 0


def cmd_resize(args) -> int:
    config = load_config()
    image_path = Path(config["image_path"])
    if not image_path.exists():
        print(f"Error: VM image not found at {image_path}.")
        return 1

    result = subprocess.run(["qemu-img", "resize", str(image_path), args.size], check=False)
    return result.returncode


def cmd_destroy(args) -> int:
    cmd_stop(args)
    config = load_config()
    image_path = Path(config["image_path"])

    if image_path.exists():
        confirm = args.yes or input(f"Delete VM image at {image_path}? [y/N] ").lower() == "y"
        if confirm:
            shutil.rmtree(image_path.parent, ignore_errors=True)
            print("VM image removed.")

    if CONFIG_FILE.exists():
        confirm = args.yes or input(f"Delete config at {CONFIG_FILE}? [y/N] ").lower() == "y"
        if confirm:
            CONFIG_FILE.unlink()
            print("Configuration removed.")

    print("Docker VM destroyed.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="docker-vm",
        description="Manage a Docker-in-VM setup on Zo Computer.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("start", help="Initialize and start the VM")
    sub.add_parser("stop", help="Stop the VM")
    sub.add_parser("restart", help="Restart the VM")
    sub.add_parser("status", help="Check if Docker is healthy")
    sub.add_parser("shell", help="SSH into the guest VM")

    p_docker = sub.add_parser("docker", help="Run a docker command")
    p_docker.add_argument("docker_args", nargs=argparse.REMAINDER, help="Arguments to pass to docker")

    p_pf_add = sub.add_parser("pf-add", help="Add a port forward")
    p_pf_add.add_argument("host", type=int, help="Host port")
    p_pf_add.add_argument("guest", type=int, help="Guest port")

    p_pf_rm = sub.add_parser("pf-rm", help="Remove a port forward")
    p_pf_rm.add_argument("host", type=int, help="Host port to remove")

    p_resize = sub.add_parser("resize", help="Resize the VM disk")
    p_resize.add_argument("size", help="New size (e.g., 100G)")

    p_destroy = sub.add_parser("destroy", help="Wipe everything and start over")
    p_destroy.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompts")

    return parser


COMMANDS = {
    "start": cmd_start,
    "stop": cmd_stop,
    "restart": cmd_restart,
    "status": cmd_status,
    "shell": cmd_shell,
    "docker": cmd_docker,
    "pf-add": cmd_pf_add,
    "pf-rm": cmd_pf_rm,
    "resize": cmd_resize,
    "destroy": cmd_destroy,
}


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return COMMANDS[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
