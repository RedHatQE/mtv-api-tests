# mtv-api-tests

## Pre Requierments

### Working with private quay.io to gain access to VDDK images

The tests require access to a VDDK image, which is stored in a private repository.

1. Make sure you part of `rh-openshift-mtv` otherwise contact Meni Yakove to add you.
   (Create new quay.io user if you don't already have one)
2. Follow the instruction to for how to `Updating the global cluster pull secret`:
   <https://docs.openshift.com/container-platform/4.18/openshift_images/managing_images/using-image-pull-secrets.html#images-update-global-pull-secret_using-image-pull-secrets>

Private vddk images
quay.io/rh-openshift-mtv/vddk-init-image:6.5
quay.io/rh-openshift-mtv/vddk-init-image:7.0.3
quay.io/rh-openshift-mtv/vddk-init-image:8.0.1

### Source providers

File `.providers.json` in the root directory of the repo with the source providers data

Deploy [openshif-mtv](https://gitlab.cee.redhat.com/md-migration/mtv-autodeploy)

install [uv](https://github.com/astral-sh/uv)

```bash
uv sync

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
      image: "quay.io/openshift-cnv/mtv-tests"
      volumeMounts:
        - mountPath: "/app/output"
          name: "output"
          readOnly: false
  volumes:
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

## Pytest

Set log collector folder: (default to `/tmp/mtv-api-tests`)

```bash
uv run pytest .... --data-collector-path <path to log collector folder>
```

After run there is `resources.json` file under `--data-collector-path` that hold all created resources during the run.
To delete all created resources using the above file run:

```bash
uv run tools/clean_cluster.py <path-to-resources.json>
```

Run without data-collector:

```bash
uv run pytest .... --skip-data-collector
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
