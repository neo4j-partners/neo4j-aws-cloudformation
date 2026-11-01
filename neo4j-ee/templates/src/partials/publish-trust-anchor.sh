publish_trust_anchor() {
  local certificateArn="$1"
  local region="$2"
  local stackName="$3"
  local parameterName="/neo4j-ee/${stackName}/ca-pem"
  local chain pem

  # Clients verify the NLB certificate against this value. A self-signed or
  # imported certificate with no chain is its own trust anchor, so the leaf
  # is published when ACM returns no chain. Every node publishes the same
  # value, so a replacement node refreshes it.
  [[ -n "${certificateArn}" ]] || fail "publish_trust_anchor requires the CertificateArn."
  echo "Publishing the certificate trust anchor to ${parameterName}..."
  chain=$(aws acm get-certificate --certificate-arn "${certificateArn}" \
    --region "${region}" --query CertificateChain --output text) \
    || fail "acm get-certificate failed for ${certificateArn}."
  if [[ -z "${chain}" || "${chain}" == "None" ]]; then
    pem=$(aws acm get-certificate --certificate-arn "${certificateArn}" \
      --region "${region}" --query Certificate --output text) \
      || fail "acm get-certificate failed for ${certificateArn}."
  else
    pem="${chain}"
  fi
  # A malformed ACM response must never overwrite a good anchor.
  [[ "${pem}" == "-----BEGIN CERTIFICATE-----"* ]] \
    || fail "ACM returned no PEM certificate for ${certificateArn}."
  aws ssm put-parameter --name "${parameterName}" --value "${pem}" \
    --type String --tier Advanced --overwrite --region "${region}" >/dev/null \
    || fail "ssm put-parameter failed for ${parameterName}."
}
