"""
Lógica de negócio do módulo de Formação (core).

Responsabilidades:
  - CRUD das ações de sensibilização/formação (PR.FC-1) + participantes.
  - Estado (planeada → realizada) com registo da data de realização.
  - Painel com o indicador do órgão de gestão formado (PR.FC-2 / RJC, arts. 25.º e 27.º).
  - Auditoria de todas as escritas (`Acao.FORMACAO_*`).
  - Payload localizado do registo de formação (o PDF é gerado no cliente).

Tudo filtrado por `empresa_id` (multi-tenant, fail-closed).
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone

from fastapi import HTTPException, Request
from sqlalchemy import func
from sqlmodel import Session, select

from app.auth.models import Utilizador
from app.formacao.models import (
    AcaoFormacao,
    EstadoFormacao,
    ParticipanteFormacao,
)
from app.formacao.schemas import (
    AcaoDetalheSchema,
    AcaoSchema,
    ListaAcoesSchema,
    LoteParticipantesSchema,
    PainelFormacaoSchema,
    ParticipanteSchema,
)
from app.shared.audit import Acao, ResultadoAcao, registar_acao
from app.shared.capacidades import (
    ClasseAcao,
    dono_na_criacao,
    exigir_ambito,
    exigir_delegacao_se_muda_dono,
)
from app.shared.pii import cifrar_pii, decifrar_pii, truncar_para_cifra
from app.shared.validacao import e_data_futura

FORMATOS = {"presencial", "online", "misto"}
_JANELA_DIAS = 365  # "no último ano" para os indicadores


# ── Leitura ──────────────────────────────────────────────────────────────────

def _get_acao(db: Session, acao_id: uuid.UUID, empresa_id: uuid.UUID) -> AcaoFormacao:
    a = db.get(AcaoFormacao, acao_id)
    if not a or a.empresa_id != empresa_id or a.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Ação de formação não encontrada.")
    return a


def _nomes(db: Session, ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    ids = {i for i in ids if i}
    if not ids:
        return {}
    return {
        u.id: (decifrar_pii(u.nome) or "")
        for u in db.exec(select(Utilizador).where(Utilizador.id.in_(ids))).all()
    }


def _contar_participantes(db: Session, acao_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    if not acao_ids:
        return {}
    linhas = db.exec(
        select(ParticipanteFormacao.acao_id, func.count())
        .where(ParticipanteFormacao.acao_id.in_(acao_ids))
        .group_by(ParticipanteFormacao.acao_id)
    ).all()
    return {aid: n for aid, n in linhas}


def _schema(a: AcaoFormacao, nome: str | None, total: int = 0) -> AcaoSchema:
    s = AcaoSchema.model_validate(a)
    s.responsavel_nome = nome
    s.total_participantes = total
    return s


def listar_acoes(
    db: Session, empresa_id: uuid.UUID, incluir_canceladas: bool = True
) -> ListaAcoesSchema:
    filtros = [AcaoFormacao.empresa_id == empresa_id, AcaoFormacao.deleted_at.is_(None)]
    if not incluir_canceladas:
        filtros.append(AcaoFormacao.estado != EstadoFormacao.CANCELADA)
    acoes = db.exec(
        select(AcaoFormacao).where(*filtros).order_by(AcaoFormacao.data.desc())
    ).all()
    nomes = _nomes(db, {a.responsavel_id for a in acoes})
    contagens = _contar_participantes(db, [a.id for a in acoes])
    return ListaAcoesSchema(
        total=len(acoes),
        acoes=[_schema(a, nomes.get(a.responsavel_id), contagens.get(a.id, 0)) for a in acoes],
    )


def obter_acao(
    db: Session, acao_id: uuid.UUID, empresa_id: uuid.UUID
) -> AcaoDetalheSchema:
    a = _get_acao(db, acao_id, empresa_id)
    participantes = db.exec(
        select(ParticipanteFormacao)
        .where(ParticipanteFormacao.acao_id == acao_id)
        .order_by(ParticipanteFormacao.created_at.asc())
    ).all()
    nomes = _nomes(db, {a.responsavel_id})
    base = _schema(a, nomes.get(a.responsavel_id), len(participantes))
    detalhe = AcaoDetalheSchema(**base.model_dump())
    detalhe.participantes = [
        ParticipanteSchema.model_validate(p) for p in participantes
    ]
    return detalhe


def _data_efetiva(a: AcaoFormacao) -> date:
    """Data em que a ação contou: a de realização se existir, senão a prevista."""
    return a.realizada_at or a.data


def painel(db: Session, empresa_id: uuid.UUID) -> PainelFormacaoSchema:
    hoje = datetime.now(timezone.utc).date()
    limite = hoje - timedelta(days=_JANELA_DIAS)
    acoes = db.exec(
        select(AcaoFormacao).where(
            AcaoFormacao.empresa_id == empresa_id, AcaoFormacao.deleted_at.is_(None)
        )
    ).all()
    realizadas_todas = [a for a in acoes if a.estado == EstadoFormacao.REALIZADA]
    realizadas = [a for a in realizadas_todas if _data_efetiva(a) >= limite]
    planeadas = sum(1 for a in acoes if a.estado == EstadoFormacao.PLANEADA)
    ids_realizadas = [a.id for a in realizadas]

    # Só conta quem esteve. Uma inscrição que não compareceu não é formação dada —
    # é a mesma regra que já se aplicava ao indicador do órgão de gestão.
    participantes_ano = 0
    if ids_realizadas:
        participantes_ano = db.exec(
            select(func.count()).select_from(ParticipanteFormacao).where(
                ParticipanteFormacao.acao_id.in_(ids_realizadas),
                ParticipanteFormacao.presente.is_(True),
            )
        ).one()

    # Órgão de gestão formado: ação realizada dirigida à gestão, ou com um
    # participante da gestão marcado como presente. Calculado sobre TODO o
    # historial para se saber a data da última e até quando vale.
    ids_gestao = set()
    if realizadas_todas:
        ids_gestao = set(
            db.exec(
                select(ParticipanteFormacao.acao_id).where(
                    ParticipanteFormacao.acao_id.in_([a.id for a in realizadas_todas]),
                    ParticipanteFormacao.orgao_gestao.is_(True),
                    ParticipanteFormacao.presente.is_(True),
                )
            ).all()
        )
    datas_gestao = [
        _data_efetiva(a) for a in realizadas_todas if a.orgao_gestao or a.id in ids_gestao
    ]
    ultima_gestao = max(datas_gestao) if datas_gestao else None

    return PainelFormacaoSchema(
        total=len([a for a in acoes if a.estado != EstadoFormacao.CANCELADA]),
        realizadas_ano=len(realizadas),
        planeadas=planeadas,
        participantes_ano=participantes_ano,
        orgao_gestao_ok=ultima_gestao is not None and ultima_gestao >= limite,
        orgao_gestao_ultima=ultima_gestao,
        orgao_gestao_valido_ate=(
            ultima_gestao + timedelta(days=_JANELA_DIAS) if ultima_gestao else None
        ),
    )


# ── Escrita ──────────────────────────────────────────────────────────────────

def _resolver_pessoa(
    db: Session, empresa_id: uuid.UUID, pessoa_id: str
) -> uuid.UUID | None:
    """Valida que o id é de uma pessoa ativa desta empresa (responsável ou participante)."""
    if not pessoa_id:
        return None
    try:
        rid = uuid.UUID(pessoa_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Pessoa inválida.")
    u = db.get(Utilizador, rid)
    if not u or u.empresa_id != empresa_id or not u.ativo or u.deleted_at is not None:
        raise HTTPException(status_code=400, detail="Pessoa inválida.")
    return rid


def criar_acao(
    db: Session, empresa_id: uuid.UUID, dados, utilizador: Utilizador,
    request: Request | None = None,
) -> AcaoSchema:
    if not dados.titulo.strip():
        raise HTTPException(status_code=400, detail="O título é obrigatório.")
    if dados.formato not in FORMATOS:
        raise HTTPException(status_code=400, detail="Formato inválido.")
    responsavel_id = dono_na_criacao(
        utilizador,
        "formacao",
        _resolver_pessoa(db, empresa_id, dados.responsavel_id),
    )
    a = AcaoFormacao(
        empresa_id=empresa_id,
        titulo=dados.titulo.strip(),
        descricao=dados.descricao,
        tipo=dados.tipo,
        formato=dados.formato,
        data=dados.data or datetime.now(timezone.utc).date(),
        duracao_horas=dados.duracao_horas,
        formador=dados.formador,
        orgao_gestao=dados.orgao_gestao,
        responsavel_id=responsavel_id,
    )
    db.add(a)
    db.flush()
    registar_acao(
        db, acao=Acao.FORMACAO_CRIADA, resultado=ResultadoAcao.SUCESSO,
        empresa_id=empresa_id, utilizador_id=utilizador.id,
        entidade_tipo="AcaoFormacao", entidade_id=a.id,
        dados_novos={"titulo": a.titulo, "tipo": a.tipo.value, "orgao_gestao": a.orgao_gestao},
        request=request,
    )
    db.flush()
    db.refresh(a)
    nome = decifrar_pii(db.get(Utilizador, responsavel_id).nome) if responsavel_id else None
    return _schema(a, nome, 0)


def _validar_realizada_nao_futura(data_acao: date) -> None:
    """
    Uma ação dada como realizada não pode estar datada no futuro: entraria nos
    indicadores e no registo de evidência como formação que ainda não aconteceu.
    """
    if e_data_futura(data_acao):
        raise HTTPException(
            status_code=400,
            detail="Uma ação realizada não pode ter data futura.",
        )


def atualizar_acao(
    db: Session, acao_id: uuid.UUID, empresa_id: uuid.UUID, dados,
    utilizador: Utilizador, request: Request | None = None,
) -> AcaoSchema:
    a = _get_acao(db, acao_id, empresa_id)
    exigir_ambito(utilizador, "formacao", ClasseAcao.OPERAR, a.responsavel_id)
    campos = dados.model_dump(exclude_unset=True)
    if "formato" in campos and campos["formato"] not in FORMATOS:
        raise HTTPException(status_code=400, detail="Formato inválido.")
    if campos.get("data") and a.estado == EstadoFormacao.REALIZADA:
        _validar_realizada_nao_futura(campos["data"])
    if "responsavel_id" in campos:
        novo_dono = _resolver_pessoa(db, empresa_id, campos.pop("responsavel_id") or "")
        exigir_delegacao_se_muda_dono(utilizador, "formacao", a.responsavel_id, novo_dono)
        a.responsavel_id = novo_dono
    for campo, valor in campos.items():
        setattr(a, campo, valor)
    if campos.get("data") and a.estado == EstadoFormacao.REALIZADA:
        # A data corrigida de uma ação já realizada é a data em que ela
        # aconteceu: o indicador do órgão de gestão conta a partir dela, como o
        # registo exportado.
        a.realizada_at = campos["data"]
    a.updated_at = datetime.now(timezone.utc)
    db.add(a)
    registar_acao(
        db, acao=Acao.FORMACAO_ATUALIZADA, resultado=ResultadoAcao.SUCESSO,
        empresa_id=empresa_id, utilizador_id=utilizador.id,
        entidade_tipo="AcaoFormacao", entidade_id=a.id,
        dados_novos={k: (v.value if hasattr(v, "value") else v) for k, v in campos.items()},
        request=request,
    )
    db.flush()
    db.refresh(a)
    total = _contar_participantes(db, [a.id]).get(a.id, 0)
    nome = decifrar_pii(db.get(Utilizador, a.responsavel_id).nome) if a.responsavel_id else None
    return _schema(a, nome, total)


def alterar_estado(
    db: Session, acao_id: uuid.UUID, empresa_id: uuid.UUID, novo: EstadoFormacao,
    utilizador: Utilizador, request: Request | None = None,
) -> AcaoSchema:
    a = _get_acao(db, acao_id, empresa_id)
    exigir_ambito(utilizador, "formacao", ClasseAcao.OPERAR, a.responsavel_id)
    if novo == EstadoFormacao.REALIZADA:
        _validar_realizada_nao_futura(a.data)
    a.estado = novo
    if novo == EstadoFormacao.REALIZADA:
        if a.realizada_at is None:
            # A data da ação (já validada como não futura), e não a de hoje: a
            # validade da formação do órgão de gestão (RJC, arts. 25.º e 27.º) conta desde
            # que ela aconteceu, não desde que alguém a marcou como feita.
            a.realizada_at = a.data
    else:
        a.realizada_at = None  # deixou de estar realizada: a data antiga não vale
    a.updated_at = datetime.now(timezone.utc)
    db.add(a)
    registar_acao(
        db, acao=Acao.FORMACAO_ESTADO_ALTERADO, resultado=ResultadoAcao.SUCESSO,
        empresa_id=empresa_id, utilizador_id=utilizador.id,
        entidade_tipo="AcaoFormacao", entidade_id=a.id,
        dados_novos={"titulo": a.titulo, "estado": novo.value},
        request=request,
    )
    db.flush()
    db.refresh(a)
    total = _contar_participantes(db, [a.id]).get(a.id, 0)
    nome = decifrar_pii(db.get(Utilizador, a.responsavel_id).nome) if a.responsavel_id else None
    return _schema(a, nome, total)


def adicionar_participantes(
    db: Session, acao_id: uuid.UUID, empresa_id: uuid.UUID, dados,
    utilizador: Utilizador, request: Request | None = None,
) -> LoteParticipantesSchema:
    """
    Inscreve um grupo inteiro num só pedido.

    Uma sessão de formação tem N pessoas; fazer N pedidos deixava o grupo meio
    inserido quando um falhava a meio. Os já inscritos são ignorados em silêncio
    (não é erro inscrever duas vezes a mesma turma) e contados à parte.
    """
    a = _get_acao(db, acao_id, empresa_id)
    exigir_ambito(utilizador, "formacao", ClasseAcao.OPERAR, a.responsavel_id)

    # Lê os já inscritos UMA vez e mantém os índices em memória: relê-los a cada
    # participante fazia o custo crescer ao quadrado do tamanho da turma.
    existentes = db.exec(
        select(ParticipanteFormacao).where(ParticipanteFormacao.acao_id == a.id)
    ).all()
    ids_ocupados = {e.utilizador_id for e in existentes if e.utilizador_id}
    nomes_ocupados = {
        (decifrar_pii(e.nome) or "").strip().casefold() for e in existentes if not e.utilizador_id
    }

    novos: list[ParticipanteFormacao] = []
    ignorados = 0
    for entrada in dados.participantes:
        utilizador_id = None
        # Cortado pelo tamanho do criptograma (vai cifrado para uma coluna de
        # 500), e não por caracteres: 255 letras acentuadas não cabiam.
        nome = truncar_para_cifra((entrada.nome or "").strip(), 500) or ""
        if entrada.utilizador_id:
            utilizador_id = _resolver_pessoa(db, empresa_id, entrada.utilizador_id)
            nome = decifrar_pii(db.get(Utilizador, utilizador_id).nome) or nome
        if not utilizador_id and not nome:
            raise HTTPException(status_code=400, detail="Indique um participante.")

        if utilizador_id and utilizador_id in ids_ocupados:
            ignorados += 1
            continue
        if not utilizador_id and nome.casefold() in nomes_ocupados:
            ignorados += 1
            continue

        p = ParticipanteFormacao(
            acao_id=a.id, empresa_id=empresa_id, utilizador_id=utilizador_id,
            nome=cifrar_pii(nome), orgao_gestao=entrada.orgao_gestao, presente=entrada.presente,
        )
        db.add(p)
        novos.append(p)
        if utilizador_id:
            ids_ocupados.add(utilizador_id)
        else:
            nomes_ocupados.add(nome.casefold())

    if novos:
        registar_acao(
            db, acao=Acao.FORMACAO_PARTICIPANTE_ADICIONADO, resultado=ResultadoAcao.SUCESSO,
            empresa_id=empresa_id, utilizador_id=utilizador.id,
            entidade_tipo="AcaoFormacao", entidade_id=a.id,
            dados_novos={
                # Sem o título, a trilha dizia «foi adicionado 1 participante»
                # e não dizia a quê.
                "titulo": a.titulo,
                "adicionados": len(novos),
                "orgao_gestao": sum(1 for p in novos if p.orgao_gestao),
            },
            request=request,
        )
    db.flush()
    for p in novos:
        db.refresh(p)
    return LoteParticipantesSchema(
        adicionados=len(novos),
        ignorados=ignorados,
        participantes=[ParticipanteSchema.model_validate(p) for p in novos],
    )


def alterar_presenca(
    db: Session, acao_id: uuid.UUID, participante_id: uuid.UUID, empresa_id: uuid.UUID,
    presente: bool, utilizador: Utilizador, request: Request | None = None,
) -> ParticipanteSchema:
    """
    Marca um participante como presente ou ausente depois da sessão.

    A presença é o que separa uma lista de convocados de prova de formação dada, e
    só se conhece depois de a sessão acontecer.
    """
    a = _get_acao(db, acao_id, empresa_id)
    exigir_ambito(utilizador, "formacao", ClasseAcao.OPERAR, a.responsavel_id)
    p = db.get(ParticipanteFormacao, participante_id)
    if not p or p.acao_id != acao_id or p.empresa_id != empresa_id:
        raise HTTPException(status_code=404, detail="Participante não encontrado.")
    p.presente = presente
    db.add(p)
    registar_acao(
        db, acao=Acao.FORMACAO_PRESENCA_MARCADA, resultado=ResultadoAcao.SUCESSO,
        empresa_id=empresa_id, utilizador_id=utilizador.id,
        entidade_tipo="AcaoFormacao", entidade_id=acao_id,
        dados_novos={
            "titulo": a.titulo,
            "participante_id": str(participante_id),
            "presente": presente,
        },
        request=request,
    )
    db.flush()
    db.refresh(p)
    return ParticipanteSchema.model_validate(p)


def remover_participante(
    db: Session, acao_id: uuid.UUID, participante_id: uuid.UUID, empresa_id: uuid.UUID,
    utilizador: Utilizador, request: Request | None = None,
) -> None:
    a = _get_acao(db, acao_id, empresa_id)
    exigir_ambito(utilizador, "formacao", ClasseAcao.OPERAR, a.responsavel_id)
    p = db.get(ParticipanteFormacao, participante_id)
    if not p or p.acao_id != acao_id or p.empresa_id != empresa_id:
        raise HTTPException(status_code=404, detail="Participante não encontrado.")
    db.delete(p)
    registar_acao(
        db, acao=Acao.FORMACAO_PARTICIPANTE_REMOVIDO, resultado=ResultadoAcao.SUCESSO,
        empresa_id=empresa_id, utilizador_id=utilizador.id,
        entidade_tipo="AcaoFormacao", entidade_id=acao_id,
        dados_novos={"titulo": a.titulo, "participante_id": str(participante_id)},
        request=request,
    )


def eliminar_acao(
    db: Session, acao_id: uuid.UUID, empresa_id: uuid.UUID,
    utilizador: Utilizador, request: Request | None = None,
) -> None:
    a = _get_acao(db, acao_id, empresa_id)
    exigir_ambito(utilizador, "formacao", ClasseAcao.ELIMINAR, a.responsavel_id)
    a.deleted_at = datetime.now(timezone.utc)
    db.add(a)
    registar_acao(
        db, acao=Acao.FORMACAO_ELIMINADA, resultado=ResultadoAcao.SUCESSO,
        empresa_id=empresa_id, utilizador_id=utilizador.id,
        entidade_tipo="AcaoFormacao", entidade_id=a.id,
        dados_novos={"titulo": a.titulo}, request=request,
    )


# ── Registo de formação (payload localizado; PDF gerado no cliente) ──────────

_REL_TEXTOS = {
    "pt": {
        "titulo": "Registo de Formação e Sensibilização em Cibersegurança",
        "subtitulo": "Evidência das ações de formação, incluindo a formação do órgão de gestão (RJC, arts. 25.º, n.º 1, al. d), e 27.º, n.º 1, al. f)).",
        "sec_acoes": "Ações de formação e sensibilização",
        "c_data": "Data", "c_titulo": "Ação", "c_tipo": "Tipo", "c_estado": "Estado",
        "c_gestao": "Órgão de gestão", "c_participantes": "Participantes",
        "sim": "Sim", "nao": "Não", "sem": "Ainda sem ações de formação registadas.",
        "tipos": {"sensibilizacao": "Sensibilização", "formacao": "Formação"},
        "estados": {"planeada": "Planeada", "realizada": "Realizada", "cancelada": "Cancelada"},
        "sec_participantes": "Participantes por ação",
        "sec_participantes_texto": (
            "Quem recebeu cada ação. A presença é o que distingue uma lista de "
            "convocados de formação efetivamente dada."
        ),
        "c_participante": "Participante", "c_presenca": "Presença",
        "presente": "Presente", "ausente": "Ausente",
        "sem_participantes": "Ainda sem participantes registados.",
    },
    "en": {
        "titulo": "Cybersecurity Training and Awareness Record",
        "subtitulo": "Evidence of training actions, including management body training (RJC, Articles 25(1)(d) and 27(1)(f)).",
        "sec_acoes": "Training and awareness actions",
        "c_data": "Date", "c_titulo": "Action", "c_tipo": "Type", "c_estado": "Status",
        "c_gestao": "Management body", "c_participantes": "Participants",
        "sim": "Yes", "nao": "No", "sem": "No training actions recorded yet.",
        "tipos": {"sensibilizacao": "Awareness", "formacao": "Training"},
        "estados": {"planeada": "Planned", "realizada": "Done", "cancelada": "Cancelled"},
        "sec_participantes": "Participants by action",
        "sec_participantes_texto": (
            "Who received each action. Attendance is what separates a list of "
            "invitees from training actually delivered."
        ),
        "c_participante": "Participant", "c_presenca": "Attendance",
        "presente": "Attended", "ausente": "Absent",
        "sem_participantes": "No participants recorded yet.",
    },
}


def documento_formacao(
    db: Session, empresa_id: uuid.UUID, locale: str | None
) -> dict:
    """Payload do registo de formação (mesma forma dos documentos premium)."""
    t = _REL_TEXTOS["en" if (locale or "").startswith("en") else "pt"]
    acoes = db.exec(
        select(AcaoFormacao)
        .where(AcaoFormacao.empresa_id == empresa_id, AcaoFormacao.deleted_at.is_(None))
        .order_by(AcaoFormacao.data.desc())
    ).all()
    contagens = _contar_participantes(db, [a.id for a in acoes])

    cabecalho = [t["c_data"], t["c_titulo"], t["c_tipo"], t["c_estado"], t["c_gestao"], t["c_participantes"]]
    linhas = [
        [
            a.data.isoformat(),
            a.titulo,
            t["tipos"].get(a.tipo.value, a.tipo.value),
            t["estados"].get(a.estado.value, a.estado.value),
            t["sim"] if a.orgao_gestao else t["nao"],
            str(contagens.get(a.id, 0)),
        ]
        for a in acoes
    ]

    if linhas:
        secoes = [{"titulo": t["sec_acoes"], "texto": "", "cabecalho": cabecalho, "linhas": linhas}]
    else:
        secoes = [{"titulo": t["sec_acoes"], "texto": t["sem"], "cabecalho": [], "linhas": []}]

    # Quem recebeu a formação. Sem isto o registo prova contagens, não pessoas — e
    # é sobre pessoas (em especial o órgão de gestão) que os arts. 25.º e 27.º do RJC são verificados.
    titulos = {a.id: a.titulo for a in acoes}
    participantes = []
    if acoes:
        participantes = db.exec(
            select(ParticipanteFormacao)
            .where(ParticipanteFormacao.acao_id.in_(list(titulos)))
            .order_by(ParticipanteFormacao.created_at.asc())
        ).all()
    linhas_part = [
        [
            titulos.get(p.acao_id, ""),
            decifrar_pii(p.nome) or "—",
            t["sim"] if p.orgao_gestao else t["nao"],
            t["presente"] if p.presente else t["ausente"],
        ]
        for p in participantes
    ]
    secoes.append(
        {
            "titulo": t["sec_participantes"],
            "texto": t["sec_participantes_texto"] if linhas_part else t["sem_participantes"],
            "cabecalho": [t["c_titulo"], t["c_participante"], t["c_gestao"], t["c_presenca"]]
            if linhas_part else [],
            "linhas": linhas_part,
        }
    )

    return {
        "titulo": t["titulo"],
        "subtitulo": t["subtitulo"],
        "data_geracao": datetime.now(timezone.utc).isoformat(),
        "secoes": secoes,
        "controlos": ["PR.FC-1", "PR.FC-2"],
    }
