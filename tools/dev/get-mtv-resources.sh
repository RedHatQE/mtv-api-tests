#! /usr/bin/env bash

RESOUECES="ns pods dv pvc pv plan migration storagemap networkmap provider host secret net-attach-def hook vm vmi"

for resource in $RESOUECES; do
  res=$(oc get "$resource" -A | grep mtv-api)
  IFS=$'\n' read -r -d '' -a array <<<"$res"

  echo "$resource:"
  for line in "${array[@]}"; do
    echo "    $line"
  done
  echo -e '\n'
done
