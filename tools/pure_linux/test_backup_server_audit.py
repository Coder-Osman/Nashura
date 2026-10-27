import unittest

from backup_server_audit import SERVER_METHODS, summarize


def block(name, instructions):
    return "\n".join([
        "# method_id: 0x1",
        f"00000000: |[0001] {name}",
        "  registers     : 2",
        "  ins            : 1",
        "  outs           : 1",
        "  insns size     : 2 16-bit code units",
        *[f"{index * 2:08x}: | {index * 2:04x}: {instruction}"
          for index, instruction in enumerate(instructions)],
        "catches       :",
        "positions     :",
    ])


class BackupServerAuditTests(unittest.TestCase):
    def fixture(self):
        calls = ["return-void"]
        return "\n".join(block(method, calls) for method in SERVER_METHODS)

    def test_requires_every_allowlisted_method(self):
        with self.assertRaises(ValueError):
            summarize(block(SERVER_METHODS[0], ["return-void"]))

    def test_summarizes_calls_without_publishing_instructions(self):
        text = self.fixture()
        name = SERVER_METHODS[0]
        text = text.replace(block(name, ["return-void"]), block(name, [
            "invoke-virtual {v0}, Lcom/example/Service;.enqueue:(Ljava/lang/String;)V",
            "if-eqz v0, 00000004",
            "return-void",
        ]))
        result = summarize(text)
        self.assertEqual(result[name]["direct_calls"],
                         ["Lcom/example/Service;.enqueue:(Ljava/lang/String;)V"])
        self.assertEqual(result[name]["control_ops"], ["if-eqz"])
        self.assertNotIn("instruction_text", result[name])

    def test_rejects_unrecognized_invoke_syntax(self):
        name = SERVER_METHODS[0]
        text = self.fixture().replace(block(name, ["return-void"]), block(name, [
            "invoke-custom {v0}, callsite@0001",
            "return-void",
        ]))
        with self.assertRaises(ValueError):
            summarize(text)


if __name__ == "__main__":
    unittest.main()
