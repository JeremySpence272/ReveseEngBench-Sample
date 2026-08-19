#!/usr/bin/env python3

import hashlib
import json
import os
import subprocess
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TOOLS_DIR = os.path.join(ROOT, "tools")
AGENTS_DIR = os.path.join(ROOT, "agents")

# Always available in every image, including tier1:
# - download/bootstrap support for image construction
# - baseline runtimes for Linux-runnable challenge subjects
BASE_APT = [
    "curl",
    "ca-certificates",
    "python3",
    "openjdk-21-jre-headless",
    "libncurses6",
]


def image_tag(agent_name, dockerfile_content, mcp_registry):
    """Deterministic image tag based on the generated image contents.

    The Dockerfile only references install.sh by path, so its content must be
    folded into the hash explicitly; otherwise editing install.sh does not bust
    the cache and a stale image is reused."""
    parts = [agent_name, dockerfile_content, mcp_registry]
    install_sh = os.path.join(AGENTS_DIR, agent_name, "install.sh")
    if os.path.isfile(install_sh):
        with open(install_sh) as f:
            parts.append(f.read())
    key = "\n".join(parts)
    digest = hashlib.sha256(key.encode()).hexdigest()[:12]
    return f"revbench-{agent_name}-{digest}"


def image_exists(tag):
    """Check if a Docker image with this tag already exists locally."""
    result = subprocess.run(
        ["docker", "image", "inspect", tag],
        capture_output=True,
    )
    return result.returncode == 0


def collect_packages(tool_manifests):
    """Deduplicate packages across all tool manifests by package manager type.

    Each manifest has a "packages" dict keyed by manager name, e.g.:
        {"apt": ["git", "curl"], "pip": ["requests"]}
    Returns a dict of {manager: sorted_deduplicated_list}.
    """
    packages = {"apt": set(BASE_APT)}
    for m in tool_manifests:
        for manager, pkgs in m.get("packages", {}).items():
            packages.setdefault(manager, set()).update(pkgs)
    # Ensure pip is available if any pip packages are requested
    if "pip" in packages:
        packages["apt"].add("python3-pip")
    return {mgr: sorted(pkgs) for mgr, pkgs in packages.items()}


def build_mcp_registry(tool_manifests):
    """Build the MCP tool registry JSON that gets baked into the image."""
    registry = {}
    for m in tool_manifests:
        if "mcp" in m:
            registry[m["name"]] = m["mcp"]
    return json.dumps(registry, indent=2) + "\n"


def generate_dockerfile(agent_name, tool_manifests):
    """Build a Dockerfile string from agent install script + tool manifests."""
    packages = collect_packages(tool_manifests)
    lines = [
        "FROM ubuntu:24.04",
        "ENV DEBIAN_FRONTEND=noninteractive",
        "",
        "# Step-budget deadline hook: surface /workspace/.harness_deadline into shell output",
        "RUN printf '%s\\n' 'if [ -z \"${_RB_DL_SHOWN:-}\" ]; then export _RB_DL_SHOWN=1; if [ -f /workspace/.harness_deadline ]; then cat /workspace/.harness_deadline >&2; fi; fi' > /etc/revbench_deadline.sh "
        "&& cp /etc/revbench_deadline.sh /etc/profile.d/zz_revbench_deadline.sh",
        "ENV BASH_ENV=/etc/revbench_deadline.sh",
        "ENV ENV=/etc/revbench_deadline.sh",
        "",
    ]

    # Install apt packages first (needed by everything else)
    if "apt" in packages:
        lines += [
            "# Install apt packages (deduped across tools)",
            "RUN apt-get update && apt-get install -y \\",
            "    " + " \\\n    ".join(packages["apt"]) + " \\",
            "    && rm -rf /var/lib/apt/lists/*",
            "",
        ]

    # Install pip packages (explicitly use system pip)
    if "pip" in packages:
        lines += [
            "# Install pip packages (deduped across tools) into system python",
            "RUN /usr/bin/pip3 install --no-cache-dir --break-system-packages "
            + " ".join(packages["pip"]),
            "",
        ]

    # pyghidra/ghidra install Ghidra to /opt/ghidra and need GHIDRA_INSTALL_DIR;
    # set it as an image ENV so it reaches non-interactive shells (bash -c), not
    # just the interactive-only /etc/environment + ~/.bashrc their installers write.
    tool_names = {m["name"] for m in tool_manifests}
    if tool_names & {"pyghidra", "ghidra"}:
        lines += [
            "ENV GHIDRA_INSTALL_DIR=/opt/ghidra",
            "",
        ]

    # Agent install script
    agent_install = os.path.join(AGENTS_DIR, agent_name, "install.sh")
    if os.path.isfile(agent_install):
        lines += [
            f"# Install agent: {agent_name}",
            f"COPY agents/{agent_name}/install.sh /tmp/agent_install.sh",
            "RUN chmod +x /tmp/agent_install.sh && /tmp/agent_install.sh && rm /tmp/agent_install.sh",
            "",
        ]

    # Tool install scripts + startup scripts
    for m in tool_manifests:
        tool_name = m["name"]
        tool_dir = os.path.join(TOOLS_DIR, tool_name)
        install_script = os.path.join(tool_dir, "install.sh")
        startup_script = os.path.join(tool_dir, "startup.sh")

        # Copy any extra files the tool needs during install
        for file_entry in m.get("files", []):
            src = file_entry["src"]  # relative to tools/<name>/
            dst = file_entry["dst"]  # absolute path in container
            lines += [
                f"COPY tools/{tool_name}/{src} {dst}",
            ]

        if os.path.isfile(install_script):
            lines += [
                f"# Install tool: {tool_name}",
                f"COPY tools/{tool_name}/install.sh /tmp/{tool_name}_install.sh",
                f"RUN chmod +x /tmp/{tool_name}_install.sh && /tmp/{tool_name}_install.sh && rm /tmp/{tool_name}_install.sh",
                "",
            ]

        if os.path.isfile(startup_script):
            lines += [
                f"COPY tools/{tool_name}/startup.sh /opt/{tool_name}-startup.sh",
                f"RUN chmod +x /opt/{tool_name}-startup.sh",
                "",
            ]

    # Bake MCP registry into the image - added to workspace in case needed later on
    lines += [
        "# MCP tool registry for agent discovery",
        "COPY mcp_registry.json /workspace/mcp.json",
        "",
    ]

    # Cleanup — remove package managers and lock down the environment
    lines += [
        "# Remove system package managers",
        "RUN rm -f /usr/bin/apt* /usr/bin/dpkg /usr/bin/curl /usr/bin/wget"
        " /usr/bin/pip* /usr/bin/git /usr/bin/unzip"
        " /usr/bin/npm /usr/bin/npx"
        " /usr/local/bin/npm /usr/local/bin/npx /usr/bin/mvn || true",
        "",
        "WORKDIR /workspace",
        'CMD ["tail", "-f", "/dev/null"]',
    ]

    return "\n".join(lines) + "\n"


def get_image(agent_name, tool_manifests):
    """Return the image tag, building it if it doesn't exist."""
    dockerfile_content = generate_dockerfile(agent_name, tool_manifests)
    mcp_registry = build_mcp_registry(tool_manifests)
    tag = image_tag(agent_name, dockerfile_content, mcp_registry)

    if image_exists(tag):
        print(f"Image {tag} already exists, skipping build.")
        return tag

    print(f"Building image {tag}...")

    # Build from repo root so COPY paths resolve correctly
    # Write both the Dockerfile and mcp_registry as temp files
    uid = uuid.uuid4().hex
    dockerfile_path = os.path.join(ROOT, f"Dockerfile_{uid}.tmp")
    registry_path = os.path.join(ROOT, f"mcp_registry_{uid}.json")

    # Inject the unique registry filename into the dockerfile content after computing the image tag
    dockerfile_content = dockerfile_content.replace(
        "COPY mcp_registry.json /workspace/mcp.json",
        f"COPY mcp_registry_{uid}.json /workspace/mcp.json",
    )

    try:
        with open(dockerfile_path, "w") as f:
            f.write(dockerfile_content)
        with open(registry_path, "w") as f:
            f.write(mcp_registry)

        subprocess.run(
            ["docker", "build", "-t", tag, "-f", dockerfile_path, ROOT],
            check=True,
        )
    finally:
        for path in (dockerfile_path, registry_path):
            if os.path.exists(path):
                os.unlink(path)

    print(f"Built image {tag}")
    return tag
