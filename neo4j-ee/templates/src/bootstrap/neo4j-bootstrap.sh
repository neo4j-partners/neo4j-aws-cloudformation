#!/bin/bash
set -euo pipefail

# Template-owned bootstrap. Delivered to /opt/neo4j/bin/neo4j-bootstrap.sh by
# cfn-init from the shared LaunchTemplate's AWS::CloudFormation::Init metadata,
# then invoked by UserData. Runtime values arrive as named, exported
# environment variables; this script reads no ambient global and takes no
# positional arguments. cfn-signal is owned solely by UserData: this fail()
# prints and exits non-zero, and the non-zero exit trips UserData's ERR trap.
fail() {
  echo "ERROR: $*" >&2
  exit 1
}

# Fail closed if any required runtime variable is unset. The value is never
# echoed, so the Secrets Manager password cannot leak into the log. Optional
# variables (advertisedDNS, bloomLicenseSecretArn, gdsLicenseSecretArn) are
# intentionally absent: they are legitimately empty when the corresponding
# feature is off (advertisedDNS is empty only for Public without TLS).
for _required in stackName region nodeCount loadBalancerDNSName \
  installGDS installBloom password _stack_id _instance_id _az; do
  [[ -n "${!_required:-}" ]] || fail "Required environment variable ${_required} is unset."
done

initialPassword="${password}"
unset password
isFirstBoot=false

# The optional OIDC values arrive base64-encoded so that no parameter value
# can run as shell code in the UserData assignments. Blank decodes to blank.
oidcIssuerUri=$(printf '%s' "${oidcIssuerUriB64:-}" | base64 -d)
oidcClientId=$(printf '%s' "${oidcClientIdB64:-}" | base64 -d)
oidcBrowserClientId=$(printf '%s' "${oidcBrowserClientIdB64:-}" | base64 -d)
oidcPrincipalClaim=$(printf '%s' "${oidcPrincipalClaimB64:-}" | base64 -d)
oidcGroupsClaim=$(printf '%s' "${oidcGroupsClaimB64:-}" | base64 -d)
oidcAdminGroup=$(printf '%s' "${oidcAdminGroupB64:-}" | base64 -d)
oidcReaderGroup=$(printf '%s' "${oidcReaderGroupB64:-}" | base64 -d)

# include partials/set-neo4j-conf.sh

# include partials/attach-data-volume.sh

# include partials/install-license.sh

# include partials/install-neo4j.sh

# include partials/install-plugins.sh

# include partials/configure-neo4j.sh

# include partials/configure-tls.sh

# include partials/configure-oidc.sh

# include partials/configure-cloudwatch.sh

install_cloudwatch_agent "${stackName}"
attach_and_mount_data_volume "${region}" "${_stack_id}" "${_az}" "${_instance_id}" isFirstBoot
install_neo4j_from_yum
install_apoc
if [[ "${installBloom}" == "true" ]]; then
  install_plugin Bloom "bloom-plugin-*.jar"
  [[ -n "${bloomLicenseSecretArn}" ]] || fail "InstallBloom=true requires BloomLicenseSecretArn to be set."
  fetch_and_install_license "${bloomLicenseSecretArn}" /var/lib/neo4j/licenses/neo4j-bloom.license "Bloom" "${region}"
fi
if [[ "${installGDS}" == "true" ]]; then
  install_plugin GDS "neo4j-graph-data-science-*.jar"
  [[ -n "${gdsLicenseSecretArn}" ]] || fail "InstallGDS=true requires GdsLicenseSecretArn to be set."
  fetch_and_install_license "${gdsLicenseSecretArn}" /var/lib/neo4j/licenses/neo4j-gds.license "GDS" "${region}"
fi
apply_base_conf
configure_tls "${advertisedDNS:-}" "${loadBalancerDNSName}"
configure_memory_recommendation
configure_cluster "${nodeCount}" "${region}" "${_stack_id}"
configure_plugin_settings "${installBloom}" "${bloomLicenseSecretArn}" "${installGDS}" "${gdsLicenseSecretArn}"
remove_jdwp_default
configure_oidc "${oidcIssuerUri}" "${oidcClientId}" "${oidcBrowserClientId}" \
  "${oidcPrincipalClaim}" "${oidcGroupsClaim}" "${oidcAdminGroup}" "${oidcReaderGroup}"
assert_security_invariant
start_neo4j "${initialPassword}" "${isFirstBoot}"
