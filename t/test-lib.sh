# Test framework for hammertime.
#
# Derived from the test framework for git, which comes with the following license:
#
# "Copyright (c) 2005 Junio C Hamano
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see https://www.gnu.org/licenses/ ."

if test -z "$TEST_DIRECTORY"
then
	# ensure that TEST_DIRECTORY is an absolute path so that it
	# is valid even if the current working directory is changed
	TEST_DIRECTORY=$(pwd)
else
	# The TEST_DIRECTORY will always be the path to the "t" directory.
	TEST_DIRECTORY=$(cd "$TEST_DIRECTORY" && pwd) || exit 1
fi
HAMMERTIME_DIR="${HAMMERTIME_DIR:-${TEST_DIRECTORY%/t}}"
if test "$TEST_DIRECTORY" = "$HAMMERTIME_DIR"
then
	echo "PANIC: Running in a $TEST_DIRECTORY that doesn't end in '/t'?" >&2
	exit 1
fi
export GIT_TEST_DEFAULT_INITIAL_BRANCH_NAME=main

TEST_NAME="$(basename "$0" .sh)"
TEST_NUMBER="${TEST_NAME%%-*}"
TEST_NUMBER="${TEST_NUMBER#t}"
TRASH_DIRECTORY="trash directory.$TEST_NAME"
case "$TRASH_DIRECTORY" in
/*) ;; # absolute path is good
 *) TRASH_DIRECTORY="$TEST_DIRECTORY/$TRASH_DIRECTORY" ;;
esac

# Since bash 5.0, checkwinsize is enabled by default which does
# update the COLUMNS variable every time a non-builtin command
# completes, even for non-interactive shells.
# Disable that since we are aiming for repeatability.
test -n "$BASH_VERSION" && shopt -u checkwinsize 2>/dev/null

# For repeatability, reset the environment to known value.
# TERM is sanitized below, after saving color control sequences.
LANG=C
LC_ALL=C
PAGER=cat
TZ=UTC
COLUMNS=80
export LANG LC_ALL PAGER TZ COLUMNS
EDITOR=:

unset VISUAL EMAIL LANGUAGE
unset XDG_CACHE_HOME
unset XDG_CONFIG_HOME
unset GITPERLLIB
unset GIT_TRACE2_PARENT_NAME
unset GIT_TRACE2_PARENT_SID
TEST_AUTHOR_LOCALNAME=author
TEST_AUTHOR_DOMAIN=example.com
GIT_AUTHOR_EMAIL=${TEST_AUTHOR_LOCALNAME}@${TEST_AUTHOR_DOMAIN}
GIT_AUTHOR_NAME='A U Thor'
GIT_AUTHOR_DATE='1112354055 +0200'
TEST_COMMITTER_LOCALNAME=committer
TEST_COMMITTER_DOMAIN=example.com
GIT_COMMITTER_EMAIL=${TEST_COMMITTER_LOCALNAME}@${TEST_COMMITTER_DOMAIN}
GIT_COMMITTER_NAME='C O Mitter'
GIT_COMMITTER_DATE='1112354055 +0200'
GIT_MERGE_VERBOSITY=5
GIT_MERGE_AUTOEDIT=no
export GIT_MERGE_VERBOSITY GIT_MERGE_AUTOEDIT
export GIT_AUTHOR_EMAIL GIT_AUTHOR_NAME
export GIT_COMMITTER_EMAIL GIT_COMMITTER_NAME
export GIT_COMMITTER_DATE GIT_AUTHOR_DATE
export EDITOR

# Protect ourselves from common misconfiguration to export
# CDPATH into the environment
unset CDPATH

unset GREP_OPTIONS
unset UNZIP

# Line feed
LF='
'

# Single quote
SQ=\'

error () {
	echo "error: $*"
	exit 1
}

rm -rf "$TRASH_DIRECTORY" || error "couldn't remove old trash directory"

git init --quiet "$TRASH_DIRECTORY" || error "cannot run git init"

cd -P "$TRASH_DIRECTORY" || error "cannot cd -P to \"$TRASH_DIRECTORY\""
TRASH_DIRECTORY=$(pwd)
HOME="$TRASH_DIRECTORY"

test_done () {
	cd "$TRASH_DIRECTORY/.." &&
		rm -rf "$TRASH_DIRECTORY"
}
