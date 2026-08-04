#!/usr/bin/env bash
# Everything the README lists as a prerequisite, installed once when the
# container is created. The devcontainer features cover az, azd, node, kubectl
# and helm; these are the two they do not carry.
set -euo pipefail

# uv, which owns the Python environment and is what every documented command
# goes through.
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"

# kubelogin, which `azd`'s predeploy hook calls to convert the kubeconfig for
# Entra authentication. `az aks install-cli` ships it alongside kubectl.
sudo az aks install-cli --install-location /usr/local/bin/kubectl \
  --kubelogin-install-location /usr/local/bin/kubelogin

uv sync
npm --prefix frontend ci

echo
echo "Ready. Next:"
echo "  az login --tenant <tenant-id>"
echo "  azd auth login --tenant-id <tenant-id>"
echo "  see README.md for the rest"
