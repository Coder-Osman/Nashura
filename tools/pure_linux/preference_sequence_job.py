"""Reproduce only the reviewed historical store1 padding sequence in two children."""

from preference_cipher_job import validate_mappings
from preference_reference_job import arguments, execute


def validate(parsed, mapped, identities, expected_records):
    rows = [{"result": result, "length": length, "expected_length": length,
             "matched": True, "output_guard": True}
            for result, length in ((0, 16), (-3, 0), (0, 32), (0, 16))]
    return (type(expected_records) is int and expected_records == 4 and
            isinstance(parsed, dict) and parsed == {
                "status": "PASS", "records": 4, "libg_loaded": False,
                "scope": "historical_store1_padding_sequence_bytes_only", "rows": rows} and
            type(parsed["records"]) is int and
            all(type(row["result"]) is int and type(row["length"]) is int and
                type(row["expected_length"]) is int and row["matched"] is True and
                row["output_guard"] is True for row in parsed["rows"]) and
            validate_mappings(mapped, identities))


def main():
    return execute(arguments(__doc__), validate, "nashura.preference-sequence-native.v1")


if __name__ == "__main__":
    raise SystemExit(main())
