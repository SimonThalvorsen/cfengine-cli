#!/bin/bash

set -e
set -x

# Setup: create a temp directory for test files
tmpdir=$(mktemp -d)
trap "rm -rf $tmpdir" EXIT

cfengine test --help

# A file with a lint error should make `cfengine test` fail on the lint step
# and not go through to build/deploy/run.
printf 'bundle agent main\n{\n  reports:\n      "missing semicolon"\n}\n' > "$tmpdir/bad.cf"

output_file=$(mktemp)
trap "rm -rf $tmpdir $output_file" EXIT

if cfengine test "$tmpdir/bad.cf" > "$output_file" 2>&1; then
	cat "$output_file"
	echo "FAIL: expected cfengine test to fail on a lint error"
	exit 1
fi
cat "$output_file"
grep -q "Lint failed" "$output_file"
grep -q "Skipping build/deploy/run" "$output_file"
