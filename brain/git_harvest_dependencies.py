"""Measure the exact source-normalizer interpreter and loaded PyYAML closure."""
from __future__ import annotations

import email.parser
import hashlib
import importlib.abc
import importlib.machinery
import importlib.util
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "brain/tools"))
import execution_environment as environment
_LOADED_YAML_FILES = None


class CapturedSourceLoader(importlib.machinery.SourceFileLoader):
    """Execute the retained source, never a timestamp-valid unmeasured .pyc."""
    def __init__(self, fullname, filename, raw):
        super().__init__(fullname, filename)
        self.raw = raw

    def get_code(self, fullname):
        return compile(self.raw, self.path, "exec", dont_inherit=True)


class CapturedYamlFinder(importlib.abc.MetaPathFinder):
    def __init__(self, root, files):
        self.root, self.files = root, files

    def find_spec(self, fullname, path=None, target=None):
        if fullname != "yaml" and not fullname.startswith("yaml."):
            return None
        relative = fullname.replace(".", "/")
        for member, package in ((relative + "/__init__.py", True), (relative + ".py", False)):
            if member in self.files:
                filename = str(self.root.parent / member)
                loader = CapturedSourceLoader(fullname, filename, self.files[member])
                return importlib.util.spec_from_file_location(fullname, filename, loader=loader,
                    submodule_search_locations=[str(Path(filename).parent)] if package else None)
        # The optional libyaml extension is measured as a native binary. Exact
        # pre/post package readback fences its load; no alternate path is used.
        for suffix in importlib.machinery.EXTENSION_SUFFIXES:
            member = relative + suffix
            if member in self.files:
                filename = str(self.root.parent / member)
                return importlib.util.spec_from_file_location(fullname, filename,
                    loader=importlib.machinery.ExtensionFileLoader(fullname, filename))
        raise ModuleNotFoundError("YAML helper is outside the retained package closure: " + fullname)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def load_yaml(package_root: Path):
    """Load only this package, without adding an entire site-packages to sys.path."""
    require(package_root.is_absolute() and package_root.resolve(strict=True) == package_root,
            "PyYAML package root must have real absolute ancestry")
    if "yaml" in sys.modules:
        require(Path(sys.modules["yaml"].__file__).resolve(strict=True) == package_root / "__init__.py",
                "another YAML package is already loaded")
        return sys.modules["yaml"]
    before = package_files(package_root)
    finder = CapturedYamlFinder(package_root, before)
    spec = finder.find_spec("yaml")
    require(spec is not None and spec.loader is not None, "cannot load the explicit PyYAML package")
    module = importlib.util.module_from_spec(spec)
    sys.meta_path.insert(0, finder)
    sys.modules["yaml"] = module
    try:
        spec.loader.exec_module(module)
        require(package_files(package_root) == before, "PyYAML changed while loading")
    except BaseException:
        sys.meta_path.remove(finder)
        for name in list(sys.modules):
            if name == "yaml" or name.startswith("yaml."):
                sys.modules.pop(name, None)
        raise
    global _LOADED_YAML_FILES
    _LOADED_YAML_FILES = before
    return module


def package_files(package):
    metadata_roots = [path for path in package.parent.iterdir() if path.name.lower() == "pyyaml-6.0.3.dist-info"]
    require(len(metadata_roots) == 1, "PyYAML distribution metadata is absent or ambiguous")
    metadata_root = metadata_roots[0]
    files = {}
    for root, prefix in ((package, "yaml"), (metadata_root, "pyyaml-6.0.3.dist-info")):
        require(root.resolve(strict=True) == root, "PyYAML metadata has unsafe ancestry")
        for path in sorted(root.rglob("*")):
            if "__pycache__" in path.parts:
                continue
            require(not path.is_symlink(), "PyYAML package closure contains a symlink")
            if path.is_dir():
                continue
            before = environment.secure_file_digest(path)
            require(before[1] <= 64 * 1024 * 1024, "PyYAML component exceeds its bound")
            raw = path.read_bytes()
            require(environment.secure_file_digest(path) == before and
                    (hashlib.sha256(raw).hexdigest(), len(raw)) == before,
                    "PyYAML component changed while captured")
            files[prefix + "/" + path.relative_to(root).as_posix()] = raw
    metadata = email.parser.Parser().parsestr(files["pyyaml-6.0.3.dist-info/METADATA"].decode())
    require(metadata.get("Name", "").lower() == "pyyaml" and metadata.get("Version") == "6.0.3",
            "loaded YAML and distribution metadata disagree")
    return files


def capture():
    import yaml
    package = Path(yaml.__file__).parent
    require(package.is_absolute() and package.resolve(strict=True) == package and package.name == "yaml",
            "loaded YAML package has an unsafe origin")
    require(yaml.__version__ == "6.0.3" and yaml.SafeLoader.__module__ == "yaml.loader" and
            yaml.safe_load.__module__ == "yaml" and Path(yaml.safe_load.__code__.co_filename) == package / "__init__.py",
            "source normalization requires the exact PyYAML6.0.3 pure SafeLoader")
    files = package_files(package)
    require(_LOADED_YAML_FILES is not None and files == _LOADED_YAML_FILES,
            "PyYAML must be loaded from the exact captured source in a fresh process")
    for name, module in tuple(sys.modules.items()):
        if name == "yaml" or name.startswith("yaml."):
            origin = getattr(module, "__file__", None)
            require(origin is not None and Path(origin).resolve(strict=True).is_relative_to(package),
                    "loaded YAML helper is outside the measured package")
            require("yaml/" + Path(origin).relative_to(package).as_posix() in files,
                    "loaded YAML helper is outside the retained component closure")
            if Path(origin).suffix == ".py":
                loader = getattr(module, "__loader__", None)
                require(isinstance(loader, CapturedSourceLoader) and
                        loader.raw == files["yaml/" + Path(origin).relative_to(package).as_posix()],
                        "loaded YAML helper did not execute the retained source bytes")
    with_libyaml = yaml.__with_libyaml__
    require(type(with_libyaml) is bool, "invalid libyaml availability")
    libyaml_version = yaml._yaml.get_version_string() if with_libyaml else None
    require(libyaml_version is None or re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", libyaml_version), "invalid libyaml version")
    runtime = {"python": environment.probe_python_runtime(executable_path=Path(sys.executable).resolve(strict=True)),
        "pyyaml": {"name": "PyYAML", "version": "6.0.3", "loader": "yaml.loader.SafeLoader",
            "python_module_loading": "captured-source-only; bytecode-cache-never-read",
            "with_libyaml": with_libyaml, "libyaml_version": libyaml_version,
            "files": [{"path": name, "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)} for name, raw in sorted(files.items())]}}
    return runtime, files
