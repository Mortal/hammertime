#!/bin/sh
# Run the unit tests in t/.

cd -- "$(dirname -- "$0")" || exit 1
status=0
for test in t/t[0-9]*.sh
do
	if $test
	then
		echo "ok   $test"
	else
		echo "FAIL $test"
		status=1
		# A test that fails before reaching test_done leaves its trash directory behind.
		rm -rf t/"trash directory."*
	fi
done
exit $status
