"""
Conversões partilhadas entre os clientes gRPC dos módulos premium: o que sai do
núcleo para o contrato (o `Ator`) e o que volta dele em forma que vários módulos
repetem (o documento-evidência).
"""
from __future__ import annotations


def ator_pb(premium_pb2, ator: dict | None):
    """Constrói a message Ator (identidade de quem age) para os RPCs de escrita.

    O sidecar exige-o em todas as escritas: sem ator (None), responde
    FAILED_PRECONDITION. Só as execuções agendadas dos conetores seguem sem ele.
    """
    if not ator:
        return None
    return premium_pb2.Ator(
        id=ator.get("id", ""),
        nome=ator.get("nome", ""),
        ambito=ator.get("ambito", ""),
    )


def documento_to_dict(pb) -> dict:
    """Documento-evidência (título, secções com tabelas) para o ecrã e o PDF."""
    return {
        "titulo": pb.titulo,
        "subtitulo": pb.subtitulo,
        "data_geracao": pb.data_geracao,
        "controlos": list(pb.controlos),
        "secoes": [
            {
                "titulo": s.titulo,
                "texto": s.texto,
                "cabecalho": list(s.cabecalho),
                "linhas": [list(l.celulas) for l in s.linhas],
            }
            for s in pb.secoes
        ],
    }
