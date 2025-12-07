import os
import paramiko
from simple_logger.logger import get_logger

LOGGER = get_logger(__name__)


def install_ssh_key_on_esxi(host, username, password, public_key, datastore_name):
    """
    Installs an SSH public key on an ESXi host with command restrictions.
    This method uses SFTP to write a temporary file and then moves it into place,
    which is more reliable than using 'echo' with redirection.

    Args:
        host (str): The hostname or IP address of the ESXi host.
        username (str): The username for SSH login (usually 'root').
        password (str): The password for the user.
        public_key (str): The SSH public key string.
        datastore_name (str): The name of the datastore for the command restriction.
    """
    command_template = (
        'command="python /vmfs/volumes/{datastore_name}/secure-vmkfstools-wrapper.py",'
        "no-port-forwarding,no-agent-forwarding,no-X11-forwarding {public_key}"
    )
    restricted_key = command_template.format(datastore_name=datastore_name, public_key=public_key)

    client = None
    sftp = None
    try:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        LOGGER.info(f"Connecting to ESXi host {host}...")
        client.connect(hostname=host, username=username, password=password)
        sftp = client.open_sftp()

        authorized_keys_path = "/etc/ssh/keys-root/authorized_keys"
        temp_authorized_keys_path = f"/tmp/authorized_keys_{os.urandom(8).hex()}"
        key_dir = "/etc/ssh/keys-root"

        # Ensure the target directory exists
        stdin, stdout, stderr = client.exec_command(f"mkdir -p {key_dir}")
        exit_status = stdout.channel.recv_exit_status()
        if exit_status != 0:
            error = stderr.read().decode("utf-8")
            raise Exception(f"Failed to create directory {key_dir}. Error: {error}")

        # Read existing content
        content = ""
        try:
            with sftp.open(authorized_keys_path, "r") as f:
                content = f.read().decode("utf-8")
        except FileNotFoundError:
            LOGGER.info(f"'{authorized_keys_path}' not found. A new file will be created.")

        if public_key in content:
            LOGGER.info("SSH key already present on ESXi host. Skipping installation.")
            return

        # Prepare new content
        if content and not content.endswith("\n"):
            content += "\n"
        content += restricted_key + "\n"

        # Write new content to a temporary file
        with sftp.open(temp_authorized_keys_path, "w") as f:
            f.write(content)

        # Move temporary file to final destination and set permissions
        command = f"mv {temp_authorized_keys_path} {authorized_keys_path} && chmod 600 {authorized_keys_path}"
        LOGGER.info(f"Moving temporary file to '{authorized_keys_path}' and setting permissions.")
        stdin, stdout, stderr = client.exec_command(command)
        exit_status = stdout.channel.recv_exit_status()

        if exit_status == 0:
            LOGGER.info("SSH key installed successfully.")
        else:
            error = stderr.read().decode("utf-8")
            # Clean up temp file on failure
            try:
                sftp.remove(temp_authorized_keys_path)
            except Exception as e:
                LOGGER.warning(f"Failed to remove temporary file {temp_authorized_keys_path}: {e}")
            raise Exception(f"Failed to move key file and set permissions. Exit status: {exit_status}. Error: {error}")

    finally:
        if sftp:
            sftp.close()
        if client:
            client.close()
            LOGGER.info("SSH connection to ESXi host closed.")


def remove_ssh_key_from_esxi(host, username, password, public_key):
    """
    Removes an SSH public key from an ESXi host's authorized_keys file.
    This method uses a temporary file to safely rewrite the authorized_keys file.

    Args:
        host (str): The hostname or IP address of the ESXi host.
        username (str): The username for SSH login (usually 'root').
        password (str): The password for the user.
        public_key (str): The SSH public key string to remove.
    """
    client = None
    sftp = None
    try:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        LOGGER.info(f"Connecting to ESXi host {host} for key removal...")
        client.connect(hostname=host, username=username, password=password)
        sftp = client.open_sftp()

        authorized_keys_path = "/etc/ssh/keys-root/authorized_keys"
        temp_authorized_keys_path = f"/tmp/authorized_keys_{os.urandom(8).hex()}"

        # Read the file
        try:
            with sftp.open(authorized_keys_path, "r") as f:
                lines = f.readlines()
        except FileNotFoundError:
            LOGGER.info(f"'{authorized_keys_path}' not found. No removal needed.")
            return

        # Filter out the key
        original_line_count = len(lines)
        new_lines = [line for line in lines if public_key not in line]

        if len(new_lines) == original_line_count:
            LOGGER.info("SSH key not found in authorized_keys file. No removal needed.")
            return

        # Write the modified content to a temporary file
        with sftp.open(temp_authorized_keys_path, "w") as f:
            f.writelines(new_lines)

        # Atomically replace the old file with the new one
        command = f"mv {temp_authorized_keys_path} {authorized_keys_path}"
        LOGGER.info(f"Removing public key by replacing {authorized_keys_path} with temporary file.")
        stdin, stdout, stderr = client.exec_command(command)
        exit_status = stdout.channel.recv_exit_status()

        if exit_status != 0:
            error = stderr.read().decode("utf-8")
            # Clean up temp file on failure
            try:
                sftp.remove(temp_authorized_keys_path)
            except Exception as e:
                LOGGER.warning(f"Failed to remove temporary file {temp_authorized_keys_path}: {e}")
            raise Exception(f"Failed to replace authorized_keys file. Exit status: {exit_status}. Error: {error}")

        LOGGER.info("SSH key removed successfully.")
    finally:
        if sftp:
            sftp.close()
        if client:
            client.close()
            LOGGER.info("SSH connection to ESXi host closed.")
