configure_oidc() {
  local issuerUri="$1"
  local clientIds="$2"
  local browserClientId="$3"
  local principalClaim="$4"
  local groupsClaim="$5"
  local adminGroup="$6"
  local readerGroup="$7"
  local provider="dbms.security.oidc.sso"
  local mappings=()

  if [[ -z "${issuerUri}" ]]; then
    echo "OIDC disabled (no OidcIssuerUri); password login only."
    return 0
  fi
  [[ -n "${clientIds}" ]] || fail "OidcIssuerUri is set but OidcClientId is empty."
  if [[ -z "${browserClientId}" ]]; then
    browserClientId="${clientIds%%,*}"
  fi
  # Browser and Bloom present an ID token issued to this client, so it must
  # pass the audience check.
  if [[ ",${clientIds}," != *",${browserClientId},"* ]]; then
    fail "OidcBrowserClientId is not one of the IDs in OidcClientId."
  fi
  if [[ -n "${adminGroup}" ]]; then
    mappings+=("\"${adminGroup}\"=admin")
  fi
  if [[ -n "${readerGroup}" ]]; then
    mappings+=("\"${readerGroup}\"=reader")
  fi
  if (( ${#mappings[@]} > 0 )) && [[ -z "${groupsClaim}" ]]; then
    fail "OidcAdminGroup or OidcReaderGroup is set but OidcGroupsClaim is empty."
  fi

  # native stays in both lists so password login (operator tooling and
  # service accounts) keeps working next to single sign-on.
  set_neo4j_conf dbms.security.authentication_providers "oidc-sso,native"
  set_neo4j_conf dbms.security.authorization_providers "oidc-sso,native"
  set_neo4j_conf "${provider}.display_name" "SSO"
  set_neo4j_conf "${provider}.auth_flow" pkce
  set_neo4j_conf "${provider}.well_known_discovery_uri" \
    "${issuerUri%/}/.well-known/openid-configuration"
  set_neo4j_conf "${provider}.audience" "${clientIds}"
  set_neo4j_conf "${provider}.params" \
    "client_id=${browserClientId};response_type=code;scope=openid profile email"
  # Validate the ID token: some providers (Cognito) put no aud claim in the
  # access token, so the audience check can only pass on the ID token.
  set_neo4j_conf "${provider}.config" \
    "token_type_principal=id_token;token_type_authentication=id_token"
  if [[ -n "${principalClaim}" ]]; then
    set_neo4j_conf "${provider}.claims.username" "${principalClaim}"
  fi
  if [[ -n "${groupsClaim}" ]]; then
    set_neo4j_conf "${provider}.claims.groups" "${groupsClaim}"
  fi
  if (( ${#mappings[@]} > 0 )); then
    local joined
    joined=$(IFS=';'; echo "${mappings[*]}")
    set_neo4j_conf "${provider}.authorization.group_to_role_mapping" "${joined}"
  fi
  echo "OIDC enabled (provider sso, ${#mappings[@]} group mapping(s))."
}
