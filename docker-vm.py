#!/usr/bin/env python3
"""
Docker ARM64 VM Manager.

A single-file, self-contained CLI for managing a Docker-in-QEMU VM on
top of gVisor. Avoids shell-out wherever a dedicated Python library
is available; the only external commands invoked are:

  * qemu-img      (disk resize - no pure-Python qcow2 writer)
  * cloud-localds (cloud-init ISO - hybrid ISO 9660 + FAT layout;
                   no stdlib equivalent)
  * qemu-system-aarch64 + ssh + docker (forwarded to the user as-is)

External Python dependencies (both optional; stdlib fallbacks exist):
  * psutil   - process management
  * requests - HTTP downloads with progress

All persistent state lives under a single hidden directory (see
_paths_for_state()), so nothing is sprinkled into the working directory
or $HOME. The location is overridable via $DOCKER_VM_STATE_DIR and
defaults to $XDG_STATE_HOME/docker-vm, or ~/.local/state/docker-vm.
"""
import argparse
import json
import os
import shutil
import shlex
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
# Paths & configuration discovery
# ---------------------------------------------------------------------------

# Well-known UEFI firmware locations, in priority order. The first one that
# exists is used. Override with $DOCKER_VM_EFI.
EFI_CANDIDATES = (
    Path("/usr/share/qemu-efi-aarch64/QEMU_EFI.fd"),  # Debian/Ubuntu
    Path("/usr/share/AAVMF/AAVMF_CODE.fd"),          # Fedora/RHEL/Arch (aarch64)
    Path("/opt/homebrew/share/qemu/edk2-aarch64-code.fd"),  # macOS Homebrew
    Path("/usr/local/share/qemu/edk2-aarch64-code.fd"),      # BSD/Homebrew (linux)
)

APP_DIR_NAME = "docker-vm"


def _state_dir() -> Path:
    """Return the directory for all persistent state.

    Resolution order:
      1. $DOCKER_VM_STATE_DIR (if set)
      2. $XDG_STATE_HOME/docker-vm
      3. ~/.local/state/docker-vm
    """
    override = os.environ.get("DOCKER_VM_STATE_DIR")
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_STATE_HOME")
    if xdg:
        return Path(xdg).expanduser() / APP_DIR_NAME
    return Path.home() / ".local" / "state" / APP_DIR_NAME


def _workspace() -> Path:
    """Return the host workspace path (used as the guest mount target).

    Resolution order:
      1. $DOCKER_VM_WORKSPACE  (preferred override)
      2. $DOCKER_HOST_WORKSPACE  (back-compat with earlier versions)
      3. /home/workspace  (canonical Zo Computer workspace)
    """
    for key in ("DOCKER_VM_WORKSPACE", "DOCKER_HOST_WORKSPACE"):
        val = os.environ.get(key)
        if val:
            return Path(val).expanduser()
    return Path("/home/workspace")


def _pid_file(state: Path) -> Path:
    """Pick a writable location for the PID file.

    Prefers $XDG_RUNTIME_DIR (per-user tmpfs), then the state dir.
    """
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        return Path(runtime) / f"{APP_DIR_NAME}.pid"
    return state / "docker-vm.pid"


def _ssh_key_path() -> Path:
    return STATE_DIR / "id_ed25519"


def _ensure_ssh_key() -> Path:
    """Ensure an SSH keypair exists in the state directory for VM access."""
    key_path = _ssh_key_path()
    if not key_path.exists():
        print("Generating SSH key for VM access...")
        key_path.parent.mkdir(parents=True, exist_ok=True)
        # We use -q to stay quiet, and -N "" for no passphrase.
        subprocess.run(
            ["ssh-keygen", "-t", "ed25519", "-f", str(key_path), "-N", "", "-q"],
            check=True
        )
    return key_path


# (EFI source resolution lives in _resolve_efi_source below.)


# ---------------------------------------------------------------------------
# Other constants
# ---------------------------------------------------------------------------

QEMU_BIN = "qemu-system-aarch64"
QEMU_PROCESS_NAME = "qemu-system-aarch64"
STATE_DIR = _state_dir()
PID_FILE = _pid_file(STATE_DIR)
EFI_PFLASH = STATE_DIR / "efi-pflash.raw"
CLOUD_INIT_ISO = STATE_DIR / "cloud-init.iso"
USER_DATA_FILE = STATE_DIR / "user-data"
CONFIG_FILE = STATE_DIR / "config.json"
LOG_FILE = STATE_DIR / "docker-vm.log"

SSH_TUNNEL_PORT = 2222
SSH_GUEST_PORT = 22
SSH_USER = "ubuntu"
SSH_PASS = "ubuntu"
DOCKER_HOST_URL = f"ssh://{SSH_USER}@localhost:{SSH_TUNNEL_PORT}"

# Runtime state for the currently running VM
STATE_FILE = STATE_DIR / "state.json"

BOOT_TIMEOUT_S = 300
BOOT_POLL_INTERVAL_S = 1.0

DEFAULT_IMAGE_URL = (
    "https://github.com/abiosoft/colima-core/releases/download/"
    "v0.10.4/ubuntu-24.04-minimal-cloudimg-arm64-docker.raw.gz"
)
COLIMA_IMAGE_VERSION = "v0.10.4"
COLIMA_IMAGE_SHORTHANDS = {
    "docker": f"https://github.com/abiosoft/colima-core/releases/download/{COLIMA_IMAGE_VERSION}/ubuntu-24.04-minimal-cloudimg-arm64-docker.raw.gz",
    "containerd": f"https://github.com/abiosoft/colima-core/releases/download/{COLIMA_IMAGE_VERSION}/ubuntu-24.04-minimal-cloudimg-arm64-containerd.raw.gz",
    "incus": f"https://github.com/abiosoft/colima-core/releases/download/{COLIMA_IMAGE_VERSION}/ubuntu-24.04-minimal-cloudimg-arm64-incus.raw.gz",
    "none": f"https://github.com/abiosoft/colima-core/releases/download/{COLIMA_IMAGE_VERSION}/ubuntu-24.04-minimal-cloudimg-arm64-none.raw.gz",
}
DEFAULT_DISK_SIZE = "50G"


def _default_config() -> dict[str, Any]:
    workspace = _workspace()
    return {
        "image_path": str(STATE_DIR / "image.qcow2"),
        "image_url": DEFAULT_IMAGE_URL,
        "disk_size": DEFAULT_DISK_SIZE,
        "log_file": str(LOG_FILE),
        "port_forwards": {
            str(SSH_TUNNEL_PORT): str(SSH_GUEST_PORT),
        },
        "workspace": str(workspace),
    }


# Legacy locations we recognise when migrating an old install.
_LEGACY_CONFIG_CANDIDATES = (
    Path("/home/workspace/.docker-vm.json"),
)


# ---------------------------------------------------------------------------
# Configuration & State
# ---------------------------------------------------------------------------

def load_state() -> dict[str, Any]:
    """Load runtime state (ports, etc.) of the running VM."""
    if STATE_FILE.exists():
        try:
            with STATE_FILE.open("r") as f:
                return json.load(f) or {}
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_state(state: dict[str, Any]) -> None:
    """Save runtime state."""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with STATE_FILE.open("w") as f:
        json.dump(state, f, indent=2)


def clear_state() -> None:
    """Remove runtime state file."""
    try:
        if STATE_FILE.exists():
            STATE_FILE.unlink()
    except OSError:
        pass


def _migrate_legacy_config() -> dict[str, Any] | None:
    """Pick up a config from a pre-XDG location, migrate to new state dir.

    Returns the migrated dict if a legacy config was found, else None.
    """
    for legacy in _LEGACY_CONFIG_CANDIDATES:
        if not legacy.exists():
            continue
        try:
            with legacy.open("r") as f:
                old = json.load(f) or {}
        except (json.JSONDecodeError, OSError) as e:
            print(
                f"Warning: could not read legacy config at {legacy}: {e}",
                file=sys.stderr,
            )
            continue
        new = _default_config()
        # Carry over user-touched values; remap image_path into the new
        # state dir if it pointed under the old /home/workspace/.docker-vm.
        for key in ("image_url", "disk_size", "log_file", "port_forwards"):
            if key in old:
                new[key] = old[key]
        old_image = old.get("image_path")
        if old_image:
            old_p = Path(old_image)
            # If the old image is at a non-standard location, keep it where
            # it is; otherwise relocate it to the new state dir.
            if old_p.parent == Path("/home/workspace") or old_p.parent == Path("/home/workspace/.docker-vm"):
                pass  # use new default
            else:
                new["image_path"] = old_image
        # Stash the old file (renamed) so it isn't accidentally re-migrated
        # on subsequent runs. The user can delete the .bak if they want.
        backup = legacy.with_suffix(legacy.suffix + ".bak")
        try:
            legacy.rename(backup)
        except OSError as e:
            print(
                f"Warning: could not move legacy config aside at {legacy}: {e}",
                file=sys.stderr,
            )
        return new
    return None


def load_config() -> dict[str, Any]:
    if CONFIG_FILE.exists():
        try:
            with CONFIG_FILE.open("r") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            print(f"Error reading {CONFIG_FILE}: {e}", file=sys.stderr)
            sys.exit(1)
        merged = _default_config()
        merged.update(data or {})
        return merged
    migrated = _migrate_legacy_config()
    if migrated is not None:
        migrated["_migrated"] = True
        save_config(migrated)
        print(f"Migrated legacy config into {CONFIG_FILE}")
        return migrated
    return _default_config()


def save_config(config: dict[str, Any]) -> None:
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with CONFIG_FILE.open("w") as f:
        json.dump(config, f, indent=2)


def ensure_config() -> dict[str, Any]:
    """Load the config, creating a default one if nothing exists yet.

    First run will print a single "Created" line. Migration from a legacy
    config prints its own "Migrated" line via ``load_config``.
    """
    if CONFIG_FILE.exists():
        return load_config()
    config = load_config()
    save_config(config)
    if config.get("_migrated"):
        return config
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
    config = load_config()
    image_path = str(Path(config.get("image_path", "")).expanduser())

    for proc in psutil.process_iter(["name", "cmdline"]):
        try:
            name = (proc.info.get("name") or "").lower()
            cmdline_list = proc.info.get("cmdline") or []
            cmdline = " ".join(cmdline_list)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

        # Check if it's a QEMU process and if it's using our specific image
        if QEMU_PROCESS_NAME in name or QEMU_PROCESS_NAME in cmdline:
            if image_path and any(image_path in arg for arg in cmdline_list):
                found.append(proc)
            elif not image_path:
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
        clear_state()
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
    clear_state()
    return not _pid_alive(pid)


# ---------------------------------------------------------------------------
# Image & cloud-init preparation
# ---------------------------------------------------------------------------

CLOUD_INIT_USER_DATA = (
    "#cloud-config\n"
    f"password: {SSH_PASS}\n"
    "chpasswd: { expire: False }\n"
    "ssh_pwauth: True\n"
    "groups:\n"
    "  - docker\n"
    "system_info:\n"
    "  default_user:\n"
    f"    name: {SSH_USER}\n"
    "    groups: [docker]\n"
)


def download_image(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    max_retries = 3
    for attempt in range(max_retries):
        try:
            if requests is not None:
                with requests.get(url, stream=True, timeout=60, allow_redirects=True) as resp:
                    resp.raise_for_status()
                    total = int(resp.headers.get("Content-Length") or 0)
                    with dest.open("wb") as f:
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
            else:
                req = urllib.request.Request(url, headers={"User-Agent": "zo-docker-vm/1.0"})
                # Increase timeout for the initial connection.
                # Subsequent reads have their own timeout in the loop if needed,
                # but urlopen's timeout covers the whole operation in some versions
                # or just the connection in others.
                with urllib.request.urlopen(req, timeout=60) as resp, dest.open("wb") as f:
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
                return
        except (Exception, KeyboardInterrupt) as e:
            if isinstance(e, KeyboardInterrupt):
                raise
            if attempt < max_retries - 1:
                print(f"\nDownload failed: {e}. Retrying ({attempt + 2}/{max_retries})...")
                time.sleep(2)
            else:
                print(f"\nError: Failed to download {url} after {max_retries} attempts.")
                raise


def build_cloud_init_iso(iso_path: Path) -> None:
    iso_path.parent.mkdir(parents=True, exist_ok=True)

    # Generate/ensure SSH key and inject it into user-data
    key_path = _ensure_ssh_key()
    pub_key = Path(str(key_path) + ".pub").read_text().strip()

    user_data = CLOUD_INIT_USER_DATA
    if not user_data.endswith("\n"):
        user_data += "\n"
    user_data += "ssh_authorized_keys:\n"
    user_data += f"  - {pub_key}\n"

    USER_DATA_FILE.write_text(user_data)
    if not shutil.which("cloud-localds"):
        print("Error: cloud-localds is not installed.", file=sys.stderr)
        sys.exit(1)
    subprocess.run(
        ["cloud-localds", str(iso_path), str(USER_DATA_FILE)],
        check=True,
    )


def _resolve_efi_source() -> Path:
    """Find the aarch64 UEFI firmware image, raising on failure.

    Resolution order:
      1. $DOCKER_VM_EFI  (if set, must exist)
      2. EFI_CANDIDATES  (first existing match in the well-known paths)

    Set DOCKER_VM_EFI to override, e.g.:
        DOCKER_VM_EFI=/path/to/QEMU_EFI.fd docker-vm init
    """
    override = os.environ.get("DOCKER_VM_EFI")
    if override:
        p = Path(override).expanduser()
        if p.exists():
            return p
        print(f"Error: DOCKER_VM_EFI={p} does not exist.", file=sys.stderr)
        sys.exit(1)
    for candidate in EFI_CANDIDATES:
        if candidate.exists():
            return candidate
    print(
        "Error: could not find an aarch64 UEFI firmware image. Tried:",
        file=sys.stderr,
    )
    for c in EFI_CANDIDATES:
        print(f"  - {c}", file=sys.stderr)
    print(
        "Set $DOCKER_VM_EFI to the path of a QEMU_EFI.fd / AAVMF_CODE.fd file.",
        file=sys.stderr,
    )
    sys.exit(1)


def ensure_efi_pflash() -> Path:
    """Copy the aarch64 UEFI firmware into the state dir (idempotent).

    QEMU's pflash must be a writable file it can mmap; we keep a single
    copy inside $STATE_DIR and reuse it across boots.

    On aarch64 'virt' machines, QEMU expects the pflash to be exactly 64MiB.
    """
    src = _resolve_efi_source()
    EFI_PFLASH.parent.mkdir(parents=True, exist_ok=True)

    # If it exists but is not 64MiB, we force a re-copy/pad.
    size_64mib = 64 * 1024 * 1024
    if EFI_PFLASH.exists() and EFI_PFLASH.stat().st_size != size_64mib:
        EFI_PFLASH.unlink()

    if not EFI_PFLASH.exists():
        with src.open("rb") as f_src, EFI_PFLASH.open("wb") as f_dst:
            shutil.copyfileobj(f_src, f_dst)
            current_size = f_dst.tell()
            if current_size < size_64mib:
                f_dst.write(b"\0" * (size_64mib - current_size))
            elif current_size > size_64mib:
                print(f"Warning: EFI source {src} is larger than 64MiB ({current_size} bytes). QEMU might fail.", file=sys.stderr)
    return EFI_PFLASH


def qemu_img_resize(image: Path, size: str) -> None:
    if not shutil.which("qemu-img"):
        print("Error: qemu-img is not installed.", file=sys.stderr)
        sys.exit(1)

    # QEMU and many storage backends prefer 1MiB alignment.
    # We attempt to align absolute sizes to the next MiB.
    aligned_size = size
    try:
        import re
        # Match simple absolute sizes like "50G", "1024M", "2000000".
        # Skip relative sizes like "+1G" or "-500M".
        m = re.match(r'^(\d+)([KMGTP]?)(B?)$', size, re.IGNORECASE)
        if m:
            val, unit, _ = m.groups()
            val = int(val)
            unit = unit.upper()
            mult = {
                "": 1,
                "K": 1024,
                "M": 1024**2,
                "G": 1024**3,
                "T": 1024**4,
                "P": 1024**5,
            }
            if unit in mult:
                bytes_val = val * mult[unit]
                mib = 1024 * 1024
                if bytes_val % mib != 0:
                    new_bytes = ((bytes_val + mib - 1) // mib) * mib
                    aligned_size = str(new_bytes)
    except Exception:
        pass

    subprocess.run(["qemu-img", "resize", str(image), aligned_size], check=True)


# ---------------------------------------------------------------------------
# QEMU launch
# ---------------------------------------------------------------------------

def build_qemu_command(config: dict[str, Any]) -> tuple[list[str], dict[str, int]]:
    """Build the QEMU command and resolve port collisions.

    Returns a tuple of (command_list, resolved_port_map).
    """
    image_path = Path(config["image_path"])
    log_file = Path(config["log_file"])
    port_forwards = config.get("port_forwards", {})

    resolved_forwards = {}
    hostfwd_args = ""
    for preferred_host, guest_port in port_forwards.items():
        actual_host = _find_available_port(int(preferred_host))
        resolved_forwards[str(guest_port)] = actual_host
        hostfwd_args += f",hostfwd=tcp::{actual_host}-:{guest_port}"
        if actual_host != int(preferred_host):
            print(f"  forward: host {actual_host} -> guest {guest_port} (preferred port {preferred_host} was busy)")
        else:
            print(f"  forward: host {actual_host} -> guest {guest_port}")

    log_file.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
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
    return cmd, resolved_forwards


def _is_port_available(port: int) -> bool:
    """Check if a local TCP port is available."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _find_available_port(preferred: int) -> int:
    """Find an available port, starting with the preferred one."""
    if _is_port_available(preferred):
        return preferred
    # Try a few next to it, then let OS pick
    for p in range(preferred + 1, preferred + 100):
        if _is_port_available(p):
            return p
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _check_docker_ready() -> bool:
    """Check if the Docker daemon is responding via the SSH tunnel."""
    try:
        # Use the port from the runtime state if available
        runtime_state = load_state()
        ssh_port = runtime_state.get("ssh_port", SSH_TUNNEL_PORT)
        key_path = _ssh_key_path()

        # We use 'ssh' directly to probe the Docker daemon on the guest.
        # This avoids issues with the local docker client trying to use
        # default SSH keys or prompting for passwords.
        cmd = [
            "ssh",
            "-p", str(ssh_port),
            "-i", str(key_path),
            "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "ConnectTimeout=5",
            f"{SSH_USER}@localhost",
            "docker version --format '{{.Server.Version}}'"
        ]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=15
        )
        return result.returncode == 0 and bool(result.stdout.strip())
    except Exception:
        return False


def _check_dependencies() -> None:
    """Ensure all required external commands are installed."""
    deps = {
        QEMU_BIN: "qemu-system-aarch64 (part of qemu-system-arm)",
        "qemu-img": "qemu-img (part of qemu-utils)",
        "cloud-localds": "cloud-localds (part of cloud-image-utils)",
        "ssh": "ssh (part of openssh-client)",
        "docker": "docker (part of docker.io)",
    }
    missing = []
    for cmd, pkg in deps.items():
        if not shutil.which(cmd):
            missing.append(pkg)

    if missing:
        print("Error: The following required dependencies are missing:", file=sys.stderr)
        for m in missing:
            print(f"  - {m}", file=sys.stderr)
        print("\nPlease install them using your package manager, e.g.:", file=sys.stderr)
        print("  sudo apt install qemu-system-arm qemu-utils cloud-image-utils openssh-client docker.io", file=sys.stderr)
        sys.exit(1)


def _wait_for_ready(port: int, timeout_s: float = BOOT_TIMEOUT_S) -> bool:
    """Poll the SSH port and Docker daemon until the guest is fully ready."""
    deadline = time.time() + timeout_s
    attempt = 0
    started = time.time()
    ssh_port_open = False
    while time.time() < deadline:
        attempt += 1
        if not ssh_port_open:
            try:
                # Check if the forwarded port is accepting connections.
                # This just means QEMU's user-net is up and the port is bound.
                with socket.create_connection(("127.0.0.1", port), timeout=1.0):
                    ssh_port_open = True
                    print(f"  SSH port {port} is open. Waiting for guest OS and Docker daemon...")
            except OSError:
                pass

        # We always check Docker readiness if we're in the loop, but we only
        # report success if BOTH the port is open and Docker responds.
        # Docker responds implies SSH is actually working in the guest.
        if ssh_port_open and _check_docker_ready():
            return True

        if attempt % 10 == 0:
            elapsed = int(time.time() - started)
            status = "waiting for SSH port..." if not ssh_port_open else "waiting for Docker daemon..."
            print(f"  still {status} ({elapsed}s)")

        time.sleep(BOOT_POLL_INTERVAL_S)
    return False


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def _init_vm(
    image: str | None = None,
    size: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Download image and prepare cloud-init ISO.

    Returns the updated config.
    """
    config = ensure_config()
    image_path = Path(config["image_path"])

    if image_path.exists() and not force:
        return config

    _resolve_efi_source()  # exits with a clear error if no firmware is found

    image_url = image or config.get("image_url", DEFAULT_IMAGE_URL)
    if image_url in COLIMA_IMAGE_SHORTHANDS:
        image_url = COLIMA_IMAGE_SHORTHANDS[image_url]

    disk_size = size or config.get("disk_size", DEFAULT_DISK_SIZE)

    if not image_path.exists() or force:
        if image_url.endswith(".gz"):
            gz_path = image_path.with_suffix(image_path.suffix + ".gz")
            print(f"Downloading {image_url}")
            download_image(image_url, gz_path)
            print(f"Decompressing {gz_path}...")
            import gzip
            with gzip.open(gz_path, "rb") as f_in:
                # We decompress to a temporary raw file first
                raw_path = image_path.with_suffix(".raw")
                with raw_path.open("wb") as f_out:
                    shutil.copyfileobj(f_in, f_out)

            print(f"Converting raw image to qcow2...")
            subprocess.run(
                ["qemu-img", "convert", "-f", "raw", "-O", "qcow2", str(raw_path), str(image_path)],
                check=True
            )
            raw_path.unlink()
            gz_path.unlink()
        else:
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

    return config


def cmd_init(args: argparse.Namespace) -> int:
    _check_dependencies()
    config = ensure_config()
    image_path = Path(config["image_path"])
    if image_path.exists() and not args.force:
        print("A Docker VM environment already exists.")
        print("Run 'docker-vm destroy' to start over, or 'docker-vm start' to boot it.")
        return 0

    _init_vm(image=args.image, size=args.size, force=args.force)
    print("Environment initialized. Run 'docker-vm start' to boot.")
    return 0


def cmd_start(args: argparse.Namespace) -> int:
    _check_dependencies()
    config = ensure_config()
    image_path = Path(config["image_path"])
    if not image_path.exists():
        print(f"VM image not found at {image_path}. Initializing...")
        config = _init_vm()

    if not EFI_PFLASH.exists():
        print("EFI pflash missing; rebuilding...")
        ensure_efi_pflash()

    if not CLOUD_INIT_ISO.exists() or not _ssh_key_path().exists():
        print("cloud-init ISO or SSH key missing; rebuilding...")
        build_cloud_init_iso(CLOUD_INIT_ISO)

    wait = not getattr(args, "no_wait", False)

    pid = get_qemu_pid()
    if pid is not None:
        print(f"Docker VM is already running (PID {pid}).")
        if wait:
            if _check_docker_ready():
                print("Docker daemon is already ready.")
            else:
                runtime_state = load_state()
                ssh_port = runtime_state.get("ssh_port", SSH_TUNNEL_PORT)
                print("Docker VM is running but daemon is not yet ready. Waiting...")
                if _wait_for_ready(ssh_port):
                    print("Docker VM is up and ready.")
                else:
                    print("Warning: Docker daemon did not become ready in time.")
                    return 1
        return 0

    cmd, resolved_ports = build_qemu_command(config)

    # Prepare runtime state
    ssh_port = resolved_ports.get(str(SSH_GUEST_PORT), SSH_TUNNEL_PORT)
    save_state({
        "ssh_port": ssh_port,
        "port_forwards": resolved_ports,
    })

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

    # Give QEMU a moment to fail if it's going to
    time.sleep(1)
    returncode = proc.poll()
    if returncode is not None:
        print(f"Error: QEMU exited prematurely with return code {returncode}.")
        print(f"Check logs at {log_path}")
        return 1

    write_pid_file(proc.pid)
    print(f"QEMU started with PID {proc.pid}. Logs: {log_path}")

    if wait:
        print(f"Waiting up to {BOOT_TIMEOUT_S}s for guest to be ready...")
        if _wait_for_ready(ssh_port):
            print("Docker VM is up and ready.")
            return 0
        print("Warning: guest did not become ready in time. Check logs.")
        return 1
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    if stop_qemu():
        print("Docker VM stopped.")
        return 0
    else:
        print("Error: Docker VM failed to stop.")
        return 1


def cmd_restart(args: argparse.Namespace) -> int:
    cmd_stop(args)
    time.sleep(1)
    start_args = argparse.Namespace(no_wait=getattr(args, "no_wait", False))
    return cmd_start(start_args)


def cmd_status(args: argparse.Namespace) -> int:
    pid = get_qemu_pid()
    if pid is None:
        print("Docker VM is not running.")
        return 1
    print(f"Docker VM is running (PID {pid}).")

    runtime_state = load_state()
    ssh_port = runtime_state.get("ssh_port", SSH_TUNNEL_PORT)
    docker_host = f"ssh://{SSH_USER}@localhost:{ssh_port}"

    ssh_open = False
    try:
        with socket.create_connection(("127.0.0.1", ssh_port), timeout=2.0):
            print(f"SSH tunnel (port {ssh_port}): open")
            ssh_open = True
    except OSError:
        print(f"SSH tunnel (port {ssh_port}): not yet accepting connections")

    docker_ready = False
    if ssh_open:
        docker_ready = _check_docker_ready()
        if docker_ready:
            print("Docker daemon: ready")
        else:
            print("Docker daemon: not yet responding")

    print("Docker daemon reachable via:")
    print(f"  export DOCKER_HOST={docker_host}")

    forwards = runtime_state.get("port_forwards", {})
    if forwards:
        print("Port forwards (HOST -> GUEST):")
        for g, h in forwards.items():
            print(f"  {h} -> {g}")

    return 0 if (pid and ssh_open and docker_ready) else 1


def cmd_shell(args: argparse.Namespace) -> int:
    runtime_state = load_state()
    ssh_port = runtime_state.get("ssh_port", SSH_TUNNEL_PORT)
    key_path = _ssh_key_path()
    cmd = [
        "ssh",
        "-p", str(ssh_port),
        "-i", str(key_path),
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

    runtime_state = load_state()
    ssh_port = runtime_state.get("ssh_port", SSH_TUNNEL_PORT)
    key_path = _ssh_key_path()

    # We use 'ssh' to run the docker command on the guest.
    # This ensures we use our SSH key for authentication.
    # We use shlex.join to properly escape arguments for the remote shell.
    remote_cmd = shlex.join(["docker", *docker_args])
    cmd = [
        "ssh",
        "-p", str(ssh_port),
        "-i", str(key_path),
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        f"{SSH_USER}@localhost",
        remote_cmd
    ]
    if sys.stdin.isatty():
        cmd.insert(1, "-t")
    return subprocess.call(cmd)


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
    guest_port = str(args.guest)

    if args.host:
        host_port = str(args.host)
        if host_port in forwards:
            print(f"Host port {host_port} is already forwarded to guest {forwards[host_port]}.")
            return 0
        forwards[host_port] = guest_port
        print(f"Added forward: host {host_port} -> guest {guest_port}.")
    else:
        # Check if guest port is already mapped
        for h, g in forwards.items():
            if g == guest_port:
                print(f"Guest port {guest_port} is already forwarded from host {h}.")
                return 0
        # If no host port given, we will resolve it at start time (one-to-one or random)
        # For now, we store it with a special marker or just use guest port as host port
        # and let the start command handle collisions.
        forwards[guest_port] = guest_port
        print(f"Added forward: guest {guest_port} (host port will be resolved at start).")

    save_config(config)
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
        _ssh_key_path(),
        Path(str(_ssh_key_path()) + ".pub"),
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
    runtime_state = load_state()
    ssh_port = runtime_state.get("ssh_port", SSH_TUNNEL_PORT)
    docker_host = f"ssh://{SSH_USER}@localhost:{ssh_port}"
    key_path = _ssh_key_path()

    print(f"export DOCKER_HOST={docker_host}")
    print(f"export DOCKER_VM_WORKSPACE={_workspace()}")
    print(f"# To use the local docker client without a password, run:")
    print(f"# ssh-add {key_path}")
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
    p_start.add_argument("--no-wait", action="store_true", help="Don't wait for guest to be ready")
    p_start.set_defaults(func=cmd_start)

    sub.add_parser("stop", help="Stop the VM").set_defaults(func=cmd_stop)

    p_restart = sub.add_parser("restart", help="Restart the VM")
    p_restart.add_argument("--no-wait", action="store_true", help="Don't wait for guest to be ready")
    p_restart.set_defaults(func=cmd_restart)
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
    p_pf_add.add_argument("guest", type=int, help="Guest port")
    p_pf_add.add_argument("host", type=int, nargs="?", help="Host port (optional)")
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
