import json
import os
import subprocess
import argparse
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import importlib.util
import sys

# Dynamically import the hyphenated script
spec = importlib.util.spec_from_file_location("docker_vm", "docker-vm.py")
docker_vm = importlib.util.module_from_spec(spec)
sys.modules["docker_vm"] = docker_vm
spec.loader.exec_module(docker_vm)

@pytest.fixture(autouse=True)
def mock_state_dir(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setenv("DOCKER_VM_STATE_DIR", str(state_dir))
    monkeypatch.setattr(docker_vm, "STATE_DIR", state_dir)
    monkeypatch.setattr(docker_vm, "CONFIG_FILE", state_dir / "config.json")
    monkeypatch.setattr(docker_vm, "PID_FILE", state_dir / "docker-vm.pid")
    monkeypatch.setattr(docker_vm, "EFI_PFLASH", state_dir / "efi-pflash.raw")
    monkeypatch.setattr(docker_vm, "CLOUD_INIT_ISO", state_dir / "cloud-init.iso")
    monkeypatch.setattr(docker_vm, "USER_DATA_FILE", state_dir / "user-data")
    monkeypatch.setattr(docker_vm, "LOG_FILE", state_dir / "docker-vm.log")
    return state_dir

def test_load_config_default(mock_state_dir):
    config = docker_vm.load_config()
    assert config["image_path"] == str(mock_state_dir / "image.qcow2")
    assert "port_forwards" in config
    assert config["port_forwards"]["2222"] == "22"

def test_save_and_load_config(mock_state_dir):
    config = docker_vm._default_config()
    config["disk_size"] = "100G"
    docker_vm.save_config(config)

    loaded = docker_vm.load_config()
    assert loaded["disk_size"] == "100G"

def test_migrate_legacy_config(mock_state_dir, tmp_path, monkeypatch):
    legacy_path = tmp_path / ".docker-vm.json"
    legacy_config = {
        "image_url": "http://example.com/image.qcow2",
        "disk_size": "20G"
    }
    with open(legacy_path, "w") as f:
        json.dump(legacy_config, f)

    monkeypatch.setattr(docker_vm, "_LEGACY_CONFIG_CANDIDATES", (legacy_path,))

    migrated = docker_vm._migrate_legacy_config()
    assert migrated is not None
    assert migrated["image_url"] == "http://example.com/image.qcow2"
    assert migrated["disk_size"] == "20G"
    assert not legacy_path.exists()
    assert Path(str(legacy_path) + ".bak").exists()

@patch("shutil.which", return_value="/usr/bin/qemu-img")
@patch("subprocess.run")
def test_qemu_img_resize(mock_run, mock_which):
    docker_vm.qemu_img_resize(Path("test.qcow2"), "10G")
    mock_run.assert_called_once_with(["qemu-img", "resize", "test.qcow2", "10G"], check=True)

@patch("subprocess.run")
def test_check_docker_ready_success(mock_run):
    mock_run.return_value = MagicMock(returncode=0, stdout="24.0.5")
    assert docker_vm._check_docker_ready() is True

@patch("subprocess.run")
def test_check_docker_ready_failure(mock_run):
    mock_run.side_effect = subprocess.CalledProcessError(1, "docker")
    assert docker_vm._check_docker_ready() is False

def test_build_qemu_command(mock_state_dir):
    config = docker_vm._default_config()
    config["port_forwards"]["8080"] = "80"
    cmd = docker_vm.build_qemu_command(config)

    cmd_str = " ".join(cmd)
    assert "qemu-system-aarch64" in cmd_str
    assert "hostfwd=tcp::2222-:22" in cmd_str
    assert "hostfwd=tcp::8080-:80" in cmd_str
    assert f"file={mock_state_dir}/image.qcow2" in cmd_str

@patch("docker_vm._qemu_processes")
def test_get_qemu_pid_from_file(mock_qemu_procs, mock_state_dir):
    pid_file = mock_state_dir / "docker-vm.pid"
    pid_file.write_text("12345")

    with patch("docker_vm._pid_alive", return_value=True):
        pid = docker_vm.get_qemu_pid()
        assert pid == 12345

@patch("docker_vm.load_config")
@patch("docker_vm.get_qemu_pid")
@patch("socket.create_connection")
@patch("docker_vm._check_docker_ready")
def test_cmd_status_running(mock_docker_ready, mock_create_conn, mock_get_pid, mock_load_config):
    mock_get_pid.return_value = 12345
    mock_load_config.return_value = docker_vm._default_config()
    mock_docker_ready.return_value = True

    args = argparse.Namespace()
    assert docker_vm.cmd_status(args) == 0

@patch("docker_vm._resolve_efi_source", return_value=Path("/tmp/fake_efi"))
@patch("shutil.copyfile")
def test_ensure_efi_pflash(mock_copy, mock_resolve, mock_state_dir):
    docker_vm.ensure_efi_pflash()
    mock_copy.assert_called_once()

@patch("docker_vm.stop_qemu")
@patch("docker_vm.load_config")
@patch("pathlib.Path.exists", return_value=True)
@patch("pathlib.Path.unlink")
@patch("shutil.rmtree")
def test_cmd_destroy(mock_rmtree, mock_unlink, mock_exists, mock_load_config, mock_stop):
    mock_load_config.return_value = docker_vm._default_config()
    args = argparse.Namespace(yes=True)

    assert docker_vm.cmd_destroy(args) == 0
    assert mock_stop.called
    assert mock_unlink.called

@patch("shutil.which", return_value="/usr/bin/cloud-localds")
@patch("subprocess.run")
def test_build_cloud_init_iso(mock_run, mock_which, mock_state_dir):
    iso_path = mock_state_dir / "cloud-init.iso"
    docker_vm.build_cloud_init_iso(iso_path)
    assert (mock_state_dir / "user-data").exists()
    mock_run.assert_called_once()

@patch("subprocess.Popen")
@patch("docker_vm.get_qemu_pid", return_value=None)
@patch("docker_vm.ensure_efi_pflash")
@patch("docker_vm.build_cloud_init_iso")
def test_cmd_start_success(mock_cloud_init, mock_efi, mock_get_pid, mock_popen, mock_state_dir):
    # Setup
    image_path = mock_state_dir / "image.qcow2"
    image_path.touch()

    mock_proc = MagicMock()
    mock_proc.pid = 9999
    mock_proc.poll.return_value = None # Process still running
    mock_popen.return_value = mock_proc

    args = argparse.Namespace(wait=False)

    with patch("time.sleep"): # Speed up test
        assert docker_vm.cmd_start(args) == 0

    assert mock_popen.called
    assert (mock_state_dir / "docker-vm.pid").read_text() == "9999"

@patch("docker_vm.get_qemu_pid")
@patch("psutil.Process" if docker_vm.psutil else "os.kill")
def test_stop_qemu(mock_kill, mock_get_pid):
    mock_get_pid.return_value = 12345

    with patch("docker_vm._pid_alive") as mock_alive:
        # Initial check, loop condition, final check
        mock_alive.side_effect = [True, False, False, False, False, False]
        assert docker_vm.stop_qemu() is True

    if docker_vm.psutil:
        mock_kill.assert_called_with(12345)
        mock_kill.return_value.terminate.assert_called()

def test_cmd_pf_add(mock_state_dir):
    args = argparse.Namespace(host=8080, guest=80)
    docker_vm.cmd_pf_add(args)

    config = docker_vm.load_config()
    assert config["port_forwards"]["8080"] == "80"

def test_cmd_pf_rm(mock_state_dir):
    config = docker_vm.load_config()
    config["port_forwards"]["8080"] = "80"
    docker_vm.save_config(config)

    args = argparse.Namespace(host=8080)
    docker_vm.cmd_pf_rm(args)

    config = docker_vm.load_config()
    assert "8080" not in config["port_forwards"]
