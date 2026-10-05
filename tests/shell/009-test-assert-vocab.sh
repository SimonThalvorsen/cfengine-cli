#!/bin/bash

set -e
set -x

tmpdir=$(mktemp -d)
output_file=$(mktemp)
trap "rm -rf $tmpdir $output_file" EXIT

cp tests/shell/fixtures/009-test-assert-vocab/test_assert_vocab.cf "$tmpdir/test_assert_vocab.cf"
cd "$tmpdir"

if cfengine test test_assert_vocab.cf >"$output_file" 2>&1; then
	cat "$output_file"
	echo "FAIL: expected test_assert_vocab.cf to fail (it has deliberately-failing assertions)"
	exit 1
fi
cat "$output_file"

grep -qF "PASS: test_assert_vocab.cf" "$output_file"
grep -qF "Lint passed, no errors found." "$output_file"

expected_fails=(
	"FAIL   test_assert_vocab.cf::test_assert_vocab::int_equals_fail  ->  expected 6, got 5"
	"FAIL   test_assert_vocab.cf::test_assert_vocab::int_less_than_fail  ->  5 is not less than 1"
	"FAIL   test_assert_vocab.cf::test_assert_vocab::str_not_equals_fail  ->  expected not 'hello', but got it"
	"FAIL   test_assert_vocab.cf::test_assert_vocab::file_contents_fail  ->  /tmp/assert_vocab_fixture.txt contents did not match the expected contents"
	"FAIL   test_assert_vocab.cf::test_assert_vocab::dir_exists_fail  ->  expected exists=True, but exists=False for /this/path/should/not/exist/hopefully"
	"FAIL   test_assert_vocab.cf::test_assert_vocab::slist_contains_fail  ->  ['apple', 'banana', 'cherry'] does not contain 'durian'"
)
for line in "${expected_fails[@]}"; do
	grep -qF "$line" "$output_file" || {
		echo "FAIL: expected line not found in output: $line"
		exit 1
	}
done

# Exactly these six should fail
fail_count=$(grep -c "^  FAIL   test_assert_vocab.cf::" "$output_file")
if [ "$fail_count" -ne 6 ]; then
	echo "FAIL: expected exactly 6 failing assertions, got $fail_count"
	exit 1
fi

grep -qF "cfengine test: 11 passed, 6 failed, 0 errors" "$output_file"
