import hashlib
from pathlib import Path
import struct
import sys


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent / "fair-final-native-command-audit-v1"))
from audit_ct86 import EXPECTED_SHA256, POSITION_HELPER, POSITION_HELPER_END, _disassemble


def signed32(value):
    value &= 0xFFFFFFFF
    return value if value < 0x80000000 else value - 0x100000000


def trunc_div500(value):
    return value // 500 if value >= 0 else -((-value) // 500)


def reference(case):
    if not case["character"]:
        return 0
    coordinate_x, coordinate_y = signed32(case["x"]), signed32(case["y"])
    if coordinate_x < -499:
        return 1
    if trunc_div500(coordinate_y) < signed32(case["lower_y"]):
        return 2
    if trunc_div500(coordinate_x) >= signed32(case["upper_x"]):
        return 3
    if trunc_div500(coordinate_y) >= signed32(case["upper_y"] - case["lower_y"]):
        return 4
    return 0 if case["terrain"] & 1 else 5


def division_certificate(multiplier=274877907, shift=37, divisor=500):
    scale = 1 << shift
    residual = divisor * multiplier - scale
    quotient_max = (1 << 31) // divisor
    residual_upper = residual * quotient_max + multiplier * (divisor - 1)
    product_upper = (1 << 31) * multiplier
    if not (0 < residual < divisor and residual_upper < scale and product_upper < (1 << 63)):
        raise ValueError("division certificate inequalities failed")
    return dict(multiplier=multiplier, shift=shift, divisor=divisor, scale=scale,
        residual=residual, quotient_max=quotient_max, remainder_max=divisor - 1,
        residual_upper=residual_upper, product_upper=product_upper,
        all_signed32_inputs=True, exhaustive_cpu_enumeration=False)


def load_code(library):
    import lief

    library = Path(library)
    if hashlib.sha256(library.read_bytes()).hexdigest() != EXPECTED_SHA256:
        raise ValueError("position oracle library SHA differs")
    binary = lief.parse(str(library))
    instructions = _disassemble(binary, POSITION_HELPER, POSITION_HELPER_END)
    arithmetic = {
        0xCFCFB0: ("mov", "w25, #0x4dd3"),
        0xCFCFB4: ("movk", "w25, #0x1062, lsl #16"),
        0xCFCFBC: ("smull", "x9, w23, w25"),
        0xCFCFC4: ("lsr", "x10, x9, #0x20"),
        0xCFCFC8: ("lsr", "x9, x9, #0x3f"),
        0xCFCFCC: ("add", "w2, w9, w10, asr #5"),
        0xCFCFE0: ("smull", "x9, w22, w25"),
        0xCFCFE8: ("lsr", "x10, x9, #0x3f"),
        0xCFCFEC: ("asr", "x9, x9, #0x25"),
        0xCFCFF0: ("add", "w1, w9, w10"),
    }
    decoded = {entry.address: (entry.mnemonic, entry.op_str) for entry in instructions}
    if any(decoded.get(address) != expected for address, expected in arithmetic.items()):
        raise ValueError("position division instruction sequence differs")
    caller = _disassemble(binary, 0xD30CB8, 0xD30CD8)
    expected = [
        ("ldp", "w1, w2, [x19, #0x28]"), ("and", "w4, w25, #1"),
        ("mov", "x0, x24"), ("mov", "x3, x26"), ("bl", "#0xcfcf50"),
        ("cbz", "w0, #0xd30ce0"), ("add", "w0, w0, #0xe"), ("b", "#0xd30aa8"),
    ]
    if [(entry.mnemonic, entry.op_str) for entry in caller] != expected:
        raise ValueError("ct86 position return mapping differs")
    code = b"".join(bytes(entry.bytes) for entry in instructions)
    return code, dict(library_sha256=EXPECTED_SHA256,
        helper_start=hex(POSITION_HELPER), helper_end=hex(POSITION_HELPER_END),
        helper_sha256=hashlib.sha256(code).hexdigest(), instruction_count=len(instructions),
        caller_sha256=hashlib.sha256(b"".join(bytes(entry.bytes) for entry in caller)).hexdigest(),
        ct86_nonzero_return_offset=14,
        arithmetic_addresses=[hex(address) for address in sorted(arithmetic)])


class ArmPositionOracle:
    PLAYER = 0x71000000
    MANAGER = 0x71001000
    CONFIG_OWNER = 0x71002000
    CONFIG = 0x71003000
    TERRAIN = 0x71004000
    CARD = 0x71005000
    UI = 0x71006000
    VTABLE = 0x71007000
    CHARACTER = 0x71008000
    STACK = 0x70008000
    STOP = 0x72000000
    NOTIFY = 0x72001000
    CALLS = (0xE2AD9C, 0x1062BB8, 0xDCCC68, 0xE379AC, 0x138A810, 0x138A700, NOTIFY)

    def __init__(self, code):
        import unicorn
        from unicorn import arm64_const

        self.constants = arm64_const
        self.cpu = unicorn.Uc(unicorn.UC_ARCH_ARM64, unicorn.UC_MODE_ARM)
        self.cpu.mem_map(0xCFC000, 0x2000)
        self.cpu.mem_write(POSITION_HELPER, code)
        self.cpu.mem_map(0x70000000, 0x10000)
        self.cpu.mem_map(self.PLAYER, 0x10000)
        pages = set()
        for target in (*self.CALLS, self.STOP):
            page = target & ~0xFFF
            if page not in pages:
                self.cpu.mem_map(page, 0x1000)
                pages.add(page)
            self.cpu.mem_write(target, bytes.fromhex("c0035fd6"))
        self.write64(self.PLAYER + 8, self.MANAGER)
        self.write64(self.PLAYER + 0x20, self.CONFIG_OWNER)
        self.write64(self.PLAYER + 0xC0, self.TERRAIN)
        self.write64(self.UI, self.VTABLE)
        self.write64(self.VTABLE + 0x238, self.NOTIFY)
        self.cpu.mem_write(self.CARD + 0x14D, b"\x29\xb3")
        self.cpu.hook_add(unicorn.UC_HOOK_CODE, self.on_code)
        self.cpu.hook_add(unicorn.UC_HOOK_MEM_WRITE, self.on_write)

    def register(self, index):
        return getattr(self.constants, "UC_ARM64_REG_X" + str(index))

    def read(self, index):
        return self.cpu.reg_read(self.register(index))

    def write64(self, address, value):
        self.cpu.mem_write(address, struct.pack("<Q", value & 0xFFFFFFFFFFFFFFFF))

    def write32(self, address, value):
        self.cpu.mem_write(address, struct.pack("<I", value & 0xFFFFFFFF))

    def on_write(self, cpu, access, address, size, value, userdata):
        if not self.STACK - 0x90 <= address or address + size > self.STACK:
            raise ValueError("position helper wrote outside its stack frame")

    def on_code(self, cpu, address, size, userdata):
        if POSITION_HELPER <= address < POSITION_HELPER_END:
            self.visited.add(address)
            return
        if address not in self.CALLS:
            raise ValueError("unexpected position helper instruction")
        arguments = tuple(self.read(index) for index in range(5))
        self.calls.append(address)
        if address == 0xE2AD9C:
            if arguments[:2] != (self.CARD, 0):
                raise ValueError("character helper arguments differ")
            result = self.CHARACTER if self.case["character"] else 0
        elif address == 0x1062BB8:
            if arguments[0] != self.MANAGER:
                raise ValueError("UI helper argument differs")
            result = self.UI
        elif address == 0xDCCC68:
            if arguments[0] != self.CONFIG_OWNER:
                raise ValueError("config helper argument differs")
            result = self.CONFIG
        elif address == 0xE379AC:
            coordinates = tuple(signed32(value) for value in arguments[1:3])
            expected = (trunc_div500(signed32(self.case["x"])), trunc_div500(signed32(self.case["y"])))
            if arguments[0] != self.TERRAIN or coordinates != expected or arguments[3:] != (0x29, 0xB3):
                raise ValueError("terrain helper arguments differ")
            result = self.case["terrain"] & 0xFFFFFFFF
        elif address == self.NOTIFY:
            if arguments[:2] != (self.UI, self.STACK - 0x60):
                raise ValueError("notification arguments differ")
            result = 0
        else:
            if arguments[0] != self.STACK - 0x60:
                raise ValueError("string helper argument differs")
            result = 0
        for index in range(19):
            cpu.reg_write(self.register(index), 0xA500000000000000 + index)
        cpu.reg_write(self.register(0), result)
        cpu.reg_write(self.constants.UC_ARM64_REG_NZCV, 0xA0000000)

    def run(self, case):
        self.case, self.calls, self.visited = case, [], set()
        self.write32(self.PLAYER + 0x188, case["upper_x"])
        self.write32(self.PLAYER + 0x18C, case["upper_y"])
        self.write32(self.CONFIG + 0x1C8, case["lower_y"])
        for index in range(31):
            self.cpu.reg_write(self.register(index), 0xB000000000000000 + index)
        saved = [self.read(index) for index in range(19, 30)]
        for index, value in enumerate((self.PLAYER, case["x"], case["y"], self.CARD, case["notify"])):
            self.cpu.reg_write(self.register(index), value & 0xFFFFFFFFFFFFFFFF)
        self.cpu.reg_write(self.register(30), self.STOP)
        self.cpu.reg_write(self.constants.UC_ARM64_REG_SP, self.STACK)
        self.cpu.reg_write(self.constants.UC_ARM64_REG_NZCV, 0xF0000000)
        self.cpu.emu_start(POSITION_HELPER, self.STOP, count=256)
        if self.cpu.reg_read(self.constants.UC_ARM64_REG_PC) != self.STOP:
            raise ValueError("position helper did not return within instruction budget")
        if saved != [self.read(index) for index in range(19, 30)]:
            raise ValueError("position helper changed callee-saved registers")
        if self.cpu.reg_read(self.constants.UC_ARM64_REG_SP) != self.STACK or self.read(30) != self.STOP:
            raise ValueError("position helper changed caller stack or return identity")
        result = self.read(0) & 0xFFFFFFFF
        expected_notifications = int(reference(case) == 5 and bool(case["notify"] & 1))
        if self.calls.count(self.NOTIFY) != expected_notifications:
            raise ValueError("position notification differs from conditional reference")
        return result, tuple(self.calls), frozenset(self.visited)
