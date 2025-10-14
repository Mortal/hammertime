import argparse
import re
import sys
from hammertime import git_show_numstat, git_merge_file

parser = argparse.ArgumentParser()
parser.add_argument("up_or_down", choices=("up", "down"))
parser.add_argument("lineno", type=int)
parser.add_argument("path")


def parse_sequencer_lines(lines: list[str]) -> list[str | tuple[str, str, str]]:
    parsed: list[str | tuple[str, str, str]] = []
    for line in lines:
        mo = re.fullmatch(
            r"^(\s*(?:p|pick|r|reword|e|edit|s|squash|f|fixup)\s*(?:-[Cc]\s*)?)([0-9a-f]+)((?:\s+.*)?)$",
            line,
            re.S,
        )
        if mo is None:
            parsed.append(line)
            continue
        verb, oid, suffix = mo.groups()
        parsed.append((verb, oid, suffix))
    return parsed


def main() -> None:
    args = parser.parse_args()
    if args.path == "-":
        lines = list(sys.stdin)
    else:
        with open(args.path) as fp:
            lines = list(fp)
    assert 1 <= args.lineno <= len(lines)
    seqlines = parse_sequencer_lines(lines)
    suflines = len(seqlines)
    while suflines > 0 and isinstance(seqlines[suflines - 1], str):
        suflines -= 1
    targetlines = (
        seqlines[args.lineno - 1 : suflines]
        if args.up_or_down == "down"
        else seqlines[: args.lineno][::-1]
    )
    resultlines = (
        seqlines[: args.lineno - 1]
        if args.up_or_down == "down"
        else seqlines[args.lineno : suflines][::-1]
    )
    assert targetlines
    assert isinstance(targetlines[0], tuple)
    targetoids = [line[1] for line in targetlines if isinstance(line, tuple)]
    numstats = {
        oid: set(numstat.path for numstat in x.numstat)
        for oid, x in zip(targetoids, git_show_numstat(targetoids), strict=True)
    }
    files = sorted(numstats[targetoids[0]])
    for ix, line in enumerate(targetlines[1:], 1):
        if isinstance(line, str):
            resultlines.append(f"{line.strip()} SKIP {ix}\n")
            continue
        newfiles: list[str] = []
        thisfiles: list[str] = []
        for f in files:
            # Can targetoids[0]'s changes to f be moved past this line?
            if f not in numstats[line[1]]:
                # Trivially yes, since this line doesn't modify f
                conflicts = 0
            elif args.up_or_down == "up":
                oid, conflicts = git_merge_file(
                    current=f"{line[1]}^:{f}",
                    base=f"{targetoids[0]}^:{f}",
                    other=f"{targetoids[0]}:{f}",
                )
            else:
                oid, conflicts = git_merge_file(
                    current=f"{line[1]}:{f}",
                    base=f"{targetoids[0]}:{f}",
                    other=f"{targetoids[0]}^:{f}",
                )
            if conflicts:
                # No, it cannot
                thisfiles.append(f)
            else:
                newfiles.append(f)
        all_files_conflict = len(thisfiles) == len(numstats[targetoids[0]])
        all_files_tried = len(files) == len(numstats[targetoids[0]])
        if all_files_conflict or (all_files_tried and thisfiles and ix > 1):
            # Emit a 'pick' line and end here
            resultlines.append(targetlines[0])
            thisfiles = files
            newfiles = []
        elif thisfiles:
            resultlines.append(
                f"x git apply --index -p1 < <(git show {targetoids[0]} -- {' '.join(thisfiles)}) && git commit -nC {targetoids[0]}\n"
            )
        files = newfiles
        if not files:
            resultlines += targetlines[ix:]
            break
        resultlines.append(line)
    if len(files) == len(numstats[targetoids[0]]):
        # Emit a 'pick' line and end here
        resultlines.append(targetlines[0])
    elif files:
        resultlines.append(
            f"x git apply --index -p1 < <(git show {targetoids[0]} -- {' '.join(files)}) && git commit -nC {targetoids[0]}\n"
        )
    if args.up_or_down == "up":
        resultlines = resultlines[::-1]
    resultlines += seqlines[suflines:]
    result = "".join(
        "".join(line) if isinstance(line, tuple) else line for line in resultlines
    )
    if args.path == "-":
        print(result, end="", flush=True)
    else:
        with open(args.path, "w") as ofp:
            ofp.write(result)


if __name__ == "__main__":
    main()
