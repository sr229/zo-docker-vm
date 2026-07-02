# 🐳 Zo-Docker: Docker in gVisor via ARM64 QEMU

Zo-Docker allows you to run a full Docker daemon inside a Zo Computer sandbox by leveraging QEMU TCG emulation to create a secure ARM64 Virtual Machine.

## 🚀 Quick Start

1. **Install the tool**: (Currently pre-installed in this workspace)
2. **Start the VM**:
   ```bash
   docker-vm start
   ```
3. **Use Docker**:
   ```bash
   export DOCKER_HOST=ssh://debian@localhost:2222
   docker run hello-world
   ```

## 🛠️ Commands

| Command | Description |
| :--- | :--- |
| `docker-vm start` | Initialize and start the VM |
| `docker-vm stop` | Stop the VM |
| `docker-vm restart` | Restart the VM |
| `docker-vm status` | Check if Docker is healthy |
| `docker-vm shell` | SSH into the guest VM |
| `docker-vm docker <cmd>` | Run a docker command directly |
| `docker-vm pf-add <guest> <host>` | Add a port forward (e.g., `8080 8080`) |
| `docker-vm pf-rm <guest>` | Remove a port forward |
| `docker-vm resize <size>` | Resize the VM disk (e.g., `100G`) |
| `docker-vm destroy` | Wipe everything and start over |

## 🛡️ How it Works

Since gVisor restricts the system calls needed for Docker (like `iptables` and specific mounts), Zo-Docker emulates an entire ARM64 machine. The Docker daemon runs inside this VM, and the host communicates with it via a secure SSH tunnel.

All management is consolidated into a single Python entry point: `docker_vm.py`. The `docker-vm` command in `PATH` and the legacy `start-docker-vm.sh` are both symlinks to this same script, so there is exactly one source of truth.

**Network Layout:**
- **Host** $\rightarrow$ **SSH Tunnel (Port 2222)** $\rightarrow$ **Guest VM** $\rightarrow$ **Docker Daemon**

## 🐍 Architecture (Python)

The project is built around `docker_vm.py`, a pure-Python CLI (using only the standard library) that:

- **Parses `.docker-vm.json`** natively with `json` instead of `grep/sed`, eliminating quoting pitfalls.
- **Manages the QEMU process** with `subprocess.Popen` and a PID file at `/var/run/docker-vm.pid`.
- **Builds QEMU arguments** dynamically from the `port_forwards` config map.
- **Exposes the full subcommand set** via `argparse` (`start`, `stop`, `restart`, `status`, `shell`, `docker`, `pf-add`, `pf-rm`, `resize`, `destroy`).

**File layout:**

| Path | Purpose |
| :--- | :--- |
| `docker_vm.py` | Single Python source of truth for the CLI and QEMU launcher. |
| `start-docker-vm.sh` | Symlink to `docker_vm.py` (backwards compatibility). |
| `/usr/local/bin/docker-vm` | Symlink to `docker_vm.py` (the documented `docker-vm ...` command). |
| `.docker-vm.json` | Persisted configuration (image path, log file, port forwards). |

## ⚙️ Configuration

All configuration is stored in `/home/workspace/.docker-vm.json`. The file is automatically created on first start and persists the state of the VM image and port forwards.

```json
{
  "image_path": "/home/workspace/.docker-vm/image.qcow2",
  "log_file": "/dev/shm/docker-vm.log",
  "port_forwards": {
    "2222": "22",
    "8080": "80",
    "9443": "443"
  }
}
```

* `image_path`: The path to the persistent `qcow2` disk image.
* `log_file`: Where QEMU's serial and debug output is piped.
* `port_forwards`: A mapping of `host port -> guest port` used to build QEMU's `hostfwd` arguments at launch.

## 🌐 Networking & Port Forwarding

The guest VM is connected to the outside world using QEMU's built-in user-mode networking (`-netdev user,id=net0`). This mode is chosen because it requires no special host privileges, fitting perfectly inside the gVisor sandbox.

Port forwarding is defined in `.docker-vm.json` and converted dynamically into QEMU's `hostfwd` arguments by `docker_vm.py` at launch:

```python
# In .docker-vm.json
"port_forwards": { "8080": "80" }

# Becomes the following QEMU flag
-netdev user,id=net0,hostfwd=tcp::8080-:80
```

You can manage forwards at runtime without manually editing the JSON file:

* `docker-vm pf-add 8080 80` — Adds a forwarding from host port `8080` to guest port `80`.
* `docker-vm pf-rm 8080` — Removes the host port `8080` forwarding.
* `docker-vm restart` — Required after modifying forwards so the VM is relaunched with the new QEMU arguments.

**Default Port Forwards (reserved by the system):**
* **2222** -> **22**: The SSH tunnel used by the Docker client (`DOCKER_HOST=ssh://debian@localhost:2222`).

## 🧪 Alternative: Native Docker in gVisor

It is possible to run the Docker daemon directly in gVisor without a VM, though this requires specific host-level configuration.
