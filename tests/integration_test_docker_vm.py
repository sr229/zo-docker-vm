import os
import shutil
import subprocess
import sys
import json
from pathlib import Path
import pytest
import importlib.util
import time

# Dynamically import the hyphenated script
spec = importlib.util.spec_from_file_location("docker_vm", "docker-vm.py")
docker_vm = importlib.util.module_from_spec(spec)
sys.modules["docker_vm"] = docker_vm
spec.loader.exec_module(docker_vm)

@pytest.fixture
def state_dir(tmp_path):
    d = tmp_path / "state"
    d.mkdir()
    return d

@pytest.fixture
def workspace(tmp_path):
    w = tmp_path / "workspace"
    w.mkdir()
    return w

@pytest.fixture(autouse=True)
def setup_env(state_dir, workspace, monkeypatch):
    monkeypatch.setenv("DOCKER_VM_STATE_DIR", str(state_dir))
    monkeypatch.setenv("DOCKER_VM_WORKSPACE", str(workspace))

    # Mock _check_dependencies to avoid failing on missing system tools in test environment
    monkeypatch.setattr(docker_vm, "_check_dependencies", lambda: None)

    # Checksum sidecar lookup hits the network; keep tests offline/deterministic.
    # Individual tests can override this if they want to exercise verification.
    monkeypatch.setattr(docker_vm, "_fetch_checksum_sidecar", lambda url: None)

    # Mock subprocess.run and Popen for calls to external tools like qemu-img, cloud-localds
    # We only mock them if they are not found on the system to allow real tests when possible.
    if not shutil.which("qemu-img"):
        monkeypatch.setattr(docker_vm, "qemu_img_resize", lambda image, size: None)

    # Always mock build_cloud_init_iso if cloud-localds is missing
    if not shutil.which("cloud-localds"):
        def fake_build_iso(iso_path):
            docker_vm._ensure_ssh_key()
            iso_path.touch()
        monkeypatch.setattr(docker_vm, "build_cloud_init_iso", fake_build_iso)

    # Update globals in docker_vm
    monkeypatch.setattr(docker_vm, "STATE_DIR", state_dir)
    monkeypatch.setattr(docker_vm, "CONFIG_FILE", state_dir / "config.json")
    monkeypatch.setattr(docker_vm, "PID_FILE", state_dir / "docker-vm.pid")
    monkeypatch.setattr(docker_vm, "EFI_PFLASH", state_dir / "efi-pflash.raw")
    monkeypatch.setattr(docker_vm, "CLOUD_INIT_ISO", state_dir / "cloud-init.iso")
    monkeypatch.setattr(docker_vm, "USER_DATA_FILE", state_dir / "user-data")
    monkeypatch.setattr(docker_vm, "LOG_FILE", state_dir / "docker-vm.log")

    # Fake EFI
    fake_efi = state_dir / "FAKE_EFI.fd"
    fake_efi.write_bytes(b"EFI CONTENT")
    monkeypatch.setenv("DOCKER_VM_EFI", str(fake_efi))

def test_efi_padding(state_dir, monkeypatch):
    # Fake EFI
    fake_efi = state_dir / "FAKE_EFI_SOURCE.fd"
    fake_efi.write_bytes(b"A" * 1024 * 1024) # 1MiB
    monkeypatch.setenv("DOCKER_VM_EFI", str(fake_efi))

    pflash = docker_vm.ensure_efi_pflash()
    assert pflash.exists()
    assert pflash.stat().st_size == 64 * 1024 * 1024

    # Test that it detects wrong size and re-pads
    pflash.unlink()
    pflash.write_bytes(b"WRONG SIZE")
    pflash = docker_vm.ensure_efi_pflash()
    assert pflash.stat().st_size == 64 * 1024 * 1024

def test_init_integration(state_dir, workspace, monkeypatch):
    # Use our local dummy image
    dummy_image = Path("tests/dummy.qcow2").absolute()
    if not dummy_image.exists():
        subprocess.run(["qemu-img", "create", "-f", "qcow2", str(dummy_image), "1M"], check=True)

    with monkeypatch.context() as m:
        def fake_download(url, dest, **kwargs):
            if url.endswith(".gz"):
                import gzip
                with gzip.open(dest, "wb") as f:
                    f.write(b"EXTENDED CONTENT TO MAKE IT AT LEAST 1M" * 100)
            else:
                shutil.copy(str(dummy_image), str(dest))

        m.setattr(docker_vm, "download_image", fake_download)

        # Mock qemu-img convert call within init if qemu-img is missing
        if not shutil.which("qemu-img"):
            original_run = subprocess.run
            def fake_run(args, **kwargs):
                if "convert" in args:
                    dest = args[-1]
                    Path(dest).touch()
                    return subprocess.CompletedProcess(args, 0)
                return original_run(args, **kwargs)
            m.setattr(subprocess, "run", fake_run)

        rc = docker_vm.main(["init", "--size", "2M"])
        assert rc == 0

    assert (state_dir / "config.json").exists()
    assert (state_dir / "image.qcow2").exists()
    assert (state_dir / "cloud-init.iso").exists()
    assert (state_dir / "efi-pflash.raw").exists()

    if shutil.which("qemu-img"):
        result = subprocess.run(["qemu-img", "info", "--output=json", str(state_dir / "image.qcow2")], capture_output=True, text=True, check=True)
        info = json.loads(result.stdout)
        assert info["virtual-size"] == 2097152

def test_migration(state_dir, monkeypatch):
    config_data = {
        "image_path": "/custom/path/image.qcow2",
        "disk_size": "20G"
    }

    # Migration uses _LEGACY_CONFIG_CANDIDATES which are Path objects.
    # It's easier to mock the whole function or the candidates.
    monkeypatch.setattr(docker_vm, "_LEGACY_CONFIG_CANDIDATES", [Path(state_dir / ".docker-vm.json")])

    legacy_file = state_dir / ".docker-vm.json"
    with legacy_file.open("w") as f:
        json.dump(config_data, f)

    config = docker_vm.load_config()
    assert config["disk_size"] == "20G"
    assert config["image_path"] == "/custom/path/image.qcow2"
    assert config["_migrated"] is True
    assert not legacy_file.exists()
    assert (state_dir / ".docker-vm.json.bak").exists()


def test_init_shorthand_integration(state_dir, workspace, monkeypatch):
    with monkeypatch.context() as m:
        urls_called = []
        def fake_download(url, dest, **kwargs):
            urls_called.append(url)
            import gzip
            with gzip.open(dest, "wb") as f:
                f.write(b"SHORTHAND CONTENT")

        m.setattr(docker_vm, "download_image", fake_download)

        # Mock qemu-img convert call within init if qemu-img is missing
        if not shutil.which("qemu-img"):
            original_run = subprocess.run
            def fake_run(args, **kwargs):
                if "convert" in args:
                    dest = args[-1]
                    Path(dest).touch()
                    return subprocess.CompletedProcess(args, 0)
                return original_run(args, **kwargs)
            m.setattr(subprocess, "run", fake_run)

        rc = docker_vm.main(["init", "--image", "containerd", "--size", "1M"])
        assert rc == 0

        assert any("containerd.raw.gz" in url for url in urls_called)
        assert (state_dir / "image.qcow2").exists()

def test_pf_integration(state_dir):
    docker_vm.save_config(docker_vm._default_config())

    # New order: pf add <guest> [<host>]
    # So to map host 8080 to guest 80:
    rc = docker_vm.main(["pf", "add", "80", "8080"])
    assert rc == 0
    config = docker_vm.load_config()
    assert config["port_forwards"]["8080"] == "80"

    rc = docker_vm.main(["pf", "ls"])
    assert rc == 0

    rc = docker_vm.main(["pf", "rm", "8080"])
    assert rc == 0
    config = docker_vm.load_config()
    assert "8080" not in config["port_forwards"]

def test_status_not_running(state_dir):
    docker_vm.save_config(docker_vm._default_config())
    rc = docker_vm.main(["status"])
    assert rc != 0

def test_status_running(state_dir, monkeypatch):
    docker_vm.save_config(docker_vm._default_config())
    (state_dir / "docker-vm.pid").write_text("12345")
    monkeypatch.setattr(docker_vm, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(docker_vm, "_check_docker_ready", lambda: True)

    # Mock socket to simulate SSH port open
    class MockSocket:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def bind(self, addr): pass
    monkeypatch.setattr("socket.create_connection", lambda addr, timeout: MockSocket())

    rc = docker_vm.main(["status"])
    assert rc == 0


def test_restart_integration(state_dir, monkeypatch):
    docker_vm.save_config(docker_vm._default_config())
    (state_dir / "image.qcow2").touch()

    # Mock stop and start
    stop_called = False
    start_called = False

    def mock_stop(args):
        nonlocal stop_called
        stop_called = True
        return 0

    def mock_start(args):
        nonlocal start_called
        start_called = True
        return 0

    monkeypatch.setattr(docker_vm, "cmd_stop", mock_stop)
    monkeypatch.setattr(docker_vm, "cmd_start", mock_start)

    rc = docker_vm.main(["restart"])
    assert rc == 0
    assert stop_called
    assert start_called


def test_logs_integration(state_dir):
    docker_vm.save_config(docker_vm._default_config())
    log_file = state_dir / "docker-vm.log"
    log_file.write_text("Hello Log")

    # We need to capture stdout to verify logs output
    from io import StringIO
    stdout = StringIO()
    sys.stdout = stdout
    try:
        rc = docker_vm.main(["logs"])
        assert rc == 0
        assert "Hello Log" in stdout.getvalue()
    finally:
        sys.stdout = sys.__stdout__

def test_resize_integration(state_dir, monkeypatch):
    docker_vm.save_config(docker_vm._default_config())
    image_path = state_dir / "image.qcow2"
    if shutil.which("qemu-img"):
        subprocess.run(["qemu-img", "create", "-f", "qcow2", str(image_path), "1M"], check=True)
    else:
        image_path.touch()

    rc = docker_vm.main(["resize", "3M"])
    assert rc == 0

    if shutil.which("qemu-img"):
        result = subprocess.run(["qemu-img", "info", "--output=json", str(image_path)], capture_output=True, text=True, check=True)
        info = json.loads(result.stdout)
        assert info["virtual-size"] == 3145728

def test_resize_alignment_integration(state_dir, monkeypatch):
    docker_vm.save_config(docker_vm._default_config())
    image_path = state_dir / "image.qcow2"
    if shutil.which("qemu-img"):
        subprocess.run(["qemu-img", "create", "-f", "qcow2", str(image_path), "1M"], check=True)
    else:
        image_path.touch()

    # 1500K is not MiB aligned, should be aligned to 2MiB
    rc = docker_vm.main(["resize", "1500K"])
    assert rc == 0

    if shutil.which("qemu-img"):
        result = subprocess.run(["qemu-img", "info", "--output=json", str(image_path)], capture_output=True, text=True, check=True)
        info = json.loads(result.stdout)
        assert info["virtual-size"] == 2 * 1024 * 1024

def test_destroy_integration(state_dir):
    docker_vm.save_config(docker_vm._default_config())
    (state_dir / "image.qcow2").touch()

    # Run destroy with --yes
    rc = docker_vm.main(["destroy", "--yes"])
    assert rc == 0
    assert not (state_dir / "image.qcow2").exists()
    assert not (state_dir / "config.json").exists()

def test_destroy_safety(state_dir, monkeypatch):
    docker_vm.save_config(docker_vm._default_config())
    (state_dir / "image.qcow2").touch()

    # Mock stop_qemu to fail
    monkeypatch.setattr(docker_vm, "stop_qemu", lambda: False)

    rc = docker_vm.main(["destroy", "--yes"])
    assert rc == 1
    # Files should still exist
    assert (state_dir / "image.qcow2").exists()
    assert (state_dir / "config.json").exists()

def test_start_already_running(state_dir, monkeypatch):
    docker_vm.save_config(docker_vm._default_config())
    (state_dir / "image.qcow2").touch()
    (state_dir / "docker-vm.pid").write_text("999999") # Hopefully non-existent

    # Mock _pid_alive to simulate it IS running
    monkeypatch.setattr(docker_vm, "_pid_alive", lambda pid: True if pid == 999999 else False)
    # Mock _check_docker_ready to return True so it doesn't try to wait
    monkeypatch.setattr(docker_vm, "_check_docker_ready", lambda: True)

    rc = docker_vm.main(["start"])
    assert rc == 0 # Should report already running


def test_start_auto_init(state_dir, workspace, monkeypatch):
    docker_vm.save_config(docker_vm._default_config())
    # image.qcow2 does NOT exist

    with monkeypatch.context() as m:
        def fake_download(url, dest, **kwargs):
            import gzip
            with gzip.open(dest, "wb") as f:
                f.write(b"DUMMY DATA")

        def fake_run(args, **kwargs):
            if "convert" in args:
                Path(args[-1]).touch()
            return subprocess.CompletedProcess(args, 0)

        m.setattr(subprocess, "run", fake_run)
        m.setattr(docker_vm, "download_image", fake_download)
        m.setattr(docker_vm, "download_image", fake_download)
        m.setattr(docker_vm, "qemu_img_resize", lambda image, size: None)
        m.setattr(docker_vm, "build_cloud_init_iso", lambda iso: iso.touch())
        # Mock Popen to avoid actually starting QEMU
        class FakeProc:
            def __init__(self, args, **kwargs):
                self.args = args
                self.pid = 12345
                self.returncode = 0
                self.stdout = None
                self.stderr = None
            def poll(self): return None # Return None to simulate still running
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def wait(self, timeout=None): return 0
            def communicate(self, input=None, timeout=None): return (None, None)
            def kill(self): pass
            def terminate(self): pass
        m.setattr(subprocess, "Popen", FakeProc)
        m.setattr(docker_vm, "_wait_for_ready", lambda port: True)

        rc = docker_vm.main(["start"])
        assert rc == 0

    assert (state_dir / "image.qcow2").exists()

def test_env_command(capsys):
    rc = docker_vm.main(["env"])
    assert rc == 0
    out, err = capsys.readouterr()
    assert "export DOCKER_HOST=" in out
    assert "export DOCKER_VM_WORKSPACE=" in out
    assert "WARNING: The VM's SSH key is not yet authorized" in err
    assert "ssh-add" in err


def test_stop_integration(state_dir, monkeypatch):
    docker_vm.save_config(docker_vm._default_config())
    pid_file = state_dir / "docker-vm.pid"
    pid_file.write_text("999999")

    # Mock _pid_alive and os.kill
    monkeypatch.setattr(docker_vm, "_pid_alive", lambda pid: True if pid == 999999 else False)
    monkeypatch.setattr(os, "kill", lambda pid, sig: None)

    # Mock psutil if it exists
    if docker_vm.psutil:
        class FakeProcess:
            def __init__(self, pid): self.pid = pid
            def terminate(self): pass
            def kill(self): pass
        monkeypatch.setattr(docker_vm.psutil, "Process", FakeProcess)

    # We need to make it "exit" eventually in the loop
    alive_status = [True, True, False]
    def mock_alive(pid):
        return alive_status.pop(0) if alive_status else False
    monkeypatch.setattr(docker_vm, "_pid_alive", mock_alive)

    rc = docker_vm.main(["stop"])
    assert rc == 0
    assert not pid_file.exists()


def test_docker_command(monkeypatch):
    docker_vm.save_config(docker_vm._default_config())

    calls = []
    def mock_call(cmd):
        calls.append(cmd)
        return 0

    monkeypatch.setattr(subprocess, "call", mock_call)
    # Mock sys.stdin.isatty to be False to avoid -t
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)

    rc = docker_vm.main(["docker", "ps"])
    assert rc == 0
    assert any("docker ps" in c for c in [" ".join(call) for call in calls])
    assert any("ssh" == call[0] for call in calls)


def test_shell_setup_integration(capsys):
    rc = docker_vm.main(["shell-setup"])
    assert rc == 0
    out, err = capsys.readouterr()
    assert "_docker_vm_activate" in out


def test_pf_live_tunnel(state_dir, monkeypatch):
    docker_vm.save_config(docker_vm._default_config())
    (state_dir / "docker-vm.pid").write_text("12345")
    monkeypatch.setattr(docker_vm, "_pid_alive", lambda pid: True)

    tunnels_started = []
    def mock_start_tunnel(host_port, guest_port, ssh_port, key_path):
        tunnels_started.append((host_port, guest_port))
        return 999 # Fake tunnel PID

    monkeypatch.setattr(docker_vm, "_start_ssh_tunnel", mock_start_tunnel)
    monkeypatch.setattr(docker_vm, "_is_port_available", lambda p: True)

    rc = docker_vm.main(["pf", "add", "8080", "9090"])
    assert rc == 0
    assert (9090, 8080) in tunnels_started

    tunnels = docker_vm._load_tunnels()
    assert "9090" in tunnels

    # Test removing live tunnel
    tunnels_stopped = []
    monkeypatch.setattr(docker_vm, "_stop_ssh_tunnel", lambda pid: tunnels_stopped.append(pid))

    rc = docker_vm.main(["pf", "rm", "9090"])
    assert rc == 0
    assert 999 in tunnels_stopped
    assert "9090" not in docker_vm._load_tunnels()


def test_port_collision_resolution(monkeypatch):
    # Mock _is_port_available to simulate port 2222 is busy
    def fake_is_port_available(port):
        if port == 2222:
            return False
        return True

    monkeypatch.setattr(docker_vm, "_is_port_available", fake_is_port_available)

    port = docker_vm._find_available_port(2222)
    assert port == 2223


def test_build_qemu_command_with_collision(state_dir, monkeypatch):
    config = docker_vm._default_config()
    config["port_forwards"] = {"2222": 22}

    # Mock _is_port_available: 2222 busy, 2223 free
    monkeypatch.setattr(docker_vm, "_is_port_available", lambda p: p != 2222)

    cmd, resolved = docker_vm.build_qemu_command(config)
    assert resolved["22"] == 2223
    assert "hostfwd=tcp::2223-:22" in " ".join(cmd)
