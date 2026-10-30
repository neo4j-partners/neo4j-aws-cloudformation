install_apoc() {
  local neo4j_home="${NEO4J_HOME:-/var/lib/neo4j}"
  local jar
  jar=$(ls "${neo4j_home}"/labs/apoc-*-core.jar 2>/dev/null | head -1 || true)
  if [ -z "$jar" ]; then
    fail "APOC core JAR not found in ${neo4j_home}/labs after installing neo4j-enterprise."
  fi
  echo "Installing APOC plugin..."
  cp "$jar" "${neo4j_home}/plugins/"
  chown neo4j:neo4j "${neo4j_home}/plugins/$(basename "$jar")"
}
install_plugin() {
  local neo4j_home="${NEO4J_HOME:-/var/lib/neo4j}"
  local label="$1" pattern="$2"
  local jar
  jar=$(ls "${neo4j_home}"/products/${pattern} 2>/dev/null | head -1 || true)
  if [ -z "$jar" ]; then
    fail "${label} JAR not found in ${neo4j_home}/products/; rebuild AMI or disable Install${label}."
  fi
  echo "Installing ${label} plugin from $jar..."
  cp "$jar" "${neo4j_home}/plugins/"
  chown neo4j:neo4j "${neo4j_home}/plugins/$(basename "$jar")"
}
