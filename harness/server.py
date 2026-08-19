#!/usr/bin/env python3
"""Per-challenge server lifecycle: build image, create internal network,
start container, wait for it to be reachable, tear down.

Used by:
- revfirmware challenges: hosts the live testing device (emulator) the agent
  probes over TCP

"""

import hashlib
import os
import subprocess
import time


def _hash_directory(directory):
    """Compute a deterministic hash of all files in a directory."""
    h = hashlib.sha256()
    for root, dirs, files in os.walk(directory):
        dirs.sort()  # Ensure consistent ordering
        for fname in sorted(files):
            fpath = os.path.join(root, fname)
            relpath = os.path.relpath(fpath, directory)
            h.update(relpath.encode())
            with open(fpath, "rb") as f:
                while chunk := f.read(8192):
                    h.update(chunk)
    return h.hexdigest()[:12]


_firmware_server_tag = None
_license_server_tag = None


def _get_license_server_image():
    """Build (or retrieve cached) the shared revpacker license-server image.

    One challenge-agnostic image bakes tiny_packer + a single 32-byte seed; every
    licensed binary was packed against that seed, so the same server validates all
    of them (only the baked IP differs per challenge). The seed lives only in this
    image, never in the agent-visible challenge dir. Hashing the whole _image/
    build context (skipping __pycache__/.pyc) invalidates the tag on any change.
    """
    global _license_server_tag
    if _license_server_tag is not None:
        return _license_server_tag

    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(here)
    ctx = os.path.join(root, "binaries", "_revpacker_license", "_image")

    h = hashlib.sha256()
    for dirpath, dirnames, filenames in os.walk(ctx):
        dirnames[:] = sorted(d for d in dirnames if d != "__pycache__")
        for fn in sorted(filenames):
            if fn.endswith(".pyc"):
                continue
            fp = os.path.join(dirpath, fn)
            h.update(os.path.relpath(fp, ctx).encode())
            with open(fp, "rb") as f:
                while chunk := f.read(8192):
                    h.update(chunk)
    tag = f"revbench-license-server-{h.hexdigest()[:12]}"

    result = subprocess.run(["docker", "image", "inspect", tag], capture_output=True)
    if result.returncode == 0:
        print(f"License server image {tag} already exists, skipping build.")
    else:
        print(f"Building license server image {tag}...")
        subprocess.run(["docker", "build", "-t", tag, ctx], check=True)
        print(f"Built license server image {tag}")
    _license_server_tag = tag
    return tag


def _get_firmware_server_image():
    """Build (or retrieve cached) the shared RevFirmware testing-server image.

    One challenge-agnostic image bakes the emulator + serve.py + all six firmware
    ELFs; the variant is chosen at container start via the FW_VARIANT env. Hashing
    the whole _server/ build context (skipping __pycache__/.pyc) invalidates the
    tag when any baked file changes.
    """
    global _firmware_server_tag
    if _firmware_server_tag is not None:
        return _firmware_server_tag

    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(here)
    ctx = os.path.join(root, "binaries", "revfirmware", "_image")

    h = hashlib.sha256()
    for dirpath, dirnames, filenames in os.walk(ctx):
        dirnames[:] = sorted(d for d in dirnames if d != "__pycache__")
        for fn in sorted(filenames):
            if fn.endswith(".pyc"):
                continue
            fp = os.path.join(dirpath, fn)
            h.update(os.path.relpath(fp, ctx).encode())
            with open(fp, "rb") as f:
                while chunk := f.read(8192):
                    h.update(chunk)
    tag = f"revbench-firmware-server-{h.hexdigest()[:12]}"

    result = subprocess.run(["docker", "image", "inspect", tag], capture_output=True)
    if result.returncode == 0:
        print(f"RevFirmware server image {tag} already exists, skipping build.")
    else:
        print(f"Building RevFirmware server image {tag}...")
        subprocess.run(["docker", "build", "-t", tag, ctx], check=True)
        print(f"Built RevFirmware server image {tag}")
    _firmware_server_tag = tag
    return tag


def build_server_image(challenge_dir, config):
    """Build (or retrieve cached) server Docker image. Returns image tag."""
    server_dir = os.path.join(challenge_dir, "server")
    profile = config.get("server", {}).get("profile", "standard")

    if config.get("type") == "revgame":
        challenge_name = config.get("language", "unknown")
    else:
        challenge_name = (
            config.get("name", "unknown").replace(" ", "_").replace("/", "_")
        )

    # revfirmware: one shared, challenge-agnostic testing-server image; the
    # emulator + all six firmware ELFs are baked, the variant chosen at run time.
    if config.get("type") == "revfirmware":
        return _get_firmware_server_image()

    # (The packer license sidecar is built via _get_license_server_image() directly
    # from the harness license slot -- it lives in a `license:` block, not `server:`.)

    if profile == "custom":
        custom_dockerfile = os.path.join(server_dir, "Dockerfile")
        if not os.path.isfile(custom_dockerfile):
            raise FileNotFoundError(
                f"Profile is 'custom' but missing {custom_dockerfile}"
            )

        content_hash = _hash_directory(server_dir)
        tag = f"revbench-server-{challenge_name}-{content_hash}"
        result = subprocess.run(
            ["docker", "image", "inspect", tag], capture_output=True
        )
        if result.returncode == 0:
            print(f"Server image {tag} already exists, skipping build.")
            return tag

        print(f"Building custom server image {tag}...")
        subprocess.run(
            ["docker", "build", "-t", tag, server_dir],
            check=True,
        )
        return tag

    raise ValueError(f"unsupported server profile: {profile!r}")


def create_network(network_name, subnet=None):
    """Create an isolated internal Docker network for the server.

    A fixed `subnet` is required for licensed-packer challenges, whose sidecar
    must sit at a static IP equal to the numeric IP baked into the binary. A
    fixed subnet draws from no pool, so the pool-exhaustion retry is skipped; an
    overlap means a parallel run already holds it (same baked IP), which retry
    cannot fix, so surface a clear error.
    """
    cmd = [
        "docker",
        "network",
        "create",
        "--driver",
        "bridge",
        "--internal",
    ]
    if subnet:
        cmd += ["--subnet", subnet]
    cmd.append(network_name)
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        stderr = e.stderr.lower()
        if subnet and "overlap" in stderr:
            raise RuntimeError(
                f"subnet {subnet} overlaps an existing network; a parallel run of "
                f"the same licensed challenge is unsupported (shared baked IP)."
            ) from e
        if "all predefined address pools have been fully subnetted" not in stderr:
            raise
        _cleanup_stale_networks()
        subprocess.run(cmd, check=True, capture_output=True)


def _cleanup_stale_networks():
    """Remove leaked revbench networks that have no attached containers."""
    result = subprocess.run(
        ["docker", "network", "ls", "--format", "{{.Name}}"],
        check=True,
        capture_output=True,
        text=True,
    )
    networks = result.stdout.splitlines()

    for name in networks:
        if not name.startswith("revbench-"):
            continue
        inspect = subprocess.run(
            ["docker", "network", "inspect", name, "--format", "{{len .Containers}}"],
            capture_output=True,
            text=True,
        )
        if inspect.returncode != 0:
            continue
        # if {{len .Containers}} is 0, the network is empty
        if inspect.stdout.strip() == "0":
            subprocess.run(["docker", "network", "rm", name], capture_output=True)


def start_server(
    container_name,
    image_tag,
    network,
    port,
    call_limit=None,
    firmware_variant=None,
    ip=None,
):
    """Start the server container attached to the isolated network."""
    is_firmware = image_tag.startswith("revbench-firmware-server-")

    cmd = [
        "docker",
        "run",
        "-d",
        "--name",
        container_name,
        "--network",
        network,
        "--hostname",
        "server",
        "--network-alias",
        "server",
        "-e",
        f"PORT={port}",
    ]

    # Licensed-packer sidecar: pin the static IP that was baked into the binary.
    if ip:
        cmd += ["--ip", ip]

    if is_firmware:
        # The variant selects which baked firmware ELF the testing device runs.
        # Generous-but-bounded limits live in the image entrypoint; fresh OTP per
        # connection still blocks replay of secret-dependent capabilities.
        cmd += ["-e", f"FW_VARIANT={firmware_variant or 'c-opt'}"]

    if call_limit is not None:
        cmd += ["-e", f"ORACLE_CALL_LIMIT={call_limit}"]
    cmd.append(image_tag)
    subprocess.run(cmd, check=True, capture_output=True)


def get_server_ip(container_name, network):
    """Retrieve the IP address of the server container on the given network."""
    result = subprocess.run(
        [
            "docker",
            "inspect",
            "-f",
            f'{{{{(index .NetworkSettings.Networks "{network}").IPAddress}}}}',
            container_name,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def wait_for_server(container_name, port, timeout=10.0):
    """Wait until the server's port is accepting connections."""
    deadline = time.time() + timeout

    # Check if socat is available
    check_socat = subprocess.run(
        ["docker", "exec", container_name, "which", "socat"], capture_output=True
    )
    has_socat = check_socat.returncode == 0

    while time.time() < deadline:
        try:
            if has_socat:
                # We use socat inside the container to check if it can connect to its own listening port
                result = subprocess.run(
                    [
                        "docker",
                        "exec",
                        container_name,
                        "sh",
                        "-c",
                        f"timeout 2 socat /dev/null TCP:localhost:{port},connect-timeout=1",
                    ],
                    capture_output=True,
                    timeout=5,
                )
            else:
                # Fallback to python for containers without socat
                result = subprocess.run(
                    [
                        "docker",
                        "exec",
                        container_name,
                        "python3",
                        "-c",
                        f"import socket; s = socket.socket(); s.settimeout(1); s.connect(('127.0.0.1', {port}))",
                    ],
                    capture_output=True,
                    timeout=5,
                )
        except subprocess.TimeoutExpired:
            time.sleep(0.5)
            continue

        if result.returncode in (0, 124):  # 0=connected, 124=timeout (but connected)
            return True
        time.sleep(0.5)

    return False


def stop_server(container_name):
    """Stop and remove the server container."""
    subprocess.run(["docker", "rm", "-f", container_name], capture_output=True)


def remove_network(network_name):
    """Remove the Docker network."""
    subprocess.run(
        ["docker", "network", "rm", network_name],
        capture_output=True,
    )
