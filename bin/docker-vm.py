#!/usr/bin/env python3
import subprocess
import os
import sys
from pathlib import Path
import json
import time
import socket
import psutil

CONFIG_PATH = Path("/home/workspace/.docker-vm.json")
TUNNELS_PATH = Path("/home/workspace/.docker-vm-tunnels.json")
EFI_PFLASH_PATH = Path("/home/workspace/efi-pflash.raw")
EFI_SOURCE = "/usr/share/qemu-efi-aarch64/QEMU_EFI.fd"

def load_config():
    with open(CONFIG_PATH, "r") as f:
        return json.load(f)

def save_config(config):
    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=4)

def load_tunnels():
    if not TUNNELS_PATH.exists():
        return {}
    with open(TUNNELS_PATH, "r") as f:
        return json.load(f)

def save_tunnels(tunnels):
    with open(TUNNELS_PATH, "w") as f:
        json.dump(tunnels, f, indent=4)

def run(cmd, args=None, env=None, capture_output=True):
    """Helper to run shell commands. Captures output by default."""
    full_cmd = cmd
    if args:
        full_cmd = f"{cmd} {' '.join(args)}"
    
    current_env = os.environ.copy()
    if env:
        current_env.update(env)
        
    try:
        process = subprocess.Popen(
            full_cmd, 
            shell=True, 
            env=current_env, 
            stdout=subprocess.PIPE if capture_output else None,
            stderr=subprocess.PIPE if capture_output else None,
            text=True
        )
        
        stdout, stderr = process.communicate()
        return process.returncode, stdout, stderr
    except Exception as e:
        print(f"Error executing command: {e}")
        return 1, "", str(e)

def run_interactive(cmd):
    """Runs a command with inherited stdio for interactive sessions."""
    subprocess.run(cmd, shell=True)

def ensure_efi_pflash():
    """Ensures the 64MB pflash file exists and contains the EFI firmware."""
    if not EFI_PFLASH_PATH.exists() or EFI_PFLASH_PATH.stat().st_size < 64 * 1024 * 1024:
        print("Preparing EFI pflash...")
        with open(EFI_PFLASH_PATH, "wb") as pflash:
            pflash.write(b"\x00" * (64 * 1024 * 1024))
            # Copy EFI firmware to the beginning
            with open(EFI_SOURCE, "rb") as efi:
                pflash.write(efi.read())

def build_qemu_command(config):
    """Builds the QEMU command based on the current configuration."""
    hostfwd_args = ""
    # Always forward SSH
    hostfwd_args += f",hostfwd=tcp::{config['vm_ssh_port']}-:22"
    
    # Add user-defined port forwards
    for guest, host in config.get("port_forwards", {}).items():
        hostfwd_args += f",hostfwd=tcp::{host}-:{guest}"
    
    cmd = [
        "qemu-system-aarch64",
        "-machine", "virt",
        "-cpu", "cortex-a57",
        "-smp", "4",
        "-m", "4G",
        "-drive", f"if=pflash,format=raw,readonly=on,file={EFI_PFLASH_PATH}",
        "-drive", f"if=virtio,format=qcow2,file={config['image_path']}",
        "-drive", f"if=virtio,format=raw,file=/home/workspace/cloud-init.iso",
        "-netdev", f"user,id=net0{hostfwd_args}",
        "-device", "virtio-net-device,netdev=net0",
        "-nographic",
        "-serial", "mon:stdio"
    ]
    return cmd

def start_qemu_foreground(config):
    """Starts QEMU in the foreground (used by the service supervisor)."""
    ensure_efi_pflash()
    cmd = build_qemu_command(config)
    print(f"Starting QEMU: {' '.join(cmd)}")
    try:
        subprocess.run(cmd)
    except KeyboardInterrupt:
        pass

def start_qemu_background(config):
    """Starts QEMU in the background."""
    ensure_efi_pflash()
    cmd = build_qemu_command(config)
    log_file = open(config.get("log_file", "/tmp/qemu-arm64.log"), "w")
    process = subprocess.Popen(
        cmd,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True
    )
    return process.pid

def resize_disk(size):
    config = load_config()
    image = config["image_path"]
    print(f"Resizing disk {image} to {size}... ", end="", flush=True)
    
    rc, _, _ = run("qemu-img resize " + image + " " + size)
    
    if rc == 0:
        config["disk_size"] = size
        save_config(config)
        print("Done!")
        print("Please run 'docker-vm restart' to apply changes.")
    else:
        print("Failed!")

def init_vm(image_url=None, disk_size="50G"):
    if CONFIG_PATH.exists() or Path("/home/workspace/debian-arm64.qcow2").exists():
        return False

    print("Initializing Docker VM environment...")
    
    if not image_url:
        image_url = "https://cloud.debian.org/images/cloud/bookworm/latest/debian-12-generic-arm64.qcow2"
    
    image_path = "/home/workspace/debian-arm64.qcow2"
    iso_path = "/home/workspace/cloud-init.iso"
    
    # 1. Download Image
    print("Downloading image...")
    rc, _, _ = run(f"curl -L {image_url} -o {image_path}")
    if rc != 0:
        print("Failed to download image.")
        return False
    
    # 2. Setup Cloud-Init
    user_data = """#cloud-config
password: debian
chpasswd: { expire: False }
ssh_pwauth: True
packages:
  - qemu-guest-agent
"""
    with open("/home/workspace/user-data", "w") as f:
        f.write(user_data)
    
    print("Generating cloud-init ISO...")
    rc, _, _ = run(f"cloud-localds {iso_path} /home/workspace/user-data")
    if rc != 0:
        print("Failed to generate cloud-init ISO.")
        return False
    
    # 3. Resize Image
    print(f"Setting disk size to {disk_size}...")
    rc, _, _ = run(f"qemu-img resize {image_path} {disk_size}")
    if rc != 0:
        print("Failed to resize image.")
        return False
    
    # 4. Prepare EFI pflash
    ensure_efi_pflash()
    
    # 5. Update Config
    config = {
        "image_path": image_path,
        "disk_size": disk_size,
        "vm_ip": "localhost",
        "vm_ssh_port": "2223",
        "vm_ssh_user": "debian",
        "vm_ssh_pass": "debian",
        "docker_host": "ssh://debian@127.0.0.1:2223",
        "log_file": "/tmp/qemu-arm64.log",
        "port_forwards": {}
    }
    save_config(config)
    
    print("✅ Environment initialized successfully!")
    return True

def destroy_vm():
    print("Tearing down the Docker VM environment...")
    
    subprocess.run(["pkill", "-f", "qemu-system-aarch64"])
    
    config = None
    if CONFIG_PATH.exists():
        config = load_config()
    
    files_to_remove = [
        config.get("image_path", "/home/workspace/debian-arm64.qcow2") if config else "/home/workspace/debian-arm64.qcow2",
        str(CONFIG_PATH),
        "/home/workspace/cloud-init.iso",
        "/home/workspace/user-data",
        "/home/workspace/.docker-vm-tunnels.json",
        str(EFI_PFLASH_PATH)
    ]
    
    for file_path in files_to_remove:
        try:
            if Path(file_path).exists():
                os.remove(file_path)
                print(f"Removed {file_path}")
        except FileNotFoundError:
            pass
        except Exception as e:
            print(f"Error removing {file_path}: {e}")
            
    # Also close any active SSH tunnels
    for proc in psutil.process_iter(['cmdline']):
        try:
            cmdline = ' '.join(proc.info['cmdline'] or [])
            if 'ssh' in cmdline and 'sshpass' in cmdline and ('-N' in cmdline or '-L' in cmdline):
                proc.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
            
    print("✅ Environment destroyed.")

def start_tunnel(guest_port, host_port=None):
    config = load_config()
    if host_port is None:
        host_port = guest_port
    
    tunnels = load_tunnels()
    if str(guest_port) in tunnels:
        print(f"Tunnel already exists for port {guest_port} (Host: {tunnels[str(guest_port)]})")
        return

    print(f"Creating dynamic tunnel: {host_port} (host) -> {guest_port} (guest)... ", end="", flush=True)
    
    ssh_cmd = (
        f"sshpass -p {config['vm_ssh_pass']} "
        f"ssh -N -f -L {host_port}:localhost:{guest_port} "
        f"-p {config['vm_ssh_port']} -o StrictHostKeyChecking=no -o ExitOnForwardFailure=yes "
        f"{config['vm_ssh_user']}@{config['vm_ip']}"
    )
    
    rc, _, _ = run(ssh_cmd)
    if rc == 0:
        tunnels[str(guest_port)] = str(host_port)
        save_tunnels(tunnels)
        print("Done!")
    else:
        print("Failed! Is the host port already in use?")

def stop_tunnel(guest_port):
    tunnels = load_tunnels()
    if str(guest_port) not in tunnels:
        print(f"No tunnel found for port {guest_port}")
        return
    
    host_port = tunnels[str(guest_port)]
    print(f"Closing tunnel {host_port} -> {guest_port}... ", end="", flush=True)
    
    # Use pgrep to find the SSH tunnel process for this specific port
    kill_cmd = f"pgrep -f 'sshpass.*-L {host_port}:localhost:{guest_port}' | xargs -r kill"
    run(kill_cmd)
    
    del tunnels[str(guest_port)]
    save_tunnels(tunnels)
    print("Done!")

def list_tunnels():
    tunnels = load_tunnels()
    if not tunnels:
        print("No active tunnels.")
        return
    
    print(f"{'Guest Port':<12} {'Host Port':<12}")
    print("-" * 24)
    for gp, hp in tunnels.items():
        print(f"{gp:<12} {hp:<12}")

def request_zo_exposure(guest_port):
    config = load_config()
    requests = config.get("zo_requests", [])
    if str(guest_port) not in requests:
        requests.append(str(guest_port))
        config["zo_requests"] = requests
        save_config(config)
    
    print(f"Requested Zo service exposure for port {guest_port}.")
    print("\n✨ To finalize this, please tell your AI assistant:")
    print(f"  'Expose VM port {guest_port} as a Zo service'")

def add_static_port_forward(guest_port, host_port):
    """Adds a static port forward to the QEMU config (requires VM restart)."""
    config = load_config()
    if "port_forwards" not in config:
        config["port_forwards"] = {}
    config["port_forwards"][str(guest_port)] = str(host_port)
    save_config(config)
    print(f"Added static port forward: {host_port} (host) -> {guest_port} (guest)")
    print("Run 'docker-vm restart' to apply.")

def remove_static_port_forward(guest_port):
    config = load_config()
    if "port_forwards" in config and str(guest_port) in config["port_forwards"]:
        del config["port_forwards"][str(guest_port)]
        save_config(config)
        print(f"Removed static port forward for port {guest_port}")
        print("Run 'docker-vm restart' to apply.")
    else:
        print(f"No static port forward found for port {guest_port}")

def list_static_port_forwards():
    config = load_config()
    forwards = config.get("port_forwards", {})
    if not forwards:
        print("No static port forwards configured.")
        return
    print(f"{'Guest Port':<12} {'Host Port':<12}")
    print("-" * 24)
    for gp, hp in forwards.items():
        print(f"{gp:<12} {hp:<12}")

def is_vm_running():
    """Check if the QEMU process is currently running."""
    try:
        result = subprocess.run(
            ["pgrep", "-f", "qemu-system-aarch64"], 
            capture_output=True, 
            text=True
        )
        return result.returncode == 0
    except Exception:
        return False

def wait_for_ssh(config, timeout=180):
    """Polls the SSH port until it responds or timeout."""
    print("Waiting for VM SSH... ", end="", flush=True)
    start_time = time.time()
    while time.time() - start_time < timeout:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(2)
        try:
            sock.connect(("127.0.0.1", int(config["vm_ssh_port"])))
            sock.close()
            print("Ready!")
            return True
        except (socket.error, ConnectionRefusedError, OSError):
            print(".", end="", flush=True)
            time.sleep(3)
        finally:
            sock.close()
    print(" Timeout!")
    return False

def wait_for_docker(timeout=180):
    """Polls the Docker daemon until it responds or timeout is reached."""
    print("Waiting for Docker... ", end="", flush=True)
    start_time = time.time()
    config = load_config()
    while time.time() - start_time < timeout:
        rc, _, _ = run("docker info", env={"DOCKER_HOST": config["docker_host"]})
        if rc == 0:
            print("Ready!")
            return True
        print(".", end="", flush=True)
        time.sleep(5)
    print(" Timeout!")
    return False

def get_vm_health():
    """Detailed health check of the VM layers."""
    config = load_config()
    health = {
        "process": False,
        "ssh": False,
        "docker": False
    }
    
    # 1. Check if process is running
    proc_check = subprocess.run("pgrep -f qemu-system-aarch64", shell=True, capture_output=True)
    health["process"] = (proc_check.returncode == 0)
    
    # 2. Check SSH port
    if health["process"]:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(2)
        try:
            sock.connect(("127.0.0.1", int(config["vm_ssh_port"])))
            health["ssh"] = True
        except (socket.error, ConnectionRefusedError, OSError):
            pass
        finally:
            sock.close()
    
    # 3. Check Docker API
    if health["ssh"]:
        rc, _, _ = run("docker info", env={"DOCKER_HOST": config["docker_host"]})
        health["docker"] = (rc == 0)
    
    return health

def update_shell_env(config):
    """Writes DOCKER_HOST to a file."""
    with open(os.environ["HOME"] + "/.docker-vm.env", "w") as f:
        f.write(f"DOCKER_HOST={config['docker_host']}\n")

def main():
    args = sys.argv[1:]
    if not args or args[0] == "help":
        print("""
Docker VM Manager - ARM64 Emulation (Python Edition)

Usage:
  docker-vm <command> [args]

Commands:
  start [--image url] [--size size]  Initialize and start the VM
  status    Check if the VM and Docker daemon are running
  shell     SSH into the VM
  docker    Run a docker command inside the VM
  logs      Tail the QEMU logs
  pf <subcommand> [args]            Manage port forwarding
    add <guest>[:<host>]            Add dynamic SSH tunnel (host -> guest)
    rm <guest>                      Remove dynamic SSH tunnel
    list                            List active dynamic tunnels
    static <add|rm|list>            Manage static QEMU port forwards
  resize    Resize the VM disk (e.g., docker-vm resize 100G)
  restart   Restart the VM
  stop      Stop the VM
  destroy   Wipe the VM disk and configuration completely
  help      Show this help

Special internal command:
  _run-vm   Run QEMU in foreground (used by service supervisor)
        """)
        sys.exit(0)

    command = args[0]

    if command == "_run-vm" or command == "run-vm":
        config = load_config()
        start_qemu_foreground(config)

    elif command == "start":
        # Check if environment exists
        if not CONFIG_PATH.exists() and not Path("/home/workspace/debian-arm64.qcow2").exists():
            image_url = None
            disk_size = "50G"
            for i in range(1, len(args)):
                if args[i] == "--image" and i + 1 < len(args):
                    image_url = args[i+1]
                elif args[i] == "--size" and i + 1 < len(args):
                    disk_size = args[i+1]
            init_vm(image_url, disk_size)
        
        if is_vm_running():
            config = load_config()
            update_shell_env(config)
        else:
            print("Starting VM...")
            config = load_config()
            pid = start_qemu_background(config)
            print(f"VM started (PID: {pid})")
            if wait_for_ssh(config):
                # Restart existing tunnels
                tunnels = load_tunnels()
                if tunnels:
                    print("Re-establishing tunnels...")
                    for guest, host in tunnels.items():
                        start_tunnel(guest, host)
            update_shell_env(config)
    
    elif command == "status":
        if not CONFIG_PATH.exists():
            print("VM environment not initialized. Run 'docker-vm start' first.")
            sys.exit(1)
        health = get_vm_health()
        print("\n--- VM Health Report ---")
        print(f"Process: {'✅' if health['process'] else '❌'}")
        print(f"SSH:     {'✅' if health['ssh'] else '❌'}")
        print(f"Docker:  {'✅' if health['docker'] else '❌'}")
        
        if health["docker"]:
            print("\n✅ Docker VM is healthy and responding!")
        elif health["process"] and not health["ssh"]:
            print("\n⚠️  VM process is running, but SSH is not responding. Still booting?")
        elif not health["process"]:
            print("\n❌ VM process is not running. Try 'docker-vm start'.")
        else:
            print("\n❌ VM is partially healthy. Check 'docker-vm logs'.")

    elif command == "shell":
        config = load_config()
        cmd = f"sshpass -p {config['vm_ssh_pass']} ssh -p {config['vm_ssh_port']} -o StrictHostKeyChecking=no {config['vm_ssh_user']}@{config['vm_ip']}"
        run_interactive(cmd)

    elif command == "docker":
        docker_args = args[1:]
        if not docker_args:
            print("Please provide a docker command. Example: docker-vm docker ps")
            sys.exit(1)
        config = load_config()
        run("docker " + " ".join(docker_args), env={"DOCKER_HOST": config["docker_host"]}, capture_output=False)

    elif command == "logs":
        config = load_config()
        subprocess.run(f"tail -f {config['log_file']}", shell=True)

    elif command == "pf":
        if len(args) < 2:
            print("Usage: docker-vm pf <add|rm|list|static> [args]")
            sys.exit(1)
        
        sub = args[1]
        pf_args = args[2:]
        
        if sub == "add":
            if not pf_args:
                print("Usage: docker-vm pf add <guest>[:<host>]")
                sys.exit(1)
            
            target = pf_args[0]
            
            if ":" in target:
                guest, host = target.split(":", 1)
            else:
                guest, host = target, None
            
            start_tunnel(guest, host)
                
        elif sub == "rm":
            if not pf_args:
                print("Usage: docker-vm pf rm <guest>")
                sys.exit(1)
            stop_tunnel(pf_args[0])
        elif sub == "list":
            list_tunnels()
        elif sub == "static":
            if len(pf_args) < 1:
                print("Usage: docker-vm pf static <add|rm|list>")
                sys.exit(1)
            static_sub = pf_args[0]
            if static_sub == "add":
                if len(pf_args) < 3:
                    print("Usage: docker-vm pf static add <guest> <host>")
                    sys.exit(1)
                add_static_port_forward(pf_args[1], pf_args[2])
            elif static_sub == "rm":
                if len(pf_args) < 2:
                    print("Usage: docker-vm pf static rm <guest>")
                    sys.exit(1)
                remove_static_port_forward(pf_args[1])
            elif static_sub == "list":
                list_static_port_forwards()
            else:
                print(f"Unknown static subcommand: {static_sub}")
                sys.exit(1)
        else:
            print(f"Unknown pf subcommand: {sub}")
            sys.exit(1)

    elif command == "restart":
        if not CONFIG_PATH.exists():
            print("VM environment not initialized. Run 'docker-vm start' first.")
            sys.exit(1)
        print("Stopping VM... ", end="", flush=True)
        run("pkill -f qemu-system-aarch64")
        time.sleep(3)
        print("Done.")
        print("Starting VM...")
        config = load_config()
        pid = start_qemu_background(config)
        print(f"VM started (PID: {pid})")
        wait_for_ssh(config)
        # Re-establish tunnels
        tunnels = load_tunnels()
        if tunnels:
            print("Re-establishing tunnels...")
            for guest, host in tunnels.items():
                start_tunnel(guest, host)

    elif command == "stop":
        print("Stopping VM... ", end="", flush=True)
        run("pkill -f qemu-system-aarch64")
        print("Done.")

    elif command == "destroy":
        destroy_vm()

    elif command == "resize":
        if len(args) < 2:
            print("Usage: docker-vm resize <size>")
            sys.exit(1)
        resize_disk(args[1])

    else:
        print(f"Unknown command: {command}")
        sys.exit(1)

if __name__ == "__main__":
    main()
