"""Adapt an identity-checked Android 11 ART APEX config for the isolated probe."""
import argparse
import hashlib
from pathlib import Path


def prepare(content, expected_sha256):
    if hashlib.sha256(content).hexdigest() != expected_sha256:
        raise ValueError("stock ART linker config identity mismatch")
    lines = content.decode("utf-8").splitlines()
    replacements = {
        "additional.namespaces = system,conscrypt,art,neuralnetworks,adbd":
            "additional.namespaces = system,com_android_conscrypt,com_android_art,neuralnetworks,adbd",
        "namespace.default.links = art,system,adbd":
            "namespace.default.links = com_android_art,system,adbd",
        "namespace.system.links = art,adbd": "namespace.system.links = com_android_art,adbd",
        "namespace.conscrypt.links = art,system":
            "namespace.conscrypt.links = com_android_art,system",
        "namespace.system.search.paths = /system/${LIB}":
            "namespace.system.search.paths = /system/${LIB}:/apex/com.android.os.statsd/${LIB}",
    }
    for old, new in replacements.items():
        if lines.count(old) != 1:
            raise ValueError("unexpected stock linker configuration: " + old)
        lines[lines.index(old)] = new
    text = "\n".join(lines) + "\n"
    for name in ("art", "conscrypt"):
        text = text.replace("namespace." + name + ".", "namespace.com_android_" + name + ".")
        text = text.replace(".link." + name + ".", ".link.com_android_" + name + ".")
    text += "\nnamespace.system.permitted.paths += /apex/com.android.os.statsd/${LIB}\n"
    # JniInvocation may be reached from the system namespace after android_servers
    # dependencies are loaded. Its real dlopen must still resolve the ART runtime.
    text += "namespace.system.link.com_android_art.shared_libs += libart.so\n"
    return text


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stock", required=True, type=Path)
    parser.add_argument("--stock-sha256", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    content = prepare(args.stock.read_bytes(), args.stock_sha256)
    with args.output.open("x") as stream:
        stream.write(content)


if __name__ == "__main__":
    main()
