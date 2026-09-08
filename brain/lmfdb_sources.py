#!/usr/bin/env python3
"""Acquire a bounded read-only LMFDB snapshot or replay its retained evidence."""
from __future__ import annotations

import argparse
import datetime as dt
import platform
import ssl
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lmfdb_source_evidence as core
import lmfdb_source_dependencies as dependencies

LOADED_IMPLEMENTATION = {name: core.read_regular(core.ROOT / name) for name in core.TOOL_FILES}


def implementation():
    core.origins()
    for module, path in ((core, "brain/lmfdb_source_evidence.py"), (dependencies, "brain/lmfdb_source_dependencies.py")):
        core.require(Path(module.__file__).resolve() == core.ROOT / path, "LMFDB helper origin differs")
    programs = {name: core.read_regular(core.ROOT / name) for name in core.TOOL_FILES}
    core.require(programs == LOADED_IMPLEMENTATION, "LMFDB implementation changed since loading")
    return programs


def require_startup():
    core.require(platform.python_implementation() == "CPython" and sys.version_info[:2] == (3, 12)
        and sys.flags.isolated == 1 and sys.flags.no_site == 1, "LMFDB acquisition requires CPython3.12 -I -S")


def now():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def runtime_identity():
    require_startup()
    profile, programs = core.current_profile(), implementation()
    runtime, packages = dependencies.capture()
    tool = {"schema": core.TOOL_SCHEMA, "profile_id": profile["profile_id"], "files": profile["files"],
        "python_startup": "CPython " + platform.python_version() + " -I -S", "runtime": runtime}
    core.validate_tool(tool, packages)
    return tool, programs, packages


class BoundedFile:
    """Reject oversized PostgreSQL message lengths before the driver allocates."""
    def __init__(self, stream, limit):
        self.stream, self.remaining = stream, limit

    def read(self, size=-1):
        core.require(type(size) is int and 0 <= size <= self.remaining, "LMFDB network receive budget exceeded")
        data = self.stream.read(size)
        core.require(len(data) <= size, "LMFDB transport returned an oversized read")
        self.remaining -= len(data)
        return data

    def write(self, data):
        return self.stream.write(data)

    def flush(self):
        return self.stream.flush()

    def close(self):
        return self.stream.close()


class BoundedSocket:
    def __init__(self, socket):
        self.socket, self.opened = socket, False

    def makefile(self, mode):
        core.require(mode == "rwb" and not self.opened, "unexpected LMFDB driver socket access")
        self.opened = True
        return BoundedFile(self.socket.makefile(mode=mode), core.POLICY["network_receive_limit_bytes"])

    def __getattr__(self, name):
        return getattr(self.socket, name)


class PinnedTLS:
    def __init__(self, pin):
        self.pin, self.certificate, self.metadata, self.socket = pin, None, None, None

    def wrap_socket(self, socket, *, server_hostname):
        core.require(server_hostname == "devmirror.lmfdb.xyz", "LMFDB TLS destination differs")
        # The publisher mirror uses a self-signed certificate. This isolated
        # connection checks a retained explicit DER pin before authentication;
        # it neither changes system trust nor claims CA authentication.
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        wrapped = context.wrap_socket(socket, server_hostname=server_hostname)
        try:
            certificate = wrapped.getpeercert(binary_form=True)
            core.require(core.sha(certificate) == self.pin, "LMFDB TLS peer differs from the reviewed explicit pin")
            self.certificate = certificate
            self.metadata = {"peer_certificate_sha256": self.pin, "tls_version": wrapped.version(), "cipher": wrapped.cipher()[0]}
            core.require(self.metadata["tls_version"] in {"TLSv1.2", "TLSv1.3"}, "LMFDB TLS version differs")
            self.socket = BoundedSocket(wrapped)
            return self.socket
        except BaseException:
            wrapped.close()
            raise


def query(plan, driver):
    tls, connection, raw, rolled_back = PinnedTLS(plan["peer_certificate_sha256"]), None, None, False
    primary_error = None
    try:
        # These are the publisher's documented public read-only login values,
        # not a user's credentials or a privileged production connection.
        connection = driver.Connection(user="lmfdb", password="lmfdb", host="devmirror.lmfdb.xyz", port=5432, database="lmfdb",
            timeout=core.POLICY["network_timeout_seconds"], ssl_context=tls,
            application_name=core.APPLICATION_NAME, startup_params=dict(core.CLIENT_STARTUP))
        connection.run(core.BEGIN)
        result = connection.run(core.SQL)
        core.require(isinstance(result, list) and len(result) == 1 and isinstance(result[0], list) and len(result[0]) == 1
            and isinstance(result[0][0], str), "LMFDB query returned an unexpected shape")
        raw = result[0][0].encode("utf-8")
        connection.run(core.END)
        rolled_back = True
        core.require(tls.certificate is not None and tls.metadata is not None, "LMFDB connection did not prove pinned TLS")
        metadata = {**tls.metadata, "transaction_rolled_back": True, "response_bytes": len(raw), "response_sha256": core.sha(raw)}
        core.normalize(plan, raw, metadata, tls.certificate)
        return raw, metadata, tls.certificate
    except BaseException as exc:
        # Retain a received complete query body even if the final transaction
        # check fails; it must never gain a complete acquisition receipt.
        exc.lmfdb_received_body = raw
        primary_error = exc
        raise
    finally:
        cleanup_errors = []
        if connection is not None:
            if not rolled_back:
                try:
                    connection.run(core.END)
                except BaseException as exc:
                    cleanup_errors.append(exc)
            try:
                connection.close()
            except BaseException as exc:
                cleanup_errors.append(exc)
        if tls.socket is not None:
            try:
                tls.socket.close()
            except BaseException as exc:
                cleanup_errors.append(exc)
        if cleanup_errors:
            if primary_error is not None:
                for error in cleanup_errors:
                    primary_error.add_note("LMFDB cleanup also failed: " + type(error).__name__)
            else:
                error = cleanup_errors[0]
                error.lmfdb_received_body = raw
                for additional in cleanup_errors[1:]:
                    error.add_note("LMFDB cleanup also failed: " + type(additional).__name__)
                raise error


def acquire(plan_path, store, package_root):
    require_startup()
    raw_plan = core.read_regular(plan_path, 1024 * 1024)
    plan = core.validate_plan(core.parse(raw_plan, "plan"))
    core.require(raw_plan == core.canonical(plan), "LMFDB plan must be canonical")
    driver = dependencies.load_driver(package_root)
    tool, programs, packages = runtime_identity()
    raw = None
    try:
        raw, metadata, certificate = query(plan, driver)
        core.require(runtime_identity() == (tool, programs, packages) and core.read_regular(plan_path) == raw_plan,
            "LMFDB runtime or plan changed during acquisition")
        files = core.capture_files(plan, raw, metadata, certificate, tool, programs, packages, now())
    except BaseException as exc:
        schema = "wikilean.lmfdb-incomplete-attempt/v1"
        body = raw if raw is not None else getattr(exc, "lmfdb_received_body", None)
        retained = {"plan.json": raw_plan, "tool.json": core.canonical(tool), "request.json": core.canonical(core.parameters(plan)),
            "failure.json": core.canonical({"schema": schema, "authority": False, "recorded_at": now(), "failure_type": type(exc).__name__}),
            **{"implementation/" + n: data for n, data in programs.items()}, **{"dependencies/" + n: data for n, data in packages.items()}}
        if body is not None:
            retained["received-query-response.json"] = body
        target = core.archive.publish(core.archive.manifest_files(retained, schema), store.parent / (store.name + "-incomplete"), schema)
        print("Incomplete LMFDB attempt retained: " + str(target), file=sys.stderr)
        raise
    target = core.archive.publish(files, store, core.CAPTURE_SCHEMA)
    core.verify_capture(target)
    return target


def export(capture_path, store):
    core.require(capture_path != store and capture_path not in store.parents and store not in capture_path.parents,
        "LMFDB capture and export stores must have disjoint ancestry")
    profile, programs = core.current_profile(), implementation()
    capture, _ = core.verify_capture(capture_path)
    files = core.build_export(capture, profile, programs, now())
    core.require(core.current_profile() == profile and implementation() == programs, "LMFDB normalizer changed")
    target = core.archive.publish(files, store, core.EXPORT_SCHEMA)
    core.verify_export(target)
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("acquire", "export", "verify-capture", "verify-export"))
    for name in ("plan", "capture", "store", "bundle", "package-root"):
        parser.add_argument("--" + name, type=Path)
    args = parser.parse_args()
    try:
        if args.mode == "acquire":
            if any(v is None for v in (args.plan, args.store, args.package_root)) or args.capture is not None or args.bundle is not None:
                parser.error("acquire requires --plan --store --package-root")
            print(acquire(args.plan, args.store, args.package_root))
        elif args.mode == "export":
            if args.capture is None or args.store is None or any(v is not None for v in (args.plan, args.bundle, args.package_root)):
                parser.error("export requires --capture --store")
            print(export(args.capture, args.store))
        else:
            if args.bundle is None or any(v is not None for v in (args.plan, args.capture, args.store, args.package_root)):
                parser.error("verification requires --bundle only")
            implementation()
            result = core.verify_export(args.bundle) if args.mode == "verify-export" else core.verify_capture(args.bundle)[1]
            print(core.canonical(result).decode())
            implementation()
    except (core.EvidenceError, core.contracts.VerificationError, OSError, ValueError, KeyError) as exc:
        print("LMFDB operation failed: " + type(exc).__name__, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
