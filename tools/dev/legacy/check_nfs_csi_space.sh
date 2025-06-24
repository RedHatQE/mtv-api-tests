#!/bin/bash

# This script checks the space on the nfs-csi storage class in a Kubernetes/OpenShift cluster.
# It first tries to find an existing pod using an nfs-csi volume.
# If no such pod is found, it creates a temporary pod and PVC to get the info, then cleans up.

TEMP_POD_NAME=""
TEMP_PVC_NAME=""
TEMP_NAMESPACE="default"

# --- Cleanup function ---
cleanup() {
  if [ -n "$TEMP_POD_NAME" ] || [ -n "$TEMP_PVC_NAME" ]; then
    echo "🧹 Cleaning up temporary resources..."
    if [ -n "$TEMP_POD_NAME" ] && oc get pod "$TEMP_POD_NAME" -n "$TEMP_NAMESPACE" &>/dev/null; then
      oc delete pod "$TEMP_POD_NAME" -n "$TEMP_NAMESPACE" --grace-period=0 --force &>/dev/null
    fi
    if [ -n "$TEMP_PVC_NAME" ] && oc get pvc "$TEMP_PVC_NAME" -n "$TEMP_NAMESPACE" &>/dev/null; then
      oc delete pvc "$TEMP_PVC_NAME" -n "$TEMP_NAMESPACE" &>/dev/null
    fi
    echo "Cleanup complete."
  fi
}

# Trap to ensure cleanup runs on script exit
trap cleanup EXIT

# --- Main script ---

# 1. Find the nfs-csi storage class and get the NFS server details.
echo "🔍 Finding nfs-csi storage class..."
STORAGE_CLASS="nfs-csi"
NFS_SERVER=$(oc get sc "$STORAGE_CLASS" -o jsonpath='{.parameters.server}' 2>/dev/null)

if [ -z "$NFS_SERVER" ]; then
  echo "❌ Error: Could not find the '$STORAGE_CLASS' storage class or the NFS server parameter."
  exit 1
fi
echo "👍 Found NFS server: $NFS_SERVER"

# 2. Try to find an existing pod using a PVC from the nfs-csi storage class.
echo "🔄 Searching for an existing pod using a bound nfs-csi volume..."
POD_NAME=""
PVC_INFO=$(oc get pvc -A --no-headers 2>/dev/null | grep "$STORAGE_CLASS" | grep 'Bound' | head -n 1)

if [ -n "$PVC_INFO" ]; then
  PVC_NAMESPACE=$(echo "$PVC_INFO" | awk '{print $1}')
  PVC_NAME=$(echo "$PVC_INFO" | awk '{print $2}')
  echo "🔎 Found existing PVC '$PVC_NAME' in namespace '$PVC_NAMESPACE'. Looking for a pod using it."

  # Find a running pod that uses this PVC
  POD_NAME=$(oc get pods -n "$PVC_NAMESPACE" -o jsonpath="{.items[?(@.status.phase=='Running')].metadata.name}" 2>/dev/null | tr ' ' '\n' | while read pod; do
    if oc get pod "$pod" -n "$PVC_NAMESPACE" -o jsonpath='{.spec.volumes[*].persistentVolumeClaim.claimName}' 2>/dev/null | grep -q "$PVC_NAME"; then
      echo "$pod"
      break
    fi
  done)
fi

DF_OUTPUT=""
if [ -n "$POD_NAME" ]; then
  echo "👍 Found existing pod '$POD_NAME' using the PVC."
  echo "📊 Executing 'df -h' in existing pod '$POD_NAME'..."
  DF_OUTPUT=$(oc exec "$POD_NAME" -n "$PVC_NAMESPACE" -- df -h 2>/dev/null)
else
  echo "🤷 No running pod found using an existing nfs-csi PVC. Creating temporary resources..."

  # --- Create temporary resources ---
  RANDOM_ID=$(head /dev/urandom | tr -dc a-z0-9 | head -c 6)
  TEMP_PVC_NAME="nfs-space-check-pvc-${RANDOM_ID}"
  TEMP_POD_NAME="nfs-space-check-pod-${RANDOM_ID}"

  echo "✨ Creating temporary PVC: $TEMP_PVC_NAME"
  cat <<EOF | oc apply -n "$TEMP_NAMESPACE" -f -
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: $TEMP_PVC_NAME
spec:
  accessModes:
  - ReadWriteOnce
  resources:
    requests:
      storage: 1Gi
  storageClassName: $STORAGE_CLASS
EOF

  echo "⏳ Waiting for PVC to be bound..."
  is_bound=false
  for _ in $( # Try for 2 minutes (24 * 5 seconds)
    seq 1 24
  ); do
    pvc_status=$(oc get pvc "$TEMP_PVC_NAME" -n "$TEMP_NAMESPACE" -o jsonpath='{.status.phase}' 2>/dev/null)
    if [ "$pvc_status" == "Bound" ]; then
      is_bound=true
      break
    fi
    sleep 5
  done

  if [ "$is_bound" != "true" ]; then
    echo "❌ Error: Timed out waiting for temporary PVC to be bound."
    echo "--- Final PVC status ---"
    oc get pvc "$TEMP_PVC_NAME" -n "$TEMP_NAMESPACE" -o yaml
    exit 1
  fi
  echo "👍 PVC is bound."

  echo "🚀 Creating temporary pod: $TEMP_POD_NAME"
  cat <<EOF | oc apply -n "$TEMP_NAMESPACE" -f -
apiVersion: v1
kind: Pod
metadata:
  name: $TEMP_POD_NAME
spec:
  containers:
  - name: inspector
    image: registry.access.redhat.com/ubi8/ubi-minimal
    command: ["/bin/sh", "-c", "sleep 3600"]
    volumeMounts:
    - mountPath: /mnt/nfs
      name: nfs-volume
  volumes:
  - name: nfs-volume
    persistentVolumeClaim:
      claimName: $TEMP_PVC_NAME
EOF

  echo "⏳ Waiting for pod to be running..."
  is_ready=false
  for _ in $( # Try for 3 minutes (36 * 5 seconds)
    seq 1 36
  ); do
    ready_status=$(oc get pod "$TEMP_POD_NAME" -n "$TEMP_NAMESPACE" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null)
    if [ "$ready_status" == "True" ]; then
      is_ready=true
      break
    fi
    sleep 5
  done

  if [ "$is_ready" != "true" ]; then
    echo "❌ Error: Timed out waiting for temporary pod to become ready."
    echo "--- Final pod status ---"
    oc get pod "$TEMP_POD_NAME" -n "$TEMP_NAMESPACE" -o yaml
    exit 1
  fi
  echo "👍 Pod is running."

  echo "📊 Executing 'df -h' in temporary pod '$TEMP_POD_NAME'..."
  DF_OUTPUT=$(oc exec "$TEMP_POD_NAME" -n "$TEMP_NAMESPACE" -- df -h 2>/dev/null)
fi

# 3. Parse and display results
if [ -z "$DF_OUTPUT" ]; then
  echo "❌ Error: Failed to get 'df -h' output from any pod."
  exit 1
fi

NFS_USAGE_LINE=$(echo "$DF_OUTPUT" | grep "$NFS_SERVER")

if [ -z "$NFS_USAGE_LINE" ]; then
  echo "❌ Error: Could not find the NFS mount from server '$NFS_SERVER' in the 'df -h' output."
  echo "Full 'df -h' output from the pod:"
  echo "$DF_OUTPUT"
  exit 1
fi

echo "✅ Success! Found storage information."
echo ""
echo "--- NFS-CSI Storage Usage ---"
echo "$NFS_USAGE_LINE" | awk '{printf "Filesystem: %s\nTotal Size: %s\nUsed Space: %s\nAvailable Space: %s\nUsage: %s\nMount Point: %s\n", $1, $2, $3, $4, $5, $6}'
echo "-----------------------------"

# Cleanup is handled by the trap
exit 0
