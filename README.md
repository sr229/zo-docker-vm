# 🐳 Zo-Docker: Docker in gVisor via ARM64 QEMU

Zo-Docker allows you to run a full Docker daemon inside a Zo Computer sandbox by leveraging QEMU TCG emulation to create a secure ARM64 Virtual Machine.

## 🚀 Quick Start

1. **Install the tool**: The whole manager is one Python file. Symlink it onto your `PATH`:
   ```bash
   ln -sf /home/workspace/zo-docker-vm/docker-vm.py /usr/local/bin/docker-vm
   ```
2. **Initialize the VM** (downloads the ARM64 cloud image and prepares cloud-init):
   ```bash
   docker-vm init
   ```
3. **Start the VM** and wait for it to come up:
   ```bash
   docker-vm start --wait
   ```
4. **Use Docker**:
   ```bash
   eval "$(docker-vm env)"
   docker run hello-world
   ```

## 📦 Requirements

### System packages

| Tool | Why |
| :--- | :--- |
| `qemu-system-aarch64` | The ARM64 VM emulator |
| `qemu-img` | Disk image creation and resizing |
| `cloud-localds` | Builds the cloud-init ISO (from the `cloud-image-utils` package) |
| `ssh` | Talks to the guest over the SSH tunnel |
| `docker` (client) | For `docker-vm docker <args...>` |

On Debian/Ubuntu:
```bash
apt install qemu-system-arm qemu-utils cloud-image-utils openssh-client docker.io
```

### Python packages

`docker-vm` is pure Python 3.10+ and only needs two third-party libraries. Both are pre-installed on the base Zo Computer image, so no action is required there. If you run the script elsewhere:

```bash
pip install psutil requests
```

`psutil` is used for cross-platform process management (replaces `pkill`/`pgrep`/`kill`).
`requests` is used for HTTP downloads with progress reporting. If it is missing, the script transparently falls back to `urllib` from the standard library.

## 📂 State directory

All persistent state (qcow2 disk image, EFI pflash, cloud-init ISO, config, log, PID) lives in a single self-contained directory, so the host `$HOME` stays clean. By default that directory is:

```
$XDG_STATE_HOME/docker-vm       # if $XDG_STATE_HOME is set
~/.local/state/docker-vm        # otherwise
```

Override it with `$DOCKER_VM_STATE_DIR` to put the VM somewhere else (for example, a second disk or a shared volume):

```bash
DOCKER_VM_STATE_DIR=/mnt/docker-vm docker-vm init
```

Nothing else is written to your home directory. Legacy installs that still have `/home/workspace/.docker-vm.json` are detected on first run and migrated into the state directory automatically (the old file is renamed to `.docker-vm.json.bak`).

### Environment variables (all optional)

| Variable | Default | Purpose |
| :--- | :--- | :--- |
| `DOCKER_VM_STATE_DIR` | `$XDG_STATE_HOME/docker-vm` or `~/.local/state/docker-vm` | Where the qcow2, EFI, ISO, log, and config live |
| `DOCKER_VM_WORKSPACE` | `/home/workspace` | Host workspace path; printed by `docker-vm env` so the guest can mount the same path |
| `DOCKER_VM_EFI` | first match in well-known paths | Path to a `QEMU_EFI.fd` / `AAVMF_CODE.fd` file |
| `XDG_STATE_HOME` | n/a | XDG base directory spec; if set, used as the state-dir parent |
| `XDG_RUNTIME_DIR` | n/a | XDG base directory spec; if set, the PID file lives here as a tmpfs |

## 🛠️ Commands

| Command | Description |
| :--- | :--- |
| `docker-vm init [--image URL] [--size SIZE] [--force]` | Download image, prepare cloud-init ISO |
| `docker-vm start [--wait]` | Boot the VM (`--wait` blocks until SSH is ready) |
| `docker-vm stop` | Stop the VM |
| `docker-vm restart` | Restart the VM (waits for SSH) |
| `docker-vm status` | Check if the VM and SSH tunnel are up |
| `docker-vm shell` | Open an interactive SSH shell in the guest |
| `docker-vm env` | Print `export DOCKER_HOST=...` for the host shell |
| `docker-vm docker <args...>` | Run a docker command against the VM |
| `docker-vm logs [-f]` | Show or tail the QEMU log |
| `docker-vm pf add <host> <guest>` | Add a port forward |
| `docker-vm pf rm <host>` | Remove a port forward |
| `docker-vm pf ls` | List port forwards |
| `docker-vm resize <size>` | Resize the VM disk (e.g., `100G`) |
| `docker-vm destroy [-y]` | Wipe the state directory and start over |

## 🏗️ Architecture

`docker-vm.py` is a single self-contained Python file. It deliberately uses dedicated Python libraries instead of shelling out wherever possible:

| Concern | Library | Replaces |
| :--- | :--- | :--- |
| Process management | `psutil` | `pkill`, `pgrep`, `kill` |
| Image downloads | `requests` (fallback: `urllib`) | `curl` |
| File IO | `pathlib`, `shutil` (stdlib) | `rm`, `cp`, `mkdir` |
| Networking checks | `socket` (stdlib) | `nc`, `sshpass` |
| Log tailing | pure-Python read/poll | `tail -f` |
| Argument parsing | `argparse` (stdlib) | hand-rolled argv parsing |
| Path resolution | `pathlib` + XDG env vars | hard-coded paths in `$HOME` |

The only external commands invoked are `qemu-img` (disk resize) and `cloud-localds` (cloud-init ISO), both of which are tiny tools with no clean Python equivalents. QEMU itself is launched via `subprocess.Popen` and its PID is tracked via `psutil` (with a `/var/run` PID file as a fallback when `XDG_RUNTIME_DIR` is unset).

**Network Layout:**
- **Host** → **SSH Tunnel (Port 2222)** → **Guest VM** → **Docker Daemon**

## 🛡️ How it Works

Since gVisor restricts the system calls needed for Docker (like `iptables` and specific mounts), Zo-Docker emulates an entire ARM64 machine. The Docker daemon runs inside this VM, and the host communicates with it via a secure SSH tunnel.

## ⚙️ Configuration

All configuration is stored in `<state-dir>/config.json` — see the [State directory](#-state-directory) section for the path. The file is automatically created on first run and persists the VM image path, log file location, and port forwards.

```json
{
  "image_path": "/home/user/.local/state/docker-vm/image.qcow2",
  "log_file": "/home/user/.local/state/docker-vm/docker-vm.log",
  "port_forwards": {
    "2222": "22",
    "8080": "80"
  }
}
```

* `image_path`: The path to the persistent `qcow2` disk image.
* `log_file`: Where QEMU's serial and debug output is piped.
* `port_forwards`: A mapping of `host port -> guest port` used to build QEMU's `hostfwd` arguments at launch.
