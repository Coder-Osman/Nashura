import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import resource
import signal
import subprocess
import sys
import tarfile
import time


SOURCE_URL = "https://download.qemu.org/qemu-10.0.13.tar.xz"
SOURCE_SHA256 = "4a59b74dac7ead8b162a7c4595dcbbeebe989b90c89cfe6bb4dec5447342f6fa"
PACKAGES = {
    "pkgconf-bin=1.8.1-4": "54efef2aef4db5fcb5e0f5b7746465000c6a779b06f3686d0ec3d2a901b20aeb",
    "libpkgconf3=1.8.1-4": "85087cd04e57fd4ab7d6e816d348c335047823ab60c78878336b74f07b352ca1",
    "libgio-2.0-dev=2.84.4-3~deb13u5": "a9bbd748ddd40a08b336e8d6b1755e698957ac640d081ddc4c330b716c6b89c3",
    "zlib1g-dev=1:1.3.dfsg+really1.3.1-1+b1": "76ed5c858e1aef38b7be93acce5910e457e77bd6271471b9d05fb42e78826224",
    "python3-pip-whl=25.1.1+dfsg-1": "e65351935aa4af2b10ae2abb5744f5059e9abad51aea6cd0cb825702010763f7",
    "python3-setuptools-whl=78.1.1-0.1": "0caf134e73ab90ffeaa5ce2529d73ea7d48078b2a9251ed9d1b7b514a61e9483",
    "libpcre2-dev=10.46-1~deb13u3": "6740621a0062e4b4cdeaa12a51f03ed0a3de0a04b949c8fa6a92c5f4c3c6b127",
    "libsysprof-capture-4-dev=48.0-2": "3cb76e124187700f5977a0acb914b3f82b2bd28105fce6f1cf2a34497b234af2",
}


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def save(path, value):
    temporary = path.with_name(path.name + ".partial")
    temporary.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")
    temporary.replace(path)


def resources(root):
    import shutil
    available = int(next(line.split()[1] for line in Path("/proc/meminfo").read_text().splitlines()
        if line.startswith("MemAvailable:"))) * 1024
    free = shutil.disk_usage(root).free
    if available < 32 * 1024**3 or free < 5 * 1024**3:
        raise ValueError("isolated build resource floor violated")
    return dict(available_bytes=available, disk_free_bytes=free)


def source_filter(member, destination):
    if PurePosixPath(member.name).parts[:2] == ("qemu-10.0.13", "roms"):
        return None
    return tarfile.data_filter(member, destination)


def process_start_ticks(pid):
    return Path("/proc", str(pid), "stat").read_text().rsplit(")", 1)[1].split()[19]


def run(root, name, arguments, environment, *, cwd=None, timeout=600):
    resources(root)
    started = time.monotonic()
    with (root / (name + ".log")).open("xb") as log:
        child = subprocess.Popen(arguments, cwd=cwd or root, env=environment,
            stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            save(root / "status.json", dict(phase=name, pid=child.pid,
                start_ticks=process_start_ticks(child.pid), command=arguments, started=time.time()))
            while child.poll() is None:
                if time.monotonic() - started > timeout:
                    raise TimeoutError(name + " exceeded bounded execution")
                resources(root)
                time.sleep(2)
            if child.returncode:
                raise RuntimeError(name + " exited " + str(child.returncode))
        finally:
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait(timeout=10)
    return dict(seconds=time.monotonic() - started, log_sha256=sha(root / (name + ".log")))


def main(root):
    started = time.monotonic()
    resource.setrlimit(resource.RLIMIT_FSIZE, (512 * 1024**2, 512 * 1024**2))
    environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    evidence = dict(schema="FairFinalPluginRuntimeBuild/v1", source_url=SOURCE_URL,
        builder_sha256=sha(__file__), packages={}, steps={}, threads=4,
        training_admissible=False, gates_closed=False, initial_resources=resources(root))
    try:
        packages = root / "packages"
        packages.mkdir(exist_ok=False)
        prefix = root / "dependencies"
        prefix.mkdir(exist_ok=False)
        for index, (package, expected) in enumerate(PACKAGES.items()):
            metadata = subprocess.run(["apt-cache", "show", package], capture_output=True,
                text=True, check=True, timeout=30).stdout
            if "SHA256: " + expected not in metadata:
                raise ValueError("APT package identity changed: " + package)
            (root / ("package-" + str(index) + ".txt")).write_text(metadata)
            evidence["steps"]["download-" + str(index)] = run(root, "download-" + str(index),
                ["apt-get", "download", package], environment, cwd=packages, timeout=120)
            matches = [path for path in packages.glob("*.deb") if sha(path) == expected]
            if len(matches) != 1:
                raise ValueError("downloaded package digest differs")
            evidence["steps"]["unpack-" + str(index)] = run(root, "unpack-" + str(index),
                ["dpkg-deb", "-x", str(matches[0]), str(prefix)], environment, timeout=60)
            evidence["packages"][package] = dict(sha256=expected, bytes=matches[0].stat().st_size)
        library = prefix / "usr/lib/x86_64-linux-gnu"
        transformations = []
        for path in sorted((library / "pkgconfig").glob("*.pc")):
            original = path.read_text()
            updated = original.replace("prefix=/usr\n", "prefix=" + str(prefix / "usr") + "\n")
            if updated != original:
                transformations.append(dict(path=str(path.relative_to(prefix)),
                    before_sha256=sha(path), after_sha256=hashlib.sha256(updated.encode()).hexdigest()))
                path.write_text(updated)
        for path in sorted(library.glob("*.so")):
            if path.is_symlink() and not path.exists():
                target = Path("/usr/lib/x86_64-linux-gnu") / path.readlink().name
                if not target.is_file():
                    transformations.append(dict(path=str(path.relative_to(prefix)),
                        unresolved_optional_link=str(path.readlink())))
                    continue
                original = str(path.readlink())
                path.unlink()
                path.symlink_to(target)
                transformations.append(dict(path=str(path.relative_to(prefix)),
                    original_link=original, replacement_link=str(target), target_sha256=sha(target)))
        evidence["dependency_relocations"] = transformations
        environment.update(LD_LIBRARY_PATH=str(library), PKG_CONFIG_PATH=str(library / "pkgconfig"),
            PKG_CONFIG=str(prefix / "usr/bin/pkgconf"))
        wheels = sorted((prefix / "usr/share/python-wheels").glob("*.whl"))
        if len(wheels) != 2:
            raise ValueError("isolated Python build wheels differ")
        environment["PYTHONPATH"] = os.pathsep.join(map(str, wheels))
        source_archive = root / "qemu-10.0.13.tar.xz"
        evidence["steps"]["source-download"] = run(root, "source-download", ["curl", "--fail",
            "--location", "--proto", "=https", "--proto-redir", "=https", "--max-time", "180",
            "--output", str(source_archive), SOURCE_URL], environment, timeout=200)
        evidence["source_sha256"] = sha(source_archive)
        if evidence["source_sha256"] != SOURCE_SHA256:
            raise ValueError("official source archive changed from pinned measured identity")
        evidence["source_authentication"] = "official HTTPS release; locally measured SHA256, not an independent signature verification"
        save(root / "build-evidence.json", evidence)
        evidence["source_excluded_subtrees"] = ["qemu-10.0.13/roms"]
        with tarfile.open(source_archive) as archive:
            archive.extractall(root, filter=source_filter)
        source = root / "qemu-10.0.13"
        build = root / "build"
        build.mkdir(exist_ok=False)
        evidence["steps"]["configure"] = run(root, "configure", [str(source / "configure"),
            "--target-list=aarch64-linux-user", "--enable-plugins", "--disable-system",
            "--disable-tools", "--disable-docs", "--disable-guest-agent", "--disable-capstone",
            "--disable-slirp", "--disable-werror", "--python=/usr/bin/python3.13"],
            environment, cwd=build, timeout=600)
        evidence["steps"]["build"] = run(root, "build", ["ninja", "-j4", "qemu-aarch64"],
            environment, cwd=build, timeout=1200)
        qemu = build / "qemu-aarch64"
        version = subprocess.run([str(qemu), "--version"], capture_output=True, text=True,
            env=environment, check=True, timeout=10).stdout
        help_text = subprocess.run([str(qemu), "-help"], capture_output=True, text=True,
            env=environment, check=True, timeout=10).stdout
        (root / "qemu-help.txt").write_text(help_text)
        if "version 10.0.13" not in version or "-plugin" not in help_text:
            raise ValueError("built runtime lacks pinned version or plugin interface")
        evidence.update(qemu=str(qemu), qemu_sha256=sha(qemu), version=version,
            plugin_header_sha256=sha(source / "include/qemu/qemu-plugin.h"),
            seconds=time.monotonic() - started, final_resources=resources(root))
        save(root / "completion.json", evidence)
    except BaseException as error:
        evidence.update(error_type=type(error).__name__, error=str(error),
            seconds=time.monotonic() - started)
        save(root / "failure.json", evidence)
        raise


if __name__ == "__main__":
    main(Path(sys.argv[1]).resolve())
