#!/bin/bash

set -e
set -x

tmpdir=$(mktemp -d)
output_file=$(mktemp)
trap "rm -rf $tmpdir $output_file" EXIT

# A plain cfbs policy-set project built on the standard community
# masterfiles -- no assert-specific setup needed, `cfengine test` auto-injects
# the assert: promise type into every test run on its own. It has a
# locally-authored module (discovered via */main.cf, same as a real cfbs
# project's own custom modules), and a single test showing the setup(act)/
# assert flow: act by calling into the project's own bundle, then assert on
# the state it left behind. The module uses the masterfiles standard library
# (m()), which must be available when running.
cp -r tests/shell/fixtures/011-test-suite-cfbs-project/. "$tmpdir/"
cd "$tmpdir"

if cfengine test >"$output_file" 2>&1; then
	cat "$output_file"
	echo "FAIL: expected the cfbs-project suite to fail (it has a deliberately-failing assertion)"
	exit 1
fi
cat "$output_file"

grep -qF "cfengine test: 1 passed, 1 failed, 0 errors" "$output_file"
grep -qF "tests/test_app_config.cf::test_app_config" "$output_file"
grep -qF "FAIL   tests/test_app_config.cf::test_app_config::app config file is world-writable (should fail)  ->  /tmp/cfengine_test_suite_app.conf has permissions 644, expected 777" "$output_file"

# --- --test-only: same result, with the standard library loaded by the
# generated policy instead of the project's promises.cf ---
if cfengine test --test-only >"$output_file" 2>&1; then
	cat "$output_file"
	echo "FAIL: expected the cfbs-project suite to fail with --test-only too"
	exit 1
fi
cat "$output_file"
grep -qF "cfengine test: 1 passed, 1 failed, 0 errors" "$output_file"
