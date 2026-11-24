#!/bin/sh

test_description='split two commits when one of them adds or deletes the file'

TEST_DIRECTORY=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
. "$TEST_DIRECTORY/test-lib.sh"
set -e
# Regression test for add/delete conflicts in htime_split_inner():
# splitting a pair of commits where the file does not exist
# in one of the three trees (first^, first, second).
echo base > other
git add other
git commit -m root --quiet

# Case 1: "not first_parent_blob: Added, then edited".
printf 'line1\nline2\nline3\n' > f
git add f
git commit -m add --quiet
printf 'line1EDIT\nline2\nline3EDIT\n' > f
git commit -am edit --quiet
$HAMMERTIME_DIR/htime.py split --first @^ --second @ > /dev/null

# Case 2: "not first_blob: Deleted, then added".
git rm -q f
git commit -m delete --quiet
printf 'line1EDIT\nline2\nline3EDIT\nline4\n' > f
git add f
git commit -m readd --quiet
$HAMMERTIME_DIR/htime.py split --first @^ --second @ > /dev/null

# Case 3: "not second_blob: Edited, then deleted".
printf 'line1EDIT\nline2EDIT\nline3EDIT\nline4\n' > f
git commit -am edit --quiet
git rm -q f
git commit -m delete --quiet
$HAMMERTIME_DIR/htime.py split --first @^ --second @ > /dev/null

test_done
