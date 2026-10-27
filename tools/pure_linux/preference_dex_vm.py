"""Bounded control-flow interpreter with explicit synthetic platform boundaries."""

import json
import re

from preference_method_audit import HEADER, INDEX, METHODS
from preference_model import DecodeFailure, PlatformFailure


SELECTED = METHODS[2:6]


def parse_methods(text):
    lines = text.splitlines()
    methods = {}
    for index, line in enumerate(lines):
        match = HEADER.fullmatch(line)
        if not match or match[1] not in SELECTED:
            continue
        name = match[1]
        if name in methods:
            raise ValueError("duplicate method")
        metadata = {}
        for preceding in lines[max(0, index - 9):index]:
            match = re.fullmatch(r"\s*(registers|ins)\s*:\s*(\d+)", preceding)
            if match:
                metadata[match[1]] = int(match[2])
        instructions, catches = {}, []
        catching = False
        pending_range = None
        complete = False
        for following in lines[index + 1:]:
            stripped = following.strip()
            if stripped == "positions     :":
                complete = True
                break
            if stripped.startswith("catches"):
                catching = True
            elif catching:
                match = re.fullmatch(r"0x([0-9a-f]+) - 0x([0-9a-f]+)", stripped)
                if match:
                    pending_range = tuple(int(value, 16) for value in match.groups())
                else:
                    match = re.fullmatch(r"Ljava/lang/Exception; -> 0x([0-9a-f]+)", stripped)
                    if not match or pending_range is None:
                        raise ValueError("unsupported catch entry")
                    catches.append((*pending_range, int(match[1], 16)))
            else:
                if "|" not in following:
                    raise ValueError("invalid instruction line")
                instruction = INDEX.sub("", following.split("|", 1)[1])
                match = re.fullmatch(r"([0-9a-f]+): (.+)", instruction)
                if not match or int(match[1], 16) in instructions:
                    raise ValueError("invalid or duplicate instruction")
                instructions[int(match[1], 16)] = match[2]
        if not complete or set(metadata) != {"registers", "ins"} or 0 not in instructions:
            raise ValueError("incomplete method")
        for start, end, handler in catches:
            if start not in instructions or end not in instructions or handler not in instructions or end <= start:
                raise ValueError("invalid catch range")
        methods[name] = {**metadata, "instructions": instructions, "catches": catches}
    if set(methods) != set(SELECTED):
        raise ValueError("selected method missing")
    return methods


class DexMachine:
    def __init__(self, methods, storage):
        self.methods = methods
        self.storage = storage
        self.visited = set()
        self.steps = 0
        self.last_slot = None

    def field(self, owner, field):
        if owner == ("app",):
            slots = {"o:Lc/a;": 0, "p:Lc/a;": 1}
            if field not in slots:
                raise ValueError("unknown application field")
            return ("slot", slots[field])
        if isinstance(owner, tuple) and owner[0] == "slot":
            kinds = {"a:Ljava/lang/Object;": "encrypt", "b:Ljava/lang/Object;": "decrypt",
                     "c:Ljava/lang/Object;": "key", "d:Ljava/lang/Object;": "preferences"}
            if field not in kinds:
                raise ValueError("unknown preference field")
            return (kinds[field], owner[1])
        raise ValueError("unsupported field owner")

    def call(self, target, arguments):
        if target == "Lcom/supercell/titan/GameApp;.getInstance:()Lcom/supercell/titan/GameApp;":
            return ("app",)
        if target == "Lc/a;.j:(Ljava/lang/String;)Ljava/lang/String;":
            return self.execute(SELECTED[0], arguments)
        if target == "Lc/a;.n:(Ljava/lang/String;Ljava/lang/String;)V":
            return self.execute(SELECTED[1], arguments)
        if target == "Lc/a;.h:(Ljava/lang/String;Ljavax/crypto/Cipher;)Ljava/lang/String;":
            value, (kind, slot) = arguments
            if kind not in {"key", "encrypt"}:
                raise ValueError("unexpected cipher")
            return self.storage.codec(slot, 0 if kind == "key" else 1, value)
        if target == "Ljava/lang/String;.isEmpty:()Z":
            return not self.string(arguments[0])
        if target == "Ljava/lang/String;.equals:(Ljava/lang/Object;)Z":
            return self.string(arguments[0]) == arguments[1]
        if target == "Landroid/util/Base64;.decode:(Ljava/lang/String;I)[B":
            if arguments[1] != 2:
                raise ValueError("unexpected Base64 flags")
            return arguments[0]
        if target == "Ljavax/crypto/Cipher;.doFinal:([B)[B":
            (kind, slot), value = arguments
            if kind != "decrypt":
                raise ValueError("unexpected cipher")
            self.last_slot = slot
            return self.storage.codec(slot, 2, value)
        if target == "Ljava/lang/String;.<init>:([BLjava/lang/String;)V":
            destination, value, encoding = arguments
            if encoding != "UTF-8" or not isinstance(destination, dict):
                raise ValueError("unsupported string construction")
            destination["string"] = value
            return None
        if target == "Lcom/supercell/titan/GameApp;.debuggerException:(Ljava/lang/Exception;)V":
            if self.last_slot is None:
                raise ValueError("exception without decode slot")
            return self.storage.decode_exception(self.last_slot)
        if target == "Lcom/supercell/titan/GameApp;.requestBackup:()V":
            return self.storage.backup()
        signature = target.split(";.", 1)
        if len(signature) != 2:
            raise ValueError("unsupported invocation")
        owner, method = signature
        if owner == "Landroid/content/SharedPreferences":
            (kind, slot), *rest = arguments
            if kind != "preferences":
                raise ValueError("invalid preferences receiver")
            if method == "contains:(Ljava/lang/String;)Z":
                return self.storage.contains(slot, *rest)
            if method == "getString:(Ljava/lang/String;Ljava/lang/String;)Ljava/lang/String;":
                return self.storage.get(slot, rest[0])
            if method == "edit:()Landroid/content/SharedPreferences$Editor;":
                return ("editor", self.storage.edit(slot))
        if owner == "Landroid/content/SharedPreferences$Editor":
            receiver, *rest = arguments
            kind, token = receiver
            if kind != "editor":
                raise ValueError("invalid editor receiver")
            if method == "commit:()Z":
                return self.storage.commit(token)
            if method == "remove:(Ljava/lang/String;)Landroid/content/SharedPreferences$Editor;":
                self.storage.change(token, 1, rest[0], None)
                return receiver
            if method == "putString:(Ljava/lang/String;Ljava/lang/String;)Landroid/content/SharedPreferences$Editor;":
                self.storage.change(token, 0, *rest)
                return receiver
        raise ValueError("unsupported invocation")

    @staticmethod
    def string(value):
        return value["string"] if isinstance(value, dict) else value

    def execute(self, name, arguments):
        method = self.methods[name]
        if len(arguments) != method["ins"]:
            raise ValueError("argument count mismatch")
        registers = {"v" + str(index): value for index, value in
                     enumerate(arguments, method["registers"] - method["ins"])}
        instructions = method["instructions"]
        ordered = sorted(instructions)
        following = dict(zip(ordered, ordered[1:]))
        pc, result, exception = 0, None, None
        while True:
            self.steps += 1
            if self.steps > 8192 or pc not in instructions:
                raise ValueError("execution bound or invalid PC")
            self.visited.add((name, pc))
            instruction = instructions[pc]
            opcode, _, operands = instruction.partition(" ")
            parts = operands.split(", ")
            next_pc = following.get(pc)
            try:
                if opcode == "return-void":
                    return None
                if opcode == "return-object":
                    return self.string(registers[operands])
                if opcode == "iget-object":
                    registers[parts[0]] = self.field(registers[parts[1]], parts[2].split(";.")[1])
                elif opcode == "check-cast":
                    if not parts[1].startswith(("Ljavax/crypto/", "Landroid/content/")):
                        raise ValueError("unsupported cast")
                elif opcode.startswith("invoke-"):
                    match = re.fullmatch(r"\{([^}]*)\}, (.+)", operands)
                    if not match:
                        raise ValueError("invalid invocation")
                    result = self.call(match[2], [registers[reg] for reg in match[1].split(", ") if reg])
                elif opcode in {"move-result", "move-result-object"}:
                    registers[operands] = result
                elif opcode == "move-exception":
                    registers[operands] = exception
                elif opcode in {"move", "move-object"}:
                    registers[parts[0]] = registers[parts[1]]
                elif opcode == "const-string":
                    registers[parts[0]] = json.loads(operands.split(", ", 1)[1])
                elif opcode == "const/4":
                    match = re.fullmatch(r"(v\d+), #int (-?\d+) // #.*", operands)
                    if not match:
                        raise ValueError("unsupported constant")
                    registers[match[1]] = int(match[2])
                elif opcode == "new-instance":
                    if parts[1] != "Ljava/lang/String;":
                        raise ValueError("unsupported allocation")
                    registers[parts[0]] = {}
                elif opcode in {"if-eqz", "if-nez", "goto", "goto/16"}:
                    match = re.fullmatch(r"(?:(v\d+), )?([0-9a-f]+) // ([+-][0-9a-f]+)", operands)
                    if not match or pc + int(match[3], 16) != int(match[2], 16):
                        raise ValueError("invalid branch displacement")
                    target = int(match[2], 16)
                    if target not in instructions:
                        raise ValueError("branch target is not an instruction")
                    if opcode.startswith("goto") or ((not registers[match[1]]) == (opcode == "if-eqz")):
                        next_pc = target
                else:
                    raise ValueError("unsupported instruction")
            except (PlatformFailure, DecodeFailure) as error:
                handlers = [handler for start, end, handler in method["catches"] if start <= pc < end]
                if len(handlers) != 1:
                    raise
                exception = error
                next_pc = handlers[0]
            pc = next_pc
