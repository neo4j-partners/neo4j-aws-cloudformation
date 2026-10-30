configure_tls() {
  local advertisedDNS="$1"
  local loadBalancerDNSName="$2"
  local boltAdvertiseDns="${3:-false}"
  local certsBase="${NEO4J_CERTS_DIR:-/var/lib/neo4j/certificates}"

  # Opt-out path (Public without TLS only — Private/ExistingVpc always pass a
  # non-empty AdvertisedDNS). Browser stays HTTP/7474, Bolt stays plaintext,
  # both advertised via the NLB DNS so in-VPC clients can still connect.
  if [[ -z "${advertisedDNS}" ]]; then
    echo "TLS disabled (no AdvertisedDNS); leaving Browser and Bolt plaintext."
    set_neo4j_conf server.default_advertised_address "${loadBalancerDNSName}"
    set_neo4j_conf server.bolt.advertised_address "${loadBalancerDNSName}:7687"
    set_neo4j_conf server.http.enabled true
    set_neo4j_conf server.http.listen_address 0.0.0.0:7474
    set_neo4j_conf server.http.advertised_address "${loadBalancerDNSName}:7474"
    set_neo4j_conf server.https.enabled false
    return 0
  fi

  echo "Configuring TLS for Bolt and HTTPS..."
  # openssl is baked into the AMI by marketplace/create-ami.sh (Placement
  # Decision Rule branch 3). It is intentionally not dnf-installed here: a
  # package install on the boot path adds a mirror dependency that would turn
  # ASG self-heal into an availability failure with no template fix.
  command -v openssl >/dev/null 2>&1 || fail "openssl not found; it must be baked into the AMI."
  local _proto _dir _key _crt
  for _proto in bolt https; do
    _dir="${certsBase}/${_proto}"
    _key="${_dir}/private.key"
    _crt="${_dir}/public.crt"
    if [[ -f "${_key}" && -f "${_crt}" ]]; then
      echo "  Reusing existing self-signed cert in ${_dir}"
    else
      echo "  Generating self-signed cert in ${_dir}..."
      mkdir -p "${_dir}"
      umask 077
      # SAN/CN must be AdvertisedDNS: the NLB re-encrypts to this instance and
      # Neo4j's Jetty enforces sniHostCheck, so the served cert must match the
      # browser's Host (AdvertisedDNS) or HTTPS fails with 400 Invalid SNI.
      openssl req -x509 -newkey rsa:2048 -nodes \
        -keyout "${_key}" -out "${_crt}" -days 3650 \
        -subj "/CN=${advertisedDNS}" \
        -addext "subjectAltName=DNS:${advertisedDNS}" >/dev/null 2>&1
      umask 022
    fi
    chown -R neo4j:neo4j "${_dir}"
    chmod 600 "${_key}"
    chmod 644 "${_crt}"
  done

  # default_advertised_address MUST stay AdvertisedDNS: it is Jetty's no-SNI
  # fallback host, and the NLB HTTPS health check on 7473 connects without
  # sending SNI. If this is not the cert SAN (AdvertisedDNS), Jetty answers
  # the health check with 400 Invalid SNI and every 7473 target goes
  # unhealthy. Only Bolt is overridden below.
  set_neo4j_conf server.default_advertised_address "${advertisedDNS}"
  # Bolt is a routed protocol: the cluster publishes server.bolt.advertised_address
  # in its routing table and every neo4j://-style client must resolve it. The
  # template sets boltAdvertiseDns=true only when AdvertisedDNS has an in-VPC
  # record: on Private when the stack creates the zone, on ExistingVpc when
  # CreatePrivateDns=true. Bolt then advertises it, and a neo4j+s:// client that
  # sends no routing address still gets a name inside the cert SAN. Otherwise
  # AdvertisedDNS may be only a synthetic cert SAN with no in-VPC record, so
  # Bolt advertises the always-resolvable NLB DNS instead. Bolt has no
  # sniHostCheck, so a non-cert-SAN host is safe there for neo4j+ssc:// clients.
  if [[ "${boltAdvertiseDns}" == "true" ]]; then
    set_neo4j_conf server.bolt.advertised_address "${advertisedDNS}:7687"
  else
    set_neo4j_conf server.bolt.advertised_address "${loadBalancerDNSName}:7687"
  fi
  set_neo4j_conf server.http.enabled false
  set_neo4j_conf server.https.enabled true
  set_neo4j_conf server.https.listen_address 0.0.0.0:7473
  set_neo4j_conf server.https.advertised_address "${advertisedDNS}:7473"
  set_neo4j_conf dbms.ssl.policy.bolt.enabled true
  set_neo4j_conf dbms.ssl.policy.bolt.base_directory "${certsBase}/bolt"
  set_neo4j_conf dbms.ssl.policy.bolt.private_key private.key
  set_neo4j_conf dbms.ssl.policy.bolt.public_certificate public.crt
  set_neo4j_conf dbms.ssl.policy.bolt.client_auth NONE
  set_neo4j_conf server.bolt.tls_level REQUIRED
  set_neo4j_conf dbms.ssl.policy.https.enabled true
  set_neo4j_conf dbms.ssl.policy.https.base_directory "${certsBase}/https"
  set_neo4j_conf dbms.ssl.policy.https.private_key private.key
  set_neo4j_conf dbms.ssl.policy.https.public_certificate public.crt
  set_neo4j_conf dbms.ssl.policy.https.client_auth NONE
}
