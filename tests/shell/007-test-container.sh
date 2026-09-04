#!/bin/bash

set -e
set -x

# Setup: create a temp directory for test files
tmpdir=$(mktemp -d)
trap "rm -rf $tmpdir" EXIT

mkdir $tmpdir/test-module
cd $tmpdir/test-module

cfengine init --policy-module --with-input --non-interactive
cfengine test


