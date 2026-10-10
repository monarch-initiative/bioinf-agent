"""A release binary this host cannot execute is host-unverifiable, not broken.

The host-side smoke verify exists to catch a wrong-architecture binary. When the binary's
own header says this host cannot run it at all — a Linux ELF on macOS, an ELF for another
CPU on Linux — a failed verify proves nothing about the install: the bytes are sha256-pinned
and freeze validates the tool inside the shipped image. The record says so (`degraded
env_manager.binary_host_unverifiable`, `host_verify: not_applicable`) instead of either lying
(proven) or failing the route (broke). A binary the host CAN run that still fails is broke.
"""
from __future__ import annotations

import struct

import pytest

import agent.mcp_server as ms
from agent.mcp_tools import env_tools
from agent.skills import env_manager as em


def _elf(machine: int, little=True) -> bytes:
    head = bytearray(64)
    head[:4] = b"\x7fELF"
    head[4] = 2
    head[5] = 1 if little else 2
    struct.pack_into("<H" if little else ">H", head, 18, machine)
    return bytes(head)


def _macho(cputype: int) -> bytes:
    return b"\xcf\xfa\xed\xfe" + struct.pack("<I", cputype) + bytes(56)


def test_the_format_is_read_off_the_header(tmp_path):
    (tmp_path / "linux").write_bytes(_elf(0x3E))
    (tmp_path / "linux_arm").write_bytes(_elf(0xB7, little=False))
    (tmp_path / "mac").write_bytes(_macho(0x0100000C))
    (tmp_path / "fat").write_bytes(b"\xca\xfe\xba\xbe" + bytes(60))
    (tmp_path / "sh").write_text("#!/bin/sh\necho hi\n")
    (tmp_path / "txt").write_text("not a program")
    assert em.executable_format(tmp_path / "linux") == {"format": "elf", "arch": "x86_64"}
    assert em.executable_format(tmp_path / "linux_arm") == {"format": "elf", "arch": "aarch64"}
    assert em.executable_format(tmp_path / "mac") == {"format": "macho", "arch": "arm64"}
    assert em.executable_format(tmp_path / "fat") == {"format": "macho", "arch": "universal"}
    assert em.executable_format(tmp_path / "sh") == {"format": "script", "arch": ""}
    assert em.executable_format(tmp_path / "txt") == {"format": "unknown", "arch": ""}
    assert em.executable_format(tmp_path / "missing") == {"format": "unknown", "arch": ""}
    assert em.executable_format(None)["format"] == "unknown"


@pytest.mark.parametrize("system, machine, fmt, expect", [
    ("Darwin", "arm64", {"format": "elf", "arch": "x86_64"}, False),
    ("Darwin", "arm64", {"format": "macho", "arch": "x86_64"}, True),       # Rosetta
    ("Darwin", "arm64", {"format": "script", "arch": ""}, True),
    ("Linux", "x86_64", {"format": "elf", "arch": "x86_64"}, True),
    ("Linux", "x86_64", {"format": "elf", "arch": "aarch64"}, False),
    ("Linux", "aarch64", {"format": "elf", "arch": "aarch64"}, True),
    ("Linux", "x86_64", {"format": "macho", "arch": "x86_64"}, False),
    ("Linux", "x86_64", {"format": "unknown", "arch": ""}, None),
    ("Linux", "x86_64", {"format": "elf", "arch": "0x9999"}, None),
])
def test_whether_this_host_can_run_it(monkeypatch, system, machine, fmt, expect):
    monkeypatch.setattr(em.platform, "system", lambda: system)
    monkeypatch.setattr(em.platform, "machine", lambda: machine)
    assert em.host_can_execute(fmt) is expect


def _route(monkeypatch, tmp_path, *, header: bytes, system: str, machine: str, verify_rc: int):
    binary = tmp_path / "share" / "tool" / "tool"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(header)
    (tmp_path / "envs" / "e").mkdir(parents=True)
    monkeypatch.setattr(ms._env_mgr, "envs_dir", tmp_path / "envs")
    monkeypatch.setattr(ms._env_mgr, "install_release_binary", lambda **kw: {
        "success": True, "tool_name": "tool", "binary_path": str(binary), "wrapper_path": "/w",
        "sha256": "a" * 64, "install_method": {"type": "binary"}, "log": [],
        "outcome": "proven", "code": "env_manager.binary_installed"})
    monkeypatch.setattr(ms._env_mgr, "run_in_env",
                        lambda *a, **k: {"returncode": verify_rc, "stdout": "", "stderr": "exec format error"})
    monkeypatch.setattr(em.platform, "system", lambda: system)
    monkeypatch.setattr(em.platform, "machine", lambda: machine)
    monkeypatch.setattr(env_tools.platform, "system", lambda: system)
    monkeypatch.setattr(env_tools.platform, "machine", lambda: machine)
    return env_tools.install_release_binary(env_name="e", tool_name="tool", url="https://x/tool.tar.gz")


def test_a_linux_binary_on_a_mac_is_host_unverifiable_and_the_install_stands(monkeypatch, tmp_path):
    r = _route(monkeypatch, tmp_path, header=_elf(0x3E), system="Darwin", machine="arm64", verify_rc=126)
    assert (r["outcome"], r["code"]) == ("degraded", "env_manager.binary_host_unverifiable")
    assert r["success"] is True and r["host_verify"] == "not_applicable"
    assert r["binary_format"] == {"format": "elf", "arch": "x86_64"}
    assert "freeze proves it inside the shipped image" in r["stderr"] and "error" not in r


def test_a_binary_the_host_can_run_that_still_fails_is_broke(monkeypatch, tmp_path):
    r = _route(monkeypatch, tmp_path, header=_elf(0x3E), system="Linux", machine="x86_64", verify_rc=1)
    assert (r["outcome"], r["code"]) == ("broke", "env_manager.binary_verify_failed")
    assert r["success"] is False and r["verify_failed"] is True and "did not execute" in r["error"]


def test_a_binary_that_verifies_is_proven(monkeypatch, tmp_path):
    r = _route(monkeypatch, tmp_path, header=_elf(0x3E), system="Linux", machine="x86_64", verify_rc=0)
    assert (r["outcome"], r["code"]) == ("proven", "env_manager.binary_installed") and r["success"] is True
