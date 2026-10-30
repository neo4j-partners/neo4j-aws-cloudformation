#!/bin/bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "${REPO_ROOT}"

echo "== Template generation =="
python3 neo4j-ee/templates/build.py --verify

echo
echo "== Python compile checks =="
python3 -m py_compile \
  neo4j-ee/deploy.py \
  neo4j-ee/browse.py \
  neo4j-ee/sample-private-app/deploy-sample-private-app.py \
  neo4j-ee/validate-private/scripts/*.py \
  neo4j-ee/src/neo4j_ee/*.py \
  test_neo4j/src/test_neo4j/*.py \
  neo4j-ee/templates/build.py
python3 -m compileall -q \
  neo4j-ee/src \
  neo4j-ee/validate-private/src \
  test_neo4j/src

echo
echo "== Unit tests =="
python3 -m unittest discover -s neo4j-ee/tests

echo
echo "== Shell syntax checks =="
# bash -n checks only its first file argument, so check each file separately.
for script in \
  neo4j-ee/templates/src/userdata.sh \
  neo4j-ee/templates/src/bootstrap/neo4j-bootstrap.sh \
  neo4j-ee/templates/src/partials/*.sh \
  neo4j-ee/*.sh \
  neo4j-ee/scripts/*.sh \
  neo4j-ee/marketplace/*.sh \
  neo4j-ee/sample-private-app/*.sh; do
  bash -n "${script}"
done

echo
echo "== Diff whitespace check =="
git diff --check

echo
echo "Local checks passed."
