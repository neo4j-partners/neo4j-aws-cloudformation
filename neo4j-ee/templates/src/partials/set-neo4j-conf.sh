set_neo4j_conf() {
  local key="$1"
  local value="$2"
  local conf="${NEO4J_CONF:-/etc/neo4j/neo4j.conf}"
  local key_pattern replacement
  key_pattern=$(printf '%s' "${key}" | sed 's/[][\\.^$*]/\\&/g')
  replacement=$(printf '%s=%s' "${key}" "${value}" | sed 's/[\\&|]/\\&/g')
  sed -i.bak "s|^#\{0,1\}${key_pattern}=.*|${replacement}|" "$conf"
  rm -f "${conf}.bak"
  if ! awk -v key="${key}" 'index($0, key "=") == 1 { found = 1 } END { exit found ? 0 : 1 }' "$conf"; then
    echo "${key}=${value}" >> "$conf"
  fi
}
