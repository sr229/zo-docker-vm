#!/usr/bin/env python3
import subprocess
import os
import sys
from pathlib import Path
import json

CONFIG_PATH = Path("/home/workspace/.docker-vm.json")

def load_config():
    with open(CONFIG_PATH, "r") as f:
        return json.load(f)

def save_config(config):
    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=4)

def run(cmd, args=None, env=None):
    """Helper to run shell commands and stream output."""
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
            stdout=subprocess.PIPE if "logs" in cmd else None,
            text=True
        )
        
        if "logs" in cmd:
            # Tail -f style streaming
            while True:
                line = process.stdout.readline()
                if not line:
                    break
                print(line, end="")
        else:
            process.wait()
            return process.returncode
    except Exception as e:
        print(f"Error executing command: {e}")
        return 1

def resize_disk(size):
    config = load_config()
    image = config["image_path"]
    print(f"Resizing disk {image} to {size}...")
    
    # 1. Resize the qcow2 file
    subprocess.run(["qemu-img", "resize", image, size], check=True)
    
    # 2. Update config
    config["disk_size"] = size
    save_config(config)
    
    print("Disk resized. Please run 'docker-vm restart' to apply changes.")
    print("Note: File system expansion will happen automatically on boot if configured, or manually via 'docker-vm expand'.")

def init_vm(image_url=None, disk_size="50G"):
    # Check if environment is already initialized
    if CONFIG_PATH.exists() or Path("/home/workspace/debian-arm64.qcow2").exists():
        print("⚠️  A Docker VM environment already exists.")
        print("If you want to start a fresh environment, please run 'docker-vm destroy' first.")
        print("\nIf you just want to ensure the VM is running, please use 'docker-vm status' or 'docker-vm restart'.")
        sys.exit(0)

    print("Initializing Docker VM environment...")
    
    if not image_url:
        image_url = "https://cloud.debian.org/images/cloud/bookworm/latest/debian-12-generic-arm64.qcow2"
    
    image_path = "/home/workspace/debian-arm64.qcow2"
    iso_path = "/home/workspace/cloud-init.iso"
    
    # 1. Download Image
    print(f"Downloading image from {image_url}...")
    subprocess.run(["curl", "-L", image_url, "-o", image_path], check=True)
    
    # 2. Setup Cloud-Init
    print("Generating cloud-init configuration...")
    user_data = """#cloud-config
password: debian
chpasswd: { expire: False }
ssh_pwauth: True
"""
    with open("/home/workspace/user-data", "w") as f:
        f.write(user_data)
    
    subprocess.run(["cloud-localds", iso_path, "/home/workspace/user-data"], check=True)
    
    # 3. Resize Image
    print(f"Setting disk size to {disk_size}...")
    subprocess.run(["qemu-img", "resize", image_path, disk_size], check=True)
    
    # 4. Update Config
    config = {
        "image_path": image_path,
        "disk_size": disk_size,
        "vm_ip": "localhost",
        "vm_ssh_port": "2222",
        "vm_ssh_user": "debian",
        "vm_ssh_pass": "debian",
        "docker_host": "ssh://debian@localhost:2222",
        "log_file": "/tmp/qemu-arm64.log"
    }
    save_config(config)
    
    print("\n✅ Environment initialized successfully!")
    print("Run 'docker-vm restart' to boot the new environment.")

def destroy_vm():
    print("Tearing down the Docker VM environment...")
    
    # 1. Stop any running VM processes
    subprocess.run(["pkill", "-f", "qemu-system-aarch64"])
    
    # 2. Delete files
    config = load_config()
    files_to_remove = [
        config.get("image_path", "/home/workspace/debian-arm64.qcow2"),
        CONFIG_PATH,
        "/home/workspace/cloud-init.iso",
        "/home/workspace/user-data"
    ]
    
    for file_path in files_to_remove:
        try:
            os.remove(file_path)
            print(f"Removed {file_path}")
        except FileNotFoundError:
            pass
        except Exception as e:
            print(f"Error removing {file_path}: {e}")
            
    print("\n✅ Environment destroyed. You can start fresh with 'docker-vm start'.")

def add_port_forward(guest_port, host_port=None):
    config = load_config()
    pf = config.get("port_forwards", {})
    
    # Default host port to guest port if not provided
    if host_port is None:
        host_port = guest_port
        
    pf[guest_port] = host_port
    config["port_forwards"] = pf
    save_config(config)
    print(f"Added local port forward: {host_port} (host) -> {guest_port} (guest)")
    print("Please run 'docker-vm restart' to apply the changes.")

def request_zo_exposure(guest_port):
    config = load_config()
    requests = config.get("zo_requests", [])
    if guest_port not in requests:
        requests.append(guest_port)
        config["zo_requests"] = requests
        save_config(config)
    
    print(f"Requested Zo service exposure for port {guest_port}.")
    print("\n✨ To finalize this, please tell your AI assistant:")
    print(f"  'Expose VM port {guest_port} as a Zo service'")

def remove_port_forward(guest_port):
    config = load_config()
    pf = config.get("port_forwards", {})
    if guest_port in pf:
        del pf[guest_port]
        config["port_forwards"] = pf
        save_config(config)
        print(f"Removed port forward for {guest_port}")
        print("Please run 'docker-vm restart' to apply the changes.")
    else:
        print(f"No port forward found for port {guest_port}")

def main():
    args = sys.argv[1:]
    if not args or args[0] == "help":
        print("""
Docker VM Manager - ARM64 Emulation (Python Edition)

Usage:
  docker-vm <command> [args]

Commands:
  start [--image url] [--size size]  Initialize and setup the VM environment
  status    Check if the VM and Docker daemon are running
  shell     SSH into the VM
  docker    Run a docker command inside the VM
  logs      Tail the QEMU logs
  pf <subcommand> [args]            Manage port forwarding
    add <guest>[:<host>] [--host|--zo]  Forward port (default: --host)
    rm <guest>                      Remove port forward
  resize    Resize the VM disk (e.g., docker-vm resize 100G)
  restart   Restart the VM service
  stop      Stop the VM service
  destroy   Wipe the VM disk and configuration completely
  help      Show this help
        """)
        sys.exit(0)

    command = args[0]

    if command == "start":
        image_url = None
        disk_size = "50G"
        
        # Simple argument parsing for --image and --size
        for i in range(1, len(args)):
            if args[i] == "--image" and i + 1 < len(args):
                image_url = args[i+1]
            elif args[i] == "--size" and i + 1 < len(args):
                disk_size = args[i+1]
        
        init_vm(image_url, disk_size)

    elif command == "status":
        print("Checking Docker daemon status...")
        config = load_config()
        status = run("docker info", env={"DOCKER_HOST": config["docker_host"]})
        if status == 0:
            print("\n✅ Docker VM is healthy and responding!")
        else:
            print("\n❌ Docker VM is not responding. Try 'docker-vm restart'.")

    elif command == "shell":
        config = load_config()
        cmd = f"sshpass -p {config['vm_ssh_pass']} ssh -p {config['vm_ssh_port']} -o StrictHostKeyChecking=no {config['vm_ssh_user']}@{config['vm_ip']}"
        run(cmd)

    elif command == "docker":
        docker_args = args[1:]
        if not docker_args:
            print("Please provide a docker command. Example: docker-vm docker ps")
            sys.exit(1)
        config = load_config()
        run("docker", docker_args, env={"DOCKER_HOST": config["docker_host"]})

    elif command == "logs":
        config = load_config()
        run(f"tail -f {config['log_file']}")

    elif command == "pf":
        if len(args) < 2:
            print("Usage: docker-vm pf <add|rm> [args]")
            sys.exit(1)
        
        sub = args[1]
        pf_args = args[2:]
        
        if sub == "add":
            if not pf_args:
                print("Usage: docker-vm pf add <guest>[:<host>] [--host|--zo]")
                sys.exit(1)
            
            target = pf_args[0]
            flags = pf_args[1:]
            
            # Parse guest:host
            if ":" in target:
                guest, host = target.split(":", 1)
            else:
                guest, host = target, None
            
            if "--zo" in flags:
                request_zo_exposure(guest)
            else:
                # Default to --host if not specified or explicitly requested
                add_port_forward(guest, host)
                
        elif sub == "rm":
            if not pf_args:
                print("Usage: docker-vm pf rm <guest>")
                sys.exit(1)
            remove_port_forward(pf_args[0])
        else:
            print(f"Unknown pf subcommand: {sub}")
            sys.exit(1)

    elif command == "restart":
        print("Restarting VM service...")
        run("pkill -f qemu-system-aarch64")
        print("Service will be auto-restarted by Zo in a few seconds.")

    elif command == "stop":
        print("Stopping VM service...")
        run("pkill -f qemu-system-aarch64")
        print("VM process stopped. Note: Zo may auto-restart it.")

    elif command == "destroy":
        destroy_vm()

    elif command == "resize":
        if len(args) < 2:
            print("Usage: docker-vm resize <size>")
            sys.exit(1)
        resize_disk(args[1])

    else:
        # Special case to keep old commands working if you prefer, 
        # but for a clean API we'll move them to 'pf'
        if command == "pf-add":
            # Delegate to pf add
            # This is just a helper for backward compatibility
            pass
        else:
            print(f"Unknown command: {command}")
            sys.exit(1)

if __name__ == "__main__":
    main()
