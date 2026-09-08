"""Exact pure-Python PostgreSQL driver closure for the public mirror acquirer.

Only the explicitly selected packages enter sys.path resolution. Python source
is compiled from captured bytes; timestamp-valid bytecode caches are never read.
"""
from __future__ import annotations

import email.parser
import hashlib
import importlib.abc
import importlib.machinery
import importlib.metadata
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "brain/tools"))
import execution_environment as environment

PACKAGES = {"pg8000": ("pg8000", "1.31.5"), "scramp": ("scramp", "1.4.17"),
    "asn1crypto": ("asn1crypto", "1.5.1"), "dateutil": ("python_dateutil", "2.9.0.post0"),
    "six": ("six", "1.17.0")}
LOADED_FILES = None
LOADED_ROOT = None


def require(condition, message):
    if not condition:
        raise ValueError(message)


def capture_files(site_packages):
    require(site_packages.is_absolute() and site_packages.resolve(strict=True) == site_packages,
        "driver package root requires real absolute ancestry")
    files = {}
    for module, (distribution, version) in PACKAGES.items():
        package = site_packages / (module + ".py" if module == "six" else module)
        metadata_root = site_packages / (distribution + "-" + version + ".dist-info")
        require(package.exists() and metadata_root.is_dir(), "missing pinned driver dependency: " + module)
        for root in (package, metadata_root):
            require(root.resolve(strict=True) == root, "driver dependency has unsafe ancestry")
            for path in ([root] if root.is_file() else sorted(root.rglob("*"))):
                if "__pycache__" in path.parts:
                    continue
                require(not path.is_symlink(), "driver dependency contains a symlink")
                if path.is_dir():
                    continue
                before = environment.secure_file_digest(path)
                require(before[1] <= 16 * 1024 * 1024, "driver dependency exceeds bound")
                raw = path.read_bytes()
                require(environment.secure_file_digest(path) == before and (hashlib.sha256(raw).hexdigest(), len(raw)) == before,
                    "driver dependency changed during capture")
                files[path.relative_to(site_packages).as_posix()] = raw
        metadata = email.parser.Parser().parsestr(files[metadata_root.name + "/METADATA"].decode())
        require(metadata.get("Name", "").lower().replace("-", "_") == distribution and metadata.get("Version") == version,
            "driver package metadata differs from declared version")
    return files


class CapturedSourceLoader(importlib.machinery.SourceFileLoader):
    def __init__(self, fullname, filename, raw):
        super().__init__(fullname, filename)
        self.raw = raw

    def get_code(self, fullname):
        return compile(self.raw, self.path, "exec", dont_inherit=True)


class DriverFinder(importlib.abc.MetaPathFinder):
    def __init__(self, root, files):
        self.root, self.files = root, files

    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".", 1)[0] not in PACKAGES:
            return None
        # Six's retained source supplies virtual aliases for standard-library
        # modules. Its own importer handles these; they are not package files.
        if fullname == "six.moves" or fullname.startswith("six.moves."):
            return None
        relative = fullname.replace(".", "/")
        for member, package in ((relative + "/__init__.py", True), (relative + ".py", False)):
            if member in self.files:
                filename = str(self.root / member)
                loader = CapturedSourceLoader(fullname, filename, self.files[member])
                return importlib.util.spec_from_file_location(fullname, filename, loader=loader,
                    submodule_search_locations=[str(Path(filename).parent)] if package else None)
        raise ModuleNotFoundError("driver module is outside retained source closure: " + fullname)

    def find_distributions(self, context=None):
        requested = getattr(context, "name", None)
        if requested is not None:
            requested = requested.lower().replace("-", "_")
        for distribution, version in PACKAGES.values():
            if requested in (None, distribution):
                yield CapturedDistribution(self.root, self.files, distribution + "-" + version + ".dist-info")


class CapturedDistribution(importlib.metadata.Distribution):
    def __init__(self, root, files, metadata_directory):
        self.root, self.retained_files, self.metadata_directory = root, files, metadata_directory

    def read_text(self, filename):
        if not isinstance(filename, str) or "/" in filename or filename in {".", ".."}:
            return None
        raw = self.retained_files.get(self.metadata_directory + "/" + filename)
        return raw.decode("utf-8") if raw is not None else None

    def locate_file(self, path):
        relative = str(path)
        require(relative in self.retained_files, "distribution file is outside retained driver closure")
        return self.root / relative


def load_driver(site_packages):
    require(not any(name.split(".", 1)[0] in PACKAGES for name in sys.modules),
        "driver dependencies must be loaded in a fresh isolated process")
    files = capture_files(site_packages)
    finder = DriverFinder(site_packages, files)
    sys.meta_path.insert(0, finder)
    try:
        import pg8000.native
        require(capture_files(site_packages) == files, "driver package closure changed while loading")
    except BaseException:
        sys.meta_path.remove(finder)
        for name in list(sys.modules):
            if name.split(".", 1)[0] in PACKAGES:
                sys.modules.pop(name, None)
        raise
    global LOADED_FILES, LOADED_ROOT
    LOADED_FILES, LOADED_ROOT = files, site_packages
    return pg8000.native


def capture():
    require(LOADED_FILES is not None and LOADED_ROOT is not None, "driver must be loaded from captured package source")
    files = capture_files(LOADED_ROOT)
    require(files == LOADED_FILES, "driver files changed after loading")
    for name, module in tuple(sys.modules.items()):
        if name.split(".", 1)[0] not in PACKAGES or name == "six.moves" or name.startswith("six.moves."):
            continue
        origin = getattr(module, "__file__", None)
        require(origin is not None and Path(origin).is_relative_to(LOADED_ROOT), "unexpected loaded driver module origin")
        relative = Path(origin).relative_to(LOADED_ROOT).as_posix()
        loader = getattr(module, "__loader__", None)
        require(relative in files and isinstance(loader, CapturedSourceLoader) and loader.raw == files[relative],
            "loaded driver module did not execute retained source bytes")
    runtime = {"python": environment.probe_python_runtime(executable_path=Path(sys.executable).resolve(strict=True)),
        "postgres_driver": {"packages": {module: {"distribution": name, "version": version} for module, (name, version) in PACKAGES.items()},
            "module_loading": "captured-source-only; no bytecode cache",
            "files": [{"path": name, "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)} for name, raw in sorted(files.items())]}}
    return runtime, files
