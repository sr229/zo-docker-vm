# 🐳 Zo-Docker: Docker in gVisor via ARM64 QEMU

Zo-Docker allows you to run a full Docker daemon inside a Zo Computer sandbox. Because gVisor restricts several system calls and mounts required by Docker (such as `iptables` and loop devices), Zo-Docker bridges this gap by launching a secure, emulated ARM64 Virtual Machine via QEMU TCG and forwarding Docker API commands over a local SSH tunnel.

---

## 🏗️ Architecture & Network Layout

```
                  ┌──────────────────────────────────────────────┐
                  │                 Host Sandbox                 │
                  │                                              │
                  │  ┌──────────────┐      ┌──────────────────┐  │
                  │  │  Docker CLI  │ ───> │ SSH Port Forward │  │
                  │  └──────────────┘      │   (Port 2222)    │  │
                  └────────────────────────┴────────┬─────────┴──┘
                                                    │
                                        (SSH Tunnel over localhost)
                                                    │
                                                    ▼
                  ┌──────────────────────────────────────────────┐
                  │                  Guest VM                    │
                  │                                              │
                  │  ┌──────────────┐      ┌──────────────────┐  │
                  │  │  SSH Server  │ ───> │  Docker Daemon   │  │
                  │  └──────────────┘      └──────────────────┘  │
                  └──────────────────────────────────────────────┘
```

* **Tunnel Port:** Local port `2222` is mapped to guest port `22` (SSH) by default.
* **Storage Mounting:** Host workspace paths (e.g. `/home/workspace`) are mapped so you can mount host directories directly into containers.

---

## 🚀 Quick Start

### 1. Install the tool
Symlink the Python manager script onto your `PATH`:
```bash
ln -sf /home/workspace/zo-docker-vm/docker-vm.py /usr/local/bin/docker-vm
```

### 2. Initialize the VM
Downloads the pre-configured ARM64 cloud image and prepares the cloud-init environment:
```bash
docker-vm init
```

### 3. Start the VM
Starts the QEMU emulator. By default, this blocks until the guest OS and the Docker daemon inside the VM are fully ready:
```bash
docker-vm start
```

### 4. Use Docker
You can interact with the Docker daemon in two ways:

* **Method A: Via the CLI Wrapper (No config needed)**
  Simply prefix your commands with `docker-vm docker`:
  ```bash
  docker-vm docker run hello-world
  ```
* **Method B: Via the Native Docker CLI**
  Set up the host environment variables to route native `docker` commands through the SSH tunnel:
  ```bash
  eval "$(docker-vm env)"
  docker run hello-world
  ```
  *Note: To use the native CLI, you must authorize the VM's SSH key. See [SSH Authorization](#-ssh-authorization) below.*

---

## 🔑 SSH Authorization

The native `docker` CLI communicates with the guest VM over SSH. Because the private key is stored in a non-standard location (`~/.local/state/docker-vm/id_ed25519`), you must tell your local SSH client how to locate it.

Choose **one** of the following methods to authorize the key:

### Method A: SSH Configuration File (Recommended - Persistent)
Append the following block to your local SSH configuration file ([~/.ssh/config](file:///root/.ssh/config)). This ensures `ssh` always uses the correct identity and flags when connecting to the VM:
```text
Host localhost
  Port 2222
  IdentityFile ~/.local/state/docker-vm/id_ed25519
  StrictHostKeyChecking no
  UserKnownHostsFile /dev/null
```

### Method B: SSH Agent (Temporary - Session-scoped)
Start the authentication agent and add the key to your current shell session:
```bash
eval $(ssh-agent)
ssh-add ~/.local/state/docker-vm/id_ed25519
```

---

## 📂 State Directory & Configuration

All VM state files (disk image, EFI flashes, metadata, log files, and keys) are kept in a single directory to prevent pollution of your `$HOME` folder:

```text
$XDG_STATE_HOME/docker-vm       # if $XDG_STATE_HOME is set
~/.local/state/docker-vm        # otherwise
```

To use a custom directory (e.g., to place the VM on a secondary block storage or shared drive), set the `$DOCKER_VM_STATE_DIR` variable:
```bash
DOCKER_VM_STATE_DIR=/mnt/docker-vm docker-vm init
```

### Configuration File (`config.json`)
The VM's configuration is managed dynamically in `<state-dir>/config.json`. Example content:
```json
{
  "image_path": "/root/.local/state/docker-vm/image.qcow2",
  "image_url": "https://github.com/abiosoft/colima-core/releases/download/v0.10.4/ubuntu-24.04-minimal-cloudimg-arm64-docker.raw.gz",
  "disk_size": "50G",
  "log_file": "/root/.local/state/docker-vm/docker-vm.log",
  "port_forwards": {
    "2222": "22"
  },
  "workspace": "/home/workspace"
}
```

---

## 🛠️ Commands Reference

| Command | Arguments | Description |
| :--- | :--- | :--- |
| `init` | `[--image URL/shorthand] [--size SIZE] [--force]` | Downloads the VM cloud image and creates the cloud-init environment. Supported shorthands: `docker` (default), `containerd`, `incus`, `none`. |
| `start` | `[--no-wait]` | Launches QEMU. Blocks until Docker is ready unless `--no-wait` is supplied. |
| `stop` | None | Gracefully stops the QEMU process. |
| `restart` | `[--no-wait]` | Restarts the VM. |
| `status` | `[--json]` | Shows whether the VM process, SSH tunnel, and Docker daemon are active. |
| `shell` | None | Opens an interactive SSH shell inside the guest VM. |
| `env` | None | Prints shell environment exports (`DOCKER_HOST`, `DOCKER_VM_WORKSPACE`) and prints SSH authorization checks. |
| `docker` | `<args...>` | Wrapper to run any Docker command inside the guest VM. |
| `logs` | `[-f/--follow]` | Tails the QEMU process serial and debug output log. |
| `pf add` | `<guest_port> [host_port]` | Registers a new port forward mapping from Host to Guest VM. |
| `pf rm` | `<host_port>` | Removes an existing port forward. |
| `pf ls` | `[--json]` | Lists all active port forward configurations. |
| `resize` | `<size>` | Expands the QEMU disk partition to a larger size (e.g. `100G`). |
| `destroy` | `[-y/--yes]` | Wipes all state data, configs, and disk files in the state directory. |

---

## ⚙️ Environment Variables

| Variable | Default | Purpose |
| :--- | :--- | :--- |
| `DOCKER_VM_STATE_DIR` | `~/.local/state/docker-vm` | State directory containing all VM disks, keys, and logs. |
| `DOCKER_VM_WORKSPACE` | `/home/workspace` | Host workspace directory path exposed to the guest VM. |
| `DOCKER_VM_EFI` | System search path | Location of the `QEMU_EFI.fd` or `AAVMF_CODE.fd` binary. |
| `XDG_STATE_HOME` | `~/.local/state` | Parent directory of the default state path. |
| `XDG_RUNTIME_DIR` | None | Parent directory for PID storage (tmpfs) if set. |
