import argparse
import string
import subprocess
from dataclasses import dataclass

parser = argparse.ArgumentParser()
parser.add_argument("base")
parser.add_argument("tip")


@dataclass(frozen=True)
class Numstat:
    added: int
    removed: int
    path: str


@dataclass(frozen=True)
class CommitNumstat:
    commit: str
    numstat: list[Numstat]


def _parse_numstat(cmdline: tuple[str, ...]) -> list[CommitNumstat]:
    numstat: list[Numstat] | None = None
    result: list[CommitNumstat] = []
    with subprocess.Popen(
        cmdline, universal_newlines=True, stdout=subprocess.PIPE
    ) as p:
        assert p.stdout
        for line in p.stdout:
            if not line.strip():
                continue
            if "\t" in line:
                added, removed, path = line.strip().split("\t", 2)
                assert numstat is not None
                numstat.append(Numstat(int(added), int(removed), path))
            else:
                assert len(line.strip()) >= 40
                assert set(line.strip()) <= set(string.hexdigits)
                numstat = []
                result.append(CommitNumstat(line.strip().lower(), numstat))
    return result


def git_show_numstat(oids: list[str]) -> list[CommitNumstat]:
    return _parse_numstat(
        ("git", "show", "--pretty=%H", "--numstat", "--no-renames", *oids)
    )


def git_log_numstat(refspec: str) -> list[CommitNumstat]:
    return _parse_numstat(
        ("git", "log", "--reverse", "--pretty=%H", "--numstat", "--no-renames", refspec)
    )


def git_rev_parse(rev: str) -> str | None:
    p = subprocess.run(
        ("git", "rev-parse", "--quiet", "--verify", rev),
        universal_newlines=True,
        stdout=subprocess.PIPE,
    )
    if p.returncode == 1:
        # Object does not exist
        return None
    p.check_returncode()
    return p.stdout.strip()


def git_write_empty_blob() -> str:
    return subprocess.check_output(
        ("git", "hash-object", "-w", "--stdin"),
        universal_newlines=True,
        stdin=subprocess.DEVNULL,
    ).strip()


def git_merge_file(*, current: str, base: str, other: str) -> tuple[str, int]:
    p = subprocess.run(
        ("git", "merge-file", "--object-id", current, base, other),
        universal_newlines=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    if not 0 <= p.returncode <= 127:
        zero_object = git_write_empty_blob()
        if git_rev_parse(current) is None:
            current = zero_object
        if git_rev_parse(base) is None:
            base = zero_object
        if git_rev_parse(other) is None:
            other = zero_object
        p = subprocess.run(
            ("git", "merge-file", "--object-id", current, base, other),
            universal_newlines=True,
            stdout=subprocess.PIPE,
        )
    if not 0 <= p.returncode <= 127:
        p.check_returncode()
    return (p.stdout.strip(), p.returncode)
