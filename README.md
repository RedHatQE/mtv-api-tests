# mtv-api-tests

## Pre Requierments

Deploy [openshif-mtv](https://gitlab.cee.redhat.com/md-migration/mtv-autodeploy)

```bash
# Install the following packages:
dnf install python3 \
  python3-devel \
  install libxml2-devel \
  install libcurl-devel \
  install openssl \
  install openssl-devel \
  install gcc

python3 -m pip install --user pipx
pipx install uv
uv sync -p python3.12 # Any python should work from 3.9 to 3.12

# make sure oc client path in $PATH
export PATH="<oc path>:$PATH"

```

Run openshift-python-wrapper in DEBUG (show the yamls requests)

```bash
export OPENSHIFT_PYTHON_WRAPPER_LOG_LEVEL=DEBUG
```

## Run inside a pod example

1. Create a PVC for the logs name: `mtv-api-tests-pvc`
2. Create a Secret with the kubeconfig content
3. Expect the `junit-report.xml` file on the PVC root folder.

```bash
oc create secret generic kubeconfig --from-file auth=<PATH TO KUBECONFIG>
```

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: mtv-api-tests
spec:
  containers:
    - env:
        - name: MARKER
          value: "tier0"
        - name: STORAGE_CLASS
          value: "nfs"
        - name: SOURCE_PROVIDER_TYPE
          value: "vsphere"
        - name: SOURCE_PROVIDER_VERSION
          value: "6.5"
      name: mtv-api-tests
      image: "quay.io/openshift-cnv/mtv-tests:v2.3"
      volumeMounts:
        - mountPath: "/cred"
          name: kubeconfig
          readOnly: true
        - mountPath: "/app/output"
          name: "output"
          readOnly: false
  volumes:
    - name: kubeconfig
      secret:
        secretName: kubeconfig
    - name: output
      persistentVolumeClaim:
        claimName: mtv-api-tests-pvc
```

## Update The Docker Image

```bash
podman build -f Dockerfile -t mtv-api-tests
podman login quay.io
podman push mtv-api-tests quay.io/openshift-cnv/mtv-tests:latest
```

## Prepare the Environment

```bash
# cd to the project root directory
export KUBECONFIG=<path to ocp cluster kubeconifig>
```

## Run Cleanup

\*Can skip this step if you only have just deployed openshift-mtv

```bash
oc delete --all vm -nopenshift-mtv --wait=true #use with care
```

```bash
oc delete -f tests/manifests/second_network.yaml --wait=true
# May already be gone (Error from server (NotFound): error when deleting "second_network.yaml":
# network-attachment-definitions.k8s.cni.cncf.io "mybridge" not found)
```

## Pytest

Set log collector folder: (default to `/tmp/mtv-api-tests`)

```bash
uv run pytest .... --log-collector-path <path> to log collector folder>
```

After run there is `resources.json` file under `--log-collector-path` that hold all created resources during the run.
To delete all created resources using the above file run:

```bash
uv run tools/clean_cluster.py <path-to-resources.json>
```

## Run options

Run without calling teardown (Do not delete created resources)

```bash
uv run pytest --skip-teardown
```

## Run Functional Tests tier1

```bash
uv run pytest -m tier1 --tc=storage_class:<storage_class>
```

## Run Scale Lab

Search for vms and import the first X

```bash
uv run pytest -m scale --tc:vm_name_search_pattern:<search> --tc=number_of_vms:X
```

## Run InterOp Tests

1. Clone this project and cd to project root directory.
2. Make Sure oc is in the PATH.
3. export KUBECONFIG=Path to kubeconfig file.
4. sh scripts/run_interop_tests.sh

## Known Issues

- pycurl may fail with error:
  ImportError: pycurl: libcurl link-time ssl backend (nss) is different from compile-time ssl backend (none/other)

  fix:

  ```bash
  export PYCURL_SSL_LIBRARY=nss # or openssl. depend on the error (link-time ssl backend (nss))
  uv run pip uninstall pycurl
  uv run pip install pycurl --no-cache-dir
  ```

- message":"network-attachment-definitions.k8s.cni.cncf.io \"mybridge\" already exists","reason":"AlreadyExists"

  fix:

  ```bash
  oc delete -f tests/manifests/second_network.yaml
  ```

- Target VM Already exists

  fix:

  ```bash
  oc delete --all vm -nopenshift-mtv #use with care
  ```

## Release new version

### requirements

- Export GitHub token

```bash
export GITHUB_TOKEN=<your_github_token>
```

- [release-it](https://github.com/release-it/release-it)

```bash
sudo npm install --global release-it
npm install --save-dev @release-it/bumper
```

### usage

- Create a release, run from the relevant branch.  
  To create a release, run:

```bash
git main
git pull
release-it # Follow the instructions

```
