"""
Modo manutenção — flag partilhada entre o restauro de backups e a API.

Enquanto a flag existir, o middleware (main.py) responde 503 a tudo exceto
/api/health e os ticks de fundo não fazem nada. A flag é criada pelo script
de restauro, sobrevive ao reinício do container (vive no volume de dados) e
é removida no fim de um arranque bem-sucedido — uma flag órfã de um restauro
falhado nunca deixa a instalação presa em manutenção.
"""
from datetime import datetime, timezone
from pathlib import Path

FLAG = Path("/app/data/manutencao.flag")


def em_manutencao() -> bool:
    return FLAG.exists()


def ativar() -> None:
    FLAG.parent.mkdir(parents=True, exist_ok=True)
    FLAG.write_text(datetime.now(timezone.utc).isoformat())


def desativar() -> None:
    FLAG.unlink(missing_ok=True)
