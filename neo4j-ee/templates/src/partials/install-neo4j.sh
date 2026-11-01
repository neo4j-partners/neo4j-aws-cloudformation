install_neo4j_from_yum() {
  echo "Installing Graph Database..."
  export NEO4J_ACCEPT_LICENSE_AGREEMENT=yes
  yum -y install neo4j-enterprise
  systemctl enable neo4j
}
set_initial_password() {
  local initialPassword="$1"
  local dataDir="${NEO4J_DATA_DIR:-/var/lib/neo4j/data}"
  local argFile
  # neo4j-admin expands an @file argument, so the password is read from a
  # 0600 file and never appears in the process list. printf is a shell
  # builtin, so writing the file starts no process either.
  argFile=$(umask 077 && mktemp)
  printf '%s\n' "${initialPassword}" > "${argFile}"
  if ! neo4j-admin dbms set-initial-password "@${argFile}"; then
    rm -f "${argFile}"
    fail "neo4j-admin dbms set-initial-password failed."
  fi
  rm -f "${argFile}"
  # The command runs as root and writes the auth file the service user reads.
  chown -R neo4j:neo4j "${dataDir}/dbms"
}
start_neo4j() {
  local initialPassword="$1"
  local isFirstBoot="$2"
  # The initial password must be set before the first start creates the
  # system database.
  if [[ "${isFirstBoot}" == "true" ]]; then
    set_initial_password "${initialPassword}"
  fi
  echo "Starting Neo4j..."
  service neo4j start
}
