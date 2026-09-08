"""Verify exact compiled AppArmor policy against the native kernel's readback.

Loading this dedicated profile is an explicit host provisioning operation. The
non-root replay launcher only compiles for comparison and reads securityfs; it
never loads policy or changes host security settings.
"""
from __future__ import annotations

import ctypes
import hashlib
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import execution_environment as environment

NAME = "wikilean-replay-v1"
PARSER = Path("/usr/sbin/apparmor_parser")
SECURITY_ROOT = Path("/sys/kernel/security/apparmor/policy")
APPARMORFS_MAGIC = 0x5A3C69F0
PROFILE_TEXT = """# WikiLean replay only: outer Docker namespace/capability restrictions and
# mandatory inner bubblewrap enforce the closed input/write/network boundary.
# Allow namespace mount setup; the reducer receives no effective capabilities.
# Docker's masked proc paths are replaced by a fresh inner PID namespace procfs.
profile wikilean-replay-v1 flags=(attach_disconnected,mediate_deleted) {
  file,
  network,
  capability,
  mount,
  umount,
  pivot_root,
  userns,
  signal,
  ptrace (trace,read,tracedby,readby) peer=wikilean-replay-v1,
  deny /proc/sysrq-trigger rwklx,
  deny /proc/{mem,kmem,kcore} rwklx,
  deny /proc/{sys,irq,bus,fs}/** wklx,
  deny /sys/** wklx,
  deny /sys/kernel/security/** rwklx,
}
"""


class AppArmorError(ValueError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AppArmorError(message)


def validate_policy(value: dict) -> dict:
    require(isinstance(value, dict) and set(value) == {
        "name", "text", "text_sha256", "binary", "binary_sha256", "binary_bytes",
        "parser_sha256", "parser_version", "kernel_abi"}, "unexpected AppArmor policy fields")
    require(value["name"] == NAME and value["text"] == PROFILE_TEXT,
            "AppArmor policy must equal the reviewed replay profile")
    require(value["text_sha256"] == hashlib.sha256(PROFILE_TEXT.encode()).hexdigest(),
            "AppArmor text digest mismatch")
    require(value["binary"] == NAME + ".bin", "unexpected AppArmor artifact filename")
    for field in ("binary_sha256", "parser_sha256"):
        environment._digest(value[field], "apparmor." + field)
    require(type(value["binary_bytes"]) is int and 0 < value["binary_bytes"] <= 16 * 1024**2,
            "invalid compiled AppArmor policy size")
    environment._exact_version(value["parser_version"], "apparmor.parser_version")
    require(isinstance(value["kernel_abi"], str) and re.fullmatch(r"v[0-9]{1,3}", value["kernel_abi"]) is not None,
            "invalid AppArmor kernel policy ABI")
    return value


def verify_artifact(value: dict, artifacts: Path) -> bytes:
    validate_policy(value)
    path = artifacts / value["binary"]
    require(environment.secure_file_digest(path) == (value["binary_sha256"], value["binary_bytes"]),
            "compiled AppArmor artifact differs from sealed policy")
    raw = path.read_bytes()
    require((hashlib.sha256(raw).hexdigest(), len(raw)) == (value["binary_sha256"], value["binary_bytes"]),
            "compiled AppArmor artifact changed while read")
    return raw


def _kernel_text(path: Path) -> str:
    """Read bounded root-owned securityfs data; never accept a normal file."""
    require(sys.platform.startswith("linux"), "AppArmor readback requires native Linux securityfs")
    # policy itself is a kernel magic link (apparmorfs:[id]), so filesystem
    # realpath cannot resolve it. raw_* links also belong to that kernel-only
    # filesystem. Validate the opened FD's AAFS_MAGIC rather than trusting a
    # userspace path, text label, or a fake ordinary file mounted at the path.
    require(path.is_absolute() and path.is_relative_to(SECURITY_ROOT) and ".." not in path.parts,
            "AppArmor readback escaped securityfs policy")
    flags = os.O_RDONLY | os.O_CLOEXEC
    if path.name not in {"raw_sha256", "raw_abi"}:
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        require(stat.S_ISREG(metadata.st_mode) and metadata.st_uid == 0 and not metadata.st_mode & 0o222,
                "AppArmor readback must be root-owned read-only kernel data")
        buffer = (ctypes.c_long * 32)()
        libc = ctypes.CDLL(None, use_errno=True)
        function = libc.fstatfs
        function.argtypes = [ctypes.c_int, ctypes.c_void_p]
        function.restype = ctypes.c_int
        require(function(descriptor, ctypes.byref(buffer)) == 0 and buffer[0] == APPARMORFS_MAGIC,
                "AppArmor readback is not from the native securityfs filesystem")
        raw = os.read(descriptor, 4097)
        require(len(raw) <= 4096 and raw.endswith(b"\n"), "invalid bounded AppArmor kernel record")
        return raw[:-1].decode("ascii", "strict")
    finally:
        os.close(descriptor)


def loaded_policy(value: dict) -> dict:
    validate_policy(value)
    matches = [path for path in (SECURITY_ROOT / "profiles").iterdir()
               if re.fullmatch(re.escape(NAME) + r"\.[0-9]+", path.name)]
    require(len(matches) == 1, "exactly one loaded replay AppArmor profile is required")
    path = matches[0]
    before = _kernel_text(path / "sha256")
    observation = {key: _kernel_text(path / filename) for key, filename in (
        ("name", "name"), ("mode", "mode"), ("binary_sha256", "raw_sha256"), ("kernel_abi", "raw_abi"))}
    require(observation == {"name": NAME, "mode": "enforce", "binary_sha256": value["binary_sha256"],
                            "kernel_abi": value["kernel_abi"]}, "loaded AppArmor policy differs from the reviewed compiled artifact")
    environment._digest(before, "kernel profile digest")
    require(_kernel_text(path / "sha256") == before, "loaded AppArmor profile changed during readback")
    return {**observation, "kernel_profile_sha256": before, "text_sha256": value["text_sha256"],
            "parser_sha256": value["parser_sha256"]}


def verify_loaded(value: dict, artifacts: Path) -> dict:
    binary = verify_artifact(value, artifacts)
    require(PARSER.resolve(strict=True) == PARSER and
            environment.secure_file_digest(PARSER)[0] == value["parser_sha256"], "AppArmor parser differs from sealed compiler")
    env = {"PATH": "/nonexistent", "HOME": "/nonexistent", "LANG": "C", "LC_ALL": "C"}
    version = subprocess.run([str(PARSER), "--config-file", "/dev/null", "--version"], env=env,
                             capture_output=True, check=False, timeout=10)
    require(version.returncode == 0 and bool(version.stdout.splitlines()) and version.stdout.splitlines()[0] ==
            ("AppArmor parser version " + value["parser_version"]).encode(), "AppArmor compiler version mismatch")
    compiled = subprocess.run([str(PARSER), "--config-file", "/dev/null", "--skip-cache", "--skip-kernel-load",
        "--stdout", "--quiet"], input=PROFILE_TEXT.encode(), env=env, capture_output=True, check=False, timeout=30)
    require(compiled.returncode == 0 and not compiled.stderr and compiled.stdout == binary,
            "reviewed AppArmor text did not reproduce the exact compiled policy")
    require(environment.secure_file_digest(PARSER)[0] == value["parser_sha256"], "AppArmor parser changed during compilation")
    return loaded_policy(value)
