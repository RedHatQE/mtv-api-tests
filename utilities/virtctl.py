import os
import platform
import shutil
import ssl
import tarfile
import urllib.request
import zipfile
from pathlib import Path

from kubernetes.dynamic import DynamicClient
from ocp_resources.console_cli_download import ConsoleCLIDownload
from simple_logger.logger import get_logger

LOGGER = get_logger(__name__)


def _get_virtctl_download_dir() -> Path:
    """Get the directory for virtctl downloads.

    Returns:
        Path to /tmp/virtctl (created if needed)
    """
    download_dir = Path("/tmp/virtctl")
    download_dir.mkdir(parents=True, exist_ok=True)
    return download_dir


def _check_existing_virtctl(download_dir: Path) -> Path | None:
    """Check if virtctl is already available.

    Args:
        download_dir: Directory where virtctl might be downloaded

    Returns:
        Path to existing virtctl binary, or None if not found
    """
    # Check if virtctl is in PATH
    existing_virtctl = shutil.which("virtctl")
    if existing_virtctl:
        LOGGER.info(f"virtctl already available in PATH at {existing_virtctl}")
        return Path(existing_virtctl)

    # Check if previously downloaded
    virtctl_binary = download_dir / "virtctl"
    if virtctl_binary.exists() and os.access(virtctl_binary, os.X_OK):
        LOGGER.info(f"virtctl already exists at {virtctl_binary}")
        return virtctl_binary

    return None


def _detect_platform() -> tuple[list[str], list[str]]:
    """Detect current OS and architecture.

    Returns:
        Tuple of (os_patterns, arch_patterns) - BOTH are lists

    Raises:
        ValueError: If OS is not supported
    """
    current_os = platform.system().lower()  # "linux", "darwin"
    current_arch = platform.machine().lower()  # "x86_64", "aarch64", "arm64"

    # Map platform.system() to link text patterns
    # Link text uses "Linux" and "Mac" (not "Darwin" or "macOS")
    os_patterns_map = {
        "linux": ["linux"],
        "darwin": ["mac"],  # Darwin (macOS) → search for "mac" in link text
    }

    # Validate OS is supported
    if current_os not in os_patterns_map:
        raise ValueError(
            f"Unsupported operating system '{current_os}' detected. Supported: {list(os_patterns_map.keys())}"
        )

    # Map architecture to expected link text patterns
    # Link text uses "x86_64" and "ARM 64" (with space, case-insensitive)
    if current_arch in ("aarch64", "arm64"):
        # Match "ARM 64" with space (case-insensitive)
        arch_patterns = ["arm 64", "arm64", "aarch64"]
    elif current_arch == "x86_64":
        arch_patterns = ["x86_64"]
    else:
        # Log warning for unknown architectures
        LOGGER.warning(
            f"Unknown architecture '{current_arch}' detected. "
            f"Attempting to match exact pattern. Common architectures: x86_64, aarch64, arm64"
        )
        arch_patterns = [current_arch]

    return os_patterns_map[current_os], arch_patterns


def _find_virtctl_download_url(
    links: list[dict[str, str]],
    os_patterns: list[str],
    arch_patterns: list[str],
) -> str:
    """Find download URL matching current platform.

    Args:
        links: List of link dicts from ConsoleCLIDownload spec
        os_patterns: List of OS patterns to match (e.g., ["linux"] or ["mac"])
        arch_patterns: List of architecture patterns (e.g., ["x86_64"])

    Returns:
        Download URL for current platform

    Raises:
        ValueError: If no matching URL found
    """
    for link in links:
        link_text = link.get("text", "").lower()

        # Check if link matches OS
        os_match = any(os_pattern in link_text for os_pattern in os_patterns)
        if not os_match:
            continue

        # Check if link matches architecture
        arch_match = any(arch_pattern in link_text for arch_pattern in arch_patterns)
        if arch_match:
            download_url = link.get("href")
            if not download_url:
                raise ValueError(f"Link matching platform has no 'href' field: {link}")
            LOGGER.info(f"Found virtctl download URL: {download_url}")
            return download_url

    # No match found
    available_links = [link.get("text", "N/A") for link in links]
    raise ValueError(
        f"Could not find download URL matching OS patterns {os_patterns} and arch patterns {arch_patterns}. "
        f"Available links: {available_links}"
    )


def _download_and_extract_virtctl(download_url: str, download_dir: Path) -> Path:
    """Download and extract virtctl binary.

    Args:
        download_url: URL to download virtctl archive from
        download_dir: Directory to extract binary to

    Returns:
        Path to extracted virtctl binary

    Raises:
        RuntimeError: If download or extraction fails
    """
    # Determine archive type (.zip for Mac, .tar.gz for Linux)
    is_zip = download_url.endswith(".zip")
    archive_extension = ".zip" if is_zip else ".tar.gz"
    archive_file_path = download_dir / f"virtctl{archive_extension}"

    # Download archive
    try:
        LOGGER.info(f"Downloading virtctl from {download_url}...")
        # Disable SSL verification for self-signed certificates
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE

        with urllib.request.urlopen(download_url, context=ssl_context) as response:
            archive_file_path.write_bytes(response.read())
        LOGGER.info(f"Downloaded virtctl to {archive_file_path}")
    except Exception as e:
        raise RuntimeError(f"Failed to download virtctl from {download_url}: {e}") from e

    # Extract archive
    try:
        LOGGER.info(f"Extracting {archive_file_path}...")
        if is_zip:
            # Mac: extract .zip file
            with zipfile.ZipFile(archive_file_path, "r") as zip_file:
                zip_file.extractall(path=download_dir)
        else:
            # Linux: extract .tar.gz file
            with tarfile.open(archive_file_path, "r:gz") as tar:
                tar.extractall(path=download_dir, filter="data")
        LOGGER.info(f"Extracted virtctl binary to {download_dir}")

        # Remove archive after successful extraction
        archive_file_path.unlink(missing_ok=True)
        LOGGER.info(f"Removed temporary archive file: {archive_file_path}")
    except Exception as e:
        raise RuntimeError(f"Failed to extract {archive_file_path}: {e}") from e

    # Find virtctl binary
    binary_name = "virtctl"
    virtctl_binary = download_dir / binary_name
    if not virtctl_binary.exists():
        # Try subdirectories
        virtctl_candidates = list(download_dir.rglob(binary_name))
        if virtctl_candidates:
            virtctl_binary = virtctl_candidates[0]
        else:
            raise RuntimeError(f"virtctl binary not found in {download_dir} after extraction")

    # Make executable
    try:
        virtctl_binary.chmod(0o755)
        LOGGER.info(f"Made {virtctl_binary} executable")
    except Exception as e:
        raise RuntimeError(f"Failed to make {virtctl_binary} executable: {e}") from e

    return virtctl_binary


def _add_to_path(virtctl_dir: str) -> None:
    """Add directory to PATH environment variable.

    Args:
        virtctl_dir: Directory path to add to PATH
    """
    current_path = os.environ.get("PATH", "")
    if virtctl_dir not in current_path:
        os.environ["PATH"] = f"{virtctl_dir}:{current_path}"
        LOGGER.info(f"Added {virtctl_dir} to PATH")


def download_virtctl_from_cluster(client: DynamicClient) -> Path:
    """Download virtctl binary from the OpenShift cluster.

    This function retrieves the ConsoleCLIDownload resource from the cluster,
    automatically detects the current OS and architecture, extracts the matching
    download URL, downloads the virtctl binary, extracts it (handles both .tar.gz
    for Linux and .zip for Mac), makes it executable, and adds it to PATH.

    Supports:
        - OS: Linux, macOS (Darwin)
        - Architecture: x86_64, aarch64/arm64

    Args:
        client: OpenShift DynamicClient instance

    Returns:
        Path to the downloaded virtctl binary

    Raises:
        ValueError: If ConsoleCLIDownload resource not found, platform unsupported,
                   or download URL not found for current platform
        RuntimeError: If download or extraction fails

    """
    LOGGER.info("Checking for virtctl availability...")

    # Get download directory
    download_dir = _get_virtctl_download_dir()

    # Check if already available
    existing = _check_existing_virtctl(download_dir)
    if existing:
        _add_to_path(str(existing.parent))
        return existing

    LOGGER.info("virtctl not found, downloading from cluster...")

    # Get ConsoleCLIDownload resource
    try:
        console_cli_download = ConsoleCLIDownload(
            client=client,
            name="virtctl-clidownloads-kubevirt-hyperconverged",
        )
        if not console_cli_download.exists:
            raise ValueError(
                "ConsoleCLIDownload resource 'virtctl-clidownloads-kubevirt-hyperconverged' not found in cluster. "
                "Ensure KubeVirt/OpenShift Virtualization is installed."
            )
    except Exception as e:
        raise ValueError(f"Failed to retrieve ConsoleCLIDownload resource: {e}") from e

    # Detect platform
    os_patterns, arch_patterns = _detect_platform()

    # Find download URL
    links = console_cli_download.instance.spec.get("links")
    if not links:
        raise ValueError("No links found in ConsoleCLIDownload resource spec")
    download_url = _find_virtctl_download_url(links, os_patterns, arch_patterns)

    # Download and extract
    virtctl_binary = _download_and_extract_virtctl(download_url, download_dir)

    # Add to PATH
    _add_to_path(str(virtctl_binary.parent))

    LOGGER.info(f"Successfully downloaded and configured virtctl at {virtctl_binary}")
    return virtctl_binary
