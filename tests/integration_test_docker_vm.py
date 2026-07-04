import os
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
        def fake_download(url, dest):
            if url.endswith(".gz"):
                import gzip
                with gzip.open(dest, "wb") as f:
                    f.write(b"EXTENDED CONTENT TO MAKE IT AT LEAST 1M" * 100)
            else:
                subprocess.run(["cp", str(dummy_image), str(dest)], check=True)

        m.setattr(docker_vm, "download_image", fake_download)

        rc = docker_vm.main(["init", "--size", "2M"])
        assert rc == 0

    assert (state_dir / "config.json").exists()
    assert (state_dir / "image.qcow2").exists()
    assert (state_dir / "cloud-init.iso").exists()
    assert (state_dir / "efi-pflash.raw").exists()

    result = subprocess.run(["qemu-img", "info", "--output=json", str(state_dir / "image.qcow2")], capture_output=True, text=True, check=True)
    info = json.loads(result.stdout)
    assert info["virtual-size"] == 2097152

def test_init_shorthand_integration(state_dir, workspace, monkeypatch):
    with monkeypatch.context() as m:
        urls_called = []
        def fake_download(url, dest):
            urls_called.append(url)
            import gzip
            with gzip.open(dest, "wb") as f:
                f.write(b"SHORTHAND CONTENT")

        m.setattr(docker_vm, "download_image", fake_download)

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

def test_resize_integration(state_dir):
    docker_vm.save_config(docker_vm._default_config())
    image_path = state_dir / "image.qcow2"
    subprocess.run(["qemu-img", "create", "-f", "qcow2", str(image_path), "1M"], check=True)

    rc = docker_vm.main(["resize", "3M"])
    assert rc == 0

    result = subprocess.run(["qemu-img", "info", "--output=json", str(image_path)], capture_output=True, text=True, check=True)
    info = json.loads(result.stdout)
    assert info["virtual-size"] == 3145728

def test_resize_alignment_integration(state_dir):
    docker_vm.save_config(docker_vm._default_config())
    image_path = state_dir / "image.qcow2"
    subprocess.run(["qemu-img", "create", "-f", "qcow2", str(image_path), "1M"], check=True)

    # 1500K is not MiB aligned, should be aligned to 2MiB
    rc = docker_vm.main(["resize", "1500K"])
    assert rc == 0

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
        def fake_download(url, dest):
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

def test_env_command():
    from io import StringIO
    stdout = StringIO()
    sys.stdout = stdout
    try:
        rc = docker_vm.main(["env"])
        assert rc == 0
        assert "export DOCKER_HOST=" in stdout.getvalue()
    finally:
        sys.stdout = sys.__stdout__


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
