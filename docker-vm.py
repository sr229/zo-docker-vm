#!/usr/bin/env python3
"""
Docker ARM64 VM Manager for Zo Computer.

A single-file, self-contained CLI for managing a Docker-in-QEMU VM on
top of gVisor. Avoids shell-out wherever a dedicated Python library
is available; the only external commands invoked are:

  * qemu-img      (disk resize - no pure-Python qcow2 writer)
  * cloud-localds (cloud-init ISO - hybrid ISO 9660 + FAT layout;
                   no stdlib equivalent)

External Python dependencies (both already on the base image):
  * psutil   - process management
  * requests - HTTP downloads with progress
"""
import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

try:
    import psutil
except ImportError:
    psutil = None

try:
    import requests
except ImportError:
    requests = None


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

WORKSPACE = Path("/home/workspace")
CONFIG_FILE = WORKSPACE / ".docker-vm.json"
PID_FILE = Path("/var/run/docker-vm.pid")
LOG_FILE = Path("/dev/shm/docker-vm.log")
QEMU_BIN = "qemu-system-aarch64"
QEMU_PROCESS_NAME = "qemu-system-aarch64"
EFI_PFLASH = WORKSPACE / "efi-pflash.raw"
EFI_SOURCE = Path("/usr/share/qemu-efi-aarch64/QEMU_EFI.fd")
CLOUD_INIT_ISO = WORKSPACE / "cloud-init.iso"
USER_DATA_FILE = WORKSPACE / "user-data"

SSH_TUNNEL_PORT = 2222
SSH_GUEST_PORT = 22
SSH_USER = "debian"
SSH_PASS = "debian"
DOCKER_HOST_URL = f"ssh://{SSH_USER}@localhost:{SSH_TUNNEL_PORT}"

BOOT_TIMEOUT_S = 90
BOOT_POLL_INTERVAL_S = 1.0

DEFAULT_IMAGE_URL = (
    "https://cloud.debian.org/images/cloud/bookworm/latest/"
    "debian-12-generic-arm64.qcow2"
)
DEFAULT_DISK_SIZE = "50G"


DEFAULT_CONFIG: dict[str, Any] = {
    "image_path": str(WORKSPACE / ".docker-vm" / "image.qcow2"),
    "image_url": DEFAULT_IMAGE_URL,
    "disk_size": DEFAULT_DISK_SIZE,
    "log_file": str(LOG_FILE),
    "port_forwards": {
        str(SSH_TUNNEL_PORT): str(SSH_GUEST_PORT),
    },
}


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def load_config() -> dict[str, Any]:
    if not CONFIG_FILE.exists():
        return DEFAULT_CONFIG.copy()
    try:
        with CONFIG_FILE.open("r") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"Error reading {CONFIG_FILE}: {e}", file=sys.stderr)
        sys.exit(1)
    merged = DEFAULT_CONFIG.copy()
    merged.update(data or {})
    return merged


def save_config(config: dict[str, Any]) -> None:
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with CONFIG_FILE.open("w") as f:
        json.dump(config, f, indent=2)


def ensure_config() -> dict[str, Any]:
    if CONFIG_FILE.exists():
        return load_config()
    config = DEFAULT_CONFIG.copy()
    save_config(config)
    print(f"Created default configuration at {CONFIG_FILE}")
    return config


# ---------------------------------------------------------------------------
# Process management (psutil-based)
# ---------------------------------------------------------------------------

def _pid_alive(pid: int) -> bool:
    if psutil is not None:
        return psutil.pid_exists(pid)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _qemu_processes() -> list:
    if psutil is None:
        return []
    found = []
    for proc in psutil.process_iter(["name", "cmdline"]):
        try:
            name = (proc.info.get("name") or "").lower()
            cmdline = " ".join(proc.info.get("cmdline") or [])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if QEMU_PROCESS_NAME in name or QEMU_PROCESS_NAME in cmdline:
            found.append(proc)
    return found


def get_qemu_pid() -> int | None:
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text().strip())
        except (ValueError, OSError):
            pid = None
        if pid and _pid_alive(pid):
            return pid
        try:
            PID_FILE.unlink()
        except OSError:
            pass
    procs = _qemu_processes()
    return procs[0].pid if procs else None


def write_pid_file(pid: int) -> None:
    try:
        PID_FILE.parent.mkdir(parents=True, exist_ok=True)
        PID_FILE.write_text(str(pid))
    except OSError as e:
        print(f"Warning: could not write {PID_FILE}: {e}", file=sys.stderr)


def clear_pid_file() -> None:
    try:
        if PID_FILE.exists():
            PID_FILE.unlink()
    except OSError:
        pass


def stop_qemu(timeout_s: float = 10.0) -> bool:
    pid = get_qemu_pid()
    if pid is None:
        clear_pid_file()
        return True
    print(f"Stopping QEMU (PID {pid})...")
    if psutil is not None:
        try:
            psutil.Process(pid).terminate()
        except psutil.NoSuchProcess:
            clear_pid_file()
            return True
    else:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    deadline = time.time() + timeout_s
    while time.time() < deadline and _pid_alive(pid):
        time.sleep(0.25)
    if _pid_alive(pid):
        print("QEMU did not exit in time; sending SIGKILL...")
        if psutil is not None:
            try:
                psutil.Process(pid).kill()
            except psutil.NoSuchProcess:
                pass
        else:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
    clear_pid_file()
    return not _pid_alive(pid)


# ---------------------------------------------------------------------------
# Image & cloud-init preparation
# ---------------------------------------------------------------------------

CLOUD_INIT_USER_DATA = (
    "#cloud-config\n"
    f"password: {SSH_PASS}\n"
    "chpasswd: { expire: False }\n"
    "ssh_pwauth: True\n"
)


def _stream_download(resp, dest: Path, total: int) -> None:
    downloaded = 0
    chunk = 256 * 1024
    for piece in resp.iter_content(chunk_size=chunk) if requests is not None else iter(lambda: resp.read(chunk), b""):
        if not piece:
            continue
        dest.write(piece)
        downloaded += len(piece)
        if total:
            pct = downloaded * 100 / total
            sys.stdout.write(
                f"\rDownloading {dest.name}: {pct:5.1f}% "
                f"({downloaded // (1024 * 1024)} MiB / "
                f"{total // (1024 * 1024)} MiB)"
            )
            sys.stdout.flush()
    if total:
        sys.stdout.write("\n")
        sys.stdout.flush()


def download_image(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if requests is not None:
        with requests.get(url, stream=True, timeout=30, allow_redirects=True) as resp:
            resp.raise_for_status()
            total = int(resp.headers.get("Content-Length") or 0)
            with dest.open("wb") as f:
                # Inline streamer that writes to an open handle
                downloaded = 0
                chunk = 256 * 1024
                for piece in resp.iter_content(chunk_size=chunk):
                    if not piece:
                        continue
                    f.write(piece)
                    downloaded += len(piece)
                    if total:
                        pct = downloaded * 100 / total
                        sys.stdout.write(
                            f"\rDownloading {dest.name}: {pct:5.1f}% "
                            f"({downloaded // (1024 * 1024)} MiB / "
                            f"{total // (1024 * 1024)} MiB)"
                        )
                        sys.stdout.flush()
                if total:
                    sys.stdout.write("\n")
                    sys.stdout.flush()
        return
    req = urllib.request.Request(url, headers={"User-Agent": "zo-docker-vm/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp, dest.open("wb") as f:
        total = int(resp.headers.get("Content-Length") or 0)
        downloaded = 0
        chunk = 256 * 1024
        while True:
            piece = resp.read(chunk)
            if not piece:
                break
            f.write(piece)
            downloaded += len(piece)
            if total:
                pct = downloaded * 100 / total
                sys.stdout.write(
                    f"\rDownloading {dest.name}: {pct:5.1f}% "
                    f"({downloaded // (1024 * 1024)} MiB / "
                    f"{total // (1024 * 1024)} MiB)"
                )
                sys.stdout.flush()
        if total:
            sys.stdout.write("\n")
            sys.stdout.flush()


def build_cloud_init_iso(iso_path: Path) -> None:
    iso_path.parent.mkdir(parents=True, exist_ok=True)
    USER_DATA_FILE.write_text(CLOUD_INIT_USER_DATA)
    if not shutil.which("cloud-localds"):
        print("Error: cloud-localds is not installed.", file=sys.stderr)
        sys.exit(1)
    subprocess.run(
        ["cloud-localds", str(iso_path), str(USER_DATA_FILE)],
        check=True,
    )


def ensure_efi_pflash() -> Path:
    if not EFI_SOURCE.exists():
        print(f"Error: UEFI firmware not found at {EFI_SOURCE}", file=sys.stderr)
        sys.exit(1)
    EFI_PFLASH.parent.mkdir(parents=True, exist_ok=True)
    if not EFI_PFLASH.exists():
        shutil.copyfile(EFI_SOURCE, EFI_PFLASH)
    return EFI_PFLASH


def qemu_img_resize(image: Path, size: str) -> None:
    if not shutil.which("qemu-img"):
        print("Error: qemu-img is not installed.", file=sys.stderr)
        sys.exit(1)
    subprocess.run(["qemu-img", "resize", str(image), size], check=True)


# ---------------------------------------------------------------------------
# QEMU launch
# ---------------------------------------------------------------------------

def build_qemu_command(config: dict[str, Any]) -> list[str]:
    image_path = Path(config["image_path"])
    log_file = Path(config["log_file"])
    port_forwards = config.get("port_forwards", {})

    hostfwd_args = ""
    for host_port, guest_port in port_forwards.items():
        hostfwd_args += f",hostfwd=tcp::{host_port}-:{guest_port}"
        print(f"  forward: host {host_port} -> guest {guest_port}")

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
        "-netdev", f"user,id=net0{hostfwd_args}",
        "-device", "virtio-net-device,netdev=net0",
        "-nographic",
        "-serial", "mon:stdio",
        "-D", str(log_file),
    ]


def _wait_for_ssh(port: int, timeout_s: float = BOOT_TIMEOUT_S) -> bool:
    """Poll the SSH port until the guest is accepting connections."""
    deadline = time.time() + timeout_s
    attempt = 0
    started = time.time()
    while time.time() < deadline:
        attempt += 1
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1.0):
                return True
        except OSError:
            pass
        if attempt % 10 == 0:
            elapsed = int(time.time() - started)
            print(f"  still waiting for guest SSH... ({elapsed}s)")
        time.sleep(BOOT_POLL_INTERVAL_S)
    return False


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def cmd_init(args: argparse.Namespace) -> int:
    config = ensure_config()
    image_path = Path(config["image_path"])
    if image_path.exists() and not args.force:
        print("A Docker VM environment already exists.")
        print("Run 'docker-vm destroy' to start over, or 'docker-vm start' to boot it.")
        return 0

    if not EFI_SOURCE.exists():
        print(f"Error: UEFI firmware not found at {EFI_SOURCE}", file=sys.stderr)
        return 1

    image_url = args.image or config.get("image_url", DEFAULT_IMAGE_URL)
    disk_size = args.size or config.get("disk_size", DEFAULT_DISK_SIZE)

    if not image_path.exists():
        print(f"Downloading {image_url}")
        download_image(image_url, image_path)

    print("Preparing EFI pflash...")
    ensure_efi_pflash()

    print("Building cloud-init ISO...")
    build_cloud_init_iso(CLOUD_INIT_ISO)

    print(f"Resizing image to {disk_size}...")
    qemu_img_resize(image_path, disk_size)

    config["image_path"] = str(image_path)
    config["image_url"] = image_url
    config["disk_size"] = disk_size
    save_config(config)

    print("Environment initialized. Run 'docker-vm start' to boot.")
    return 0


def cmd_start(args: argparse.Namespace) -> int:
    config = ensure_config()
    image_path = Path(config["image_path"])
    if not image_path.exists():
        print(f"Error: VM image not found at {image_path}.")
        print("Run 'docker-vm init' to download and prepare an image.")
        return 1

    if not EFI_PFLASH.exists():
        print("EFI pflash missing; rebuilding...")
        ensure_efi_pflash()

    if not CLOUD_INIT_ISO.exists():
        print("cloud-init ISO missing; rebuilding...")
        build_cloud_init_iso(CLOUD_INIT_ISO)

    pid = get_qemu_pid()
    if pid is not None:
        print(f"Docker VM is already running (PID {pid}).")
        return 0

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

    write_pid_file(proc.pid)
    print(f"QEMU started with PID {proc.pid}. Logs: {log_path}")

    if args.wait:
        print(f"Waiting up to {BOOT_TIMEOUT_S}s for guest SSH on port {SSH_TUNNEL_PORT}...")
        if _wait_for_ssh(SSH_TUNNEL_PORT):
            print("Docker VM is up and accepting SSH connections.")
            return 0
        print("Warning: guest SSH did not come up in time. Check logs.")
        return 1
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    stop_qemu()
    print("Docker VM stopped.")
    return 0


def cmd_restart(args: argparse.Namespace) -> int:
    cmd_stop(args)
    time.sleep(1)
    start_args = argparse.Namespace(wait=True)
    return cmd_start(start_args)


def cmd_status(args: argparse.Namespace) -> int:
    pid = get_qemu_pid()
    if pid is None:
        print("Docker VM is not running.")
        return 1
    print(f"Docker VM is running (PID {pid}).")
    try:
        with socket.create_connection(("127.0.0.1", SSH_TUNNEL_PORT), timeout=2.0):
            print("SSH tunnel: open")
    except OSError:
        print("SSH tunnel: not yet accepting connections")
        return 1
    print("Docker daemon: reachable via")
    print(f"  export DOCKER_HOST={DOCKER_HOST_URL}")
    return 0


def cmd_shell(args: argparse.Namespace) -> int:
    cmd = [
        "ssh",
        "-p", str(SSH_TUNNEL_PORT),
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        f"{SSH_USER}@localhost",
    ]
    os.execvp("ssh", cmd)
    return 0


def cmd_docker(args: argparse.Namespace) -> int:
    docker_args = list(args.docker_args or [])
    if not docker_args:
        print("Usage: docker-vm docker <docker args...>", file=sys.stderr)
        return 1
    env = os.environ.copy()
    env["DOCKER_HOST"] = DOCKER_HOST_URL
    return subprocess.call(["docker", *docker_args], env=env)


def cmd_logs(args: argparse.Namespace) -> int:
    config = ensure_config()
    log_path = Path(config["log_file"])
    if not log_path.exists():
        print(f"Log file not found at {log_path}.")
        return 1
    if args.follow:
        # Pure-Python tail: poll the file for new bytes.
        with log_path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            print(f"Tailing {log_path} (Ctrl-C to stop)...")
            try:
                while True:
                    line = f.readline()
                    if not line:
                        time.sleep(0.25)
                        continue
                    sys.stdout.write(line.decode("utf-8", errors="replace"))
                    sys.stdout.flush()
            except KeyboardInterrupt:
                pass
        return 0
    print(log_path.read_text(errors="replace"))
    return 0


def cmd_pf_add(args: argparse.Namespace) -> int:
    config = ensure_config()
    forwards = config.setdefault("port_forwards", {})
    host_port = str(args.host)
    guest_port = str(args.guest)
    if host_port in forwards:
        print(f"Host port {host_port} is already forwarded to guest {forwards[host_port]}.")
        return 0
    forwards[host_port] = guest_port
    save_config(config)
    print(f"Added forward: host {host_port} -> guest {guest_port}.")
    print("Run 'docker-vm restart' to apply the change.")
    return 0


def cmd_pf_rm(args: argparse.Namespace) -> int:
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


def cmd_pf_list(args: argparse.Namespace) -> int:
    config = ensure_config()
    forwards = config.get("port_forwards", {})
    if not forwards:
        print("No port forwards configured.")
        return 0
    print(f"{'HOST':<8} {'GUEST':<8}")
    print(f"{'-' * 8} {'-' * 8}")
    for host, guest in forwards.items():
        print(f"{host:<8} {guest:<8}")
    return 0


def cmd_resize(args: argparse.Namespace) -> int:
    config = load_config()
    image_path = Path(config["image_path"])
    if not image_path.exists():
        print(f"Error: VM image not found at {image_path}.")
        return 1
    qemu_img_resize(image_path, args.size)
    config["disk_size"] = args.size
    save_config(config)
    print(f"Resized {image_path} to {args.size}.")
    print("Run 'docker-vm restart' to boot the resized image.")
    return 0


def cmd_destroy(args: argparse.Namespace) -> int:
    stop_qemu()
    config = load_config()
    image_path = Path(config["image_path"])

    paths_to_remove = []
    for p in [
        image_path,
        CLOUD_INIT_ISO,
        USER_DATA_FILE,
        EFI_PFLASH,
        PID_FILE,
        CONFIG_FILE,
    ]:
        if p.exists():
            paths_to_remove.append(p)

    if not paths_to_remove:
        print("Nothing to destroy.")
        return 0

    print("The following files will be removed:")
    for p in paths_to_remove:
        print(f"  {p}")
    if not args.yes:
        try:
            ans = input("Continue? [y/N] ").strip().lower()
        except EOFError:
            ans = "n"
        if ans != "y":
            print("Aborted.")
            return 1

    for p in paths_to_remove:
        try:
            if p.is_dir():
                shutil.rmtree(p)
            else:
                p.unlink()
            print(f"Removed {p}")
        except OSError as e:
            print(f"Error removing {p}: {e}", file=sys.stderr)

    print("Docker VM destroyed.")
    return 0


def cmd_env(args: argparse.Namespace) -> int:
    """Print shell commands to set up the environment."""
    print(f"export DOCKER_HOST={DOCKER_HOST_URL}")
    print(f"export DOCKER_VM_WORKSPACE={WORKSPACE}")
    return 0


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="docker-vm",
        description="Manage a Docker-in-QEMU VM on Zo Computer.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="Download image and prepare cloud-init ISO")
    p_init.add_argument("--image", help="Image URL (default: Debian cloud bookworm arm64)")
    p_init.add_argument("--size", help="Disk size, e.g. 100G (default: 50G)")
    p_init.add_argument("--force", action="store_true", help="Re-run even if image exists")
    p_init.set_defaults(func=cmd_init)

    p_start = sub.add_parser("start", help="Boot the VM")
    p_start.add_argument("--wait", action="store_true", help="Wait until guest SSH is ready")
    p_start.set_defaults(func=cmd_start)

    sub.add_parser("stop", help="Stop the VM").set_defaults(func=cmd_stop)
    sub.add_parser("restart", help="Restart the VM").set_defaults(func=cmd_restart)
    sub.add_parser("status", help="Check VM and SSH tunnel status").set_defaults(func=cmd_status)
    sub.add_parser("shell", help="Open an interactive SSH shell in the guest").set_defaults(func=cmd_shell)
    sub.add_parser("env", help="Print environment variables for the host shell").set_defaults(func=cmd_env)

    p_docker = sub.add_parser("docker", help="Run a docker command against the VM")
    p_docker.add_argument("docker_args", nargs=argparse.REMAINDER, help="Arguments for docker")
    p_docker.set_defaults(func=cmd_docker)

    p_logs = sub.add_parser("logs", help="Show or tail the QEMU log")
    p_logs.add_argument("-f", "--follow", action="store_true", help="Follow log output")
    p_logs.set_defaults(func=cmd_logs)

    p_pf = sub.add_parser("pf", help="Manage port forwards")
    p_pf_sub = p_pf.add_subparsers(dest="pf_command", required=True)

    p_pf_add = p_pf_sub.add_parser("add", help="Add a port forward")
    p_pf_add.add_argument("host", type=int, help="Host port")
    p_pf_add.add_argument("guest", type=int, help="Guest port")
    p_pf_add.set_defaults(func=cmd_pf_add)

    p_pf_rm = p_pf_sub.add_parser("rm", help="Remove a port forward")
    p_pf_rm.add_argument("host", type=int, help="Host port")
    p_pf_rm.set_defaults(func=cmd_pf_rm)

    p_pf_ls = p_pf_sub.add_parser("ls", help="List port forwards")
    p_pf_ls.set_defaults(func=cmd_pf_list)

    p_resize = sub.add_parser("resize", help="Resize the VM disk")
    p_resize.add_argument("size", help="New size, e.g. 100G")
    p_resize.set_defaults(func=cmd_resize)

    p_destroy = sub.add_parser("destroy", help="Remove all VM artifacts")
    p_destroy.add_argument("-y", "--yes", action="store_true", help="Skip confirmation")
    p_destroy.set_defaults(func=cmd_destroy)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
