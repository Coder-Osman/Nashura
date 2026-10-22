"""Validate and summarize the frozen step-24 owner-swapped evaluation."""

import argparse
import json
import os
from pathlib import Path

import paramiko

from native_runner.training.fair_search.evaluation import paired_confidence


HOSTS = ("35", "37", "39", "41")
ROOT = "/home/zhichun/fair-eval-20261011-step24"
CANDIDATE_SHA = "4c4b68877d6fa25f8c0ae692e177b8bd1fb81238b64f4823cb002f6c47ac6627"
BASELINE_SHA = "26d592cd58876eceb60c9f34a5ce02f18df24d003991d74e4fdd1af90a5f1ee7"


def read_file(sftp, path):
    try:
        with sftp.open(path) as stream:
            return stream.read().decode()
    except FileNotFoundError:
        return None


def collect(password):
    records = {}
    shards = []
    for host_index, host in enumerate(HOSTS):
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(f"172.16.48.{host}", username="zhichun", password=password, timeout=10)
        try:
            sftp = client.open_sftp()
            try:
                for slot in range(8):
                    shard = host_index * 8 + slot
                    start, end = shard * 1000 // 32, (shard + 1) * 1000 // 32
                    directory = f"{ROOT}/shard-{shard:02d}"
                    identity_text = read_file(sftp, f"{directory}/identity.json")
                    if identity_text is None:
                        shards.append(dict(shard=shard, host=host, games=0, state="starting"))
                        continue
                    identity = json.loads(identity_text)
                    if (identity["candidate_sha256"] != CANDIDATE_SHA
                            or identity["baseline_sha256"] != BASELINE_SHA
                            or identity["start_pair"] != start or identity["pairs"] != end - start
                            or identity["seed_base"] != 440000 or identity["mode"] != "policy-direct"
                            or identity["deck"] != "fixed-hog"):
                        raise ValueError(f"shard {shard} identity differs")
                    matches = read_file(sftp, f"{directory}/matches.jsonl") or ""
                    rows = [json.loads(line) for line in matches.splitlines()]
                    for row in rows:
                        pair, owner = row["pair"], row["candidate_owner"]
                        if (not start <= pair < end or owner not in (0, 1)
                                or row["seed"] != 440000 + pair or row["winner"] not in (None, 0, 1)
                                or row["score"] != (.5 if row["winner"] is None
                                                    else float(row["winner"] == owner))
                                or (pair, owner) in records):
                            raise ValueError(f"invalid or duplicate game in shard {shard}")
                        records[pair, owner] = row
                    shards.append(dict(shard=shard, host=host, games=len(rows), expected=2 * (end - start)))
            finally:
                sftp.close()
        finally:
            client.close()
    complete_pairs = [pair for pair in range(1000) if (pair, 0) in records and (pair, 1) in records]
    scores = [(records[pair, 0]["score"], records[pair, 1]["score"]) for pair in complete_pairs]
    summary = dict(candidate_source_checkpoint_sha256="f7da03e8edb8a8f103e0fd80fc6e57464580b735df091cc9c08ae19415dd3c3b",
                   candidate_optimizer_steps=24, baseline_optimizer_steps=0,
                   completed_games=len(records), expected_games=2000,
                   complete_pairs=len(scores), expected_pairs=1000,
                   candidate_wins=sum(row["score"] == 1 for row in records.values()),
                   draws=sum(row["score"] == .5 for row in records.values()),
                   candidate_losses=sum(row["score"] == 0 for row in records.values()),
                   paired_result=paired_confidence(scores) if scores else None,
                   shards=shards)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    summary = collect(os.environ["FAIR_EVAL_SSH_PASSWORD"])
    data = json.dumps(summary, sort_keys=True, indent=2) + "\n"
    if args.output:
        args.output.write_text(data)
    print(data)


if __name__ == "__main__":
    main()
