"""Le lancement d'uvicorn dans l'image : la passerelle garde une connexion inactive plus longtemps que ses clients."""

import json
from pathlib import Path


def test_uvicorn_keeps_an_idle_connection_longer_than_its_clients() -> None:
    dockerfile = Path(__file__).resolve().parents[1] / "Dockerfile"
    cmd_lines = [line for line in dockerfile.read_text(encoding="utf-8").splitlines() if line.startswith("CMD ")]
    cmd = json.loads(cmd_lines[-1][len("CMD ") :])
    assert cmd[0] == "uvicorn"
    # Vigie API garde 30 s une connexion à la passerelle : si le serveur la ferme avant, une requête peut partir sur une
    # connexion déjà fermée (5 s par défaut des deux côtés, « Server disconnected without sending a response »).
    assert int(cmd[cmd.index("--timeout-keep-alive") + 1]) > 30
