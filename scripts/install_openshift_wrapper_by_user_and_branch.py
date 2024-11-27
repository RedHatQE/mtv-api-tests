import os
import shutil

import click


@click.command()
@click.option("--user", "-u", help="Github user fork to fetch", required=True)
@click.option("--branch", "-b", help="Github branch to fetch", required=True)
def install_mr(user, branch):
    """
    Install openshift-python-wrapper of forked user branch (no PR needed).
    """
    tmp_dir = "/tmp"
    ocp_python_wrapper_name = "openshift-python-wrapper"
    ocp_cloned_path = os.path.join(tmp_dir, ocp_python_wrapper_name)
    shutil.rmtree(path=ocp_cloned_path, ignore_errors=True)
    ocp_python_wrapper_git = f"https://github.com/{user}/openshift-python-wrapper.git"
    current_dir = os.path.abspath(path=os.curdir)
    os.chdir(path=tmp_dir)
    os.system(command=f"git clone {ocp_python_wrapper_git}")
    os.chdir(path=ocp_cloned_path)
    os.system(command=f"git fetch {ocp_python_wrapper_git} {branch}")
    os.system(command=f"git checkout -b {branch} FETCH_HEAD")
    os.chdir(path=current_dir)
    os.system(f"pip install -U {ocp_cloned_path}")


if __name__ == "__main__":
    install_mr()
