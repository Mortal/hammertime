import argparse
from hammertime import git_log_numstat, git_merge_file

parser = argparse.ArgumentParser()
parser.add_argument("base")
parser.add_argument("tip")


def main() -> None:
    args = parser.parse_args()
    revs = git_log_numstat(f"{args.base}..{args.tip}")
    by_path: dict[str, list[str]] = {}
    for rev_numstat in revs:
        for numstat in rev_numstat.numstat:
            by_path.setdefault(numstat.path, []).append(rev_numstat.commit)
    # Move last revision as far back as we can
    for numstat in revs[-1].numstat:
        for rev in by_path[numstat.path][::-1]:
            # Try to apply revs[-1].commit's changes to numstat.path
            # just before rev
            oid, conflicts = git_merge_file(
                current=f"{rev}^:{numstat.path}",
                base=f"{revs[-1].commit}^:{numstat.path}",
                other=f"{revs[-1].commit}:{numstat.path}",
            )
            if revs[-1].commit == rev:
                assert not conflicts
                continue
            print(f"{conflicts}\t{revs[-1].commit}\tbefore\t{rev}\t{numstat.path}")
    # Move first revision as far forward as we can
    for numstat in revs[0].numstat:
        for rev in by_path[numstat.path]:
            # Try to revert revs[0].commit's changes to numstat.path on top of rev
            oid, conflicts = git_merge_file(
                current=f"{rev}:{numstat.path}",
                base=f"{revs[0].commit}:{numstat.path}",
                other=f"{revs[0].commit}^:{numstat.path}",
            )
            if revs[0].commit == rev:
                assert not conflicts
                continue
            print(f"{conflicts}\t{revs[0].commit}\tafter\t{rev}\t{numstat.path}")
    # for path in by_path:
    #     for a, b in zip(by_path[path][:-1], by_path[path][1:], strict=True):
    #         oid, conflicts = git_merge_file(current=f"{a}^:{path}", base=f"{a}:{path}", other=f"{b}:{path}")
    #         if conflicts:
    #             print(f"{conflicts}\t{a}\t{b}\t{path}")


if __name__ == "__main__":
    main()
