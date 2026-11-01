install_neo4j_from_yum() {
  echo "Installing Graph Database..."
  export NEO4J_ACCEPT_LICENSE_AGREEMENT=yes
  yum -y install neo4j-enterprise
  systemctl enable neo4j
}
set_initial_password() {
  local initialPassword="$1"
  local argFile
  # neo4j-admin expands an @file argument, so the password is read from a
  # file and never appears in the process list. printf is a shell builtin,
  # so writing the file starts no process either. neo4j-admin parses the
  # file as shell-like tokens. The alphanumeric Password pattern is what
  # keeps the file safe to parse.
  argFile=$(umask 077 && mktemp)
  printf '%s\n' "${initialPassword}" > "${argFile}"
  # Admin commands run as the service user, so the auth file they write
  # has the right owner. The file is readable only by that user.
  if ! chown neo4j:neo4j "${argFile}" || ! chmod 0400 "${argFile}"; then
    rm -f "${argFile}"
    fail "Could not hand the initial password file to the neo4j user."
  fi
  if ! runuser -u neo4j -- neo4j-admin dbms set-initial-password "@${argFile}"; then
    rm -f "${argFile}"
    fail "neo4j-admin dbms set-initial-password failed."
  fi
  rm -f "${argFile}"
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
