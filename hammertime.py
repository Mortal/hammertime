"""Thin wrappers around the git command-line interface.

Each public function shells out to git and parses its output; used by
htime.py to manipulate commits, the index and HEAD. Set the
DEBUG_GIT_COMMANDS environment variable to echo every git command run.
"""

import os
import string
import subprocess
from dataclasses import dataclass

# Set this env.var to debug what the script is doing in API or git terms:
DEBUG_GIT_COMMANDS = bool(os.environ.get("DEBUG_GIT_COMMANDS"))

git_env = {
    **os.environ,
    "GIT_LITERAL_PATHSPECS": "1",
    "GIT_OPTIONAL_LOCKS": "0",
    "GIT_PAGER": "cat",
    "GIT_NO_REPLACE_OBJECTS": "1",
}


def public[T](f: T) -> T:
    """No-op marker decorator identifying a function as part of this module's API."""
    return f


@dataclass(frozen=True)
class Numstat:
    path: str


@dataclass(frozen=True)
class CommitNumstat:
    commit: str
    numstat: list[Numstat]


def _parse_numstat(cmdline: tuple[str, ...]) -> list[CommitNumstat]:
    # Output format: one 40-hex-digit line per commit, followed by
    # tab-separated "<added>\t<removed>\t<path>" lines ("-" for binary files).
    if DEBUG_GIT_COMMANDS:
        print(*cmdline, flush=True)
    numstat: list[Numstat] | None = None
    result: list[CommitNumstat] = []
    with subprocess.Popen(
        cmdline, universal_newlines=True, stdout=subprocess.PIPE, env=git_env
    ) as p:
        assert p.stdout
        for line in p.stdout:
            if not line.strip():
                continue
            if "\t" in line:
                _added, _removed, path = line.strip().split("\t", 2)
                assert numstat is not None
                numstat.append(Numstat(path))
            else:
                assert len(line.strip()) >= 40
                assert set(line.strip()) <= set(string.hexdigits)
                numstat = []
                result.append(CommitNumstat(line.strip().lower(), numstat))
    return result


@public
def git_rev_parse_git_dir() -> str:
    cmdline = ["git", "rev-parse", "--git-dir"]
    if DEBUG_GIT_COMMANDS:
        print("$", *cmdline, flush=True)
    return subprocess.check_output(cmdline, text=True).rstrip("\n")


@public
def git_cherry_pick_in_progress() -> bool:
    g = git_rev_parse_git_dir()
    return os.path.isfile(f"{g}/CHERRY_PICK_HEAD")


@public
def git_sequencer_status() -> str:
    """Name of the in-progress git operation (rebase/merge/...), or ""."""
    g = git_rev_parse_git_dir()
    if os.path.isdir(f"{g}/rebase-merge"):
        return "rebase"
    if os.path.isdir(f"{g}/rebase-apply"):
        return "rebase"
    if os.path.isfile(f"{g}/MERGE_HEAD"):
        return "merge"
    if os.path.isfile(f"{g}/CHERRY_PICK_HEAD"):
        return "cherry-pick"
    if os.path.isfile(f"{g}/REVERT_HEAD"):
        return "revert"
    if os.path.isfile(f"{g}/sequencer/todo"):
        return "sequencer"
    if os.path.isfile(f"{g}/BISECT_LOG"):
        return "bisect"
    return ""


@public
def git_show_numstat(oid: str) -> CommitNumstat:
    return _parse_numstat(
        (
            "git",
            "show",
            "--pretty=%H",
            "--numstat",
            "--no-renames",
            "--end-of-options",
            oid,
            "--",
        )
    )[0]


@public
def git_log_numstat(refspec: str) -> list[CommitNumstat]:
    return _parse_numstat(
        (
            "git",
            "log",
            "--reverse",
            "--pretty=%H",
            "--numstat",
            "--no-renames",
            "--end-of-options",
            refspec,
            "--",
        )
    )


@public
def git_rev_parse_head() -> str:
    res = git_rev_parse("HEAD")
    assert res is not None
    return res


@public
def git_rev_parse(refspec: str) -> str | None:
    cmdline = ["git", "rev-parse", "--quiet", "--verify", "--end-of-options", refspec]
    if DEBUG_GIT_COMMANDS:
        print(*cmdline, flush=True)
    proc = subprocess.run(cmdline, text=True, stdout=subprocess.PIPE, env=git_env)
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
        env=git_env,
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
        env=git_env,
    )
    # git merge-file exits 0 on success, 1..127 = number of conflicting
    # hunks, and >127 indicates an error (e.g. a nonexistent object).
    if not 0 <= p.returncode <= 127:
        # Retry, substituting an empty blob for any object that does not
        # exist (e.g. a file added or deleted by one of the commits).
        zero_object = git_write_empty_blob()
        if git_rev_parse(current) is None:
            current = zero_object
        if git_rev_parse(base) is None:
            base = zero_object
        if git_rev_parse(other) is None:
            other = zero_object
        # Unlike the first run above, errors from the git merge-file below
        # are fatal, so let it inherit our stderr and print any errors.
        p = subprocess.run(
            ("git", "merge-file", "--object-id", current, base, other),
            universal_newlines=True,
            stdout=subprocess.PIPE,
            stderr=None,
            env=git_env,
        )
    if not 0 <= p.returncode <= 127:
        p.check_returncode()
    return (p.stdout.strip(), p.returncode)


@public
def git_ls_tree_mode(rev: str, path: str, cwd: str | None = None) -> str | None:
    """Return the file mode (e.g. "100644") of path in rev, or None if absent.

    The path is relative to the toplevel, so pass cwd=toplevel when running
    from a subdirectory (git ls-tree pathspecs are relative to the cwd).
    """
    cmdline = ["git", "ls-tree", rev, "--", path]
    if DEBUG_GIT_COMMANDS:
        print(*cmdline, flush=True)
    output = subprocess.check_output(cmdline, text=True, cwd=cwd, env=git_env)
    if not output:
        return None
    # Output format: "<mode> <type> <oid>\t<path>"
    mode = output.split(" ", 1)[0]
    assert mode in ("100644", "100755", "120000", "160000"), mode
    return mode


@public
def git_update_index_blob(
    path: str, blob_oid: str, mode: str, cwd: str | None = None
) -> None:
    """Stage a blob's content at path in the index, without touching the worktree."""
    cmdline = [
        "git",
        "update-index",
        "--add",
        "--cacheinfo",
        f"{mode},{blob_oid},{path}",
    ]
    if DEBUG_GIT_COMMANDS:
        print(*cmdline, flush=True)
    subprocess.check_call(cmdline, cwd=cwd, env=git_env)


@public
def git_rev_parse_show_toplevel() -> str:
    cmdline = ["git", "rev-parse", "--show-toplevel"]
    if DEBUG_GIT_COMMANDS:
        print("$", *cmdline, flush=True)
    return subprocess.check_output(cmdline, text=True, env=git_env).rstrip("\n")


@public
def git_write_tree() -> str:
    cmdline = ["git", "write-tree"]
    if DEBUG_GIT_COMMANDS:
        print(*cmdline, flush=True)
    return subprocess.check_output(cmdline, text=True, env=git_env).rstrip("\n")


@public
def git_files_with_unstaged_changes() -> list[str]:
    cmdline = ["git", "diff", "--name-only", "-z"]
    if DEBUG_GIT_COMMANDS:
        print("$", *cmdline, flush=True)
    diff_filenames = subprocess.check_output(cmdline, text=True, env=git_env).rstrip(
        "\0"
    )
    return diff_filenames.split("\0") if diff_filenames else []


@public
def git_files_with_staged_changes() -> list[str]:
    cmdline = ["git", "diff", "--name-only", "-z", "--cached"]
    if DEBUG_GIT_COMMANDS:
        print("$", *cmdline, flush=True)
    diff_filenames = subprocess.check_output(cmdline, text=True, env=git_env).rstrip(
        "\0"
    )
    return diff_filenames.split("\0") if diff_filenames else []


@public
def git_any_staged_changes() -> bool:
    cmdline = ["git", "diff", "--quiet", "--cached"]
    if DEBUG_GIT_COMMANDS:
        print(*cmdline, flush=True)
    proc = subprocess.run(cmdline, check=False, env=git_env)
    if proc.returncode == 1:
        return True
    proc.check_returncode()
    return False


@public
def git_is_same(refspec1: str, refspec2: str) -> bool:
    cmdline = ["git", "diff", "--quiet", "--end-of-options", refspec1, refspec2]
    if DEBUG_GIT_COMMANDS:
        print(*cmdline, flush=True)
    proc = subprocess.run(cmdline, check=False, env=git_env)
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
        subprocess.check_call(cmdline, env=git_env)
    else:
        git_set_head(head)
        git_set_staging(staging)


@public
def git_set_head(head: str) -> None:
    cmdline = ["git", "reset", "-q", "--soft", head, "--"]
    if DEBUG_GIT_COMMANDS:
        print(*cmdline, flush=True)
    subprocess.check_call(cmdline, env=git_env)


@public
def git_set_staging(
    staging: str,
    cwd: str | None = None,
    *,
    file_list: list[str] | tuple[str, ...] | None = None,
) -> None:
    "Note, must be called from the toplevel of the repository."
    cmdline = ["git", "reset", "-q", staging, "--", *(file_list or ["."])]
    if DEBUG_GIT_COMMANDS:
        print(*cmdline, flush=True)
    subprocess.check_call(cmdline, cwd=cwd, env=git_env)


@public
def git_apply_cached_from_git_show(
    refspec: str,
    cwd: str | None = None,
    *,
    file_list: list[str] | tuple[str, ...] | None = None,
) -> int:
    cmdline1 = ["git", "show", "--end-of-options", refspec, "--", *(file_list or [])]
    if DEBUG_GIT_COMMANDS:
        print(*cmdline1, flush=True)
    with subprocess.Popen(cmdline1, stdout=subprocess.PIPE, cwd=cwd, env=git_env) as p:
        cmdline2 = ["git", "apply", "--allow-empty", "--cached", "-"]
        if DEBUG_GIT_COMMANDS:
            print(*cmdline2, flush=True)
        proc = subprocess.run(
            cmdline2, stdin=p.stdout, check=False, cwd=cwd, env=git_env
        )
        p.wait()
        return proc.returncode


@public
def git_apply_cached_unidiff_zero_from_str(
    patch: str,
    cwd: str | None = None,
) -> None:
    cmdline = ["git", "apply", "--unidiff-zero", "--allow-empty", "--cached", "-"]
    if DEBUG_GIT_COMMANDS:
        print(*cmdline, flush=True)
    try:
        subprocess.run(
            cmdline, input=patch, check=True, cwd=cwd, text=True, env=git_env
        )
    except subprocess.CalledProcessError:
        print(patch)
        raise


@public
def git_apply_cached_recount(patch: str, cwd: str | None = None) -> None:
    cmdline = ["git", "apply", "--allow-empty", "--cached", "--recount", "-"]
    if DEBUG_GIT_COMMANDS:
        print(*cmdline, flush=True)
    subprocess.run(cmdline, text=True, input=patch, check=True, cwd=cwd, env=git_env)


@public
def git_commit_with_same_authorship(
    commit_hash: str, file_list: list[str] | tuple[str, ...] | None = None
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
    subprocess.check_call(cmdline, env=git_env)


@public
def git_amend(file_list: list[str] | tuple[str, ...] | None) -> None:
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
    subprocess.run(cmdline, text=True, check=True, env=git_env)


@public
def git_amend_with_commit_msg(
    commit_msg: str, file_list: list[str] | None = None
) -> None:
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
    subprocess.run(cmdline, text=True, input=commit_msg, check=True, env=git_env)


@public
def git_commit_tree(tree: str, message: str, parents: list[str]) -> str:
    cmdline = ["git", "commit-tree", tree]
    for p in parents:
        cmdline += ["-p", p]
    if DEBUG_GIT_COMMANDS:
        print(*cmdline, flush=True)
    return subprocess.run(
        cmdline, input=message, text=True, stdout=subprocess.PIPE, env=git_env
    ).stdout.strip()


@public
def git_reset_patch_first_hunk(
    refspec: str, file_list: list[str] | tuple[str, ...] | None = None
) -> None:
    """Stage the first hunk of refspec's changes by driving interactive
    "git reset -p" with the canned answers split/yes/quit."""
    cmdline = ["git", "reset", "-p", refspec, "--", *(file_list or ())]
    stdin = "s\ny\nq\n"
    if DEBUG_GIT_COMMANDS:
        print(f"printf '%s' {repr(stdin)} |", *cmdline, flush=True)
    subprocess.run(
        cmdline,
        input=stdin,
        text=True,
        stdout=subprocess.DEVNULL,
        check=True,
        env=git_env,
    )


@public
def git_show_commit_subject(refspec: str) -> str:
    cmdline = ["git", "show", "-s", "--pretty=%s", refspec]
    if DEBUG_GIT_COMMANDS:
        print("$", *cmdline, flush=True)
    output = subprocess.check_output(cmdline, text=True, env=git_env).strip()
    return output.strip()


@public
def git_show_commit_message(refspec: str) -> str:
    return git_show_commit_author_timestamp_and_message(refspec)[1]


@public
def git_show_commit_author_timestamp_and_message(refspec: str) -> tuple[str, str]:
    # Get the commit's author timestamp (%aI) and full commit message (%B).
    cmdline = ["git", "show", "-s", "--pretty=%aI %B", refspec]
    if DEBUG_GIT_COMMANDS:
        print("$", *cmdline, flush=True)
    output = subprocess.check_output(cmdline, text=True, env=git_env).strip()
    time_and_msg = output.split(None, 1)
    if len(time_and_msg) == 1:
        return output.strip(), ""
    time, msg = time_and_msg
    return time, msg
