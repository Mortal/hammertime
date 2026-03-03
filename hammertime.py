import argparse
import os
import string
import subprocess
from dataclasses import dataclass

# Set this env.var to debug what the script is doing in API or git terms:
DEBUG_GIT_COMMANDS = bool(os.environ.get("DEBUG_GIT_COMMANDS"))

parser = argparse.ArgumentParser()
parser.add_argument("base")
parser.add_argument("tip")


def public[T](f: T) -> T:
    return f


@dataclass(frozen=True)
class Numstat:
    added: int | None
    removed: int | None
    path: str


@dataclass(frozen=True)
class CommitNumstat:
    commit: str
    numstat: list[Numstat]


def _parse_numstat(cmdline: tuple[str, ...]) -> list[CommitNumstat]:
    if DEBUG_GIT_COMMANDS:
        print(*cmdline, flush=True)
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
                if added == "-" and removed == "-":
                    numstat.append(Numstat(None, None, path))
                else:
                    numstat.append(Numstat(int(added), int(removed), path))
            else:
                assert len(line.strip()) >= 40
                assert set(line.strip()) <= set(string.hexdigits)
                numstat = []
                result.append(CommitNumstat(line.strip().lower(), numstat))
    return result


@public
def git_show_numstat(oid: str) -> CommitNumstat:
    return _parse_numstat(
        ("git", "show", "--pretty=%H", "--numstat", "--no-renames", oid)
    )[0]


@public
def git_log_numstat(refspec: str) -> list[CommitNumstat]:
    return _parse_numstat(
        ("git", "log", "--reverse", "--pretty=%H", "--numstat", "--no-renames", refspec)
    )


@public
def git_rev_parse(refspec: str) -> str | None:
    cmdline = ["git", "rev-parse", "--quiet", "--verify", "--end-of-options", refspec]
    if DEBUG_GIT_COMMANDS:
        print(*cmdline, flush=True)
    proc = subprocess.run(cmdline, text=True, stdout=subprocess.PIPE)
    if proc.returncode == 1:
        # Object does not exist
        return None
    proc.check_returncode()
    res = proc.stdout.strip()
    assert res
    return res


@public
def git_write_empty_blob() -> str:
    cmdline = ["git", "hash-object", "-w", "--stdin"]
    if DEBUG_GIT_COMMANDS:
        print(*cmdline, flush=True)
    return subprocess.check_output(
        cmdline,
        universal_newlines=True,
        stdin=subprocess.DEVNULL,
    ).strip()


@public
def git_merge_file(*, current: str, base: str, other: str) -> tuple[str, int]:
    cmdline = ["git", "merge-file", "--object-id", current, base, other]
    if DEBUG_GIT_COMMANDS:
        print(*cmdline, flush=True)
    p = subprocess.run(
        cmdline,
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


@public
def git_rev_parse_show_toplevel() -> str:
    cmdline = ["git", "rev-parse", "--show-toplevel"]
    if DEBUG_GIT_COMMANDS:
        print("$", *cmdline, flush=True)
    return subprocess.check_output(cmdline, text=True).rstrip("\n")


@public
def git_write_tree() -> str:
    cmdline = ["git", "write-tree"]
    if DEBUG_GIT_COMMANDS:
        print(*cmdline, flush=True)
    return subprocess.check_output(cmdline, text=True).rstrip("\n")


@public
def git_files_with_unstaged_changes() -> list[str]:
    cmdline = ["git", "diff", "--name-only", "-z"]
    if DEBUG_GIT_COMMANDS:
        print("$", *cmdline, flush=True)
    diff_filenames = subprocess.check_output(cmdline, text=True).rstrip("\0")
    return diff_filenames.split("\0") if diff_filenames else []


@public
def git_files_with_staged_changes() -> list[str]:
    cmdline = ["git", "diff", "--name-only", "-z", "--cached"]
    if DEBUG_GIT_COMMANDS:
        print("$", *cmdline, flush=True)
    diff_filenames = subprocess.check_output(cmdline, text=True).rstrip("\0")
    return diff_filenames.split("\0") if diff_filenames else []


@public
def git_any_staged_changes() -> bool:
    cmdline = ["git", "diff", "--quiet", "--cached"]
    if DEBUG_GIT_COMMANDS:
        print(*cmdline, flush=True)
    proc = subprocess.run(cmdline, check=False)
    if proc.returncode == 1:
        return True
    proc.check_returncode()
    return False


@public
def git_is_same(refspec1: str, refspec2: str) -> bool:
    cmdline = ["git", "diff", "--quiet", "--end-of-options", refspec1, refspec2]
    if DEBUG_GIT_COMMANDS:
        print(*cmdline, flush=True)
    proc = subprocess.run(cmdline, check=False)
    if proc.returncode == 1:
        return False
    proc.check_returncode()
    return True


@public
def git_set_head_and_staging(head: str, staging: str | None) -> None:
    if staging is None or head == staging:
        cmdline = ["git", "reset", "-q", head, "--"]
        if DEBUG_GIT_COMMANDS:
            print(*cmdline, flush=True)
        subprocess.check_call(cmdline)
    else:
        git_set_head(head)
        git_set_staging(staging)


@public
def git_set_head(head: str) -> None:
    cmdline = ["git", "reset", "-q", "--soft", head, "--"]
    if DEBUG_GIT_COMMANDS:
        print(*cmdline, flush=True)
    subprocess.check_call(cmdline)


@public
def git_set_staging(staging: str, cwd: str | None = None) -> None:
    cmdline = ["git", "reset", "-q", staging, "--", "."]
    if DEBUG_GIT_COMMANDS:
        print(*cmdline, flush=True)
    subprocess.check_call(cmdline, cwd=cwd)


@public
def git_apply_cached_from_git_show(refspec: str, cwd: str | None = None) -> int:
    cmdline1 = ["git", "show", "--end-of-options", refspec]
    if DEBUG_GIT_COMMANDS:
        print(*cmdline1, flush=True)
    with subprocess.Popen(cmdline1, stdout=subprocess.PIPE, cwd=cwd) as p:
        cmdline2 = ["git", "apply", "--allow-empty", "--cached", "-"]
        if DEBUG_GIT_COMMANDS:
            print(*cmdline2, flush=True)
        proc = subprocess.run(cmdline2, stdin=p.stdout, check=False, cwd=cwd)
        p.wait()
        return proc.returncode


@public
def git_apply_cached_recount(patch: str, cwd: str | None = None) -> None:
    cmdline = ["git", "apply", "--allow-empty", "--cached", "--recount", "-"]
    if DEBUG_GIT_COMMANDS:
        print(*cmdline, flush=True)
    subprocess.run(cmdline, text=True, input=patch, check=True, cwd=cwd)


@public
def git_commit_with_same_authorship(
    commit_hash: str, file_list: list[str] | None = None
) -> None:
    cmdline = [
        "git",
        "commit",
        "--allow-empty",
        "--allow-empty-message",
        "-qnC",
        commit_hash,
        "--",
    ]
    if file_list is not None:
        assert file_list
        cmdline += file_list
    if DEBUG_GIT_COMMANDS:
        print(*cmdline, flush=True)
    subprocess.check_call(cmdline)


@public
def git_amend(file_list: list[str] | None) -> None:
    cmdline = [
        "git",
        "commit",
        "--amend",
        "--no-edit",
        "--",
    ]
    if file_list is not None:
        assert file_list
        cmdline += file_list
    if DEBUG_GIT_COMMANDS:
        print(*cmdline, flush=True)
    subprocess.run(cmdline, text=True, check=True)


@public
def git_amend_with_commit_msg(commit_msg: str, file_list: list[str] | None) -> None:
    cmdline = [
        "git",
        "commit",
        "--allow-empty",
        "--allow-empty-message",
        "--amend",
        "-qnF",
        "-",
        "--",
    ]
    if file_list is not None:
        assert file_list
        cmdline += file_list
    if DEBUG_GIT_COMMANDS:
        print(*cmdline, flush=True)
    subprocess.run(cmdline, text=True, input=commit_msg, check=True)


@public
def git_commit_tree(tree: str, message: str, parents: list[str]) -> str:
    cmdline = ["git", "commit-tree", tree]
    for p in parents:
        cmdline += ["-p", p]
    if DEBUG_GIT_COMMANDS:
        print(*cmdline, flush=True)
    return subprocess.run(
        cmdline, input=message, text=True, stdout=subprocess.PIPE
    ).stdout.strip()


@public
def git_show_commit_message(refspec: str) -> str:
    return git_show_commit_author_timestamp_and_message(refspec)[1]


@public
def git_show_commit_author_timestamp_and_message(refspec: str) -> tuple[str, str]:
    # Get the merge commit's timestamp (%aI) and full commit message (%B).
    cmdline = ["git", "show", "-s", "--pretty=%aI %B", refspec]
    if DEBUG_GIT_COMMANDS:
        print("$", *cmdline, flush=True)
    output = subprocess.check_output(cmdline, text=True).strip()
    return output.split(None, 1)
