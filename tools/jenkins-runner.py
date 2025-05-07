import argparse
import os
import re
import sys
from typing import Any

import jenkins as jenkins_api
from mtv_iib import get_mtv_latest_iib


def main(
    job_name: str,
    cluster: str,
    iib: bool = False,
    deploy_ocp: bool = False,
    git_branch: str = "main",
    openshift_python_wrapper_git_branch: str | None = None,
) -> None:
    os.environ["PYTHONHTTPSVERIFY"] = "0"
    url = "https://jenkins-csb-mtv-qe-main.dno.corp.redhat.com"
    api = jenkins_api.Jenkins(url=url)

    supported_jobs: list[str] = [
        "mtv-2.8-ocp-4.18-test-release-gate",
        "mtv-2.8-ocp-4.17-test-release-gate",
        "mtv-2.8-ocp-4.16-test-release-gate",
        "mtv-2.8-ocp-4.18-test-stage-gate",
        "mtv-2.8-ocp-4.17-test-stage-gate",
        "mtv-2.8-ocp-4.16-test-stage-gate",
    ]

    if job_name not in supported_jobs:
        print(f"Job {job_name} is not supported")
        sys.exit(1)

    ocp_version = re.findall(r"ocp-(\d.\d+)", job_name)
    if not ocp_version:
        print("No OCP version found in job name")
        sys.exit(1)

    mtv_version = re.findall(r"mtv-(\d.\d+)", job_name)
    if not mtv_version:
        print("No MTV version found in job name")
        sys.exit(1)

    ocp_version = ocp_version[0]
    mtv_version = mtv_version[0]

    params: dict[str, Any] = {
        "CLUSTER_NAME": cluster,
        "GIT_BRANCH": git_branch,
        "USE_UNMERGED_OPENSHIFT_PYTHON_WRAPPER": True if openshift_python_wrapper_git_branch else False,
        "OPENSHIFT_PYTHON_WRAPPER_GIT_BRANCH": openshift_python_wrapper_git_branch,
        "DEPLOY_OCP": deploy_ocp,
        "FIPS_ENABLED": True,
        "DEPLOY_NFS_CSI": deploy_ocp,
    }
    if iib:
        iib_dict = get_mtv_latest_iib(version=mtv_version)[f"v{ocp_version}"]

        params["DEPLOY_MTV"] = True
        params["IIB_NO"] = iib_dict["IIB"]
        params["MTV_VERSION"] = iib_dict["MTV"].split("-")[0]

    api.build_job(name=job_name, parameters=params)
    print(f"{url}/{job_name}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        prog="Jenkins job runner",
        description="Run MTV Jenkins jobs",
    )
    parser.add_argument("-j", "--job-name", help="Jenkins job name", required=True)
    parser.add_argument("-c", "--cluster", help="Cluster name", required=True)
    parser.add_argument("--iib", help="install MTV using IIB", action="store_true")
    parser.add_argument("--deploy", help="Deploy the OCP cluster", action="store_true")
    parser.add_argument("--branch", help="Git branch", default="main")
    parser.add_argument("--ocp-wrapper-branch", help="OpenShift Python wrapper git branch")
    args = parser.parse_args()

    main(
        job_name=args.job_name,
        cluster=args.cluster,
        iib=args.iib,
        deploy_ocp=args.deploy,
        git_branch=args.branch,
        openshift_python_wrapper_git_branch=args.ocp_wrapper_branch,
    )
