#!/bin/bash

set -e
set -x

tmpdir=$(mktemp -d)
output_file=$(mktemp)
trap "rm -rf $tmpdir $output_file" EXIT

cp -r tests/shell/fixtures/012-test-full-policy/. "$tmpdir/"
cd "$tmpdir"

# The project's real policy (services/init.cf, wired into the default
# bundlesequence via def.json's control_common_bundlesequence_end) writes a
# file whose contents depend on a variable with no project-level default --
# only a per-test fixture supplies it. Without --full-policy, that bundle
# only ever runs once, during the shared setup/bootstrap step, with no
# fixture in scope, so the variable is never resolved. With --full-policy,
# the real policy re-converges fresh inside each test's own container, using
# that test's own fixture.

# --- Without --full-policy: the real bundle only ran once, with no
# per-test fixture in scope, so neither test's expected content matches. ---
if cfengine test >"$output_file" 2>&1; then
	cat "$output_file"
	echo "FAIL: expected the suite to fail without --full-policy"
	exit 1
fi
cat "$output_file"
grep -qF "cfengine test: 0 passed, 2 failed, 0 errors" "$output_file"

# --- With --full-policy: the real bundle re-converges fresh per test file,
# using that test's own fixture, so both pass. ---
if ! cfengine test --full-policy >"$output_file" 2>&1; then
	cat "$output_file"
	echo "FAIL: expected the suite to pass with --full-policy"
	exit 1
fi
cat "$output_file"
grep -qF "cfengine test: 2 passed, 0 failed, 0 errors" "$output_file"
