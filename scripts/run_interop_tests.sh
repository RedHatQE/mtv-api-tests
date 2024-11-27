echo Deploying Required Packages...
sudo yum install python3 python3-devel libxml2-devel libcurl openssl openssl-devel gcc -y
sudo pip3 install uv

echo Verifying oc client and kubeconfig

if ! command -v oc >/dev/null 2>&1; then
  echo Cannot find oc in path
  exit 1
fi

if ! test -f "$KUBECONFIG"; then
  echo Cannot find KUBECONFIG file "$KUBECONFIG" or KUBECONFIG enviroment parameter is not set
  exit 1
fi

echo Running Interop Tests...
uv run pytest -m interop --tc-file=config-interop.json
