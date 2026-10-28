"""Identity-bound provider and catalog experiments; no battle calls or ready writes."""
import argparse
import base64
from parsed_tables import observations as parsed_table_observations
import json
import os
from pathlib import Path
import shutil
import sys
import xml.etree.ElementTree as ET

from preference_hook_evidence import observations as preference_observations
from preference_hook_evidence import native_queue_observations

from elf_audit import Elf64
from native_init_job import check_inputs, digest, run


ENGINE_SHA = "110aa2b5cac391c498645e072b0d88729428c2c2845e7e2737ca8ee979059783"
ENTRY_GUARDS = {
    "STRING": (0x767f3c, "678076dcebd45809617481c463e518663dfe5db67a9313fc0309b7a2a11fdb61"),
    "DELETE": (0x17e4b50, "98d6557566711c92e2f44d0c79fd912ac72060a5e40a47549f7ff41da5398d4c"),
    "CLEAR": (0x12e157c, "678076dcebd45809617481c463e518663dfe5db67a9313fc0309b7a2a11fdb61"),
    "DATA": (0x12e166c, "678076dcebd45809617481c463e518663dfe5db67a9313fc0309b7a2a11fdb61"),
    "UPDATE": (0x12e1714, "678076dcebd45809617481c463e518663dfe5db67a9313fc0309b7a2a11fdb61"),
    "CONTENT": (0x8001a4, "041505b15bf9b5814480d13d3f0d3d424b0ae1ff43e1242fc3ca890e3c436508"),
    "FILESYSTEM": (0x12dfed4, "82336434962db5e12ee984baca6f26be0c3a8886a0bb147ebaac906144ca24a8"),
    "OPEN": (0x12e03e4, "effa4790633f22ce4f2309693d54b4b477acd63dc91c8a5db7c5f0404f95d90a"),
    "GENERIC": (0x12e24ec, "6a9e67557ce5382fd640a3aa2c505e16c9fd105174c99da2badc5386665acd60"),
    "LIST": (0xe16fe4, "7ac720a2b26e423c729dddda1bf9f261b4cc5c44f6407a2137d500a11250622c"),
    "SEARCH": (0x12e26dc, "ba357369c45991b26ca113a297a26123e15f781b8a7c615778e56a77f2eecfbb"),
    "FACTORY_CTOR": (0x1194a40, "f83473572cb2d0b5872f3e0d0194f82b3c5e0b206105d425e7a836da9fbfbef6"),
    "FACTORY_SET": (0x11919d4, "933f712dea4e29227706a7844942154c6d804c243643bd187471059f18c3a977"),
    "NEW": (0x17e4b20, "4c01601c2937d9813120d00323e5b2bea35565aa96b2133be1614bacadcfd4f3"),
    "PACKED_SET": (0x119597c, "aadda736b4e5e684f617e8f8c8ccc2e324046cc142b250fe7a77609460e61a8e"),
    "PACKED_GET": (0x1195988, "fb05bc8f3f12893f5bc6523bb8a03cf01fef41ed91ba9ed19728d01496b86c31"),
    "PACKED_CALLER": (0x756c94, "b4b279699b72f9baa983ae2ed68e5cec486451c15364ac53c6c730854000d990"),
    "LISTENER_CTOR": (0x1252638, "05cbc854af4c20e3ea12f8b57ef5dafc02100c974398aaa4688fd1760947b04a"),
    "LISTENER_DTOR": (0x12526c0, "678076dcebd45809617481c463e518663dfe5db67a9313fc0309b7a2a11fdb61"),
    "PRELOAD": (0xe16a08, "041505b15bf9b5814480d13d3f0d3d424b0ae1ff43e1242fc3ca890e3c436508"),
    "TABLE_LOAD": (0xe1bdcc, "a1010f8ac6d6912832266b2308a0bb3c3607fd5366592fce1d54188a1d53bfa2"),
    "LOADER_CTOR": (0xc828d8, "fbacee1d75c363a988c3b0e162495fab9c0eca607291b47de667c6284c23b7a2"),
    "LOADER_START": (0xc833ec, "7ac720a2b26e423c729dddda1bf9f261b4cc5c44f6407a2137d500a11250622c"),
    "LOADER_STEP": (0xc8346c, "c0d3b895fdcbcb0047c3c8b79528770c75af7d7bac5d98242bf48f78852ba609"),
    "LOADER_DTOR": (0xc8eb64, "678076dcebd45809617481c463e518663dfe5db67a9313fc0309b7a2a11fdb61"),
    "CONFIG_WRITE": (0x140b7b4, "933f712dea4e29227706a7844942154c6d804c243643bd187471059f18c3a977"),
    "CONTEXT_GET": (0xdccb88, "bfb80a9c1f22429f8fca1b05a763abedc72565dcfb45c039a1407d49941859b6"),
    "TABLE_CTOR": (0xdc76ac, "678076dcebd45809617481c463e518663dfe5db67a9313fc0309b7a2a11fdb61"),
    "CSV_GET": (0x12101d4, "a5b69ec0fdb9b5118edacf3e9e32c76e87982c7b15007c0ab769ffa1ca13982d"),
    "OWNER_GETTER": (0x7557d0, "0d608b10262186b18f47bbec627b72bb2a345da5f8697cc041d7a7c63c163b60"),
    "APP_VERSION_WRAPPER": (0x140cc8c, "d44c0cc818e6bbac2db216c3f7beaae643e11e57accf313cb69c10d61869538d"),
    "APP_VERSION_STRING": (0x138a810, "678076dcebd45809617481c463e518663dfe5db67a9313fc0309b7a2a11fdb61"),
}


def entry_guards(path):
    import hashlib
    if digest(path) != ENGINE_SHA:
        raise ValueError("provider requires exact engine identity")
    elf = Elf64(path.read_bytes())
    guards = {}
    for name, (offset, expected) in ENTRY_GUARDS.items():
        position = elf.address(offset, 16)
        content = elf.data[position:position + 16]
        if hashlib.sha256(content).hexdigest() != expected:
            raise ValueError("entry identity mismatch: " + name)
        guards[name] = content.hex()
    return guards


def registration_observations(text, mode):
    rows = [line.split("\t") for line in text.splitlines()]
    expected = {"data", "update"} if mode == "both" else {mode}
    returns = [row[2] for row in rows if row[:2] == ["PHASE", "register_return"] and len(row) == 3]
    returned = set(returns)
    try:
        slots = {row[1] for row in rows if len(row) == 4 and row[0] == "PROVIDER_SLOT" and
                 int(row[2], 16) and int(row[3], 16)}
        ready = [int(row[1]) for row in rows if len(row) == 2 and row[0] == "PROVIDER_CONTENT_READY"]
    except ValueError:
        return {"complete": False, "malformed": True}
    vtables = {row[1] for row in rows if len(row) == 3 and row[0] == "PROVIDER_VTABLE" and
               row[2] == {"data": "18c3370", "update": "18c3400"}.get(row[1])}
    namespaces = {row[1] for row in rows if len(row) == 5 and row[0] == "PROVIDER_STRING" and
                  row[2] == "32" and row[3] == str(len(row[1]) + 1) and row[4] == row[1] + ":"}
    strings = [row for row in rows if row[0] == "PROVIDER_STRING"]
    valid_strings = True
    for row in strings:
        try:
            valid_strings &= (len(row) == 5 and row[1] in expected and row[2] in {"8", "32"} and
                              int(row[3]) == len(row[4].encode()) and bool(row[4]))
        except (ValueError, IndexError):
            valid_strings = False
    string_slots = {(row[1], row[2]) for row in strings if len(row) == 5}
    valid_strings &= string_slots == {(name, offset) for name in expected for offset in ["8", "32"]}
    string_guards = [row[1] for row in rows if len(row) == 2 and row[0] == "STRING_GUARD"]
    unique_tags = {"PROVIDER_SLOT": 4, "PROVIDER_VTABLE": 3, "PROVIDER_STRING": 5, "PROVIDER_CONTENT_READY": 2,
                   "PROVIDER_SLOTS_RELEASED": 2, "PROVIDER_RESULT": 2}
    malformed = any(row[0] in unique_tags and len(row) != unique_tags[row[0]] for row in rows)
    malformed |= any(row[:2] == ["PHASE", "register_return"] and len(row) != 3 for row in rows)
    for tag in unique_tags:
        tagged = [row for row in rows if row[0] == tag]
        identities = [tuple(row[1:3]) for row in tagged] if tag == "PROVIDER_STRING" else (
            [row[1] for row in tagged if len(row) > 1] if tag in {"PROVIDER_SLOT", "PROVIDER_VTABLE"} else [tag] * len(tagged))
        malformed |= len(identities) != len(set(identities))
    return {"returned": sorted(returned), "nonzero_slots": sorted(slots), "ready": ready,
            "malformed": malformed,
            "complete": not malformed and len(returns) == len(expected) and returned == expected and slots == expected and vtables == expected and
            namespaces == expected and valid_strings and len(string_guards) >= len(expected) and
            all(value == "1" for value in string_guards) and ready == [0] and
            ["PROVIDER_SLOTS_RELEASED", "1"] in rows and
            ["PHASE", "provider_release_return"] in rows and ["PROVIDER_RESULT", "0"] in rows and
            ["PHASE", "after_close"] in rows}


def resource_inventory(roots):
    observed = {}
    for label, root in roots.items():
        if root.resolve() == Path("/") or root.is_symlink() or not root.is_dir():
            raise ValueError("inventory requires a dedicated non-symlink resource root")
        entries = {}
        for path in sorted(root.rglob("*")):
            if path.is_symlink():
                raise ValueError("resource symlinks rejected")
            if path.is_file():
                entries[str(path.relative_to(root))] = digest(path)
        observed[label] = entries
    return observed


def preference_write_entry_counts(paths, material=None):
    if material is not None and len(material) != 80:
        raise ValueError("preference material must contain two keys and one IV")
    counts = []
    for slot, path in enumerate(paths):
        root = ET.fromstring(path.read_bytes())
        if root.tag != "map" or root.attrib:
            raise ValueError("preference XML root must be a plain map")
        keys = []
        for child in root:
            if child.tag != "string" or set(child.attrib) != {"name"} or len(child):
                raise ValueError("preference XML entries must be strings")
            keys.append(child.attrib["name"])
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate preference key")
        if not keys:
            counts.append(1)
        elif material is None or slot >= 2:
            counts.append(None)
        else:
            try:
                from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
            except ImportError:
                counts.append(None)
                continue
            plaintext = b"LOADING_SCREEN_STATE_LABEL"
            padding = 16 - len(plaintext) % 16
            encryptor = Cipher(algorithms.AES(material[slot * 32:slot * 32 + 32]), modes.ECB()).encryptor()
            encoded = base64.b64encode(encryptor.update(plaintext + bytes([padding]) * padding) +
                                       encryptor.finalize()).decode("ascii")
            counts.append(len(keys) + int(encoded not in keys))
    return tuple(counts)


def strace_launch(inferior, output, environment, resource_trace=False):
    host_environment = {key: value for key, value in environment.items() if not key.startswith("LD_")}
    command = ["strace", "-ff", "-tt", "-s", "128", "-o", str(output / "syscalls")]
    if resource_trace:
        command.extend(["-e", "trace=openat,close,fcntl,dup,dup3,chdir,fchdir,fstat,newfstatat,mmap,munmap,lseek,read,pread64,exit_group",
                        "-e", "raw=read,pread64"])
    for key, value in sorted(environment.items()):
        if key.startswith("LD_"):
            command.extend(["-E", key + "=" + value])
    return [*command, *inferior], host_environment


def resource_observations(output, text, expected):
    rows = [line.split("\t") for line in text.splitlines()]
    resource = output / "resource.bin"
    actual = digest(resource) if resource.is_file() else None
    semantics = [row[1:] for row in rows if row[0] == "RESOURCE_SEMANTICS"]
    lengths = [row[2] for row in rows if len(row) == 3 and row[:2] == ["RESOURCE_VALID_LENGTH", "1"]]
    try:
        length_valid = len(lengths) == 1 and 1 <= int(lengths[0]) <= 1024 * 1024
        reads = [int(row[1]) for row in rows if len(row) == 2 and row[0] == "RESOURCE_READ"]
        reads_valid = bool(reads) and all(amount > 0 for amount in reads) and sum(reads) == int(lengths[0])
    except (ValueError, IndexError):
        length_valid = False
        reads_valid = False
    shapes = {"RESOURCE_VALID_LENGTH": 3, "RESOURCE_READ": 2, "RESOURCE_FILE_KIND": 2,
              "RESOURCE_FILE_VTABLE": 2, "RESOURCE_SEEK_RESULT": 2,
              "RESOURCE_SEMANTICS": 7, "RESOURCE_BYTES": 2}
    malformed = any(row[0] in shapes and len(row) != shapes[row[0]] for row in rows)
    kinds = [row[1:] for row in rows if row[0] == "RESOURCE_FILE_KIND"]
    closed = [row for row in rows if row[:2] == ["PHASE", "resource_close_return"]]
    passed = (not malformed and expected is not None and actual == expected and length_valid and reads_valid and
              resource.is_file() and resource.stat().st_size == int(lengths[0]) and kinds in ([["apk"]], [["disk"]]) and
              [row for row in rows if row[0] == "RESOURCE_VALID_LENGTH"] == [["RESOURCE_VALID_LENGTH", "1", lengths[0]]] and
              [row for row in rows if row[0] == "RESOURCE_BYTES"] == [["RESOURCE_BYTES", lengths[0]]] and
              [row for row in rows if row[0] == "RESOURCE_SEEK_RESULT"] == [["RESOURCE_SEEK_RESULT", "0"]] and
              semantics == [["0", lengths[0], "1", "0", "1", "1"]] and
              closed == [["PHASE", "resource_close_return"]] and
              [row for row in rows if row[0] == "RESOURCE_FILE_VTABLE"] == [["RESOURCE_FILE_VTABLE", "1"]])
    return {"sha256": actual, "expected_sha256": expected, "complete": passed}


def catalog_observations(text, mode, expected_preload_requests=368):
    rows = [line.split("\t") for line in text.splitlines()]
    lists = [row[1:] for row in rows if row[0] == "CATALOG_LIST"]
    listing = lists == [["224", "158"]]
    preloaded = (["PHASE", "catalog_preload_return"] in rows and
                 [row for row in rows if row[0] == "CATALOG_PRELOAD"] == [["CATALOG_PRELOAD", "3", str(expected_preload_requests)]] and
                 [row for row in rows if row[0] == "CATALOG_READY_AFTER_PRELOAD"] == [["CATALOG_READY_AFTER_PRELOAD", "0"]] and
                 ["LISTENER_CANARY", "1"] in rows and
                 ["PHASE", "listener_destructor_return"] in rows)
    batches = [["CATALOG_LOAD_RETURN", str(start), str(int(start == 156)), "0"]
               for start in [0, 39, 78, 117, 156]]
    table_loaded = ([row for row in rows if row[0] == "CATALOG_LOAD_RETURN"] == batches and
                    [row for row in rows if row[0] == "CATALOG_TABLE_LOAD_COMPLETE"] == [["CATALOG_TABLE_LOAD_COMPLETE", "1"]])
    loader_destroyed = (["PHASE", "loader_destructor_return"] in rows and
                        ["LOADER_CANARY", "1"] in rows)
    ready_observed = [row for row in rows if row[0] == "CATALOG_READY_AFTER_LOADER"] == [["CATALOG_READY_AFTER_LOADER", "1"]]
    complete = mode is None or (mode == "inspect" and ["CATALOG_READY_AFTER_INSPECT", "0"] in rows)
    if mode == "list":
        complete = listing
    if mode == "preload":
        complete = listing and preloaded
    if mode == "load":
        complete = listing and preloaded and table_loaded
    if mode == "loader-construct":
        complete = (listing and preloaded and loader_destroyed and
                    ["PHASE", "loader_constructor_return"] in rows and
                    ["CATALOG_READY_AFTER_LOADER", "0"] in rows and
                    ["PHASE", "loader_start_enter"] not in rows)
    if mode == "loader":
        complete = listing and preloaded and loader_destroyed and ready_observed
    return {"mode": mode, "complete": complete, "list_verified": listing,
            "expected_preload_requests": expected_preload_requests,
            "preload_returned": preloaded, "table_load_completed": table_loaded,
            "loader_destroyed": loader_destroyed, "ready_observed": ready_observed,
            "catalog_ready_pass": False,
            "gate_reason": "requires independent repeat and full resource identity/semantic evidence"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--payload", type=Path, required=True)
    parser.add_argument("--probe", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--mode", choices=["data", "update", "both"], required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--update-root", type=Path, required=True)
    parser.add_argument("--resource-name")
    parser.add_argument("--expected-resource-sha256")
    parser.add_argument("--resource-exercise", action="store_true")
    parser.add_argument("--disk-namespace", choices=["data:", "asset:"])
    parser.add_argument("--catalog-mode", choices=["inspect", "list", "preload", "load", "loader", "loader-construct"])
    parser.add_argument("--data-path", help="Logical native provider root; inventory remains limited to --data-root")
    parser.add_argument("--resource-trace", action="store_true",
                        help="Bounded resource syscalls with raw read arguments; no buffer contents")
    parser.add_argument("--search-root", type=Path)
    parser.add_argument("--factory-mode", choices=["construct", "register"])
    parser.add_argument("--resource-manifest", type=Path, required=True)
    parser.add_argument("--asset-root", type=Path)
    parser.add_argument("--asset-index", type=Path)
    parser.add_argument("--asset-index-sha256")
    parser.add_argument("--asset-backend", type=Path)
    parser.add_argument("--asset-backend-sha256")
    parser.add_argument("--observe-targets", type=Path)
    parser.add_argument("--owner-getter-only", action="store_true",
                        help="Call the observed owner getter and stop before catalog or factory initialization")
    parser.add_argument("--app-version", help="Explicit verified package version for the bounded owner experiment")
    parser.add_argument("--loading-config-candidate", action="store_true")
    parser.add_argument("--native-backup-queue", action="store_true",
                        help="Explicit native filesystem candidate; never implies Android backup parity")
    for name in ("preference-material", "preference-xml0", "preference-xml1"):
        parser.add_argument("--" + name, type=Path)
        parser.add_argument("--" + name + "-sha256")
    parser.add_argument("--inspect-parsed-tables", action="store_true")
    parser.add_argument("--inspect-row-ids", action="store_true")
    parser.add_argument("--expected-preload-requests", type=int, default=368,
                        help="Explicit observation profile, bound to the resource manifest; not a semantic gate")
    parser.add_argument("--timeout", type=int, default=55)
    arguments = parser.parse_args()
    if bool(arguments.resource_name) != bool(arguments.expected_resource_sha256):
        parser.error("resource name and expected digest must be paired")
    if arguments.resource_exercise and not arguments.resource_name:
        parser.error("resource exercise requires a resource name")
    if arguments.owner_getter_only and (arguments.catalog_mode or arguments.factory_mode or arguments.resource_name):
        parser.error("owner getter experiment must not initialize factory, catalog, or a resource")
    if arguments.app_version is not None and (not arguments.owner_getter_only or arguments.app_version != "15.535.13"):
        parser.error("app version requires the observed package version and owner getter experiment")
    if arguments.inspect_row_ids and not arguments.inspect_parsed_tables:
        parser.error("row identity observation requires parsed tables")
    asset_arguments = [arguments.asset_root, arguments.asset_index, arguments.asset_index_sha256,
                       arguments.asset_backend, arguments.asset_backend_sha256]
    if any(asset_arguments) and not all(asset_arguments):
        parser.error("asset root, index, backend and both expected hashes must be supplied together")
    preference_arguments = [getattr(arguments, name + suffix)
                            for name in ("preference_material", "preference_xml0", "preference_xml1")
                            for suffix in ("", "_sha256")]
    if any(preference_arguments) and not all(preference_arguments):
        parser.error("preference material and both XML seeds require all three explicit hashes")
    if any(preference_arguments) and arguments.loading_config_candidate:
        parser.error("strict preference backend and legacy loading candidate are exclusive")
    if arguments.native_backup_queue and not all(preference_arguments):
        parser.error("native backup candidate requires identity-bound preference inputs")
    if not 1 <= arguments.timeout <= 120:
        parser.error("timeout must be 1..120 seconds")
    if not 1 <= arguments.expected_preload_requests <= 16384:
        parser.error("expected preload requests must be 1..16384")
    payload, probe = arguments.payload.resolve(), arguments.probe.resolve()
    before = check_inputs(payload)
    guards = entry_guards(payload / "libs/libg.so")
    for path in [arguments.data_root, arguments.update_root]:
        if not path.is_dir():
            raise ValueError("resource root must exist")
    roots = {"data": arguments.data_root.resolve(), "update": arguments.update_root.resolve()}
    if arguments.search_root:
        roots["search"] = arguments.search_root.resolve()
    if arguments.asset_root:
        roots["assets"] = arguments.asset_root.resolve()
    resources_before = resource_inventory(roots)
    if resources_before != json.loads(arguments.resource_manifest.read_text()):
        raise ValueError("resource inventory differs from expected manifest")
    extras = {"resource_manifest": (arguments.resource_manifest.resolve(), digest(arguments.resource_manifest))}
    if all(asset_arguments):
        for label, path, expected in [("asset_index", arguments.asset_index, arguments.asset_index_sha256),
                                      ("asset_backend", arguments.asset_backend, arguments.asset_backend_sha256)]:
            if digest(path) != expected:
                raise ValueError(label + " identity mismatch")
            extras[label] = (path.resolve(), expected)
    if arguments.observe_targets:
        extras["observation_targets"] = (arguments.observe_targets.resolve(), digest(arguments.observe_targets))
    if all(preference_arguments):
        for name in ("preference_material", "preference_xml0", "preference_xml1"):
            path, expected = getattr(arguments, name).resolve(), getattr(arguments, name + "_sha256")
            if digest(path) != expected:
                raise ValueError(name + " identity mismatch")
            extras[name] = (path, expected)
        preference_counts = preference_write_entry_counts(
            [extras["preference_xml" + str(slot)][0] for slot in range(2)],
            extras["preference_material"][0].read_bytes())
    output = arguments.output.resolve()
    output.mkdir(exist_ok=False)
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(("LD_", "NASHURA_"))}
    environment.update({"LD_LIBRARY_PATH": str(payload / "libs"), "NASHURA_PROVIDER_MODE": arguments.mode,
                        "NASHURA_DATA_PATH": arguments.data_path or str(arguments.data_root.resolve()),
                        "NASHURA_UPDATE_PATH": str(arguments.update_root.resolve())})
    environment.update({"NASHURA_" + name + "_GUARD": value for name, value in guards.items()})
    if arguments.loading_config_candidate:
        configuration = output / "loading-config"
        configuration.mkdir(exist_ok=False)
        environment["NASHURA_LOADING_CONFIG_DIRECTORY"] = str(configuration)
    if all(preference_arguments):
        configuration = output / "preference-store"
        configuration.mkdir(mode=0o700, exist_ok=False)
        for slot in range(2):
            shutil.copyfile(extras["preference_xml" + str(slot)][0],
                            configuration / ("store-" + str(slot) + ".xml"))
        environment.update(NASHURA_PREFERENCE_DIRECTORY=str(configuration),
                           NASHURA_PREFERENCE_MATERIAL=str(extras["preference_material"][0]),
                           NASHURA_PREFERENCE_CRYPTO=str(payload / "libs/libcrypto.so"))
        if arguments.native_backup_queue:
            environment["NASHURA_PREFERENCE_BACKUP_BACKEND"] = "native-filesystem-queue-v1"
    if arguments.inspect_parsed_tables:
        if arguments.catalog_mode not in {"loader", "loader-construct"}:
            parser.error("parsed table observation requires a loader experiment")
        environment["NASHURA_INSPECT_PARSED_TABLES"] = "1"
    if arguments.inspect_row_ids:
        environment["NASHURA_INSPECT_ROW_IDS"] = "1"
    if arguments.resource_name:
        environment["NASHURA_RESOURCE_NAME"] = arguments.resource_name
    if arguments.resource_exercise:
        environment["NASHURA_RESOURCE_EXERCISE"] = "1"
    if arguments.disk_namespace:
        environment["NASHURA_DISK_NAMESPACE"] = arguments.disk_namespace
    if arguments.catalog_mode:
        environment["NASHURA_CATALOG_MODE"] = arguments.catalog_mode
    if arguments.search_root:
        environment.update(NASHURA_SEARCH_PROVIDER="1", NASHURA_SEARCH_PATH=str(arguments.search_root.resolve()))
    if arguments.owner_getter_only:
        environment["NASHURA_OWNER_GETTER_ONLY"] = "1"
    if arguments.app_version is not None:
        environment["NASHURA_APP_VERSION"] = arguments.app_version
    if arguments.factory_mode:
        environment["NASHURA_FACTORY_MODE"] = arguments.factory_mode
    if all(asset_arguments):
        environment.update(LD_PRELOAD=str(arguments.asset_backend.resolve()),
                           NASHURA_NATIVE_ASSET_ROOT=str(arguments.asset_root.resolve()),
                           NASHURA_NATIVE_ASSET_INDEX=str(arguments.asset_index.resolve()))
    inferior = [str(payload / "linker64"), str(probe), str(payload / "libs/libg.so"),
                guards["CONTENT"], guards["FILESYSTEM"]]
    if arguments.observe_targets:
        inferior_environment = {**environment, "NASHURA_OBSERVE_STOP": "1"}
        environment = {key: value for key, value in environment.items() if not key.startswith("LD_")}
        script = Path(__file__).with_name("native_init_gdb.py").resolve()
        launcher = Path(__file__).with_name("native_init_attach.py").resolve()
        extras.update({"gdb_script": (script, digest(script)), "gdb_launcher": (launcher, digest(launcher))})
        decoder = Path(__file__).with_name("observed_string.py").resolve()
        extras["string_decoder"] = (decoder, digest(decoder))
        environment.update(NASHURA_GDB_TARGETS=str(arguments.observe_targets.resolve()),
                           NASHURA_GDB_OUTPUT=str(output / "observation.json"),
                           NASHURA_GDB_GUARD_RANGES=json.dumps({"libg.so": [entry[0] for entry in ENTRY_GUARDS.values()]}))
        specification = {"inferior": inferior, "environment": inferior_environment, "script": str(script)}
        specification_path = output / "attach-spec.json"
        with specification_path.open("x") as stream:
            json.dump(specification, stream, indent=2)
        command = [sys.executable, str(launcher), str(specification_path)]
    else:
        command, environment = strace_launch(inferior, output, environment, arguments.resource_trace)
    probe_before = digest(probe)
    result = run(command, output, environment, arguments.timeout)
    postcheck_errors = []
    try:
        after = check_inputs(payload)
    except Exception as error:
        after = None
        postcheck_errors.append("input identity: " + str(error))
    text = (output / "stdout.log").read_text(errors="replace")
    stderr_text = (output / "stderr.log").read_text(errors="replace")
    preference = preference_observations(text, stderr_text, preference_counts) if all(preference_arguments) else None
    backup_candidate = native_queue_observations(text, stderr_text, preference_counts) if arguments.native_backup_queue else None
    observations = registration_observations(text, arguments.mode)
    try:
        resources_after = resource_inventory(roots)
    except Exception as error:
        resources_after = None
        postcheck_errors.append("resource identity: " + str(error))
    extras_after = {}
    for label, (path, expected) in extras.items():
        try:
            extras_after[label] = digest(path)
        except OSError as error:
            postcheck_errors.append(label + ": " + str(error))
    auxiliary_unchanged = all(extras_after.get(label) == expected for label, (_, expected) in extras.items())
    try:
        probe_unchanged = digest(probe) == probe_before
    except OSError as error:
        probe_unchanged = False
        postcheck_errors.append("probe identity: " + str(error))
    backend_binding = None
    if arguments.asset_backend:
        bindings = [row.split("\t")[1:] for row in text.splitlines() if row.startswith("NATIVE_ASSET_BINDING\t")]
        backend_binding = bindings == [[str(arguments.asset_backend.resolve())]]
    catalog = catalog_observations(text, arguments.catalog_mode, arguments.expected_preload_requests)
    parsed_tables = parsed_table_observations(text) if arguments.inspect_parsed_tables else None
    candidate_bound = ("LOADING_CONFIG_INSTALL_RESULT\t0" in text.splitlines() and
                       "LOADING_CONFIG_HOOK\tisolated_native_candidate_no_android_backup_parity" in text.splitlines()) if arguments.loading_config_candidate else None
    resource_result = resource_observations(output, text, arguments.expected_resource_sha256) if arguments.resource_name else None
    passed = result["exit_code"] == 0 and not result["timed_out"] and before == after and observations["complete"]
    passed = passed and resources_before == resources_after and (resource_result is None or resource_result["complete"])
    passed = passed and (parsed_tables is None or parsed_tables["complete"])
    generic_return = ([row for row in text.splitlines() if row.startswith("PHASE\tgeneric_register_return")] ==
                      ["PHASE\tgeneric_register_return\t" + arguments.disk_namespace]) if arguments.disk_namespace else None
    passed = passed and (generic_return is not False) and (backend_binding is not False) and (candidate_bound is not False) and catalog["complete"] and auxiliary_unchanged and not postcheck_errors
    if arguments.owner_getter_only:
        lines = text.splitlines()
        passed = passed and all(lines.count(line) == 1 for line in (
            "PHASE\towner_getter_enter", "PHASE\towner_getter_return",
            "OWNER_SLOT_BEFORE_NONNULL\t0", "OWNER_GETTER_RESULT\t1\t1\t1", "OWNER_VTABLE_MATCH\t1"))
    if preference is not None:
        passed = passed and preference["configuration_contract_pass"]
    if arguments.observe_targets:
        try:
            observation = json.loads((output / "observation.json").read_text())
            passed = passed and observation["exit_code"] == 0 and not observation.get("error")
        except (OSError, ValueError, KeyError) as error:
            postcheck_errors.append("observation: " + str(error))
            passed = False
    result.update({"mode": arguments.mode, "disk_namespace": arguments.disk_namespace,
                   "generic_register_returned": generic_return, "inputs_before": before, "inputs_after": after,
                   "inputs_unchanged": before == after, "probe_sha256": probe_before,
                   "probe_unchanged": probe_unchanged, "observations": observations,
                   "backend_binding_verified": backend_binding,
                   "status": ("RESOURCE_READ_PASS" if resource_result else "REGISTRATION_ONLY_PASS") if passed else "BLOCKED",
                   "resources_before": resources_before, "resources_after": resources_after,
                   "resources_unchanged": resources_before == resources_after,
                   "resource_observations": resource_result,
                   "catalog": catalog, "postcheck_errors": postcheck_errors,
                   "parsed_tables": parsed_tables,
                   "auxiliary_before": {label: expected for label, (_, expected) in extras.items()},
                   "auxiliary_after": extras_after, "auxiliary_unchanged": auxiliary_unchanged,
                   "catalog_ready_pass": False, "resource_access_verified": bool(passed and resource_result),
                   "loading_config_candidate": arguments.loading_config_candidate,
                   "loading_config_candidate_bound": candidate_bound,
                   "strict_preference_backend": bool(all(preference_arguments)),
                   "strict_preference_observations": preference,
                   "preference_current_runtime_identity": "UNRESOLVED" if all(preference_arguments) else None,
                   "preference_backup_service": "UNRESOLVED" if all(preference_arguments) else None,
                   "native_backup_queue_candidate": arguments.native_backup_queue,
                   "owner_getter_only": arguments.owner_getter_only,
                   "app_version_bridge_candidate": arguments.app_version is not None,
                   "native_backup_candidate_observations": backup_candidate,
                   "native_backup_android_parity": "NOT_VERIFIED" if arguments.native_backup_queue else None,
                   "outputs": {path.name: digest(path) for path in output.iterdir() if path.is_file()}})
    if not result["probe_unchanged"]:
        result["status"] = "BLOCKED"
    elif passed and arguments.owner_getter_only:
        result["status"] = "OWNER_GETTER_OBSERVED"
    elif passed and arguments.catalog_mode:
        result["status"] = "CATALOG_" + arguments.catalog_mode.upper().replace("-", "_") + "_OBSERVED"
    (output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({key: result[key] for key in ["exit_code", "status", "observations"]}))
    return int(result["status"] == "BLOCKED")


if __name__ == "__main__":
    raise SystemExit(main())
