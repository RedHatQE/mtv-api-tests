uv run pytest -m "${MARKER}" \
  --junit-xml="${JUNIT_FILE_PATH:-/app/output/junit-report.xml}" \
  --tc=storage_class:"${STORAGE_CLASS}" \
  --tc=source_provider_type:"${SOURCE_PROVIDER_TYPE}" \
  --tc=source_provider_version:"${SOURCE_PROVIDER_VERSION}" \
  --tc=target_namespace:"${TARGET_NAMESPACE}" \
  --tc=insecure_verify_skip:"${INSECURE_VERIFY_SKIP}"

cat /app/output/junit-report.xml
