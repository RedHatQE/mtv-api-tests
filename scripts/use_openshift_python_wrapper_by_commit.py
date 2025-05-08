import argparse
import os


def install_mr(commit):
    """
    Install openshift-python-wrapper commit.
    """
    ocp_python_wrapper_git = "https://github.com/RedHatQE/openshift-python-wrapper.git"
    os.system(f"uv add -U git+{ocp_python_wrapper_git}@{commit}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        prog="OCP wrapper commit install",
    )
    parser.add_argument("-c", "--commit", help="openshift-python-wrapper commit to install", required=True)
    args = parser.parse_args()

    install_mr(commit=args.commit)
