#!/bin/sh

test_description='test no newline at end of file'

. ./test-lib.sh
set -e
printf 'line1\nline2\nline3\n' > file
git add file
git commit -m first --quiet
printf 'line1\nNEW\nline2\nline3\n' > file
git commit -am new --quiet
A=`git rev-parse @`
printf 'line1\nNEWEDIT\nline2\nline3\nNEWEOF' > file
git commit -am eof --quiet
B=`git rev-parse @`
$HAMMERTIME_DIR/htime.py split --first $A --second $B > /dev/null
test_done
