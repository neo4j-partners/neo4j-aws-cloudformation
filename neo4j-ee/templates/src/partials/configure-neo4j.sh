apply_base_conf() {
  local base="${NEO4J_BASE_CONF:-/opt/neo4j/conf/neo4j-base.conf}"
  echo "Applying static base configuration..."
  # `|| [[ -n "${line}" ]]` processes a final line with no trailing newline.
  while IFS= read -r line || [[ -n "${line}" ]]; do
    [[ -z "${line}" || "${line}" == \#* ]] && continue
    if [[ "${line}" != *=* || "${line}" == =* ]]; then
      fail "Malformed line in neo4j-base.conf: '${line}'"
    fi
    set_neo4j_conf "${line%%=*}" "${line#*=}"
  done < "${base}"
}

assert_security_invariant() {
  local conf="${NEO4J_CONF:-/etc/neo4j/neo4j.conf}"
  local value
  value=$(awk -F= '/^internal\.dbms\.cypher_ip_blocklist=/ {sub(/^[^=]*=/, ""); print; exit}' "${conf}")
  if [[ -z "${value}" ]]; then
    fail "Security invariant missing: internal.dbms.cypher_ip_blocklist is absent or empty in ${conf}."
  fi
}

configure_memory_recommendation() {
  local recommendation line
  local applied=false
  recommendation=$(neo4j-admin server memory-recommendation)
  while IFS= read -r line || [[ -n "${line}" ]]; do
    [[ -z "${line}" || "${line}" == \#* ]] && continue
    [[ "${line}" == *=* && "${line}" != =* ]] || continue
    set_neo4j_conf "${line%%=*}" "${line#*=}"
    applied=true
  done <<< "${recommendation}"
  [[ "${applied}" == "true" ]] || fail "Neo4j memory recommendation returned no configuration keys."
}

configure_cluster() {
  local nodeCount="$1"
  local region="$2"
  local _stack_id="$3"
  if [[ ${nodeCount} == 1 ]]; then
    echo "Running on a single node."
    return 0
  fi
  echo "Running on multiple nodes.  Configuring membership in neo4j.conf..."
  local -r privateIP="$(hostname -i | awk '{print $NF}')"
  set_neo4j_conf server.cluster.listen_address 0.0.0.0:6000
  set_neo4j_conf server.cluster.advertised_address "${privateIP}:6000"
  set_neo4j_conf server.cluster.raft.listen_address 0.0.0.0:7000
  set_neo4j_conf server.cluster.raft.advertised_address "${privateIP}:7000"
  set_neo4j_conf server.routing.listen_address 0.0.0.0:7688
  set_neo4j_conf server.routing.advertised_address "${privateIP}:7688"
  set_neo4j_conf initial.dbms.default_primaries_count 3
  set_neo4j_conf initial.dbms.default_secondaries_count "$(expr ${nodeCount} - 3)"
  set_neo4j_conf dbms.cluster.minimum_initial_system_primaries_count "${nodeCount}"
  coreMembers=""
  for attempt in $(seq 1 30); do
    asgNames=$(aws autoscaling describe-auto-scaling-groups --region "$region" --query "AutoScalingGroups[?Tags[?Key=='StackID' && Value=='${_stack_id}'] && Tags[?Key=='Role' && Value=='neo4j-cluster-node']].AutoScalingGroupName" --output text 2>/dev/null | tr '\t' '\n' | grep -v '^$' | grep -v '^None$' | xargs || true)
    if [[ -n "${asgNames}" ]]; then
      instanceIds=$(aws autoscaling describe-auto-scaling-groups --region "$region" --auto-scaling-group-names ${asgNames} --query "AutoScalingGroups[].Instances[].InstanceId" --output text 2>/dev/null | tr '\t' '\n' | grep -v '^$' | grep -v '^None$' | xargs || true)
      if [[ -n "${instanceIds}" ]]; then
        coreMembers=$(aws ec2 describe-instances --region "$region" --instance-ids ${instanceIds} --query "Reservations[].Instances[].PrivateIpAddress" --output text 2>/dev/null | tr '\t' '\n' | grep -v '^$' | grep -v '^None$' | awk '{print $1":6000"}' | paste -sd, - || true)
      fi
    fi
    foundCount=0
    [[ -n "${coreMembers}" ]] && foundCount=$(echo "${coreMembers}" | awk -F, '{print NF}')
    [[ ${foundCount} -ge ${nodeCount} ]] && break
    echo "Peer discovery attempt ${attempt}/30: found ${foundCount}/${nodeCount} members, retrying in 10s..."
    sleep 10
  done
  foundCount=0
  [[ -n "${coreMembers}" ]] && foundCount=$(echo "${coreMembers}" | awk -F, '{print NF}')
  if [[ -z "${coreMembers}" || ${foundCount} -lt ${nodeCount} ]]; then
    fail "Peer discovery failed after 5 minutes."
  fi
  echo "CoreMembers = ${coreMembers}"
  set_neo4j_conf dbms.cluster.discovery.resolver_type LIST
  set_neo4j_conf dbms.cluster.endpoints "${coreMembers}"
}

configure_plugin_settings() {
  local installBloom="$1"
  local bloomLicenseSecretArn="$2"
  local installGDS="$3"
  local gdsLicenseSecretArn="$4"
  echo "Configuring plugin settings in neo4j.conf..."
  if [[ "${installBloom}" == "true" ]]; then
    set_neo4j_conf server.unmanaged_extension_classes "com.neo4j.bloom.server=/bloom,semantics.extension=/rdf"
    if [[ -n "${bloomLicenseSecretArn}" ]]; then
      set_neo4j_conf dbms.bloom.license_file /var/lib/neo4j/licenses/neo4j-bloom.license
    fi
  fi
  if [[ "${installGDS}" == "true" && -n "${gdsLicenseSecretArn}" ]]; then
    set_neo4j_conf gds.enterprise.license_file /var/lib/neo4j/licenses/neo4j-gds.license
  fi
}

remove_jdwp_default() {
  local conf="${NEO4J_CONF:-/etc/neo4j/neo4j.conf}"
  sed -i.bak '/jdwp/d' "${conf}"
  rm -f "${conf}.bak"
}
