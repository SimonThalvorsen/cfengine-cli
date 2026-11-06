#!/usr/bin/env bash

set -e
# set -x

echo "These tests expect cfengine CLI to be installed globally or in venv"

echo "Looking for CFEngine CLI:"
cfengine --version

echo "Check that test files are in expected location:"
ls -al tests/lint/*.cf

rm -rf tmp
mkdir -p tmp

echo "Run lint tests:"
for file in tests/lint/*.cf; do
  if echo "$file" | grep -q '\.x\.cf$'; then
    # File ends with .x.cf, we expect it to:
    #  - Fail (non-zero exit code)
    #  - Output the correct error message

    expected="$(echo $file | sed 's/\.x\.cf$/.expected.txt/')"
    if [ ! -f "$expected" ]; then
      echo "FAIL: Missing expected output file: $expected"
      exit 1
    fi
    output="tests/lint/$(basename $file .x.cf).output.txt"
    if cfengine lint "$file" > "$output" 2>&1; then
      echo "FAIL: $file - expected lint failure but got success"
      exit 1
    fi
    diff -u "$expected" "$output"
    echo "OK (expected failure): $file"
  else
    # Expect success
    if ! cfengine lint "$file"; then
      echo "FAIL: $file - expected lint success but got failure"
      exit 1
    fi
    echo "OK: $file"
  fi
done

echo "All lint tests successful!"
