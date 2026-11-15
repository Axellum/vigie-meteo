#!/usr/bin/env bash
set -euo pipefail

# Script de préparation d'une VM Oracle Cloud Ubuntu/Debian ARM64 (2 OCPU, 12 Go RAM)
echo "=== 1. Mise à jour système et installation Docker ==="
sudo apt-get update
sudo apt-get install -y ca-certificates curl gnupg ufw

if ! command -v docker >/dev/null 2>&1; then
    curl -fsSL https://get.docker.com | sudo sh
    sudo usermod -aG docker "$USER"
fi

echo "=== 2. Ouverture des ports HTTP/HTTPS dans iptables Oracle ==="
# Sur Oracle Cloud Ubuntu, iptables bloque 80/443 par défaut même si la Security List OCI est ouverte
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 80 -j ACCEPT
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 443 -j ACCEPT
if command -v netfilter-persistent >/dev/null 2>&1; then
    sudo netfilter-persistent save
fi

echo "=== 3. Préparation du fichier .env ==="
if [ ! -f "../../.env" ]; then
    cp ../../.env.example ../../.env
    echo "Fichier .env créé. Pensez à y renseigner METEOFRANCE_API_KEY et METEO_API_KEY !"
fi

echo "=== Terminé. Lancez : cd deploy/oracle && docker compose up -d --build ==="
