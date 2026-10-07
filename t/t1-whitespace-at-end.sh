#!/bin/sh

test_description='test no newline at end of file'

TEST_DIRECTORY=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
. "$TEST_DIRECTORY/test-lib.sh"
set -e
printf 'line1\nline2\nline3\n' > file
git add file
git commit -m first --quiet
printf 'line1\nNEW\nline2\nline3\n' > file
git commit -am new --quiet
printf 'line1\nNEWEDIT\nline2\nline3\nNEWEOF' > file
git commit -am eof --quiet
$HAMMERTIME_DIR/htime.py split --first @^ --second @ > /dev/null
printf 'line1\nNEWEDIT\nline2\nline3\nNEWEOF\n' > file
git commit -am noeol-to-eol --quiet
$HAMMERTIME_DIR/htime.py split --first @^ --second @ > /dev/null
printf 'line1\nNEWEDIT\nline2\nline3\nNEWEOFEDIT\n' > file
git commit -am eoledit --quiet
$HAMMERTIME_DIR/htime.py split --first @^ --second @ > /dev/null
printf 'line1\nNEWEDIT\nline2\nline3\nNEWEOFEDIT' > file
git commit -am eolremove --quiet
$HAMMERTIME_DIR/htime.py split --first @^ --second @ > /dev/null
printf 'line1\nline2\nline3' > file
git commit -am reset --quiet
printf 'line1EDIT\nline2\nline3' > file
git commit -am edit1 --quiet
printf 'line1EDIT\nline2EDIT\nline3' > file
git commit -am edit2 --quiet
$HAMMERTIME_DIR/htime.py split --first @^ --second @ > /dev/null

test_done
