fetch_and_install_license() {
  local _arn="$1"
  local _dest="$2"
  local _label="$3"
  local _region="$4"
  local _dir
  _dir=$(dirname "$_dest")
  mkdir -p "$_dir"
  local _secret
  if ! _secret=$(aws secretsmanager get-secret-value --region "${_region}" \
        --secret-id "$_arn" --query SecretString --output text \
        --cli-connect-timeout 10 --cli-read-timeout 30); then
    fail "Failed to fetch ${_label} license secret ${_arn} (network, IAM denial, or secret not found in this region)."
  fi
  if [ -z "$_secret" ] || [ "$_secret" = "None" ]; then
    fail "${_label} license secret ${_arn} returned an empty SecretString payload."
  fi
  umask 077
  printf '%s' "$_secret" > "$_dest"
  umask 022
  unset _secret
  chown neo4j:neo4j "$_dir" "$_dest"
  chmod 600 "$_dest"
  echo "Installed ${_label} license at $_dest"
}
