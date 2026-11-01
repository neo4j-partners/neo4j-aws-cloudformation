# Runs a Cypher script from stdin against the system database as the neo4j
# admin user. The admin password travels in the NEO4J_PASSWORD environment
# variable and the script on stdin, so no secret reaches argv.
_system_cypher() {
  local address="$1"
  local adminPassword="$2"
  NEO4J_USERNAME=neo4j NEO4J_PASSWORD="${adminPassword}" \
    cypher-shell -a "${address}" -d system --format plain
}
create_app_user() {
  local appUsername="$1"
  local appPassword="$2"
  local adminPassword="$3"
  local advertisedDNS="$4"
  local timeoutSeconds="${5:-600}"
  local retrySeconds="${6:-10}"
  local address="bolt://localhost:7687"
  local deadline count

  [[ "${appUsername}" =~ ^[A-Za-z][A-Za-z0-9_]*$ ]] \
    || fail "Application username is not a plain identifier."
  # The secret is generated without punctuation. Checking it here keeps the
  # quoted :param literal below safe from Cypher injection.
  [[ "${appPassword}" =~ ^[A-Za-z0-9]+$ ]] \
    || fail "Application password secret must be alphanumeric."
  command -v cypher-shell >/dev/null 2>&1 || fail "cypher-shell not found."
  # Bolt requires TLS whenever AdvertisedDNS is set. The node certificate
  # names AdvertisedDNS rather than localhost, so this loopback connection
  # skips name verification.
  [[ -z "${advertisedDNS}" ]] || address="bolt+ssc://localhost:7687"

  # Every node runs this on its first boot. Only the system database leader
  # accepts the write, and a direct bolt connection gets no server-side
  # routing. The other nodes retry until the user replicates to them.
  echo "Ensuring application user ${appUsername} exists..."
  deadline=$(( $(date +%s) + timeoutSeconds ))
  while true; do
    if printf '%s\n' \
      ":param {pw: '${appPassword}'}" \
      "CREATE USER ${appUsername} IF NOT EXISTS SET PASSWORD \$pw CHANGE NOT REQUIRED;" \
      "GRANT ROLE architect TO ${appUsername};" \
      | _system_cypher "${address}" "${adminPassword}" >/dev/null; then
      echo "Application user ${appUsername} is ready."
      return 0
    fi
    count=$(printf '%s\n' \
      "SHOW USERS YIELD user, roles WHERE user = '${appUsername}' AND 'architect' IN roles RETURN count(*) AS n;" \
      | _system_cypher "${address}" "${adminPassword}" 2>/dev/null | tail -n 1 || true)
    if [[ "${count}" == "1" ]]; then
      echo "Application user ${appUsername} replicated from the leader."
      return 0
    fi
    (( $(date +%s) < deadline )) \
      || fail "Timed out after ${timeoutSeconds}s creating application user ${appUsername}."
    sleep "${retrySeconds}"
  done
}
