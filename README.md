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
| `docker-vm destroy [-y]` | Wipe everything and start over |

## 🏗️ Architecture

`docker-vm.py` is a single self-contained Python file. It deliberately uses dedicated Python libraries instead of shelling out wherever possible:

| Concern | Library | Replaces |
| :--- | :--- | :--- |
| Process management | `psutil` | `pkill`, `pgrep`, `kill` |
| Image downloads | `requests` (fallback: `urllib`) | `curl` |
| File IO | `pathlib`, `shutil` (stdlib) | `rm`, `cp`, `mkdir` |
| Networking checks | `socket` (stdlib) | `nc`, `sshpass` |
| Argument parsing | `argparse` (stdlib) | hand-rolled argv parsing |

The only external commands invoked are `qemu-img` (disk resize) and `cloud-localds` (cloud-init ISO), both of which are tiny tools with no clean Python equivalents. QEMU itself is launched via `subprocess.Popen` and its PID is tracked in `/var/run/docker-vm.pid`.

**Network Layout:**
- **Host** → **SSH Tunnel (Port 2222)** → **Guest VM** → **Docker Daemon**

## 🛡️ How it Works

Since gVisor restricts the system calls needed for Docker (like `iptables` and specific mounts), Zo-Docker emulates an entire ARM64 machine. The Docker daemon runs inside this VM, and the host communicates with it via a secure SSH tunnel.

## ⚙️ Configuration

All configuration is stored in `/home/workspace/.docker-vm.json`. The file is automatically created on first run and persists the state of the VM image, log file, and port forwards.

```json
{
  "image_path": "/home/workspace/.docker-vm/image.qcow2",
  "log_file": "/dev/shm/docker-vm.log",
  "port_forwards": {
    "2222": "22",
    "8080": "80"
  }
}
```

* `image_path`: The path to the persistent `qcow2` disk image.
* `log_file`: Where QEMU's serial and debug output is piped.
* `port_forwards`: A mapping of `host port -> guest port` used to build QEMU's `hostfwd` arguments at launch.
