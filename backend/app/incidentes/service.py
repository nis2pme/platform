"""
Lógica de negócio do módulo de Incidentes.

Responsabilidades:
  - CRUD do incidente + linha temporal append-only (prova de "quem fez o quê").
  - Prazos das notificações do Regime Jurídico da Cibersegurança (arts. 41.º a
    44.º) e da notificação à CNPD (RGPD, art. 33.º). A conta é a de
    `app.incidentes.prazos`; aqui só se juntam os factos do incidente.
  - Registo das notificações enviadas, com a cópia congelada do que se entregou.
  - Notificações in-app (e email) quando um marco obrigatório se aproxima ou é
    ultrapassado.
  - Auditoria de todas as escritas (`Acao.INCIDENTE_*`).
  - Documentos localizados (as notificações e o relatório interno); o PDF é
    gerado no cliente.

Tudo filtrado por `empresa_id` (multi-tenant, fail-closed).
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

from fastapi import HTTPException, Request
from sqlalchemy import func
from sqlmodel import Session, select

from app.auth.models import RoleUtilizador, Utilizador
from app.empresas.models import Empresa, TipoEntidade
from app.incidentes.models import (
    EstadoIncidente,
    Incidente,
    IncidenteEvento,
    IncidenteNotificacao,
    TipoEventoIncidente,
)
from app.incidentes.prazos import MARCOS, EntradaPrazos, Prazo, calcular_prazos
from app.incidentes.schemas import (
    EventoSchema,
    IncidenteDetalheSchema,
    IncidenteSchema,
    ListaIncidentesSchema,
    NotificacaoDetalheSchema,
    NotificacaoResumoSchema,
    PainelIncidentesSchema,
    PrazoSchema,
    _CamposRJC,
)
from app.incidentes.textos import MARCOS_TXT, REFERENCIA_MARCO, lingua
from app.notificacoes.catalogo import Codigo, chave
from app.notificacoes.models import Notificacao
from app.notificacoes.service import (
    criar_notificacao,
    dar_por_resolvidas,
    pendentes_da_entidade,
)
from app.shared.audit import Acao, ResultadoAcao, registar_acao
from app.shared.capacidades import (
    ClasseAcao,
    dono_na_criacao,
    exigir_ambito,
    exigir_delegacao_se_muda_dono,
)
from app.shared.i18n import MsgsI18n, locale_de_request, traduzir
from app.shared.pii import decifrar_pii

# Partes interessadas para comunicações (RS.NC-1, RC.CO-1). `destinatarios` são
# os destinatários dos serviços (RJC, art. 48.º); as últimas cinco são as
# autoridades que o relatório final indica (RJC, art. 44.º, n.º 2, al. e), iv)).
PARTES = {
    "pessoal", "clientes", "autoridade", "fornecedores", "outros", "destinatarios",
    "ministerio_publico", "policia_judiciaria", "cnpd", "gns", "autoridade_setorial",
}
_PARTES_AUTORIDADES = (
    "ministerio_publico", "policia_judiciaria", "cnpd", "gns", "autoridade_setorial",
)

# Canais por onde uma notificação se entrega.
CANAIS = ("myciber", "email", "telefone", "outro")

# Documentos de notificação à autoridade (a CNPD recebe o relatório interno).
TIPOS_DOCUMENTO = ("notificacao_inicial", "atualizacao", "fim_impacto", "relatorio_final", "intercalar")

# Coluna do incidente que cada notificação preenche. O intercalar repete-se e
# não tem coluna: a tabela das notificações é a fonte.
_COLUNA_MARCO = {
    "notificacao_inicial": "notificacao_inicial_at",
    "atualizacao": "atualizacao_at",
    "fim_impacto": "fim_impacto_notificado_at",
    "relatorio_final": "relatorio_final_at",
    "cnpd": "cnpd_notificado_at",
}

# Janela de aviso antes de um prazo expirar (horas).
_JANELA_AVISO_H = 24
# Folga para relógios desacertados ao recusar datas no futuro.
_FOLGA = timedelta(minutes=5)

# Campos do incidente cifrados em repouso (TextoCifrado no modelo): não vão em
# claro para a trilha, que não se apaga nem se anonimiza.
_CAMPOS_CIFRADOS = {
    "licoes_aprendidas", "criterios_fecho", "excecao_24h",
    "representante_nome", "representante_telefone", "representante_email",
    "causa", "efeitos", "medidas", "situacao_residual",
}
_CAMPOS_RJC = tuple(_CamposRJC.model_fields)
_BOOLEANOS = {"resolvido_2h", "atualizacao_necessaria", "cnpd_aplicavel"}
_DATAS = {"significativo_em", "impacto_inicio_em", "fim_impacto_em", "intercalar_pedido_em", "ocorrido_at"}

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

_TAXONOMIA = Path(__file__).parent / "dados" / "taxonomia_incidentes.json"


def _utc(dt: datetime | None) -> datetime | None:
    """Garante datetime tz-aware em UTC (as colunas guardam naive)."""
    if dt is None:
        return None
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _erro(status: int, codigo: str, catalogo: dict, locale: str | None, **params) -> HTTPException:
    return HTTPException(
        status_code=status,
        detail={"codigo": codigo, "mensagem": traduzir(catalogo, locale, **params), **params},
    )


# ── Taxonomia ────────────────────────────────────────────────────────────────

@lru_cache(maxsize=1)
def taxonomia() -> dict:
    """A taxonomia de incidentes (as duas línguas), lida uma vez."""
    with _TAXONOMIA.open(encoding="utf-8") as f:
        return json.load(f)


@lru_cache(maxsize=1)
def _tipos() -> dict[str, tuple[dict, dict]]:
    """Código do tipo → (classe, tipo)."""
    return {
        t["codigo"]: (classe, t)
        for classe in taxonomia()["classes"]
        for t in classe["tipos"]
    }


def _validar_categoria(valor: str | None, locale: str | None) -> str:
    """Código válido da taxonomia. As categorias antigas convertem-se (um
    cliente ainda com a interface anterior continua a conseguir registar)."""
    tax = taxonomia()
    if not valor:
        return tax["omissao"]
    valor = tax["categorias_anteriores"].get(valor, valor)
    if valor not in _tipos():
        raise _erro(400, "categoria_invalida", MsgsI18n.INCIDENTE_CATEGORIA_INVALIDA, locale)
    return valor


def rotulo_categoria(codigo: str, locale: str | None) -> str:
    """«Classe / Tipo» na língua pedida; um código desconhecido sai tal como está."""
    lg = lingua(locale)
    par = _tipos().get(codigo)
    if par is None:
        return codigo
    classe, tipo = par
    return f"{classe[lg]} / {tipo[lg]}"


# ── Prazos ───────────────────────────────────────────────────────────────────

def entidade_voluntaria(empresa: Empresa | None) -> bool:
    """Fora do âmbito do regime, as notificações são voluntárias (RJC, art. 45.º)."""
    if empresa is None:
        return False
    tipo = getattr(empresa.tipo_entidade, "value", empresa.tipo_entidade)
    return tipo == TipoEntidade.BASE.value


def entrada_prazos(
    inc: Incidente, *, voluntario: bool, intercalar_ultimo_at: datetime | None
) -> EntradaPrazos:
    """Os factos do incidente de que os prazos dependem. É a única ponte entre o
    modelo e o motor de prazos: o ecrã, o tick, o fecho, os documentos e o
    dossiê passam todos por aqui."""
    return EntradaPrazos(
        significativo=inc.significativo,
        conhecido_at=inc.conhecido_at,
        significativo_em=inc.significativo_em,
        fim_impacto_em=inc.fim_impacto_em,
        resolvido_2h=bool(inc.resolvido_2h),
        excecao_24h=bool((inc.excecao_24h or "").strip()),
        atualizacao_necessaria=bool(inc.atualizacao_necessaria),
        intercalar_pedido_em=inc.intercalar_pedido_em,
        cnpd_aplicavel=bool(inc.cnpd_aplicavel),
        voluntario=voluntario,
        notificacao_inicial_at=inc.notificacao_inicial_at,
        atualizacao_at=inc.atualizacao_at,
        fim_impacto_notificado_at=inc.fim_impacto_notificado_at,
        relatorio_final_at=inc.relatorio_final_at,
        intercalar_ultimo_at=intercalar_ultimo_at,
        cnpd_notificado_at=inc.cnpd_notificado_at,
    )


@dataclass
class _Contexto:
    """O que os prazos precisam além do incidente: se a empresa está no âmbito
    e o que já foi enviado."""
    voluntario: bool
    intercalar_ultimo: dict[uuid.UUID, datetime] = field(default_factory=dict)
    com_notificacoes: set[uuid.UUID] = field(default_factory=set)


def _contexto(db: Session, empresa_id: uuid.UUID, ids: list[uuid.UUID]) -> _Contexto:
    ctx = _Contexto(voluntario=entidade_voluntaria(db.get(Empresa, empresa_id)))
    if not ids:
        return ctx
    linhas = db.exec(
        select(
            IncidenteNotificacao.incidente_id,
            IncidenteNotificacao.tipo,
            func.max(IncidenteNotificacao.enviada_em),
        )
        .where(
            IncidenteNotificacao.empresa_id == empresa_id,
            IncidenteNotificacao.incidente_id.in_(ids),  # type: ignore[union-attr]
        )
        .group_by(IncidenteNotificacao.incidente_id, IncidenteNotificacao.tipo)
    ).all()
    for incidente_id, tipo, ultima in linhas:
        ctx.com_notificacoes.add(incidente_id)
        if tipo == "intercalar":
            ctx.intercalar_ultimo[incidente_id] = ultima
    return ctx


def _calcular(inc: Incidente, ctx: _Contexto, agora: datetime) -> list[Prazo]:
    return calcular_prazos(
        entrada_prazos(
            inc, voluntario=ctx.voluntario,
            intercalar_ultimo_at=ctx.intercalar_ultimo.get(inc.id),
        ),
        agora,
    )


def prazos_do_incidente(db: Session, inc: Incidente, agora: datetime) -> list[Prazo]:
    """Estado de cada marco do incidente face a `agora`."""
    return _calcular(inc, _contexto(db, inc.empresa_id, [inc.id]), agora)


def prazo_para_json(p: Prazo) -> dict:
    """Um marco pronto a serializar, com as datas em ISO 8601 (dossiê)."""
    d = asdict(p)
    for chave in ("prazo", "cumprido_at"):
        d[chave] = d[chave].isoformat() if d[chave] else None
    return d


def _em_risco(p: Prazo) -> bool:
    """Marco obrigatório por cumprir cujo prazo passou ou passa nas próximas 24 h."""
    if not p.obrigatorio or p.cumprido or p.prazo is None:
        return False
    return p.em_atraso or (p.horas_restantes is not None and p.horas_restantes <= _JANELA_AVISO_H)


def _por_cumprir(inc: Incidente, prazos: list[Prazo]) -> list[Prazo]:
    """Marcos obrigatórios ainda por cumprir. Os do regime só contam num
    incidente classificado como significativo; o da CNPD conta sempre."""
    return [
        p for p in prazos
        if p.obrigatorio and not p.cumprido and (inc.significativo is True or p.marco == "cnpd")
    ]


# ── Leitura ──────────────────────────────────────────────────────────────────

def _get_incidente(db: Session, incidente_id: uuid.UUID, empresa_id: uuid.UUID) -> Incidente:
    inc = db.get(Incidente, incidente_id)
    if not inc or inc.empresa_id != empresa_id or inc.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Incidente não encontrado.")
    return inc


def _nomes(db: Session, ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    ids = {i for i in ids if i}
    if not ids:
        return {}
    return {
        u.id: (decifrar_pii(u.nome) or "")
        for u in db.exec(select(Utilizador).where(Utilizador.id.in_(ids))).all()
    }


def _schema(inc: Incidente, agora: datetime, nome: str | None, ctx: _Contexto) -> IncidenteSchema:
    s = IncidenteSchema.model_validate(inc)
    s.responsavel_nome = nome
    s.voluntario = ctx.voluntario
    s.intercalar_ultimo_at = _utc(ctx.intercalar_ultimo.get(inc.id))
    s.tem_notificacoes = inc.id in ctx.com_notificacoes
    s.prazos = [PrazoSchema(**asdict(p)) for p in _calcular(inc, ctx, agora)]
    return s


def _schema_de(db: Session, inc: Incidente) -> IncidenteSchema:
    nome = decifrar_pii(db.get(Utilizador, inc.responsavel_id).nome) if inc.responsavel_id else None
    ctx = _contexto(db, inc.empresa_id, [inc.id])
    return _schema(inc, datetime.now(timezone.utc), nome, ctx)


def listar_incidentes(
    db: Session, empresa_id: uuid.UUID, incluir_fechados: bool = True
) -> ListaIncidentesSchema:
    agora = datetime.now(timezone.utc)
    filtros = [Incidente.empresa_id == empresa_id, Incidente.deleted_at.is_(None)]
    if not incluir_fechados:
        filtros.append(Incidente.estado != EstadoIncidente.FECHADO)
    incs = db.exec(
        select(Incidente).where(*filtros).order_by(Incidente.conhecido_at.desc())
    ).all()
    nomes = _nomes(db, {i.responsavel_id for i in incs})
    ctx = _contexto(db, empresa_id, [i.id for i in incs])
    return ListaIncidentesSchema(
        total=len(incs),
        incidentes=[_schema(i, agora, nomes.get(i.responsavel_id), ctx) for i in incs],
    )


def obter_incidente(
    db: Session, incidente_id: uuid.UUID, empresa_id: uuid.UUID, locale: str | None
) -> IncidenteDetalheSchema:
    """O incidente com a linha temporal, cujas frases saem na língua de `locale`."""
    agora = datetime.now(timezone.utc)
    inc = _get_incidente(db, incidente_id, empresa_id)
    eventos = db.exec(
        select(IncidenteEvento)
        .where(IncidenteEvento.incidente_id == incidente_id)
        .order_by(IncidenteEvento.created_at.asc())
    ).all()
    nomes = _nomes(db, {inc.responsavel_id} | {e.autor_id for e in eventos})
    ctx = _contexto(db, empresa_id, [inc.id])
    base = _schema(inc, agora, nomes.get(inc.responsavel_id), ctx)
    detalhe = IncidenteDetalheSchema(**base.model_dump())
    detalhe.eventos = [
        EventoSchema(
            id=e.id, tipo=e.tipo, texto=texto_evento(e, locale), parte=e.parte,
            autor_id=e.autor_id, autor_nome=nomes.get(e.autor_id), created_at=e.created_at,
        )
        for e in eventos
    ]
    return detalhe


def painel(db: Session, empresa_id: uuid.UUID) -> PainelIncidentesSchema:
    agora = datetime.now(timezone.utc)
    incs = db.exec(
        select(Incidente).where(
            Incidente.empresa_id == empresa_id, Incidente.deleted_at.is_(None)
        )
    ).all()
    abertos = [i for i in incs if i.estado != EstadoIncidente.FECHADO]
    signif_abertos = [i for i in abertos if i.significativo]
    ctx = _contexto(db, empresa_id, [i.id for i in abertos])
    em_risco = sum(1 for i in abertos for p in _calcular(i, ctx, agora) if _em_risco(p))
    return PainelIncidentesSchema(
        total=len(incs),
        abertos=len(abertos),
        significativos_abertos=len(signif_abertos),
        prazos_em_risco=em_risco,
    )


# ── Escrita ──────────────────────────────────────────────────────────────────

def _resolver_responsavel(
    db: Session, empresa_id: uuid.UUID, responsavel_id: str
) -> uuid.UUID | None:
    """Valida que o responsável é um utilizador ativo do tenant (ou None)."""
    if not responsavel_id:
        return None
    try:
        rid = uuid.UUID(responsavel_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Responsável inválido.")
    u = db.get(Utilizador, rid)
    if not u or u.empresa_id != empresa_id or not u.ativo or u.deleted_at is not None:
        raise HTTPException(status_code=400, detail="Responsável inválido.")
    return rid


# O que o próprio sistema escreve na linha temporal. A linha é append-only e
# segue para o dossiê, por isso grava o que aconteceu (um código e os seus
# parâmetros) e não a frase: esta compõe-se na leitura, na língua de quem lê.
_EV_REGISTADO = "incidente_registado"
_EV_ESTADO = "estado_alterado"
_EV_NOTIFICACAO = "notificacao_enviada"

_EVENTO_TEXTOS = {
    "pt": {
        "criado": "Incidente registado.",
        "estado": "Estado alterado para «{estado}».",
        "fecho_em_aberto": "Encerrado com {n} marco(s) por cumprir: {marcos}.",
        "notificacao": "Notificação enviada: {marco} (canal: {canal}{referencia}).",
        "referencia": "; referência: {ref}",
        "estados": {
            "aberto": "Aberto", "em_analise": "Em análise", "contido": "Contido",
            "resolvido": "Resolvido", "fechado": "Fechado",
        },
        "marcos": MARCOS_TXT["pt"],
        "canais": {"myciber": "MyCiber", "email": "Email", "telefone": "Telefone", "outro": "Outro"},
    },
    "en": {
        "criado": "Incident recorded.",
        "estado": "Status changed to “{estado}”.",
        "fecho_em_aberto": "Closed with {n} milestone(s) still due: {marcos}.",
        "notificacao": "Notification sent: {marco} (channel: {canal}{referencia}).",
        "referencia": "; reference: {ref}",
        "estados": {
            "aberto": "Open", "em_analise": "Under analysis", "contido": "Contained",
            "resolvido": "Resolved", "fechado": "Closed",
        },
        "marcos": MARCOS_TXT["en"],
        "canais": {"myciber": "MyCiber", "email": "Email", "telefone": "Phone", "outro": "Other"},
    },
}


def texto_evento(evento: IncidenteEvento, locale: str | None) -> str:
    """A frase de uma linha da linha temporal, na língua pedida.

    Numa linha do sistema compõe-se a partir do código e dos parâmetros, e a
    nota que o utilizador juntou vai no fim. Uma linha sem código (escrita por
    um utilizador, ou anterior ao código) devolve o texto gravado, tal como
    está; um código que esta versão não conhece também.
    """
    if not evento.codigo:
        return evento.texto
    tx = _EVENTO_TEXTOS[lingua(locale)]
    p = json.loads(evento.params) if evento.params else {}
    if evento.codigo == _EV_REGISTADO:
        frase = tx["criado"]
    elif evento.codigo == _EV_ESTADO:
        estado = p.get("estado", "")
        frase = tx["estado"].format(estado=tx["estados"].get(estado, estado))
        em_aberto = p.get("marcos_em_aberto") or []
        if em_aberto:
            frase += " " + tx["fecho_em_aberto"].format(
                n=len(em_aberto), marcos=", ".join(tx["marcos"].get(m, m) for m in em_aberto)
            )
    elif evento.codigo == _EV_NOTIFICACAO:
        marco, canal, referencia = p.get("marco", ""), p.get("canal", ""), p.get("referencia")
        frase = tx["notificacao"].format(
            marco=tx["marcos"].get(marco, marco),
            canal=tx["canais"].get(canal, canal),
            referencia=tx["referencia"].format(ref=referencia) if referencia else "",
        )
    else:
        return evento.texto
    return f"{frase} {evento.texto}" if evento.texto else frase


def _evento(
    db: Session, inc: Incidente, tipo: TipoEventoIncidente, codigo: str, params: dict,
    nota: str, autor: Utilizador,
) -> None:
    """Acrescenta uma linha do sistema à linha temporal: o que aconteceu, em
    código e parâmetros, e a nota do utilizador à parte."""
    db.add(
        IncidenteEvento(
            incidente_id=inc.id, empresa_id=inc.empresa_id, tipo=tipo,
            codigo=codigo, params=json.dumps(params, ensure_ascii=False),
            texto=nota, autor_id=autor.id,
        )
    )


def _normalizar(campo: str, valor):
    """Texto vazio passa a nulo; datas passam a UTC."""
    if campo in _DATAS:
        return _utc(valor)
    if isinstance(valor, str):
        valor = valor.strip()
        return valor or None
    return valor


def _validar_factos(inc: Incidente, agora: datetime, locale: str | None) -> None:
    """Coerência das datas e dos campos das notificações, sobre os valores já
    aplicados ao incidente (o pedido falha e a transação reverte-se)."""
    limite = agora + _FOLGA
    conhecido = _utc(inc.conhecido_at)
    for campo in ("significativo_em", "impacto_inicio_em", "fim_impacto_em", "intercalar_pedido_em"):
        valor = _utc(getattr(inc, campo))
        if valor is not None and valor > limite:
            raise _erro(400, "data_futura", MsgsI18n.INCIDENTE_DATA_FUTURA, locale, campo=campo)
    sig = _utc(inc.significativo_em)
    if sig is not None and sig < conhecido:
        raise _erro(
            400, "data_anterior_ao_conhecimento", MsgsI18n.INCIDENTE_ANTES_DO_CONHECIMENTO,
            locale, campo="significativo_em",
        )
    inicio, fim = _utc(inc.impacto_inicio_em), _utc(inc.fim_impacto_em)
    if inicio is not None and fim is not None and fim < inicio:
        raise _erro(400, "fim_impacto_antes_do_inicio", MsgsI18n.INCIDENTE_FIM_ANTES_DO_INICIO, locale)
    if inc.representante_email and not _EMAIL_RE.match(inc.representante_email):
        raise _erro(400, "email_invalido", MsgsI18n.INCIDENTE_EMAIL_INVALIDO, locale)
    if (
        inc.utilizadores_afetados is not None and inc.utilizadores_total is not None
        and inc.utilizadores_afetados > inc.utilizadores_total
    ):
        raise _erro(400, "utilizadores_invalidos", MsgsI18n.INCIDENTE_UTILIZADORES_INVALIDOS, locale)


def _para_trilha(campo: str, valor):
    if campo in _CAMPOS_CIFRADOS:
        return "***" if valor else None
    if isinstance(valor, datetime):
        return _utc(valor).isoformat()
    return valor.value if hasattr(valor, "value") else valor


def criar_incidente(
    db: Session, empresa_id: uuid.UUID, dados, utilizador: Utilizador,
    request: Request | None = None,
) -> IncidenteSchema:
    locale = locale_de_request(request)
    if not dados.titulo.strip():
        raise HTTPException(status_code=400, detail="O título é obrigatório.")
    categoria = _validar_categoria(dados.categoria, locale)
    responsavel_id = dono_na_criacao(
        utilizador,
        "incidentes",
        _resolver_responsavel(db, empresa_id, dados.responsavel_id),
    )
    agora = datetime.now(timezone.utc)
    conhecido = _utc(dados.conhecido_at) or agora

    rjc = {c: _normalizar(c, getattr(dados, c)) for c in _CAMPOS_RJC}
    for c in _BOOLEANOS:
        rjc[c] = bool(rjc[c])
    # Registado já como significativo, sem dizer desde quando: desde que se
    # soube dele (é o lado que não empurra os prazos para a frente).
    if dados.significativo and rjc["significativo_em"] is None:
        rjc["significativo_em"] = conhecido

    inc = Incidente(
        empresa_id=empresa_id,
        titulo=dados.titulo.strip(),
        descricao=dados.descricao,
        categoria=categoria,
        severidade=dados.severidade,
        significativo=dados.significativo,
        responsavel_id=responsavel_id,
        conhecido_at=conhecido,
        ocorrido_at=_utc(dados.ocorrido_at),
        **rjc,
    )
    _validar_factos(inc, agora, locale)
    db.add(inc)
    db.flush()
    _evento(db, inc, TipoEventoIncidente.ESTADO, _EV_REGISTADO, {}, "", utilizador)

    registar_acao(
        db, acao=Acao.INCIDENTE_CRIADO, resultado=ResultadoAcao.SUCESSO,
        empresa_id=empresa_id, utilizador_id=utilizador.id,
        entidade_tipo="Incidente", entidade_id=inc.id,
        dados_novos={
            "titulo": inc.titulo, "categoria": inc.categoria,
            "severidade": inc.severidade.value, "significativo": inc.significativo,
            "significativo_em": _para_trilha("significativo_em", inc.significativo_em),
        },
        request=request,
    )
    # Algum marco pode já estar em risco (ex.: conhecido_at no passado): avisa
    # desde já quem trata dos prazos legais.
    _notificar_prazos(db, inc, agora)
    db.flush()
    db.refresh(inc)
    return _schema_de(db, inc)


def atualizar_incidente(
    db: Session, incidente_id: uuid.UUID, empresa_id: uuid.UUID, dados,
    utilizador: Utilizador, request: Request | None = None,
) -> IncidenteSchema:
    locale = locale_de_request(request)
    inc = _get_incidente(db, incidente_id, empresa_id)
    exigir_ambito(utilizador, "incidentes", ClasseAcao.OPERAR, inc.responsavel_id)
    agora = datetime.now(timezone.utc)
    campos = dados.model_dump(exclude_unset=True)
    if "categoria" in campos:
        campos["categoria"] = _validar_categoria(campos["categoria"], locale)
    anteriores: dict = {}
    if "responsavel_id" in campos:
        novo_dono = _resolver_responsavel(db, empresa_id, campos.pop("responsavel_id") or "")
        exigir_delegacao_se_muda_dono(
            utilizador, "incidentes", inc.responsavel_id, novo_dono
        )
        anteriores["responsavel_id"] = str(inc.responsavel_id) if inc.responsavel_id else None
        inc.responsavel_id = novo_dono
        campos["responsavel_id"] = str(novo_dono) if novo_dono else None
    era_significativo = inc.significativo is True
    for campo, valor in campos.items():
        if campo == "responsavel_id":
            continue
        if campo in _CAMPOS_RJC or campo == "ocorrido_at":
            valor = _normalizar(campo, valor)
            campos[campo] = valor
        anteriores[campo] = _para_trilha(campo, getattr(inc, campo))
        setattr(inc, campo, valor)
    # Passou agora a significativo sem dizer desde quando: desde agora.
    if inc.significativo is True and not era_significativo and inc.significativo_em is None:
        anteriores["significativo_em"] = None
        inc.significativo_em = agora
        campos["significativo_em"] = agora
    _validar_factos(inc, agora, locale)
    inc.updated_at = agora
    db.add(inc)
    registar_acao(
        db, acao=Acao.INCIDENTE_ATUALIZADO, resultado=ResultadoAcao.SUCESSO,
        empresa_id=empresa_id, utilizador_id=utilizador.id,
        entidade_tipo="Incidente", entidade_id=inc.id,
        dados_anteriores=anteriores or None,
        dados_novos={k: _para_trilha(k, v) for k, v in campos.items()},
        request=request,
    )
    db.flush()
    # Os factos mudaram: os prazos podem ter mudado de data, ficado dispensados
    # ou entrado na janela de aviso.
    _notificar_prazos(db, inc, agora)
    db.flush()
    db.refresh(inc)
    return _schema_de(db, inc)


def _exigir_criterios_de_fecho(
    inc: Incidente, agora: datetime, prazos: list[Prazo] | None = None
) -> None:
    """Encerrar um incidente com marcos obrigatórios por cumprir exige que fique
    escrito com que critérios se encerrou.

    **Não se bloqueia o fecho.** Há razões legítimas para encerrar com marcos por
    registar — a comunicação pode ter sido feita fora da plataforma, o incidente
    pode ter sido reclassificado. Bloquear obrigaria as pessoas a preencher
    campos a fingir para conseguirem fechar, o que é pior do que o problema.

    O que se exige é a frase que explica. O campo já existe para isto e já vai
    para o dossiê; sem ela, o dossiê mostra o incidente encerrado e a secção de
    critérios de fecho simplesmente desaparece — não diz «sem critérios», não
    diz nada. Quem lê não distingue um encerramento fundamentado de um
    esquecimento.

    Só contam os marcos obrigatórios: os voluntários (entidade fora do âmbito)
    e os dispensados (regra das 2 h, atualização não necessária) não.
    """
    if prazos is None:
        prazos = calcular_prazos(
            entrada_prazos(inc, voluntario=False, intercalar_ultimo_at=None), agora
        )
    por_cumprir = _por_cumprir(inc, prazos)
    if not por_cumprir:
        return
    if (inc.criterios_fecho or "").strip():
        return
    raise HTTPException(
        status_code=422,
        detail={
            "codigo": "criterios_fecho_obrigatorios",
            "marcos_por_cumprir": [p.marco for p in por_cumprir],
        },
    )


def alterar_estado(
    db: Session, incidente_id: uuid.UUID, empresa_id: uuid.UUID, novo: EstadoIncidente,
    nota: str, utilizador: Utilizador, request: Request | None = None,
) -> IncidenteSchema:
    inc = _get_incidente(db, incidente_id, empresa_id)
    exigir_ambito(utilizador, "incidentes", ClasseAcao.OPERAR, inc.responsavel_id)
    agora = datetime.now(timezone.utc)
    anterior = inc.estado
    prazos = prazos_do_incidente(db, inc, agora)
    if novo == EstadoIncidente.FECHADO:
        _exigir_criterios_de_fecho(inc, agora, prazos)
    inc.estado = novo
    if novo == EstadoIncidente.FECHADO:
        inc.fechado_at = agora
    else:
        inc.fechado_at = None  # reaberto: a data de fecho antiga deixa de valer
    inc.updated_at = agora
    db.add(inc)
    params: dict = {"estado": novo.value}
    if novo == EstadoIncidente.FECHADO:
        # O que ficou por cumprir entra na linha temporal, que é append-only e
        # vai para o dossiê. Um encerramento com obrigações em aberto deixa de
        # depender de alguém se lembrar de o escrever.
        params["marcos_em_aberto"] = [p.marco for p in _por_cumprir(inc, prazos)]
    _evento(db, inc, TipoEventoIncidente.ESTADO, _EV_ESTADO, params, nota.strip(), utilizador)
    registar_acao(
        db, acao=Acao.INCIDENTE_ESTADO_ALTERADO, resultado=ResultadoAcao.SUCESSO,
        empresa_id=empresa_id, utilizador_id=utilizador.id,
        entidade_tipo="Incidente", entidade_id=inc.id,
        dados_anteriores={"estado": anterior.value},
        dados_novos={"estado": novo.value},
        request=request,
    )
    db.flush()
    db.refresh(inc)
    return _schema_de(db, inc)


# ── Notificações enviadas ────────────────────────────────────────────────────

def hash_documento(doc: dict) -> str:
    """SHA-256 (hex) do JSON canónico de um documento."""
    canonico = json.dumps(doc, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonico.encode("utf-8")).hexdigest()


def registar_notificacao(
    db: Session, incidente_id: uuid.UUID, empresa_id: uuid.UUID, dados,
    utilizador: Utilizador, request: Request | None = None,
) -> IncidenteDetalheSchema:
    """Marca uma notificação como enviada e guarda a cópia do que se entregou.

    A cópia é o documento desse tipo tal como está agora (para a CNPD, o
    relatório interno), cifrada, com o hash do JSON canónico. Só de acrescentar:
    uma notificação registada não se altera nem se apaga."""
    locale = locale_de_request(request)
    tipo, canal = dados.tipo, dados.canal
    if tipo not in MARCOS:
        raise _erro(400, "tipo_notificacao_invalido", MsgsI18n.INCIDENTE_TIPO_NOTIFICACAO_INVALIDO, locale)
    if canal not in CANAIS:
        raise _erro(400, "canal_invalido", MsgsI18n.INCIDENTE_CANAL_INVALIDO, locale)
    inc = _get_incidente(db, incidente_id, empresa_id)
    exigir_ambito(utilizador, "incidentes", ClasseAcao.OPERAR, inc.responsavel_id)
    agora = datetime.now(timezone.utc)

    if tipo != "intercalar":
        ja = (
            getattr(inc, _COLUNA_MARCO[tipo]) is not None
            or db.exec(
                select(IncidenteNotificacao.id).where(
                    IncidenteNotificacao.incidente_id == inc.id,
                    IncidenteNotificacao.tipo == tipo,
                )
            ).first() is not None
        )
        if ja:
            raise _erro(409, "notificacao_ja_registada", MsgsI18n.INCIDENTE_NOTIFICACAO_JA_REGISTADA, locale)
    if tipo == "fim_impacto" and inc.fim_impacto_em is None:
        raise _erro(400, "fim_impacto_sem_data", MsgsI18n.INCIDENTE_FIM_IMPACTO_SEM_DATA, locale)
    if tipo == "cnpd" and not inc.cnpd_aplicavel:
        raise _erro(400, "cnpd_nao_aplicavel", MsgsI18n.INCIDENTE_CNPD_NAO_APLICAVEL, locale)
    enviada = _utc(dados.enviada_em) or agora
    if enviada > agora + _FOLGA:
        raise _erro(400, "data_futura", MsgsI18n.INCIDENTE_DATA_FUTURA, locale, campo="enviada_em")
    if enviada < _utc(inc.conhecido_at):
        raise _erro(
            400, "data_anterior_ao_conhecimento", MsgsI18n.INCIDENTE_ANTES_DO_CONHECIMENTO,
            locale, campo="enviada_em",
        )

    # O documento congela-se ANTES de a coluna do marco mudar: é o que se enviou.
    if tipo == "cnpd":
        doc = documento_incidente(db, inc.id, empresa_id, locale)
    else:
        doc = documento_notificacao(db, inc.id, empresa_id, tipo, locale)
    hash_hex = hash_documento(doc)
    referencia = (dados.referencia or "").strip() or None
    notif = IncidenteNotificacao(
        incidente_id=inc.id, empresa_id=empresa_id, tipo=tipo,
        enviada_em=enviada, canal=canal, referencia=referencia,
        conteudo=json.dumps(doc, ensure_ascii=False), hash_conteudo=hash_hex,
        autor_id=utilizador.id,
    )
    db.add(notif)
    if tipo in _COLUNA_MARCO:
        setattr(inc, _COLUNA_MARCO[tipo], enviada)
    inc.updated_at = agora
    db.add(inc)

    _evento(
        db, inc, TipoEventoIncidente.MARCO, _EV_NOTIFICACAO,
        {"marco": tipo, "canal": canal, "referencia": referencia}, dados.nota.strip(), utilizador,
    )
    db.flush()
    registar_acao(
        db, acao=Acao.INCIDENTE_NOTIFICACAO_REGISTADA, resultado=ResultadoAcao.SUCESSO,
        empresa_id=empresa_id, utilizador_id=utilizador.id,
        entidade_tipo="Incidente", entidade_id=inc.id,
        dados_novos={
            "notificacao_id": str(notif.id), "tipo": tipo, "canal": canal,
            "referencia": referencia, "enviada_em": enviada.isoformat(),
            "hash_conteudo": hash_hex,
        },
        request=request,
    )
    # O marco foi cumprido (ou, no intercalar, o prazo seguinte mudou): os
    # avisos que descreviam o anterior saem da lista, e se o seguinte já estiver
    # em risco nasce o aviso dele.
    _notificar_prazos(db, inc, agora)
    db.flush()
    db.refresh(inc)
    return obter_incidente(db, inc.id, empresa_id, locale)


def _resumo_notificacao(n: IncidenteNotificacao, nomes: dict) -> dict:
    return dict(
        id=n.id, tipo=n.tipo, enviada_em=n.enviada_em, canal=n.canal,
        referencia=n.referencia, hash_conteudo=n.hash_conteudo,
        autor_nome=nomes.get(n.autor_id), created_at=n.created_at,
    )


def _notificacoes(db: Session, incidente_id: uuid.UUID, empresa_id: uuid.UUID) -> list[IncidenteNotificacao]:
    return list(db.exec(
        select(IncidenteNotificacao)
        .where(
            IncidenteNotificacao.incidente_id == incidente_id,
            IncidenteNotificacao.empresa_id == empresa_id,
        )
        .order_by(IncidenteNotificacao.enviada_em.asc(), IncidenteNotificacao.created_at.asc())
    ).all())


def listar_notificacoes(
    db: Session, incidente_id: uuid.UUID, empresa_id: uuid.UUID
) -> list[NotificacaoResumoSchema]:
    _get_incidente(db, incidente_id, empresa_id)
    notifs = _notificacoes(db, incidente_id, empresa_id)
    nomes = _nomes(db, {n.autor_id for n in notifs})
    return [NotificacaoResumoSchema(**_resumo_notificacao(n, nomes)) for n in notifs]


def _conteudo(n: IncidenteNotificacao) -> dict | None:
    try:
        return json.loads(n.conteudo) if n.conteudo else None
    except ValueError:
        return None


def obter_notificacao(
    db: Session, incidente_id: uuid.UUID, empresa_id: uuid.UUID, notificacao_id: uuid.UUID
) -> NotificacaoDetalheSchema:
    _get_incidente(db, incidente_id, empresa_id)
    n = db.get(IncidenteNotificacao, notificacao_id)
    if n is None or n.incidente_id != incidente_id or n.empresa_id != empresa_id:
        raise HTTPException(status_code=404, detail="Notificação não encontrada.")
    nomes = _nomes(db, {n.autor_id})
    return NotificacaoDetalheSchema(**_resumo_notificacao(n, nomes), conteudo=_conteudo(n))


def notificacoes_para_dossie(db: Session, empresa_id: uuid.UUID) -> dict[uuid.UUID, list[dict]]:
    """As notificações enviadas de cada incidente, com o conteúdo decifrado."""
    notifs = db.exec(
        select(IncidenteNotificacao)
        .where(IncidenteNotificacao.empresa_id == empresa_id)
        .order_by(IncidenteNotificacao.enviada_em.asc(), IncidenteNotificacao.created_at.asc())
    ).all()
    nomes = _nomes(db, {n.autor_id for n in notifs})
    saida: dict[uuid.UUID, list[dict]] = {}
    for n in notifs:
        saida.setdefault(n.incidente_id, []).append({
            "id": str(n.id),
            "tipo": n.tipo,
            "enviada_em": _utc(n.enviada_em).isoformat(),
            "canal": n.canal,
            "referencia": n.referencia,
            "hash_conteudo": n.hash_conteudo,
            "autor": nomes.get(n.autor_id),
            "criado_em": _utc(n.created_at).isoformat(),
            "conteudo": _conteudo(n),
        })
    return saida


# ── Linha temporal ───────────────────────────────────────────────────────────

def adicionar_evento(
    db: Session, incidente_id: uuid.UUID, empresa_id: uuid.UUID, dados,
    utilizador: Utilizador, request: Request | None = None,
) -> EventoSchema:
    inc = _get_incidente(db, incidente_id, empresa_id)
    exigir_ambito(utilizador, "incidentes", ClasseAcao.OPERAR, inc.responsavel_id)
    if not dados.texto.strip():
        raise HTTPException(status_code=400, detail="O texto é obrigatório.")
    parte = None
    if dados.tipo == TipoEventoIncidente.COMUNICACAO:
        parte = dados.parte if dados.parte in PARTES else "outros"
    ev = IncidenteEvento(
        incidente_id=inc.id, empresa_id=empresa_id, tipo=dados.tipo,
        texto=dados.texto.strip(), parte=parte, autor_id=utilizador.id,
    )
    db.add(ev)
    inc.updated_at = datetime.now(timezone.utc)
    db.add(inc)
    registar_acao(
        db, acao=Acao.INCIDENTE_EVENTO_ADICIONADO, resultado=ResultadoAcao.SUCESSO,
        empresa_id=empresa_id, utilizador_id=utilizador.id,
        entidade_tipo="Incidente", entidade_id=inc.id,
        dados_novos={"tipo": dados.tipo.value, "parte": parte},
        request=request,
    )
    db.flush()
    db.refresh(ev)
    return EventoSchema(
        id=ev.id, tipo=ev.tipo, texto=ev.texto, parte=ev.parte,
        autor_id=ev.autor_id, autor_nome=decifrar_pii(utilizador.nome), created_at=ev.created_at,
    )


def eliminar_incidente(
    db: Session, incidente_id: uuid.UUID, empresa_id: uuid.UUID,
    utilizador: Utilizador, request: Request | None = None,
) -> None:
    inc = _get_incidente(db, incidente_id, empresa_id)
    exigir_ambito(utilizador, "incidentes", ClasseAcao.ELIMINAR, inc.responsavel_id)
    # O que já foi entregue à autoridade é prova: o incidente fica.
    tem = db.exec(
        select(IncidenteNotificacao.id).where(IncidenteNotificacao.incidente_id == inc.id)
    ).first()
    if tem is not None:
        raise _erro(
            409, "incidente_com_notificacoes", MsgsI18n.INCIDENTE_COM_NOTIFICACOES,
            locale_de_request(request),
        )
    inc.deleted_at = datetime.now(timezone.utc)
    db.add(inc)
    registar_acao(
        db, acao=Acao.INCIDENTE_ELIMINADO, resultado=ResultadoAcao.SUCESSO,
        empresa_id=empresa_id, utilizador_id=utilizador.id,
        entidade_tipo="Incidente", entidade_id=inc.id,
        dados_novos={"titulo": inc.titulo},
        request=request,
    )


# ── Avisos de prazos ─────────────────────────────────────────────────────────

def _destinatarios(db: Session, empresa_id: uuid.UUID, responsavel_id) -> list[uuid.UUID]:
    """Responsável (se houver) + administradores da empresa."""
    ids = {
        u.id
        for u in db.exec(
            select(Utilizador).where(
                Utilizador.empresa_id == empresa_id,
                Utilizador.ativo.is_(True),
                Utilizador.role.in_([RoleUtilizador.ADMIN, RoleUtilizador.SUBADMIN]),
            )
        ).all()
    }
    if responsavel_id:
        ids.add(responsavel_id)
    return list(ids)


_CODIGOS_PRAZO = (Codigo.INCIDENTE_PRAZO_RISCO, Codigo.INCIDENTE_PRAZO_ATRASO)


def _notificar_prazos(
    db: Session, inc: Incidente, agora: datetime, ctx: _Contexto | None = None
) -> int:
    """Põe os avisos de prazos do incidente de acordo com a realidade. Devolve o
    nº de notificações in-app criadas.

    - Um marco obrigatório em risco ou em atraso tem aviso. A chave inclui o
      prazo: se a base mudar (a data em que passou a significativo, o fim do
      impacto, o último intercalar), o prazo é outro e volta a avisar.
    - Os avisos de marcos cumpridos, dispensados, voluntários, sem prazo, ainda
      longe ou com o prazo mudado saem da lista.

    Ultrapassar o prazo é um evento próprio, não uma atualização do aviso
    anterior: o código muda e a notificação nova apaga a que dizia que ainda
    havia tempo. A deduplicação e essa substituição vivem no catálogo.

    Também despacha o alerta por EMAIL (um por marco e prazo; gate + dedup
    próprios)."""
    from app.notificacoes.email import alertar_prazo_incidente

    if ctx is None:
        ctx = _contexto(db, inc.empresa_id, [inc.id])
    agora = _utc(agora)
    ativos: dict[str, tuple[Prazo, str]] = {}
    if inc.estado != EstadoIncidente.FECHADO and inc.deleted_at is None:
        for p in _calcular(inc, ctx, agora):
            if _em_risco(p):
                ativos[p.marco] = (p, p.prazo.isoformat())

    # Avisos que deixaram de descrever a realidade. A chave é
    # «código:incidente:marco:prazo» — o prazo tem «:», por isso só se parte
    # nas três primeiras.
    obsoletos = []
    for n in pendentes_da_entidade(
        db, empresa_id=inc.empresa_id, entidade_id=inc.id, codigos=_CODIGOS_PRAZO,
    ):
        partes = (n.chave_dedup or "").split(":", 3)
        atual = ativos.get(partes[2]) if len(partes) == 4 else None
        if atual is None or partes[3] != atual[1]:
            obsoletos.append(n)
    if obsoletos:
        dar_por_resolvidas(db, obsoletos)

    if not ativos:
        return 0
    criadas = 0
    destinos = _destinatarios(db, inc.empresa_id, inc.responsavel_id)
    for marco, (p, prazo_iso) in ativos.items():
        alertar_prazo_incidente(
            db, empresa_id=inc.empresa_id, incidente_id=inc.id,
            titulo_incidente=inc.titulo, marco=marco, em_atraso=p.em_atraso,
            prazo=p.prazo,
        )
        codigo = (
            Codigo.INCIDENTE_PRAZO_ATRASO if p.em_atraso
            else Codigo.INCIDENTE_PRAZO_RISCO
        )
        partes = (inc.id, marco, prazo_iso)
        for uid in destinos:
            if _avisado_ha_pouco(db, uid, codigo, partes, agora):
                continue
            criada = criar_notificacao(
                db,
                empresa_id=inc.empresa_id,
                utilizador_id=uid,
                codigo=codigo,
                params={"titulo": inc.titulo, "marco": marco, "prazo": prazo_iso},
                entidade_id=inc.id,
                dedup_partes=partes,
            )
            if criada is not None:
                criadas += 1
    return criadas


# Um aviso que a pessoa marcou como lido volta enquanto o prazo continuar em
# risco — mas não a cada verificação (de hora a hora): no máximo uma vez
# neste intervalo.
_REAVISO_H = 6


def _avisado_ha_pouco(
    db: Session, utilizador_id: uuid.UUID, codigo: str, partes: tuple, agora: datetime,
) -> bool:
    return db.exec(
        select(Notificacao.id).where(
            Notificacao.utilizador_id == utilizador_id,
            Notificacao.chave_dedup == chave(codigo, *partes),
            Notificacao.created_at >= agora - timedelta(hours=_REAVISO_H),
        )
    ).first() is not None


def verificar_prazos_incidentes(db: Session) -> int:
    """Varre os incidentes abertos e põe os avisos de prazos em dia.
    Chamado por um tick periódico no arranque (ver main.py). Idempotente.

    Um incidente ainda "por avaliar" (`significativo` a NULL) conta como
    significativo: o relógio legal corre desde o conhecimento, não desde a
    classificação. Um classificado como não significativo só entra se houver
    violação de dados pessoais (o prazo da CNPD não depende do regime)."""
    agora = datetime.now(timezone.utc)
    incs = db.exec(
        select(Incidente).where(
            Incidente.deleted_at.is_(None),
            Incidente.estado != EstadoIncidente.FECHADO,
        )
    ).all()
    por_empresa: dict[uuid.UUID, list[Incidente]] = {}
    for inc in incs:
        por_empresa.setdefault(inc.empresa_id, []).append(inc)
    total = 0
    for empresa_id, lista in por_empresa.items():
        ctx = _contexto(db, empresa_id, [i.id for i in lista])
        for inc in lista:
            total += _notificar_prazos(db, inc, agora, ctx)
    # Commit incondicional: mesmo sem notificações in-app novas pode haver
    # registos de dedup de EMAIL (email_envios) ou avisos resolvidos pendentes
    # nesta transação.
    db.commit()
    return total


# ── Documentos (payload localizado; PDF gerado no cliente) ───────────────────

_REL_TEXTOS = {
    "pt": {
        "titulo": "Relatório de Incidente de Cibersegurança",
        "subtitulo": (
            "Relatório interno do incidente (Regime Jurídico da Cibersegurança e RGPD). "
            "A plataforma não submete à autoridade."
        ),
        "subtitulo_notif": "Preparado para a MyCiber: a plataforma não submete à autoridade.",
        "sec_ident": "Identificação",
        "sec_descricao": "Descrição do incidente",
        "sec_impacto": "Impacto",
        "sec_causa": "Causa",
        "sec_efeitos": "Efeitos",
        "sec_medidas": "Medidas adotadas",
        "sec_residual": "Situação residual",
        "sec_representante": "Representante para contacto da autoridade",
        "sec_marcos": "Prazos legais",
        "sec_enviadas": "Notificações enviadas",
        "sec_crono": "Cronologia",
        "sec_comunic": "Comunicações às partes interessadas",
        "sec_licoes": "Lições aprendidas",
        "sec_fecho": "Critérios de encerramento",
        "sec_conteudo": "Conteúdo da notificação",
        "l_titulo": "Título", "l_categoria": "Tipo de incidente", "l_severidade": "Severidade",
        "l_signif": "Significativo", "l_signif_em": "Significativo desde",
        "l_estado": "Estado", "l_conhecido": "Conhecido em",
        "l_ocorrido": "Ocorrido em", "l_responsavel": "Responsável",
        "l_imp_inicio": "Início do impacto significativo", "l_imp_fim": "Fim do impacto significativo",
        "l_2h": "Resolvido nas 2 horas após a deteção", "l_cnpd": "Violação de dados pessoais",
        "l_ambito": "Notificações ao abrigo do regime",
        "voluntarias": "Voluntárias (entidade fora do âmbito, RJC, art. 45.º)",
        "obrigatorias": "Obrigatórias",
        "l_utilizadores": "Utilizadores afetados", "l_zona": "Zona geográfica",
        "l_transf": "Impacto transfronteiriço", "l_paises": "Países afetados",
        "l_recuperacao": "Tempo estimado de recuperação",
        "l_incidente": "Incidente", "l_id": "Referência interna", "l_documento": "Documento",
        "l_prazo": "Prazo", "l_base_legal": "Base legal",
        "sim": "Sim", "nao": "Não", "por_avaliar": "Por avaliar", "nao_def": "—",
        "em_falta_txt": "(em falta)",
        "de": "de", "em_curso": "em curso",
        "dias": "{n} dia(s)", "horas": "{n} h", "minutos": "{n} min",
        "cr_data": "Data", "cr_tipo": "Tipo", "cr_autor": "Autor", "cr_texto": "Registo",
        "cr_parte": "Parte",
        "mc_marco": "Marco", "mc_prazo": "Prazo", "mc_estado": "Estado", "mc_base": "Base legal",
        "cumprido": "Cumprido", "em_falta": "Em falta", "em_atraso": "Em atraso",
        "dispensado": "Dispensado", "voluntario": "Voluntário", "sem_prazo": "Sem prazo",
        "provisorio": "provisório",
        "nt_tipo": "Notificação", "nt_enviada": "Enviada em", "nt_canal": "Canal",
        "nt_ref": "Referência", "nt_hash": "Hash (SHA-256)",
        "cp_campo": "Campo", "cp_valor": "Valor", "cp_ref": "Referência legal",
        "motivos": {
            "regra_2h": "resolvido em 2 h", "nao_necessaria": "não necessária",
            "sem_fim_impacto": "sem fim de impacto", "excecao_justificada": "exceção justificada",
            "relatorio_final_entregue": "relatório final entregue",
        },
        "marcos": MARCOS_TXT["pt"],
        "tipos": {"nota": "Nota", "acao": "Ação", "decisao": "Decisão", "estado": "Estado", "comunicacao": "Comunicação", "marco": "Marco"},
        "severidades": {"baixa": "Baixa", "media": "Média", "alta": "Alta", "critica": "Crítica"},
        "estados": {
            "aberto": "Aberto", "em_analise": "Em análise", "contido": "Contido",
            "resolvido": "Resolvido", "fechado": "Fechado",
        },
        "partes": {
            "pessoal": "Pessoal", "clientes": "Clientes", "autoridade": "Autoridade",
            "fornecedores": "Fornecedores", "outros": "Outros",
            "destinatarios": "Destinatários dos serviços",
            "ministerio_publico": "Ministério Público", "policia_judiciaria": "Polícia Judiciária",
            "cnpd": "CNPD", "gns": "Gabinete Nacional de Segurança",
            "autoridade_setorial": "Autoridade setorial",
        },
        "canais": _EVENTO_TEXTOS["pt"]["canais"],
    },
    "en": {
        "titulo": "Cybersecurity Incident Report",
        "subtitulo": (
            "Internal incident report (Portuguese Cybersecurity Legal Framework and GDPR). "
            "The platform does not submit to the authority."
        ),
        "subtitulo_notif": "Prepared for MyCiber: the platform does not submit to the authority.",
        "sec_ident": "Identification",
        "sec_descricao": "Incident description",
        "sec_impacto": "Impact",
        "sec_causa": "Cause",
        "sec_efeitos": "Effects",
        "sec_medidas": "Measures taken",
        "sec_residual": "Residual situation",
        "sec_representante": "Representative for contact by the authority",
        "sec_marcos": "Legal deadlines",
        "sec_enviadas": "Notifications sent",
        "sec_crono": "Timeline",
        "sec_comunic": "Communications to stakeholders",
        "sec_licoes": "Lessons learned",
        "sec_fecho": "Closure criteria",
        "sec_conteudo": "Notification content",
        "l_titulo": "Title", "l_categoria": "Incident type", "l_severidade": "Severity",
        "l_signif": "Significant", "l_signif_em": "Significant since",
        "l_estado": "Status", "l_conhecido": "Known at",
        "l_ocorrido": "Occurred at", "l_responsavel": "Owner",
        "l_imp_inicio": "Start of significant impact", "l_imp_fim": "End of significant impact",
        "l_2h": "Resolved within 2 hours of detection", "l_cnpd": "Personal data breach",
        "l_ambito": "Notifications under the framework",
        "voluntarias": "Voluntary (entity outside the scope, RJC, art. 45)",
        "obrigatorias": "Mandatory",
        "l_utilizadores": "Affected users", "l_zona": "Geographical area",
        "l_transf": "Cross-border impact", "l_paises": "Affected countries",
        "l_recuperacao": "Estimated recovery time",
        "l_incidente": "Incident", "l_id": "Internal reference", "l_documento": "Document",
        "l_prazo": "Deadline", "l_base_legal": "Legal basis",
        "sim": "Yes", "nao": "No", "por_avaliar": "To assess", "nao_def": "—",
        "em_falta_txt": "(missing)",
        "de": "of", "em_curso": "ongoing",
        "dias": "{n} day(s)", "horas": "{n} h", "minutos": "{n} min",
        "cr_data": "Date", "cr_tipo": "Type", "cr_autor": "Author", "cr_texto": "Entry",
        "cr_parte": "Stakeholder",
        "mc_marco": "Milestone", "mc_prazo": "Deadline", "mc_estado": "Status", "mc_base": "Legal basis",
        "cumprido": "Done", "em_falta": "Missing", "em_atraso": "Overdue",
        "dispensado": "Waived", "voluntario": "Voluntary", "sem_prazo": "No deadline yet",
        "provisorio": "provisional",
        "nt_tipo": "Notification", "nt_enviada": "Sent at", "nt_canal": "Channel",
        "nt_ref": "Reference", "nt_hash": "Hash (SHA-256)",
        "cp_campo": "Field", "cp_valor": "Value", "cp_ref": "Legal reference",
        "motivos": {
            "regra_2h": "resolved within 2 h", "nao_necessaria": "not needed",
            "sem_fim_impacto": "no end of impact yet", "excecao_justificada": "justified exception",
            "relatorio_final_entregue": "final report delivered",
        },
        "marcos": MARCOS_TXT["en"],
        "tipos": {"nota": "Note", "acao": "Action", "decisao": "Decision", "estado": "Status", "comunicacao": "Communication", "marco": "Milestone"},
        "severidades": {"baixa": "Low", "media": "Medium", "alta": "High", "critica": "Critical"},
        "estados": {
            "aberto": "Open", "em_analise": "Under analysis", "contido": "Contained",
            "resolvido": "Resolved", "fechado": "Closed",
        },
        "partes": {
            "pessoal": "Staff", "clientes": "Customers", "autoridade": "Authority",
            "fornecedores": "Suppliers", "outros": "Others",
            "destinatarios": "Service recipients",
            "ministerio_publico": "Public Prosecutor's Office", "policia_judiciaria": "Criminal Police (PJ)",
            "cnpd": "CNPD (data protection authority)", "gns": "National Security Office (GNS)",
            "autoridade_setorial": "Sectoral authority",
        },
        "canais": _EVENTO_TEXTOS["en"]["canais"],
    },
}


def _fmt(dt: datetime | None) -> str:
    """Data e hora em Lisboa, com o desvio para UTC escrito ao lado.

    O documento pode seguir para a autoridade: uma hora sem fuso lê-se de duas
    maneiras. Sem base de fusos no sistema, fica em UTC — e di-lo."""
    dt = _utc(dt)
    if not dt:
        return "—"
    try:
        from zoneinfo import ZoneInfo

        local = dt.astimezone(ZoneInfo("Europe/Lisbon"))
    except Exception:  # noqa: BLE001 — sem base de fusos: fica em UTC
        local = dt
    desvio = local.strftime("%z") or "+0000"
    return f"{local.strftime('%Y-%m-%d %H:%M')} (UTC{desvio[:3]}:{desvio[3:]})"


def _fmt_duracao(inicio: datetime, fim: datetime, t: dict) -> str:
    minutos = max(0, int((fim - inicio).total_seconds() // 60))
    dias, resto = divmod(minutos, 24 * 60)
    horas, mins = divmod(resto, 60)
    partes = []
    if dias:
        partes.append(t["dias"].format(n=dias))
    if horas or dias:
        partes.append(t["horas"].format(n=horas))
    partes.append(t["minutos"].format(n=mins))
    return " ".join(partes)


def _sim_nao(valor: bool | None, t: dict) -> str | None:
    if valor is None:
        return None
    return t["sim"] if valor else t["nao"]


class _Factos:
    """Os valores já formatados que os documentos usam, calculados uma vez."""

    def __init__(self, db: Session, inc: Incidente, t: dict, locale: str | None, agora: datetime):
        self.inc, self.t = inc, t
        self.categoria = (
            None if inc.categoria == taxonomia()["omissao"]
            else rotulo_categoria(inc.categoria, locale)
        )
        self.severidade = t["severidades"].get(inc.severidade.value, inc.severidade.value)
        self.inicio_detecao = inc.ocorrido_at is None
        self.inicio = _fmt(inc.ocorrido_at or inc.conhecido_at)
        inicio_dur = _utc(inc.impacto_inicio_em or inc.ocorrido_at or inc.conhecido_at)
        fim_dur = _utc(inc.fim_impacto_em)
        if fim_dur is not None and fim_dur < inicio_dur:
            # Descoberto depois de o impacto acabar e sem o início registado: a
            # duração não se sabe (não é zero). Fica em falta, a pedir o início.
            self.duracao = None
        elif fim_dur is not None:
            self.duracao = _fmt_duracao(inicio_dur, fim_dur, t)
        else:
            self.duracao = f"{_fmt_duracao(inicio_dur, agora, t)} ({t['em_curso']})"
        if inc.utilizadores_afetados is None:
            self.utilizadores = None
        elif inc.utilizadores_total is not None:
            self.utilizadores = f"{inc.utilizadores_afetados} {t['de']} {inc.utilizadores_total}"
        else:
            self.utilizadores = str(inc.utilizadores_afetados)
        self.transfronteirico = _sim_nao(inc.transfronteirico, t)
        if inc.transfronteirico and inc.paises_afetados:
            self.transfronteirico += f" ({inc.paises_afetados})"
        rep = [x for x in (inc.representante_nome, inc.representante_telefone, inc.representante_email) if x]
        self.representante = " · ".join(rep) or None

        comunic = db.exec(
            select(IncidenteEvento)
            .where(
                IncidenteEvento.incidente_id == inc.id,
                IncidenteEvento.tipo == TipoEventoIncidente.COMUNICACAO,
                IncidenteEvento.parte.in_(_PARTES_AUTORIDADES),  # type: ignore[union-attr]
            )
            .order_by(IncidenteEvento.created_at.asc())
        ).all()
        autoridades = [f"{t['partes'][e.parte]}: {_fmt(e.created_at)}" for e in comunic]
        if inc.cnpd_notificado_at is not None:
            autoridades.append(
                f"{t['partes']['cnpd']} ({REFERENCIA_MARCO[lingua(locale)]['cnpd']}): "
                f"{_fmt(inc.cnpd_notificado_at)}"
            )
        self.autoridades = "; ".join(autoridades) or None


def _ref(lg: str, art: int, n: int, al: str | None = None, sub: str | None = None) -> str:
    if lg == "en":
        return f"RJC, article {art}({n})" + (f"({al})" if al else "") + (f"({sub})" if sub else "")
    texto = f"RJC, art. {art}.º, n.º {n}"
    if al:
        texto += f", al. {al})"
    if sub:
        texto += f", subal. {sub})"
    return texto


_ROTULOS_CAMPOS = {
    "pt": {
        "representante": "Representante (nome, telefone, email), quando diferente do ponto de contacto permanente",
        "inicio": "Data e hora do início do incidente",
        "inicio_detecao": "Data e hora da deteção do incidente (início por determinar)",
        "descricao": "Descrição do incidente",
        "categoria": "Tipo de incidente (taxonomia)",
        "causa": "Causa",
        "efeitos": "Efeitos produzidos",
        "efeitos_avaliacao": "Efeitos produzidos e avaliação do impacto",
        "utilizadores_afetados": "Número de utilizadores afetados",
        "duracao": "Duração do incidente",
        "zona_geografica": "Zona geográfica afetada",
        "transfronteirico": "Impacto transfronteiriço",
        "outra_informacao": "Outra informação relevante (severidade na avaliação interna)",
        "gravidade": "Avaliação da gravidade",
        "fim_impacto": "Data e hora do fim do impacto significativo",
        "medidas": "Medidas adotadas",
        "tempo_recuperacao": "Tempo estimado para a recuperação total dos serviços",
        "inicio_impacto": "Data e hora em que o incidente assumiu o impacto significativo",
        "fim_impacto_final": "Data e hora em que o incidente perdeu o impacto significativo",
        "situacao_residual": "Situação residual do impacto",
        "outras_autoridades": "Notificação a outras autoridades (Ministério Público, CNPD, setoriais)",
    },
    "en": {
        "representante": "Representative (name, phone, email), when different from the permanent point of contact",
        "inicio": "Date and time the incident started",
        "inicio_detecao": "Date and time the incident was detected (start not determined)",
        "descricao": "Incident description",
        "categoria": "Incident type (taxonomy)",
        "causa": "Cause",
        "efeitos": "Effects",
        "efeitos_avaliacao": "Effects and impact assessment",
        "utilizadores_afetados": "Number of affected users",
        "duracao": "Duration of the incident",
        "zona_geografica": "Affected geographical area",
        "transfronteirico": "Cross-border impact",
        "outra_informacao": "Other relevant information (severity in the internal assessment)",
        "gravidade": "Severity assessment",
        "fim_impacto": "Date and time the significant impact ended",
        "medidas": "Measures taken",
        "tempo_recuperacao": "Estimated time for full recovery of services",
        "inicio_impacto": "Date and time the incident became significant",
        "fim_impacto_final": "Date and time the incident ceased to be significant",
        "situacao_residual": "Residual impact situation",
        "outras_autoridades": "Notification to other authorities (Public Prosecutor, CNPD, sectoral)",
    },
}


def _campo(chave: str, rotulo: str, valor, ref: str, *, obrigatorio: bool = True) -> dict:
    vazio = valor is None or (isinstance(valor, str) and not valor.strip())
    return {
        "chave": chave, "rotulo": rotulo, "valor": None if vazio else valor,
        "em_falta": obrigatorio and vazio, "obrigatorio": obrigatorio,
        "referencia_legal": ref,
    }


def _campos_documento(tipo: str, f: _Factos, lg: str) -> list[dict]:
    """Os campos de cada notificação, pela ordem da lei (RJC, arts. 42.º a 44.º)."""
    inc, r = f.inc, _ROTULOS_CAMPOS[lg]
    rotulo_inicio = r["inicio_detecao"] if f.inicio_detecao else r["inicio"]

    if tipo in ("notificacao_inicial", "atualizacao"):
        if tipo == "atualizacao":
            # Revê a informação do n.º 2 e junta a avaliação da gravidade e do impacto.
            def ref(al, sub=None):
                return f"{_ref(lg, 42, 3)}; {_ref(lg, 42, 2, al, sub)}"
        else:
            def ref(al, sub=None):
                return _ref(lg, 42, 2, al, sub)
        campos = [
            _campo("representante", r["representante"], f.representante, ref("a"), obrigatorio=False),
            _campo("inicio", rotulo_inicio, f.inicio, ref("b")),
            _campo("descricao", r["descricao"], inc.descricao, ref("c")),
            _campo("categoria", r["categoria"], f.categoria, ref("c")),
            _campo("causa", r["causa"], inc.causa, ref("c")),
            _campo(
                "efeitos", r["efeitos_avaliacao"] if tipo == "atualizacao" else r["efeitos"],
                inc.efeitos, ref("c"),
            ),
            _campo("utilizadores_afetados", r["utilizadores_afetados"], f.utilizadores, ref("d", "i")),
            _campo("duracao", r["duracao"], f.duracao, ref("d", "ii")),
            _campo("zona_geografica", r["zona_geografica"], inc.zona_geografica, ref("d", "iii")),
            _campo("transfronteirico", r["transfronteirico"], f.transfronteirico, ref("d", "iii")),
        ]
        if tipo == "atualizacao":
            campos.append(_campo("gravidade", r["gravidade"], f.severidade, _ref(lg, 42, 3)))
        else:
            campos.append(_campo(
                "outra_informacao", r["outra_informacao"], f.severidade, ref("d", "iv"),
                obrigatorio=False,
            ))
        return campos

    if tipo in ("fim_impacto", "intercalar"):
        art, n = (43, 2) if tipo == "fim_impacto" else (44, 4)
        campos = []
        if tipo == "fim_impacto":
            campos.append(_campo("fim_impacto", r["fim_impacto"], _fmt(inc.fim_impacto_em) if inc.fim_impacto_em else None, _ref(lg, 43, 1)))
        campos += [
            _campo("descricao", r["descricao"], inc.descricao, _ref(lg, art, n, "a")),
            _campo("categoria", r["categoria"], f.categoria, _ref(lg, art, n, "a")),
            _campo("causa", r["causa"], inc.causa, _ref(lg, art, n, "a"), obrigatorio=False),
            _campo("efeitos", r["efeitos"], inc.efeitos, _ref(lg, art, n, "a"), obrigatorio=False),
            _campo("medidas", r["medidas"], inc.medidas, _ref(lg, art, n, "b")),
            _campo("utilizadores_afetados", r["utilizadores_afetados"], f.utilizadores, _ref(lg, art, n, "c", "i")),
            _campo("duracao", r["duracao"], f.duracao, _ref(lg, art, n, "c", "ii")),
            _campo("zona_geografica", r["zona_geografica"], inc.zona_geografica, _ref(lg, art, n, "c", "iii")),
            _campo("transfronteirico", r["transfronteirico"], f.transfronteirico, _ref(lg, art, n, "c", "iii")),
            _campo("tempo_recuperacao", r["tempo_recuperacao"], inc.tempo_recuperacao, _ref(lg, art, n, "c", "iv")),
        ]
        return campos

    # Relatório final (RJC, art. 44.º, n.º 2).
    return [
        _campo("inicio_impacto", r["inicio_impacto"], _fmt(inc.impacto_inicio_em) if inc.impacto_inicio_em else None, _ref(lg, 44, 2, "a")),
        _campo("fim_impacto", r["fim_impacto_final"], _fmt(inc.fim_impacto_em) if inc.fim_impacto_em else None, _ref(lg, 44, 2, "b")),
        _campo("utilizadores_afetados", r["utilizadores_afetados"], f.utilizadores, _ref(lg, 44, 2, "c", "i")),
        _campo("duracao", r["duracao"], f.duracao, _ref(lg, 44, 2, "c", "ii")),
        _campo("zona_geografica", r["zona_geografica"], inc.zona_geografica, _ref(lg, 44, 2, "c", "iii")),
        _campo("transfronteirico", r["transfronteirico"], f.transfronteirico, _ref(lg, 44, 2, "c", "iii")),
        _campo("descricao", r["descricao"], inc.descricao, _ref(lg, 44, 2, "c", "iv")),
        _campo("categoria", r["categoria"], f.categoria, _ref(lg, 44, 2, "c", "iv")),
        _campo("causa", r["causa"], inc.causa, _ref(lg, 44, 2, "c", "iv")),
        _campo("efeitos", r["efeitos"], inc.efeitos, _ref(lg, 44, 2, "c", "iv")),
        _campo("medidas", r["medidas"], inc.medidas, _ref(lg, 44, 2, "d")),
        _campo("situacao_residual", r["situacao_residual"], inc.situacao_residual, _ref(lg, 44, 2, "e")),
        _campo("tempo_recuperacao", r["tempo_recuperacao"], inc.tempo_recuperacao, _ref(lg, 44, 2, "e", "iii")),
        _campo(
            "outras_autoridades", r["outras_autoridades"], f.autoridades, _ref(lg, 44, 2, "e", "iv"),
            obrigatorio=bool(inc.cnpd_aplicavel),
        ),
        _campo(
            "outra_informacao", r["outra_informacao"], f.severidade, _ref(lg, 44, 2, "e", "v"),
            obrigatorio=False,
        ),
    ]


def _secao_texto(titulo: str, texto: str | None, t: dict) -> dict:
    return {"titulo": titulo, "texto": texto or t["nao_def"], "cabecalho": [], "linhas": []}


def _secao_tabela(titulo: str, cabecalho: list, linhas: list) -> dict:
    return {"titulo": titulo, "texto": "", "cabecalho": cabecalho, "linhas": linhas}


def _estado_prazo(p: Prazo, t: dict) -> str:
    if p.cumprido:
        return f"{t['cumprido']} ({_fmt(p.cumprido_at)})"
    if p.dispensado:
        return f"{t['dispensado']} ({t['motivos'].get(p.motivo, p.motivo)})"
    if p.voluntario:
        return t["voluntario"]
    if p.prazo is None:
        return t["sem_prazo"]
    if p.em_atraso:
        return t["em_atraso"]
    if p.motivo == "excecao_justificada":
        return f"{t['em_falta']} ({t['motivos']['excecao_justificada']})"
    return t["em_falta"]


def _fmt_prazo(p: Prazo, t: dict) -> str:
    if p.prazo is None:
        return t["nao_def"]
    texto = _fmt(p.prazo)
    return f"{texto} ({t['provisorio']})" if p.provisorio else texto


def documento_notificacao(
    db: Session, incidente_id: uuid.UUID, empresa_id: uuid.UUID, tipo: str, locale: str | None
) -> dict:
    """Documento de uma notificação à autoridade, com os campos que a lei pede
    pela ordem da lei, cada um com a sua referência legal. Os campos
    obrigatórios vazios ficam marcados como em falta."""
    if tipo not in TIPOS_DOCUMENTO:
        raise _erro(400, "tipo_notificacao_invalido", MsgsI18n.INCIDENTE_TIPO_NOTIFICACAO_INVALIDO, locale)
    agora = datetime.now(timezone.utc)
    inc = _get_incidente(db, incidente_id, empresa_id)
    lg = lingua(locale)
    t = _REL_TEXTOS[lg]
    f = _Factos(db, inc, t, locale, agora)
    campos = _campos_documento(tipo, f, lg)
    prazo = next((p for p in prazos_do_incidente(db, inc, agora) if p.marco == tipo), None)

    ident = [
        f"{t['l_incidente']}: {inc.titulo}",
        f"{t['l_id']}: {inc.id}",
        f"{t['l_documento']}: {MARCOS_TXT[lg][tipo]}",
        f"{t['l_base_legal']}: {REFERENCIA_MARCO[lg][tipo]}",
    ]
    if prazo is not None:
        ident.append(f"{t['l_prazo']}: {_fmt_prazo(prazo, t)}; {_estado_prazo(prazo, t)}")
    linhas = [
        [c["rotulo"], c["valor"] if c["valor"] is not None else (t["em_falta_txt"] if c["em_falta"] else t["nao_def"]), c["referencia_legal"]]
        for c in campos
    ]
    return {
        "titulo": MARCOS_TXT[lg][tipo],
        "subtitulo": t["subtitulo_notif"],
        "data_geracao": agora.isoformat(),
        "tipo": tipo,
        "incidente_id": str(inc.id),
        "referencia_legal": REFERENCIA_MARCO[lg][tipo],
        "secoes": [
            _secao_texto(t["sec_ident"], "\n".join(ident), t),
            _secao_tabela(t["sec_conteudo"], [t["cp_campo"], t["cp_valor"], t["cp_ref"]], linhas),
        ],
        "campos": campos,
        "em_falta": sum(1 for c in campos if c["em_falta"]),
    }


def documento_incidente(
    db: Session, incidente_id: uuid.UUID, empresa_id: uuid.UUID, locale: str | None
) -> dict:
    """Relatório interno completo do incidente (mesma forma dos documentos premium)."""
    agora = datetime.now(timezone.utc)
    inc = _get_incidente(db, incidente_id, empresa_id)
    lg = lingua(locale)
    t = _REL_TEXTOS[lg]
    f = _Factos(db, inc, t, locale, agora)
    ctx = _contexto(db, empresa_id, [inc.id])

    eventos = db.exec(
        select(IncidenteEvento)
        .where(IncidenteEvento.incidente_id == incidente_id)
        .order_by(IncidenteEvento.created_at.asc())
    ).all()
    enviadas = _notificacoes(db, inc.id, empresa_id)
    nomes = _nomes(db, {inc.responsavel_id} | {e.autor_id for e in eventos})

    signif = (
        t["sim"] if inc.significativo else t["nao"] if inc.significativo is False else t["por_avaliar"]
    )
    ident = [
        f"{t['l_titulo']}: {inc.titulo}",
        f"{t['l_categoria']}: {rotulo_categoria(inc.categoria, locale)}",
        f"{t['l_severidade']}: {f.severidade}",
        f"{t['l_signif']}: {signif}",
    ]
    if inc.significativo_em:
        ident.append(f"{t['l_signif_em']}: {_fmt(inc.significativo_em)}")
    ident += [
        f"{t['l_estado']}: {t['estados'].get(inc.estado.value, inc.estado.value)}",
        f"{t['l_conhecido']}: {_fmt(inc.conhecido_at)}",
        f"{t['l_ocorrido']}: {_fmt(inc.ocorrido_at)}",
        f"{t['l_imp_inicio']}: {_fmt(inc.impacto_inicio_em)}",
        f"{t['l_imp_fim']}: {_fmt(inc.fim_impacto_em)}",
        f"{t['l_2h']}: {_sim_nao(bool(inc.resolvido_2h), t)}",
        f"{t['l_cnpd']}: {_sim_nao(bool(inc.cnpd_aplicavel), t)}",
        f"{t['l_ambito']}: {t['voluntarias'] if ctx.voluntario else t['obrigatorias']}",
        f"{t['l_responsavel']}: {nomes.get(inc.responsavel_id) or t['nao_def']}",
    ]
    impacto = "\n".join([
        f"{t['l_utilizadores']}: {f.utilizadores or t['nao_def']}",
        f"{t['l_zona']}: {inc.zona_geografica or t['nao_def']}",
        f"{t['l_transf']}: {f.transfronteirico or t['nao_def']}",
        f"{t['l_recuperacao']}: {inc.tempo_recuperacao or t['nao_def']}",
    ])

    marcos_linhas = [
        [
            t["marcos"].get(p.marco, p.marco),
            _fmt_prazo(p, t),
            _estado_prazo(p, t),
            REFERENCIA_MARCO[lg].get(p.marco, ""),
        ]
        for p in _calcular(inc, ctx, agora)
    ]
    enviadas_linhas = [
        [
            t["marcos"].get(n.tipo, n.tipo), _fmt(n.enviada_em),
            t["canais"].get(n.canal, n.canal), n.referencia or t["nao_def"], n.hash_conteudo,
        ]
        for n in enviadas
    ]
    crono_linhas = [
        [
            _fmt(e.created_at), t["tipos"].get(e.tipo.value, e.tipo.value),
            nomes.get(e.autor_id) or t["nao_def"], texto_evento(e, locale),
        ]
        for e in eventos
    ]
    comunic_linhas = [
        [_fmt(e.created_at), t["partes"].get(e.parte, e.parte) if e.parte else t["nao_def"], e.texto]
        for e in eventos if e.tipo == TipoEventoIncidente.COMUNICACAO
    ]

    secoes = [
        _secao_texto(t["sec_ident"], "\n".join(ident), t),
        _secao_texto(t["sec_descricao"], inc.descricao, t),
        _secao_texto(t["sec_impacto"], impacto, t),
    ]
    for chave, valor in (
        ("sec_causa", inc.causa), ("sec_efeitos", inc.efeitos),
        ("sec_medidas", inc.medidas), ("sec_residual", inc.situacao_residual),
        ("sec_representante", f.representante),
    ):
        if valor:
            secoes.append(_secao_texto(t[chave], valor, t))
    if marcos_linhas:
        secoes.append(_secao_tabela(
            t["sec_marcos"], [t["mc_marco"], t["mc_prazo"], t["mc_estado"], t["mc_base"]], marcos_linhas,
        ))
    if enviadas_linhas:
        secoes.append(_secao_tabela(
            t["sec_enviadas"],
            [t["nt_tipo"], t["nt_enviada"], t["nt_canal"], t["nt_ref"], t["nt_hash"]],
            enviadas_linhas,
        ))
    if crono_linhas:
        secoes.append(_secao_tabela(t["sec_crono"], [t["cr_data"], t["cr_tipo"], t["cr_autor"], t["cr_texto"]], crono_linhas))
    if comunic_linhas:
        secoes.append(_secao_tabela(t["sec_comunic"], [t["cr_data"], t["cr_parte"], t["cr_texto"]], comunic_linhas))
    if inc.licoes_aprendidas:
        secoes.append(_secao_texto(t["sec_licoes"], inc.licoes_aprendidas, t))
    if inc.criterios_fecho:
        secoes.append(_secao_texto(t["sec_fecho"], inc.criterios_fecho, t))

    return {
        "titulo": t["titulo"],
        "subtitulo": t["subtitulo"],
        "data_geracao": agora.isoformat(),
        "secoes": secoes,
        # Controlos a que o relatório dá evidência (o "anexar como evidência"
        # liga ao primeiro que existir no framework do tenant).
        "controlos": ["RS.GI-1", "RS.GI-3"],
    }
