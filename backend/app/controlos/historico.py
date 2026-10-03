"""História de conformidade por controlo.

Uma função só, chamada de todos os sítios que mudam o estado de um controlo. Ter
um sítio é o que garante que a história não fica com buracos: uma transição
registada em cinco sítios e esquecida no sexto é pior do que não haver história
nenhuma, porque parece completa.

O que se guarda é **o que era verdade e quando** — não quem carregou no botão,
que é o que o log de auditoria já faz. As duas coisas coexistem de propósito e
respondem a perguntas diferentes.
"""
import json
import uuid
from datetime import datetime, timezone

from sqlmodel import Session

from app.controlos.models import ControloEstadoHistorico, OrigemTransicao


def registar_transicao(
    db: Session,
    *,
    controlo_empresa,
    estado_novo,
    empresa=None,
    origem: OrigemTransicao = OrigemTransicao.UTILIZADOR,
    utilizador_id: uuid.UUID | None = None,
    ocorrido_em: datetime | None = None,
) -> ControloEstadoHistorico | None:
    """Regista uma transição de estado, se houver mesmo transição.

    Devolve `None` quando o estado não muda. Gravar não-transições encheria a
    tabela de linhas que dizem "continua igual" e tornaria inútil a única
    propriedade que a torna barata: **uma linha por mudança real**. Uma gravação
    da ficha que não altere o estado não é história de conformidade.

    `controlo_empresa` é lido ANTES de se lhe atribuir o estado novo — quem chama
    passa o estado a aplicar, e é esta função que compara. Chamar depois de
    atribuir registaria sempre "sem mudança".
    """
    anterior = getattr(controlo_empresa.estado, "value", controlo_empresa.estado)
    novo = getattr(estado_novo, "value", estado_novo)
    if anterior == novo:
        return None

    # Fotografia das evidências ligadas ao controlo neste momento. Guarda-se a
    # lista e não um join: as ligações mudam depois, e a pergunta a que isto
    # responde é "com que prova é que isto foi dado como implementado ENTÃO".
    from app.evidencias import ligacoes

    evidencias_ligadas = [
        str(evidencia.id)
        for evidencia in ligacoes.evidencias_de(
            db,
            requisito_id=controlo_empresa.id,
            empresa_id=controlo_empresa.empresa_id,
        )
    ]

    nivel = None
    if empresa is not None:
        nivel = getattr(empresa.nivel_qnrcs, "value", empresa.nivel_qnrcs)

    linha = ControloEstadoHistorico(
        empresa_id=controlo_empresa.empresa_id,
        controlo_empresa_id=controlo_empresa.id,
        estado_anterior=anterior,
        estado_novo=novo,
        nivel_qnrcs_em_vigor=nivel,
        evidencias=json.dumps(evidencias_ligadas) if evidencias_ligadas else None,
        origem=origem,
        utilizador_id=utilizador_id,
        ocorrido_em=ocorrido_em or datetime.now(timezone.utc),
    )
    db.add(linha)
    return linha


def estado_em(
    db: Session, *, controlo_empresa_id: uuid.UUID, momento: datetime
) -> ControloEstadoHistorico | None:
    """O estado de um controlo numa data — a pergunta que a F5 existe para responder.

    Devolve a última transição ocorrida até `momento`. `None` significa que o
    controlo não tinha história ainda nessa data, que é diferente de estar num
    estado qualquer: quem mostra isto tem de saber distinguir "não sabemos" de
    "não iniciado".
    """
    from sqlmodel import select

    return db.exec(
        select(ControloEstadoHistorico)
        .where(
            ControloEstadoHistorico.controlo_empresa_id == controlo_empresa_id,
            ControloEstadoHistorico.ocorrido_em <= momento,
        )
        .order_by(ControloEstadoHistorico.ocorrido_em.desc())
    ).first()


def historia_de(
    db: Session, *, controlo_empresa_id: uuid.UUID
) -> list[ControloEstadoHistorico]:
    """Transições de um controlo, da mais antiga para a mais recente."""
    from sqlmodel import select

    return list(
        db.exec(
            select(ControloEstadoHistorico)
            .where(ControloEstadoHistorico.controlo_empresa_id == controlo_empresa_id)
            .order_by(ControloEstadoHistorico.ocorrido_em)
        ).all()
    )
