attach_and_mount_data_volume() {
  local region="$1"
  local _stack_id="$2"
  local _az="$3"
  local _instance_id="$4"
  local first_boot_var="$5"
  printf -v "${first_boot_var}" "%s" "false"
  echo "Attaching data volume..."
  local data_dir="${NEO4J_DATA_DIR:-/var/lib/neo4j/data}"
  local fstab_path="${FSTAB_PATH:-/etc/fstab}"
  local nvme_device_glob="${NVME_DEVICE_GLOB:-/dev/nvme?n1}"
  local allow_regular_devices="${ALLOW_REGULAR_NVME_DEVICES:-false}"
  local vols vc vid uuid
  local dev=""
  vols=$(aws ec2 describe-volumes --region "$region" \
    --filters "Name=tag:StackID,Values=${_stack_id}" "Name=tag:Role,Values=neo4j-cluster-data" "Name=availability-zone,Values=${_az}" \
    --query "Volumes[*].VolumeId" --output text)
  vc=$(echo "${vols}" | wc -w | xargs)
  [[ "${vc}" -eq 1 ]] || fail "Expected 1 data volume in ${_az}, found ${vc}."
  vid="${vols}"
  echo "Data volume: ${vid}"
  local s=0 state=""
  while true; do
    state=$(aws ec2 describe-volumes --region "$region" --volume-ids "${vid}" --query "Volumes[0].State" --output text 2>/dev/null || echo unknown)
    [[ "${state}" != "available" ]] || break
    (( s < 600 )) || fail "Volume ${vid} not available after 10m (state=${state})."
    echo "  Volume state: ${state}, waiting..."
    sleep 10; s=$(( s + 10 ))
  done
  local ok=false
  for t in 1 2 3; do
    if aws ec2 attach-volume --region "$region" --volume-id "${vid}" --instance-id "${_instance_id}" --device /dev/sdf >/dev/null 2>&1; then
      ok=true; break
    fi
    sleep 5
  done
  [[ "${ok}" == "true" ]] || fail "attach-volume failed for ${vid}."
  local astate=""
  for i in $(seq 1 24); do
    astate=$(aws ec2 describe-volumes --region "$region" --volume-ids "${vid}" --query "Volumes[0].Attachments[0].State" --output text 2>/dev/null || true)
    [[ "${astate}" != "attached" ]] || break
    sleep 5
  done
  [[ "${astate}" == "attached" ]] || fail "Volume ${vid} not attached in 2m."
  local vid_serial
  vid_serial=$(echo "${vid}" | tr -d '-')
  udevadm settle --timeout=30 || true
  for t in $(seq 1 15); do
    for d in ${nvme_device_glob}; do
      [[ -b "$d" || ( "${allow_regular_devices}" == "true" && -e "$d" ) ]] || continue
      local d_serial
      d_serial=$(lsblk -no SERIAL "$d" 2>/dev/null | tr -d ' ')
      if [[ "$d_serial" == "$vid_serial" ]]; then
        dev="$d"; break 2
      fi
    done
    sleep 2
  done
  [[ -n "${dev}" && ( -b "${dev}" || ( "${allow_regular_devices}" == "true" && -e "${dev}" ) ) ]] || fail "Could not resolve NVMe device for ${vid}."
  if ! blkid "${dev}" > /dev/null 2>&1; then
    mkfs.xfs "${dev}"
    printf -v "${first_boot_var}" "%s" "true"
  fi
  uuid=$(blkid -s UUID -o value "${dev}")
  [[ -n "${uuid}" ]] || fail "No UUID on ${dev}."
  mkdir -p "${data_dir}"
  echo "UUID=${uuid}  ${data_dir}  xfs  defaults,nofail,noatime,x-systemd.device-timeout=30  0 2" >> "${fstab_path}"
  mount "${data_dir}"
  [[ "${!first_boot_var}" != "true" ]] || chown neo4j:neo4j "${data_dir}"
}
