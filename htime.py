import json
import re
import subprocess
import sys
import textwrap
from dataclasses import dataclass
from typing import Literal, NotRequired, TypedDict

from cliparse import make_cliparser
from hammertime import (
    git_amend_with_commit_msg,
    git_any_staged_changes,
    git_apply_cached_from_git_show,
    git_apply_cached_recount,
    git_commit_with_same_authorship,
    git_files_with_staged_changes,
    git_is_same,
    git_merge_file,
    git_rev_parse,
    git_rev_parse_show_toplevel,
    git_set_head,
    git_set_head_and_staging,
    git_set_staging,
    git_show_commit_message,
    git_show_numstat,
)


subcommand, main = make_cliparser(__doc__, "htime", "htime_")


@subcommand
def htime_open(rebaseline: str) -> None:
    todo = parse_sequencer_line(rebaseline)
    assert todo is not None
    assert todo.oid
    subprocess.call(("git", "show", "--stat", "-U", todo.oid, "--"))


@subcommand
def htime_write(rebaseline: str) -> None:
    """
    Read hand-edited "git show" on stdin and create new commits to replace `rebaseline`.

    The `rebaseline` must be a git rebase todo line, e.g. "pick abcd123 Edit foo.c",
    and the output of this subcommand is a JSON document that can be passed
    to the "update" subcommand to update the full todo list.
    """
    todo = parse_sequencer_line(rebaseline)
    assert todo is not None
    assert todo.oid
    patchlines = sys.stdin.read()
    res = htime_write_inner(patchlines, len(todo.oid))
    print(json.dumps(res))


class TodoEdits(TypedDict):
    replace: NotRequired[str]
    justbelow: NotRequired[str]
    movedown: NotRequired[str]


def parse_git_patch(thepatch: str) -> tuple[str, str]:
    if "\r" in thepatch:
        assert thepatch.count("\r\n") == thepatch.count("\r")
        thepatch = thepatch.replace("\r", "")
    mo = re.match(r"^commit\s+([0-9a-fA-F]+)", thepatch)
    if mo is None:
        raise Exception("input does not look like a git patch")
    commit_hash = mo.group(1)
    headers, sep, mainmatter = thepatch.partition("\n\n")
    commit_message, sep, rest = mainmatter.partition("\ndiff --git ")
    commit_message, sep, rest = commit_message.partition("\n---\n")
    return commit_hash, textwrap.dedent(commit_message.strip("\n"))


def htime_write_inner(patchlines: str, oidlen: int) -> TodoEdits:
    if git_any_staged_changes():
        raise SystemExit("refuse to run when there are staged changes")
    commit_hash_patch, commit_msg = parse_git_patch(patchlines)
    commit_hash = git_rev_parse(commit_hash_patch)
    assert commit_hash
    head_sha = git_rev_parse("HEAD")
    assert head_sha
    if commit_hash is None:
        commit_hash = head_sha
    commit_msg_change = (
        commit_msg and git_show_commit_message(commit_hash) != commit_msg
    )
    patchsubject = commit_msg.splitlines()[0] if commit_msg else ""
    toplevel = git_rev_parse_show_toplevel()
    git_set_head_and_staging(f"{commit_hash}^", None)
    try:
        git_apply_cached_recount(patchlines, cwd=toplevel)
        # Edits applied to index. Diff with previous patch to see if anything changed.
        git_set_head(commit_hash)
        edited_files = git_files_with_staged_changes()
        if not commit_msg_change and not edited_files:
            print("No changes")
            return {}
        if commit_msg_change:
            subject = commit_msg.splitlines()[0]
        else:
            subject = (
                f'Changes to "{patchsubject}"'
                if patchsubject
                else f"Changes to {commit_hash[:oidlen]}"
            )
        revertsubject = (
            f'Revert changes to "{patchsubject}"'
            if patchsubject
            else f"Revert changes to {commit_hash[:oidlen]}"
        )
        # Edits applied to index, and HEAD is at head_sha.
        if not edited_files:
            # Amend existing commit with the updated commit message.
            git_set_head(f"{commit_hash}^")
            git_commit_with_same_authorship(commit_hash, None)
            git_amend_with_commit_msg(commit_msg, None)
            newhead = git_rev_parse("HEAD")
            assert newhead
            subject = commit_msg.splitlines()[0]
            res: TodoEdits = {"replace": f"pick {newhead} {subject}".rstrip()}
            print("No hand-edited files, just a commit message update")
            return res
        git_commit_with_same_authorship(commit_hash, None)
        if commit_msg_change:
            git_amend_with_commit_msg(commit_msg, None)
        hammer1 = git_rev_parse("HEAD")
        assert hammer1
        if commit_msg_change:
            res = {"justbelow": f"f -C {hammer1} {subject}".rstrip()}
        else:
            res = {"justbelow": f"f {hammer1} {subject}".rstrip()}
        if not edited_files:
            print("No hand-edited files, just a commit message update")
            return res
        git_set_staging(commit_hash)
        git_commit_with_same_authorship(commit_hash, None)
        git_amend_with_commit_msg(revertsubject, None)
        hammer2 = git_rev_parse("HEAD")
        assert hammer2
        res["movedown"] = f"pick {hammer2} {revertsubject}".rstrip()
        return res
    finally:
        git_set_head_and_staging(head_sha, None)


@subcommand
def htime_update(rebaseline: str, result: str) -> None:
    lines = sys.stdin.read().splitlines()
    lineno = lines.index(rebaseline)
    assert result.startswith("{"), repr(result)
    edits: TodoEdits = json.loads(result)
    assert isinstance(edits, dict)
    targetline = parse_sequencer_line(rebaseline)
    assert targetline is not None
    assert targetline.oid
    if "replace" in edits:
        repl = parse_sequencer_line(edits["replace"], targetline)
        assert repl is not None
        assert repl.oid
        lines[lineno : lineno + 1] = [str(repl)]
    ins = lineno + 1
    if "justbelow" in edits:
        jb = parse_sequencer_line(edits["justbelow"], targetline)
        assert jb is not None
        assert jb.oid
        lines[ins:ins] = [str(jb)]
        ins += 1
    if "movedown" in edits:
        moveline = parse_sequencer_line(edits["movedown"], targetline)
        assert moveline is not None
        assert moveline.oid
        targetnumstat = git_show_numstat(moveline.oid)
        movefiles = sorted(ns.path for ns in targetnumstat.numstat)
        while True:
            line = parse_sequencer_line(lines[ins]) if ins < len(lines) else None
            skip = 0
            while line is None and ins + skip + 1 < len(lines):
                skip += 1
                line = parse_sequencer_line(lines[ins + skip])
            if line is None:
                # Doesn't need to be applied
                lines[ins:ins] = [f"# {moveline}"]
                break
            ins += skip
            if not line.oid:
                # Emit the 'pick' line here
                lines[ins:ins] = [f"{moveline}"]
                break
            conflictfile = move_conflict("down", moveline.oid, line.oid, movefiles)
            if conflictfile is not None:
                # Emit the 'pick' line here
                lines[ins:ins] = [f"{moveline} # {conflictfile}"]
                break
            ins += 1
    print("\n".join(lines))


Squash = Literal["prev", "cur", "both"]


@dataclass(frozen=True)
class ParsedVerb:
    edit: bool
    squash: Squash | None

    def combine(self, edit: bool, squash: Squash) -> "tuple[ParsedVerb, Squash]":
        edit = self.edit or edit
        if self.squash is None:
            return ParsedVerb(edit, None), squash
        if squash == "cur":
            return ParsedVerb(edit, "cur" if squash == "cur" else self.squash), "cur"
        return ParsedVerb(edit, self.squash), squash

    def to_verb(self) -> str:
        match (self.edit, self.squash):
            case (False, None):
                return "pick "
            case (True, None):
                return "r "
            case (True, "cur"):
                return "f -c "
            case (False, "cur"):
                return "f -C "
            case (False, "prev"):
                return "f "
            case _:
                return "s "


def parse_verb(verb: str) -> ParsedVerb | None:
    if verb.startswith("p"):
        return ParsedVerb(False, None)
    if verb.startswith("r"):
        return ParsedVerb(True, None)
    if verb.startswith("s"):
        return ParsedVerb(True, "both")
    if verb.startswith("f"):
        if "-c" in verb:
            return ParsedVerb(True, "cur")
        if "-C" in verb:
            return ParsedVerb(False, "cur")
        return ParsedVerb(False, "prev")
    return None


@dataclass(frozen=True)
class SequencerLine:
    verb: str
    oid: str
    hash: str
    suffix: str

    def update(self, *, verb: str | None = None, oid: str | None = None, suffix: str | None = None) -> "SequencerLine":
        if verb is None:
            verb = self.verb
        else:
            assert verb.endswith(" ")
        if oid is None:
            oid = self.oid
        else:
            oid = oid[: len(self.oid)]
        if suffix is None:
            suffix = self.suffix
        else:
            assert suffix.endswith("\n") == self.suffix.endswith("\n")
        return SequencerLine(verb, oid, self.hash, suffix)

    def __str__(self) -> str:
        return f"{self.verb}{self.oid}{self.hash}{self.suffix}"


def parse_sequencer_line(
    line: str, like: SequencerLine | None = None
) -> SequencerLine | None:
    """
    >>> assert parse_sequencer_line('t asd') is not None
    """
    mo = re.fullmatch(
        r"^(?:(\s*(?:p|pick|r|reword|e|edit|s|squash|f|fixup)\s*(?:-[Cc]\s*)?)([0-9a-f]+)(\s+#?\s*)((?:.*)?)|(\s*[a-z][a-z-]*)(.*))\Z",
        line,
        re.S,
    )
    if mo is None:
        return None
    verb, oid, hash, suffix, otherverb, otherarg = mo.groups()
    if otherverb:
        if otherverb in ("d", "drop", "l", "label", "b", "break", "u", "update-ref"):
            # These are safe to move past - pretend they don't match
            return None
        # exec, reset, merge -> these are dangerous
        return SequencerLine(otherverb, "", "", otherarg)
    if like:
        oid = oid[: len(like.oid)]
    if like:
        hash = like.hash
    return SequencerLine(verb, oid, hash, suffix)


def move_conflict(
    up_or_down: Literal["down", "up"], moveoid: str, lineoid: str, movefiles: list[str]
) -> str | None:
    numstats = {ns.path for ns in git_show_numstat(lineoid).numstat}
    for path in movefiles:
        # Can moveoid's changes to `path` be moved past this line?
        if path not in numstats:
            # Trivially yes, since this line doesn't modify `path`
            continue
        if up_or_down == "up":
            errmsg = f"Cannot move {moveoid} up above {lineoid}"
            # Move B up above A: Try to apply B to A^.
            oid, conflicts = git_merge_file(
                current=f"{lineoid}^:{path}",
                base=f"{moveoid}^:{path}",
                other=f"{moveoid}:{path}",
            )
            if conflicts:
                return f"{errmsg}: Merge conflict on {path}"
            # Then apply A and check that we obtain AB.
            oid, conflicts = git_merge_file(
                current=oid,
                base=f"{lineoid}^:{path}",
                other=f"{lineoid}:{path}",
            )
            if conflicts:
                return f"{errmsg}: Commits cancel out on {path}"
            expected, expectedconflicts = git_merge_file(
                current=f"{lineoid}:{path}",
                base=f"{moveoid}^:{path}",
                other=f"{moveoid}:{path}",
            )
            if expectedconflicts:
                raise Exception(f"{errmsg}: Initial state has a conflict on {path}")
            if expected != oid:
                # This means the patches can be applied in either order,
                # but the result differs depending on the order.
                return f"{errmsg}: Commits are non-commutative on {path}"
        else:
            errmsg = f"Cannot move {moveoid} down below {lineoid}"
            # Move A down below B: Try to revert A on B.
            oid, conflicts = git_merge_file(
                current=f"{lineoid}:{path}",
                base=f"{moveoid}:{path}",
                other=f"{moveoid}^:{path}",
            )
            if conflicts:
                return f"{errmsg}: Merge conflict on {path}"
            # Then revert B and check that we obtain the base file.
            oid, conflicts = git_merge_file(
                current=oid,
                base=f"{lineoid}:{path}",
                other=f"{lineoid}^:{path}",
            )
            if conflicts:
                return f"{errmsg}: Commits cancel out on {path}"
            expected, expectedconflicts = git_merge_file(
                current=f"{lineoid}^:{path}",
                base=f"{moveoid}:{path}",
                other=f"{moveoid}^:{path}",
            )
            if expectedconflicts:
                raise Exception(f"{errmsg}: Initial state has a conflict on {path}")
            if expected != oid:
                # This means the patches can be applied in either order,
                # but the result differs depending on the order.
                return f"{errmsg}: Commits are non-commutative on {path}"
    return None


@subcommand
def htime_move(lineno: int, up_or_down: Literal["down", "up"]) -> None:
    lines = sys.stdin.read().splitlines()
    assert 1 <= lineno <= len(lines)
    targetline = parse_sequencer_line(lines[lineno - 1])
    if targetline is None or not targetline.oid:
        print(json.dumps({"message": "Please put the cursor on a 'pick' line"}))
        return
    moveoid = targetline.oid
    movefiles = [ns.path for ns in git_show_numstat(moveoid).numstat]
    dd = 1 if up_or_down == "down" else -1
    ix = lineno - 1 + dd
    extra = 0
    mv = 0
    conflictmessage: str | None = None
    while 0 <= ix < len(lines):
        line = parse_sequencer_line(lines[ix])
        if line is None:
            extra += 1
            ix += dd
            continue
        if not line.oid:
            # exec, reset, merge -> these are dangerous
            conflictmessage = f"Don't want to move past '{line.verb}' command"
            break
        conflictmessage = move_conflict(up_or_down, moveoid, line.oid, movefiles)
        if conflictmessage is not None:
            break
        mv += extra + 1
        extra = 0
        ix += dd
    if mv == 0:
        if conflictmessage:
            print(json.dumps({"message": conflictmessage}))
        else:
            print("{}")
    else:
        print(json.dumps({"movelines": mv}))


@subcommand
def htime_cleanup() -> None:
    """
    Update a git rebase todo list, remaking commit objects
    to turn trivial cherry-picks into actual fast-forwards.
    After swapping two commits early in a long todo list,
    remaking commit objects can be significantly faster than
    having git do the full cherry picks.
    """
    lines: list[SequencerLine] = []
    inputlines: list[str | int] = []
    for line in sys.stdin.read().splitlines(True):
        parsed = parse_sequencer_line(line)
        if parsed is None:
            inputlines.append(line)
        else:
            inputlines.append(len(lines))
            lines.append(parsed)
    if not lines:
        # No parse results
        raise Exception("Nothing to do")
    head = git_rev_parse(f"{lines[0].oid}^")
    assert head, lines[0]
    stat_ff = 0
    stat_recommit = 0
    needs_full_cherry_pick = 0
    for i, parsed in enumerate(lines):
        parent = git_rev_parse(f"{parsed.oid}^")
        if same_oid(parent, head):
            # Fast-forward
            stat_ff += 1
            head = parsed.oid
        elif git_is_same(parent, head):
            stat_recommit += 1
            if needs_full_cherry_pick:
                # No reason to make a new commit object,
                # as git is anyway going to do a full cherry-pick.
                head = parsed.oid
            else:
                git_set_head_and_staging(head, parsed.oid)
                git_commit_with_same_authorship(parsed.oid)
                head = git_rev_parse("@")
                lines[i] = parsed.update(oid=head)
        else:
            if not stat_recommit and not needs_full_cherry_pick:
                # We haven't done any quick remakes of commit objects.
                # Try to do the cherry-pick without a worktree.
                git_set_head_and_staging(head, head)
                err = git_apply_cached_from_git_show(parsed.oid)
                if err:
                    lines[i] = parse_sequencer_line(f"{str(lines[i]).rstrip()} # CONFLICT\n")
                    needs_full_cherry_pick += 1
                    head = parsed.oid
                    continue
                git_commit_with_same_authorship(parsed.oid)
                head = git_rev_parse("@")
                assert head
                lines[i] = parsed.update(oid=head)
                lines[i] = parse_sequencer_line(f"{str(lines[i]).rstrip()} # SUCCESS\n")
            else:
                needs_full_cherry_pick += 1
                head = parsed.oid
                continue
        # The current line's parent is the previous line.
        # Should we squash them together?
        b = parse_verb(parsed.verb)
        if b is not None and b.squash is not None and i > 0:
            a = parse_verb(lines[i - 1].verb)
            if a is not None:
                c, d = a.combine(b.edit, b.squash)
                newverb = c.to_verb()
                newsuf = lines[i - 1].suffix
                git_set_head_and_staging(f"{lines[i - 1].oid}^", parsed.oid)
                if d == "prev":
                    git_commit_with_same_authorship(lines[i - 1].oid)
                else:
                    git_commit_with_same_authorship(parsed.oid)
                    if d == "both":
                        commitmsg = "\n\n".join(
                            [
                                git_show_commit_message(lines[i - 1].oid),
                                git_show_commit_message(parsed.oid),
                                ]
                            )
                        git_amend_with_commit_msg(commitmsg, None)
                    else:
                        newsuf = parsed.suffix
                lines[i - 1] = SequencerLine("", "", "", "")
                head = git_rev_parse("@")
                assert head is not None
                lines[i] = parsed.update(verb=newverb, oid=head, suffix=newsuf)
    for x in inputlines:
        print(x if isinstance(x, str) else lines[x], end="")


def same_oid(a: str, b: str) -> bool:
    assert 4 <= len(a) <= 40
    assert 4 <= len(b) <= 40
    assert int(a, 16)
    assert int(b, 16)
    return a.startswith(b) if len(a) > len(b) else b.startswith(a)


if __name__ == "__main__":
    main()
