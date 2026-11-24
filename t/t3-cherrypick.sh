#!/bin/sh

test_description='cherry-pick one commit on top of another with hunk splitting'

TEST_DIRECTORY=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
. "$TEST_DIRECTORY/test-lib.sh"
set -e
# Regression tests for cherrypick_prepare()/cherrypick_apply(): cherry-pick A
# on top of B where the two lineages edit the same file, so some hunks of A
# apply cleanly on top of B and others conflict.
seq 1 20 > f
git add f
git commit -qm root
root=$(git rev-parse HEAD)

# B branch: edits line 2.
git checkout -qb bbranch "$root"
sed -i 's/^2$/2B/' f
git commit -qam B
# A lineage: an unrelated edit to line 6 and an inserted line (the "unrelated"
# diff B..A^), then commit A edits line 2 (conflicts with B), line 15
# (applies cleanly) and adds file g (applies cleanly).
git checkout -qb ahead "$root"
sed -i -e '1a\
1U' -e 's/^6$/6U/' f
git commit -qam U
sed -i -e 's/^2$/2A/' -e 's/^15$/15A/' f
echo newg > g
git add g
git commit -qam A
# The output is a single "git cherry-pick" command line naming the clean
# commit (the part of A that applies on top of B) and the conflicts commit
# (the leftover hunks of A, on top of A^).
out=$($HAMMERTIME_DIR/htime.py cherrypick @ --onto bbranch)
printf '%s\n' "$out" | grep -q '^git cherry-pick [0-9a-f]\{40\} [0-9a-f]\{40\}$'
pickclean=$(printf '%s\n' "$out" | awk '{print $3}')
pickconflict=$(printf '%s\n' "$out" | awk '{print $4}')
# The clean commit must contain B's edit and A's clean hunks, but neither
# A's conflicting edit nor any of the unrelated B..A^ changes.
git cat-file -p "$pickclean:f" > cleanfile
grep -q '^2B$' cleanfile
grep -q '^15A$' cleanfile
! grep -q '^2A$' cleanfile
! grep -q '^1U$' cleanfile
! grep -q '^6U$' cleanfile
test newg = "$(git cat-file -p "$pickclean:g")"
# The conflicts commit holds A's conflicting hunk applied on top of A^
# (so it still contains the unrelated B..A^ changes, but not the hunks
# that were moved into the clean commit).
git cat-file -p "$pickconflict:f" > confile
grep -q '^2A$' confile
grep -q '^1U$' confile
grep -q '^6U$' confile
! grep -q '^15A$' confile

# The same from a subdirectory (patches must apply repo-root-relatively).
mkdir sub
out=$(cd sub && $HAMMERTIME_DIR/htime.py cherrypick @ --onto bbranch)
pickclean=$(printf '%s\n' "$out" | awk '{print $3}')
git cat-file -p "$pickclean:f" | grep -q '^15A$'
rmdir sub

# A commit whose hunks all apply cleanly gets no conflicts commit.
git checkout -q ahead
sed -i 's/^19$/19C/' f
git commit -qam C
out=$($HAMMERTIME_DIR/htime.py cherrypick @ --onto bbranch)
printf '%s\n' "$out" | grep -q '^git cherry-pick [0-9a-f]\{40\}$'
pickclean=$(printf '%s\n' "$out" | awk '{print $3}')
git cat-file -p "$pickclean:f" | grep -q '^19C$'
git cat-file -p "$pickclean:f" | grep -q '^2B$'

# A deletes a file that B modified: nothing applies cleanly.
git checkout -q ahead
git rm -q f
git commit -qm Deletion
out=$($HAMMERTIME_DIR/htime.py cherrypick @ --onto bbranch)
printf '%s\n' "$out" | grep -q '^No hunks could be applied cleanly$'
! printf '%s\n' "$out" | grep -q 'git cherry-pick'
# B also deletes the file: the deletion is already in B, nothing to do.
git checkout -qb bbranch2 bbranch
git rm -q f
git commit -qm Bdeletes
out=$($HAMMERTIME_DIR/htime.py cherrypick ahead --onto bbranch2)
printf '%s\n' "$out" | grep -q '^Nothing to apply$'
# An empty commit has nothing to cherry-pick either.
git commit -q --allow-empty -m Empty
out=$($HAMMERTIME_DIR/htime.py cherrypick @ --onto @^)
printf '%s\n' "$out" | grep -q '^Nothing to apply$'

test_done
