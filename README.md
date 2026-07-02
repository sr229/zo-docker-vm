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

**Network Layout:**
- **Host** $\rightarrow$ **SSH Tunnel (Port 2222)** $\rightarrow$ **Guest VM** $\rightarrow$ **Docker Daemon**

## 🧪 Alternative: Native Docker in gVisor

It is possible to run the Docker daemon directly in gVisor without a VM, though this requires specific host-level configurations and involves several compromises.

### Requirements
To enable native Docker, the gVisor runtime (`runsc`) must be started with the following flags:
- `--net-raw`
- `--allow-packet-socket-write`

### Known Compromises & Configuration
- **Storage**: The containerd image store requires a `tmpfs` mount at `/var/lib/docker` or a snapshotter that supports the gVisor filesystem.
- **Networking**: Since `iptables` is not supported in gVisor, standard Docker port mapping (`-p`) will not work. Containers must use `--network=host` to expose services.

### VM vs. Native Comparison

| Feature | QEMU VM (Zo-Docker) | Native gVisor |
| :--- | :--- | :--- |
| **Setup** | Plug-and-play (via `docker-vm`) | Requires host `runsc` config |
| **Performance** | Slower (TCG Emulation) | Fast (Native) |
| **Networking** | Full `iptables` support | Host networking only |
| **Isolation** | Strong (Hardware Virtualization) | Medium (Syscall Interception) |
| **Resources** | Higher overhead | Lightweight |
