import argparse


def bounded_seconds(value, maximum):
    try:
        seconds = int(value)
    except (TypeError, ValueError) as error:
        raise argparse.ArgumentTypeError("timeout must be integer seconds") from error
    if isinstance(value, bool) or str(seconds) != str(value) or not 1 <= seconds <= maximum:
        raise argparse.ArgumentTypeError(f"timeout must be between 1 and {maximum} seconds")
    return seconds


def training_timeout(value):
    return bounded_seconds(value, 21600)


def phase_timeout(value):
    return bounded_seconds(value, 43200)
