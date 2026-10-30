install_cloudwatch_agent() {
  local stackName="$1"
  local config_path="${CW_AGENT_CONFIG:-/opt/aws/amazon-cloudwatch-agent/etc/amazon-cloudwatch-agent.json}"
  local ctl_path="${CW_AGENT_CTL:-/opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl}"
  echo "Configuring CloudWatch agent..."
  cat > "${config_path}" <<CWCONFIG
{
  "logs": {
    "logs_collected": {
      "files": {
        "collect_list": [
          {
            "file_path": "/var/log/neo4j/security.log",
            "log_group_name": "/neo4j/${stackName}/application",
            "log_stream_name": "{instance_id}/security",
            "retention_in_days": 90
          },
          {
            "file_path": "/var/log/neo4j/debug.log",
            "log_group_name": "/neo4j/${stackName}/application",
            "log_stream_name": "{instance_id}/debug",
            "retention_in_days": 90
          },
          {
            "file_path": "/var/log/cloud-init-output.log",
            "log_group_name": "/neo4j/${stackName}/application",
            "log_stream_name": "{instance_id}/cloud-init-output",
            "retention_in_days": 90
          }
        ]
      }
    }
  }
}
CWCONFIG
  "${ctl_path}" -a fetch-config -m ec2 -s -c "file:${config_path}"
}
