"""
Router de auditoria — NIS2PME.
Expõe o histórico de ações da empresa ao admin/auditor.

Prefixo base: /api (incluído em main.py)
Prefixo do router: /audit-logs

## O que este router NÃO faz

Não compõe texto para o ecrã. Devolve o código da ação, a família, a severidade e
a lista de campos alterados; quem traduz e desenha é o frontend. A versão anterior
montava aqui frases como «estado: sim → não», com as palavras em português dentro
do backend, e a mesma lógica existia outra vez em JavaScript.

A exceção é o `alvo`: para o construir é preciso decifrar dados que só existem
deste lado.

## Coerência dos indicadores

Os indicadores vivem em `/resumo` e aceitam EXATAMENTE os mesmos filtros da
listagem. É isso que faz um número significar o mesmo conjunto que a tabela
mostra — antes quatro dos cinco ignoravam os filtros e ficavam parados enquanto
a tabela mudava debaixo deles.
"""
import csv
import io
import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Iterator, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import distinct, func, or_
from sqlmodel import Session, select

from app.auth.models import Utilizador
from app.config import get_settings
from app.shared.anonimizacao import mascarar_pii_em_dados
from app.shared.audit import Acao, AuditLog, ResultadoAcao, registar_acao
from app.shared.audit_catalogo import (
    CATALOGO,
    CODIGOS_FALHA_SEGURANCA,
    FAMILIAS,
    codigos_da_familia,
    definicao,
    entidade_apresentacao,
    entidades_armazenadas,
    resolver_codigo,
)
from app.shared.capacidades import ClasseAcao, require_capability
from app.shared.dependencies import (
    SessionDep,
    get_current_user,
)
from app.shared.hashes import hash_ip_auditoria
from app.shared.i18n import MsgsI18n, locale_de_request, traduzir
from app.shared.pii import decifrar_pii
from app.shared.utils import celula_csv

router = APIRouter(prefix="/audit-logs", tags=["Auditoria"])

AdminOuAuditorDep = Depends(
    require_capability("auditoria", ClasseAcao.VER)
)

# Alterações incluídas em cada linha da listagem. O detalhe traz todas; aqui há
# um teto para uma ação que mexeu em trinta campos não inchar a página inteira.
MAX_ALTERACOES_LISTA = 5

# Linhas lidas por cada volta do gerador da exportação.
LOTE_EXPORTACAO = 1000

Ordem = Literal["recentes", "antigos"]
Resultado = Literal["sucesso", "falha"]


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class Alteracao(BaseModel):
    campo: str
    antes: Any | None = None
    depois: Any | None = None


class AuditLogSchema(BaseModel):
    """Detalhe de um registo — inclui o JSON cru para a vista técnica."""

    id: uuid.UUID
    created_at: datetime
    utilizador_id: uuid.UUID | None = None
    utilizador_nome: str | None = None
    acao: str
    acao_canonica: str
    familia: str
    severidade: str
    entidade_tipo: str | None = None
    entidade_id: uuid.UUID | None = None
    alteracoes: list[Alteracao] = []
    dados_anteriores: str | None = None
    dados_novos: str | None = None
    ip_address: str | None = None
    user_agent: str | None = None
    resultado: str


class AuditLogResumoSchema(BaseModel):
    id: uuid.UUID
    created_at: datetime
    utilizador_id: uuid.UUID | None = None
    utilizador_nome: str | None = None
    # `acao` é o código tal como está gravado; `acao_canonica` é o que ele
    # significa hoje. Diferem apenas nas linhas anteriores a uma correção.
    acao: str
    acao_canonica: str
    familia: str
    severidade: str
    entidade_tipo: str | None = None
    entidade_id: uuid.UUID | None = None
    alvo: str | None = None
    alteracoes: list[Alteracao] = []
    alteracoes_total: int = 0
    ip_address: str | None = None
    resultado: str


class ListaAuditLogsSchema(BaseModel):
    total: int
    logs: list[AuditLogResumoSchema]


class DefinicaoAcaoSchema(BaseModel):
    codigo: str
    familia: str
    entidade: str
    severidade: str


class CatalogoSchema(BaseModel):
    familias: list[str]
    acoes: list[DefinicaoAcaoSchema]


class PeriodoSchema(BaseModel):
    inicio: datetime | None = None
    fim: datetime | None = None


class ContagemFamiliaSchema(BaseModel):
    familia: str
    n: int


class ResumoAuditSchema(BaseModel):
    periodo: PeriodoSchema
    total: int
    por_resultado: dict[str, int]
    autenticacao: dict[str, int]
    ips_distintos: int
    # Linhas gravadas antes de existir impressão do endereço. Sem este número,
    # `ips_distintos` seria menor do que a realidade sem o dizer.
    ips_sem_hash: int
    top_familias: list[ContagemFamiliaSchema]


class MesArquivadoSchema(BaseModel):
    mes: str
    ficheiro: str
    bytes: int


class RetencaoSchema(BaseModel):
    retencao_dias: int
    mais_antigo_em_linha: datetime | None = None
    meses_arquivados: list[MesArquivadoSchema]


OFFSET_COM_ESPACO_RE = re.compile(
    r"^(?P<prefixo>.+T.+)\s(?P<offset>\d{2}:\d{2})$"
)


# ---------------------------------------------------------------------------
# Leitura de valores gravados
# ---------------------------------------------------------------------------


def _parse_json_seguro(valor: str | None) -> Any:
    if not valor:
        return None
    try:
        return json.loads(valor)
    except (TypeError, ValueError):
        return None


def _parse_datetime_query(
    valor: str | None,
    campo: str,
    request: Request,
) -> datetime | None:
    if valor is None:
        return None

    normalizado = valor.strip()
    if not normalizado:
        return None

    if normalizado.endswith("Z"):
        normalizado = f"{normalizado[:-1]}+00:00"

    match = OFFSET_COM_ESPACO_RE.match(normalizado)
    if match:
        normalizado = (
            f"{match.group('prefixo')}+{match.group('offset')}"
        )

    try:
        parsed = datetime.fromisoformat(normalizado)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=traduzir(
                MsgsI18n.AUDIT_DATA_INVALIDA, locale_de_request(request), campo=campo
            ),
        ) from exc

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    return parsed


def _chave_tecnica(chave: str) -> bool:
    """
    Chave que só diz alguma coisa a uma máquina: identificadores, listas de
    identificadores e impressões digitais de conteúdo.

    É a regra do que a vista amigável esconde pelo NOME do campo; o que esconde
    pelo VALOR (um identificador, seja qual for o nome) está em
    `_limpar_objeto_para_ui`. A vista técnica do detalhe mostra o JSON inteiro,
    por isso nada disto se perde — só deixa de competir com o que uma pessoa
    precisa de ler.
    """
    return (
        chave == "id"
        or chave.endswith(("_id", "_ids", "_hash"))
        or chave.startswith("hash_")
        or chave == "sha256"
    )


def _is_uuid_like(valor: Any) -> bool:
    if not isinstance(valor, str):
        return False
    try:
        uuid.UUID(valor)
    except ValueError:
        return False
    return True


def _limpar_objeto_para_ui(valor: Any) -> Any:
    """
    Tira o que não diz nada a quem lê o registo: as chaves técnicas e os valores
    que são identificadores.

    Um campo cujo valor é um identificador (a referência de um dossiê, as
    dependências de um ativo) sai por inteiro, em vez de ficar «Campo: —», que
    só mostra que o campo existe. Vale também dentro de um valor que é um
    dicionário ou uma lista.
    """
    if isinstance(valor, list):
        return [_limpar_objeto_para_ui(item) for item in valor if not _is_uuid_like(item)]
    if isinstance(valor, dict):
        return {
            chave: _limpar_objeto_para_ui(item)
            for chave, item in valor.items()
            if not _chave_tecnica(chave) and not _is_uuid_like(item)
        }
    return valor


def _iguais(a: Any, b: Any) -> bool:
    return json.dumps(a, sort_keys=True, default=str) == json.dumps(
        b, sort_keys=True, default=str
    )


def _vazio(valor: Any) -> bool:
    """Nada que valha a pena mostrar: ausente, texto vazio, lista ou dict vazios."""
    return valor is None or valor == "" or valor == {} or valor == []


def _calcular_alteracoes(
    dados_ant: str | None, dados_nov: str | None
) -> list[Alteracao]:
    """Diferença campo a campo entre o estado anterior e o novo."""
    anteriores = _limpar_objeto_para_ui(_parse_json_seguro(dados_ant) or {})
    novos = _limpar_objeto_para_ui(_parse_json_seguro(dados_nov) or {})
    if not isinstance(anteriores, dict):
        anteriores = {}
    if not isinstance(novos, dict):
        novos = {}

    # Numa atualização o "antes" guarda o estado completo e o "depois" só o que
    # mudou: uma chave que falta no "depois" não mudou. Com a união das duas,
    # «Dimensão: pequena → —» dizia que se tinha apagado a dimensão de uma empresa
    # em que ninguém mexeu. Só sem "depois" nenhum (uma eliminação) é que tudo o
    # que havia passa a nada.
    chaves = list(novos.keys()) if novos else list(anteriores.keys())
    alteracoes = [
        Alteracao(campo=chave, antes=anteriores.get(chave), depois=novos.get(chave))
        for chave in chaves
        # Numa criação não há estado anterior, portanto TODOS os campos "mudam".
        # Sem esta guarda, um campo que ficou por preencher aparecia como uma
        # alteração de nada para nada — ruído que empurrava para fora da linha
        # aquilo que realmente aconteceu.
        if not _iguais(anteriores.get(chave), novos.get(chave))
        and not (_vazio(anteriores.get(chave)) and _vazio(novos.get(chave)))
    ]
    if alteracoes:
        return alteracoes

    # Sem diferença calculável (criação com valores nulos, por exemplo): mostra
    # o que ficou registado, para a linha não aparecer vazia.
    return [
        Alteracao(campo=chave, depois=valor)
        for chave, valor in novos.items()
        if not _vazio(valor)
    ]


def _log_referencia_anonimizado(
    log: AuditLog,
    anonimizados_ids: set[uuid.UUID],
) -> bool:
    """Verifica se o log referencia um utilizador anonimizado.

    Verifica três vetores:
    1. O actor (utilizador_id) foi anonimizado.
    2. A entidade do log é um Utilizador anonimizado.
    3. Algum campo *_id nos JSON do log aponta para um utilizador anonimizado
       (ex: implementador_id em logs de delegação).
    """
    if not anonimizados_ids:
        return False
    if log.utilizador_id and log.utilizador_id in anonimizados_ids:
        return True
    if (
        log.entidade_tipo == "Utilizador"
        and log.entidade_id is not None
        and log.entidade_id in anonimizados_ids
    ):
        return True
    for dados_json in (log.dados_anteriores, log.dados_novos):
        dados = _parse_json_seguro(dados_json)
        if isinstance(dados, dict):
            for chave, valor in dados.items():
                if chave.endswith("_id") and _is_uuid_like(valor):
                    try:
                        if uuid.UUID(valor) in anonimizados_ids:
                            return True
                    except ValueError:
                        pass
    return False


def _uuids_referenciados(logs: list[AuditLog]) -> set[uuid.UUID]:
    """Todos os utilizadores que um lote de registos toca, direta ou indiretamente."""
    ids: set[uuid.UUID] = set()
    for log in logs:
        if log.utilizador_id is not None:
            ids.add(log.utilizador_id)
        if log.entidade_tipo == "Utilizador" and log.entidade_id is not None:
            ids.add(log.entidade_id)
        for dados_json in (log.dados_anteriores, log.dados_novos):
            dados = _parse_json_seguro(dados_json)
            if isinstance(dados, dict):
                for chave, valor in dados.items():
                    if chave.endswith("_id") and _is_uuid_like(valor):
                        try:
                            ids.add(uuid.UUID(valor))
                        except ValueError:
                            pass
    return ids


def _contexto_do_lote(
    db: Session, logs: list[AuditLog]
) -> tuple[dict[uuid.UUID, Utilizador], set[uuid.UUID]]:
    """
    Utilizadores referenciados pelo lote e, desses, os que estão anonimizados.

    Uma consulta por lote, não uma por linha — e é a mesma para a listagem e para
    a exportação, para que o direito ao apagamento valha nas duas. Uma exportação
    que trouxesse a PII que a listagem esconde seria a forma mais fácil de
    contornar a anonimização.
    """
    ids = _uuids_referenciados(logs)
    if not ids:
        return {}, set()
    utilizadores = {
        u.id: u
        for u in db.exec(
            select(Utilizador).where(Utilizador.id.in_(list(ids)))  # type: ignore[attr-defined]
        ).all()
    }
    anonimizados = {
        uid for uid, u in utilizadores.items() if u.anonimizado_at is not None
    }
    return utilizadores, anonimizados


def _dados_visiveis(
    log: AuditLog, anonimizados: set[uuid.UUID]
) -> tuple[str | None, str | None]:
    if _log_referencia_anonimizado(log, anonimizados):
        return (
            mascarar_pii_em_dados(log.dados_anteriores),
            mascarar_pii_em_dados(log.dados_novos),
        )
    return log.dados_anteriores, log.dados_novos


def _get_resumo_alvo(
    log: AuditLog, dados_ant: str | None, dados_nov: str | None
) -> str | None:
    """
    Aquilo sobre que a ação incidiu, quando o registo o sabe dizer.

    Devolve `None` — e não um travessão — quando não há nome: quem apresenta é
    que decide o que mostrar em vez dele, e um travessão vindo daqui obrigava a
    tratá-lo como se fosse um nome.
    """
    novos = _parse_json_seguro(dados_nov) or {}
    anteriores = _parse_json_seguro(dados_ant) or {}
    if not isinstance(novos, dict):
        novos = {}
    if not isinstance(anteriores, dict):
        anteriores = {}

    if log.acao == Acao.UTILIZADOR_DELEGACAO_ATRIBUIDA:
        controlo = novos.get("controlo_codigo") or anteriores.get(
            "controlo_codigo"
        ) or "—"
        implementador = (
            novos.get("implementador_nome")
            or novos.get("implementador_email")
            or "—"
        )
        return f"{controlo} → {implementador}"

    if log.acao.startswith("evidencia."):
        return (
            novos.get("titulo")
            or anteriores.get("titulo")
            or novos.get("ficheiro_nome")
            or anteriores.get("ficheiro_nome")
            or None
        )

    # Campos que identificam o alvo, por ordem de preferência. `interruptor` está
    # cá porque uma alteração de política grava qual foi o interruptor mexido e
    # nada mais o identifica — sem isto, todas as alterações de política ficavam
    # sem referência e não se distinguiam umas das outras.
    for campo in (
        "nome", "titulo", "controlo_codigo", "ficheiro_nome", "interruptor",
    ):
        valor = novos.get(campo) or anteriores.get(campo)
        if valor:
            return valor

    # email só como fallback final; se foi mascarado, não é informativo — omite
    email = novos.get("email") or anteriores.get("email")
    if email and email != "[anonimizado]":
        return email
    return None


# ---------------------------------------------------------------------------
# Filtros — os mesmos para listar, contar e exportar
# ---------------------------------------------------------------------------


def _criterios(
    *,
    empresa_id: uuid.UUID | None,
    q: str | None = None,
    data_inicio: datetime | None = None,
    data_fim: datetime | None = None,
    acao: str | None = None,
    familia: str | None = None,
    resultado: str | None = None,
    entidade_tipo: str | None = None,
    entidade_id: uuid.UUID | None = None,
    utilizador_id: uuid.UUID | None = None,
    ip: str | None = None,
) -> list:
    """
    Constrói a cláusula de filtro. Uma só função para os três endpoints: se cada
    um montasse a sua, os indicadores voltariam a contar um conjunto diferente
    daquele que a tabela mostra.
    """
    criterios: list = [
        AuditLog.empresa_id == empresa_id,
        # A renovação de sessão acontece a cada poucos minutos e não é uma ação
        # de ninguém — afogaria tudo o resto.
        AuditLog.acao != Acao.REFRESH_TOKEN,
    ]

    if familia:
        criterios.append(AuditLog.acao.in_(codigos_da_familia(familia)))  # type: ignore[union-attr]
    if acao:
        criterios.append(AuditLog.acao.contains(acao))  # type: ignore[union-attr]
    if q:
        termo = f"%{q.strip()}%"
        criterios.append(
            or_(
                AuditLog.acao.ilike(termo),
                AuditLog.entidade_tipo.ilike(termo),
                AuditLog.dados_anteriores.ilike(termo),
                AuditLog.dados_novos.ilike(termo),
            )
        )
    if data_inicio is not None:
        criterios.append(AuditLog.created_at >= data_inicio)
    if data_fim is not None:
        criterios.append(AuditLog.created_at <= data_fim)
    if resultado:
        criterios.append(AuditLog.resultado == resultado)
    if entidade_tipo:
        criterios.append(
            AuditLog.entidade_tipo.in_(entidades_armazenadas(entidade_tipo))  # type: ignore[union-attr]
        )
    if entidade_id:
        criterios.append(AuditLog.entidade_id == entidade_id)
    if utilizador_id:
        criterios.append(AuditLog.utilizador_id == utilizador_id)
    if ip:
        # Comparação exata sobre a impressão indexada. O endereço está cifrado
        # com um esquema não determinístico: procurá-lo por semelhança no texto
        # cifrado devolveria sempre zero, e decifrar a tabela para comparar seria
        # ler tudo a cada pesquisa.
        criterios.append(AuditLog.ip_hash == hash_ip_auditoria(ip.strip()))

    return criterios


def _validar_intervalo(
    data_inicio: datetime | None, data_fim: datetime | None
) -> None:
    if data_inicio is not None and data_fim is not None and data_fim < data_inicio:
        raise HTTPException(
            status_code=400,
            detail="A data final não pode ser anterior à data inicial.",
        )


def _agregar(db: Session, criterios: list) -> tuple[int, dict, dict, dict]:
    """
    Contagens do conjunto filtrado: total, repartição por resultado, falhas de
    autenticação e peso de cada família.

    Uma única agregação dá tudo — o número de códigos distintos é pequeno e
    limitado pelo catálogo, por isso somar em Python é mais barato do que
    percorrer a tabela quatro vezes, que era o que a versão anterior fazia.
    """
    linhas = db.exec(
        select(AuditLog.acao, AuditLog.resultado, func.count())
        .where(*criterios)
        .group_by(AuditLog.acao, AuditLog.resultado)  # type: ignore[arg-type]
    ).all()

    total = 0
    por_resultado = {ResultadoAcao.SUCESSO.value: 0, ResultadoAcao.FALHA.value: 0}
    autenticacao = {"sucessos": 0, "falhas": 0}
    por_familia: dict[str, int] = {}

    for codigo, res, n in linhas:
        n = int(n)
        total += n
        chave = getattr(res, "value", res)
        por_resultado[chave] = por_resultado.get(chave, 0) + n
        familia = definicao(codigo).familia
        por_familia[familia] = por_familia.get(familia, 0) + n
        # O conjunto que conta como falha de acesso vem do catálogo. A versão
        # anterior era uma lista escrita à mão que apanhava a password errada e
        # esquecia a conta bloqueada e o endereço travado — precisamente os dois
        # sinais que interessam num ataque.
        if resolver_codigo(codigo) in CODIGOS_FALHA_SEGURANCA:
            autenticacao["falhas"] += n
        elif codigo == Acao.LOGIN_SUCESSO:
            autenticacao["sucessos"] += n

    return total, por_resultado, autenticacao, por_familia


def _contar_ips(db: Session, criterios: list) -> tuple[int, int]:
    """Endereços distintos e linhas antigas que ainda não têm impressão."""
    distintos: int = db.exec(
        select(func.count(distinct(AuditLog.ip_hash))).where(
            *criterios, AuditLog.ip_hash.isnot(None)  # type: ignore[union-attr]
        )
    ).one()  # type: ignore[assignment]
    sem_hash: int = db.exec(
        select(func.count()).select_from(AuditLog).where(
            *criterios,
            AuditLog.ip_hash.is_(None),  # type: ignore[union-attr]
            AuditLog.ip_address.isnot(None),  # type: ignore[union-attr]
        )
    ).one()  # type: ignore[assignment]
    return distintos, sem_hash


def _validar_familia(familia: str | None, request: Request) -> None:
    if familia and familia not in FAMILIAS:
        raise HTTPException(
            status_code=422,
            detail=traduzir(
                MsgsI18n.AUDIT_FAMILIA_DESCONHECIDA,
                locale_de_request(request),
                familia=familia,
            ),
        )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get(
    "",
    response_model=ListaAuditLogsSchema,
    summary="Listar registos de auditoria da empresa (admin/auditor)",
    dependencies=[AdminOuAuditorDep],
)
def listar_audit_logs(
    request: Request,
    db: SessionDep,
    utilizador_atual: Utilizador = Depends(get_current_user),
    q: str | None = Query(None, description="Pesquisa textual simples"),
    data_inicio_raw: str | None = Query(
        None,
        alias="data_inicio",
        description="Filtrar registos criados a partir desta data/hora",
    ),
    data_fim_raw: str | None = Query(
        None,
        alias="data_fim",
        description="Filtrar registos criados até esta data/hora",
    ),
    acao: str | None = Query(
        None, description="Filtrar por fragmento de código de ação, ex: 'login'"
    ),
    familia: str | None = Query(
        None, description="Filtrar por família de ações (ver /audit-logs/catalogo)"
    ),
    resultado: Resultado | None = Query(None, description="'sucesso' ou 'falha'"),
    entidade_tipo: str | None = Query(
        None,
        description=(
            "Filtrar por entidade afetada. Aceita o nome apresentado ('Controlo') "
            "ou o nome interno ('ControloEmpresaV2')."
        ),
    ),
    entidade_id: uuid.UUID | None = Query(
        None, description="Filtrar por ID exato da entidade"
    ),
    utilizador_id: uuid.UUID | None = Query(
        None, description="Filtrar por utilizador que executou a ação"
    ),
    ip: str | None = Query(
        None, description="Filtrar por endereço IP exato"
    ),
    ordem: Ordem = Query("recentes", description="'recentes' ou 'antigos'"),
    limite: int = Query(100, le=500, ge=1),
    offset: int = Query(0, ge=0),
):
    """
    Devolve os registos de auditoria da empresa autenticada.
    Os logs são imutáveis por design — retenção mínima 12 meses
    (RJC).
    """
    data_inicio = _parse_datetime_query(data_inicio_raw, "data_inicio", request)
    data_fim = _parse_datetime_query(data_fim_raw, "data_fim", request)
    _validar_intervalo(data_inicio, data_fim)
    _validar_familia(familia, request)

    criterios = _criterios(
        empresa_id=utilizador_atual.empresa_id,
        q=q,
        data_inicio=data_inicio,
        data_fim=data_fim,
        acao=acao,
        familia=familia,
        resultado=resultado,
        entidade_tipo=entidade_tipo,
        entidade_id=entidade_id,
        utilizador_id=utilizador_id,
        ip=ip,
    )

    total: int = db.exec(
        select(func.count()).select_from(AuditLog).where(*criterios)
    ).one()  # type: ignore[assignment]

    # Desempate por id: uma só operação escreve vários registos no mesmo commit,
    # portanto há created_at repetidos. Sem segunda coluna a ordenação não é
    # determinística e a paginação repetiria umas linhas e saltaria outras.
    coluna = AuditLog.created_at.desc() if ordem == "recentes" else AuditLog.created_at.asc()  # type: ignore[union-attr]
    segunda = AuditLog.id.desc() if ordem == "recentes" else AuditLog.id.asc()  # type: ignore[union-attr]

    logs = list(
        db.exec(
            select(AuditLog)
            .where(*criterios)
            .order_by(coluna, segunda)
            .offset(offset)
            .limit(limite)
        ).all()
    )

    utilizadores_map, anonimizados = _contexto_do_lote(db, logs)

    resultado_final: list[AuditLogResumoSchema] = []
    for log in logs:
        nome: str | None = None
        if log.utilizador_id:
            u = utilizadores_map.get(log.utilizador_id)
            if u:
                nome = decifrar_pii(u.nome)

        dados_ant, dados_nov = _dados_visiveis(log, anonimizados)
        alteracoes = _calcular_alteracoes(dados_ant, dados_nov)
        definicao_acao = definicao(log.acao, log.entidade_tipo)
        alvo = _get_resumo_alvo(log, dados_ant, dados_nov)

        resultado_final.append(
            AuditLogResumoSchema(
                id=log.id,
                created_at=log.created_at,
                utilizador_id=log.utilizador_id,
                utilizador_nome=nome,
                acao=log.acao,
                acao_canonica=definicao_acao.codigo,
                familia=definicao_acao.familia,
                severidade=definicao_acao.severidade,
                entidade_tipo=entidade_apresentacao(log.entidade_tipo),
                entidade_id=log.entidade_id,
                alvo=alvo,
                alteracoes=alteracoes[:MAX_ALTERACOES_LISTA],
                alteracoes_total=len(alteracoes),
                ip_address=decifrar_pii(log.ip_address),
                resultado=log.resultado,
            )
        )

    # Sem indicadores nesta resposta: eles vivem em `/resumo`, com os mesmos
    # filtros. Separar as duas coisas é o que evita recontar a tabela inteira a
    # cada mudança de página.
    return ListaAuditLogsSchema(total=total, logs=resultado_final)


@router.get(
    "/catalogo",
    response_model=CatalogoSchema,
    summary="Vocabulário das ações de auditoria",
    dependencies=[AdminOuAuditorDep],
)
def obter_catalogo():
    """
    Todas as ações que podem aparecer na trilha, com família e severidade.

    O seletor de filtro é construído a partir daqui. Antes era uma lista escrita
    à mão no frontend, e por isso havia famílias inteiras de ações que nenhuma
    opção do filtro alcançava.
    """
    return CatalogoSchema(
        familias=list(FAMILIAS),
        acoes=[
            DefinicaoAcaoSchema(
                codigo=d.codigo,
                familia=d.familia,
                entidade=d.entidade,
                severidade=d.severidade,
            )
            for d in sorted(CATALOGO.values(), key=lambda d: (d.familia, d.codigo))
        ],
    )


@router.get(
    "/resumo",
    response_model=ResumoAuditSchema,
    summary="Indicadores do conjunto filtrado",
    dependencies=[AdminOuAuditorDep],
)
def obter_resumo(
    request: Request,
    db: SessionDep,
    utilizador_atual: Utilizador = Depends(get_current_user),
    q: str | None = Query(None),
    data_inicio_raw: str | None = Query(None, alias="data_inicio"),
    data_fim_raw: str | None = Query(None, alias="data_fim"),
    acao: str | None = Query(None),
    familia: str | None = Query(None),
    resultado: Resultado | None = Query(None),
    entidade_tipo: str | None = Query(None),
    entidade_id: uuid.UUID | None = Query(None),
    utilizador_id: uuid.UUID | None = Query(None),
    ip: str | None = Query(None),
):
    """
    Indicadores calculados sobre EXATAMENTE o mesmo conjunto que a listagem
    devolve com os mesmos parâmetros.

    Endpoint separado da listagem de propósito: assim mudar de página não obriga
    a recalcular contagens sobre a tabela inteira.
    """
    data_inicio = _parse_datetime_query(data_inicio_raw, "data_inicio", request)
    data_fim = _parse_datetime_query(data_fim_raw, "data_fim", request)
    _validar_intervalo(data_inicio, data_fim)
    _validar_familia(familia, request)

    criterios = _criterios(
        empresa_id=utilizador_atual.empresa_id,
        q=q,
        data_inicio=data_inicio,
        data_fim=data_fim,
        acao=acao,
        familia=familia,
        resultado=resultado,
        entidade_tipo=entidade_tipo,
        entidade_id=entidade_id,
        utilizador_id=utilizador_id,
        ip=ip,
    )

    total, por_resultado, autenticacao, por_familia = _agregar(db, criterios)
    ips_distintos, ips_sem_hash = _contar_ips(db, criterios)

    # Sem intervalo pedido, o período comunicado é o que os dados abrangem — não
    # se inventa uma janela que o utilizador não escolheu.
    inicio_efetivo, fim_efetivo = data_inicio, data_fim
    if inicio_efetivo is None or fim_efetivo is None:
        limites = db.exec(
            select(func.min(AuditLog.created_at), func.max(AuditLog.created_at)).where(
                *criterios
            )
        ).one()
        inicio_efetivo = inicio_efetivo or limites[0]
        fim_efetivo = fim_efetivo or limites[1]

    return ResumoAuditSchema(
        periodo=PeriodoSchema(inicio=inicio_efetivo, fim=fim_efetivo),
        total=total,
        por_resultado=por_resultado,
        autenticacao=autenticacao,
        ips_distintos=ips_distintos,
        ips_sem_hash=ips_sem_hash,
        top_familias=[
            ContagemFamiliaSchema(familia=f, n=n)
            for f, n in sorted(por_familia.items(), key=lambda par: -par[1])[:6]
        ],
    )


@router.get(
    "/retencao",
    response_model=RetencaoSchema,
    summary="Janela de retenção e meses já arquivados",
    dependencies=[AdminOuAuditorDep],
)
def obter_retencao(
    db: SessionDep,
    utilizador_atual: Utilizador = Depends(get_current_user),
):
    """
    Diz até onde a listagem vê e o que existe para lá disso.

    Sem isto, quem consulta a trilha não tem como distinguir «não aconteceu» de
    «já saiu da janela» — e concluiria que a história começa no dia do corte.
    """
    from app.auditoria.arquivo import listar_arquivos_do_tenant

    empresa_id = utilizador_atual.empresa_id
    mais_antigo = db.exec(
        select(func.min(AuditLog.created_at)).where(AuditLog.empresa_id == empresa_id)
    ).one()

    return RetencaoSchema(
        retencao_dias=get_settings().AUDIT_RETENCAO_DIAS,
        mais_antigo_em_linha=mais_antigo,
        meses_arquivados=[
            MesArquivadoSchema(**m) for m in listar_arquivos_do_tenant(str(empresa_id))
        ],
    )


# ---------------------------------------------------------------------------
# Exportação
# ---------------------------------------------------------------------------

_COLUNAS_CSV = (
    "timestamp_utc",
    "action_code",
    "action_family",
    "severity",
    "result",
    "user_id",
    "user_name",
    "entity_type",
    "entity_id",
    "target",
    "ip_address",
    "data_before",
    "data_after",
)


# Cada célula passa pela regra partilhada das exportações (fórmulas neutralizadas).
_celula = celula_csv


def _gerar_csv(
    empresa_id: uuid.UUID, criterios_kwargs: dict, corte: datetime
) -> Iterator[str]:
    """
    Produz o ficheiro por lotes, com sessão própria.

    A sessão do pedido já foi fechada quando este gerador corre: o corpo de uma
    resposta em streaming só é consumido depois de a dependência de sessão ter
    terminado. Usar a sessão do pedido aqui seria lê-la depois de fechada.
    """
    # Import local: o atributo do módulo é relido a cada chamada, o que permite
    # apontá-lo para outra base nos testes.
    from app.database import engine

    buffer = io.StringIO()
    # `;` e não `,`: é o separador que o Excel em português espera. Com vírgula, a
    # exportação abria-se toda na coluna A (a mesma escolha da exportação do inventário).
    escritor = csv.writer(buffer, delimiter=";")

    def despejar() -> str:
        texto = buffer.getvalue()
        buffer.seek(0)
        buffer.truncate(0)
        return texto

    # Marca de ordenação de bytes: sem ela o Excel em português lê o ficheiro
    # como Latin-1 e todos os acentos saem trocados.
    yield "﻿"
    escritor.writerow(_COLUNAS_CSV)
    yield despejar()

    criterios = _criterios(empresa_id=empresa_id, **criterios_kwargs)
    # Congela o conjunto no instante do pedido: sem este limite, os registos
    # escritos durante a exportação (incluindo o desta mesma exportação)
    # empurravam a paginação e duplicavam linhas.
    criterios.append(AuditLog.created_at <= corte)

    with Session(engine) as sessao:
        offset = 0
        while True:
            lote = list(
                sessao.exec(
                    select(AuditLog)
                    .where(*criterios)
                    .order_by(AuditLog.created_at.asc(), AuditLog.id.asc())  # type: ignore[union-attr]
                    .offset(offset)
                    .limit(LOTE_EXPORTACAO)
                ).all()
            )
            if not lote:
                break

            utilizadores_map, anonimizados = _contexto_do_lote(sessao, lote)

            for log in lote:
                nome = None
                if log.utilizador_id:
                    u = utilizadores_map.get(log.utilizador_id)
                    if u:
                        # Devolve None se a decifra falhar — a célula fica vazia,
                        # nunca com a palavra "None" lá escrita.
                        nome = decifrar_pii(u.nome)
                dados_ant, dados_nov = _dados_visiveis(log, anonimizados)
                d = definicao(log.acao, log.entidade_tipo)
                escritor.writerow([
                    _celula(log.created_at.isoformat()),
                    _celula(log.acao),
                    _celula(d.familia),
                    _celula(d.severidade),
                    _celula(getattr(log.resultado, "value", log.resultado)),
                    _celula(log.utilizador_id),
                    _celula(nome),
                    _celula(entidade_apresentacao(log.entidade_tipo)),
                    _celula(log.entidade_id),
                    _celula(_get_resumo_alvo(log, dados_ant, dados_nov)),
                    _celula(decifrar_pii(log.ip_address)),
                    _celula(dados_ant),
                    _celula(dados_nov),
                ])

            yield despejar()
            offset += LOTE_EXPORTACAO


@router.get(
    "/exportar.csv",
    summary="Exportar os registos filtrados em CSV",
    dependencies=[AdminOuAuditorDep],
    response_class=StreamingResponse,
)
def exportar_csv(
    request: Request,
    db: SessionDep,
    utilizador_atual: Utilizador = Depends(get_current_user),
    q: str | None = Query(None),
    data_inicio_raw: str | None = Query(None, alias="data_inicio"),
    data_fim_raw: str | None = Query(None, alias="data_fim"),
    acao: str | None = Query(None),
    familia: str | None = Query(None),
    resultado: Resultado | None = Query(None),
    entidade_tipo: str | None = Query(None),
    entidade_id: uuid.UUID | None = Query(None),
    utilizador_id: uuid.UUID | None = Query(None),
    ip: str | None = Query(None),
):
    """
    Exporta em CSV o mesmo conjunto que a listagem mostra com os mesmos filtros.

    O ficheiro é técnico: códigos de ação e campos estruturados, cabeçalhos
    estáveis em inglês. Um CSV é entrada de outra ferramenta, não um ecrã — se
    os cabeçalhos mudassem com o idioma, ninguém conseguiria automatizar nada
    em cima dele.
    """
    data_inicio = _parse_datetime_query(data_inicio_raw, "data_inicio", request)
    data_fim = _parse_datetime_query(data_fim_raw, "data_fim", request)
    _validar_intervalo(data_inicio, data_fim)
    _validar_familia(familia, request)

    empresa_id = utilizador_atual.empresa_id
    corte = datetime.now(timezone.utc)
    criterios_kwargs = dict(
        q=q,
        data_inicio=data_inicio,
        data_fim=data_fim,
        acao=acao,
        familia=familia,
        resultado=resultado,
        entidade_tipo=entidade_tipo,
        entidade_id=entidade_id,
        utilizador_id=utilizador_id,
        ip=ip,
    )

    criterios = _criterios(empresa_id=empresa_id, **criterios_kwargs)
    criterios.append(AuditLog.created_at <= corte)
    total: int = db.exec(
        select(func.count()).select_from(AuditLog).where(*criterios)
    ).one()  # type: ignore[assignment]

    maximo = get_settings().AUDIT_EXPORT_MAX_LINHAS
    if total > maximo:
        raise HTTPException(
            status_code=422,
            detail=traduzir(
                MsgsI18n.AUDIT_EXPORTACAO_EXCEDE_MAXIMO,
                locale_de_request(request),
                total=total,
                maximo=maximo,
            ),
        )

    # A auditoria da exportação é escrita e persistida ANTES de o ficheiro
    # começar a sair. Dentro do gerador, um download cancelado a meio deixaria
    # a extração sem rasto nenhum.
    registar_acao(
        db,
        acao=Acao.AUDIT_EXPORTADO,
        empresa_id=empresa_id,
        utilizador_id=utilizador_atual.id,
        entidade_tipo="AuditLog",
        dados_novos={
            "linhas": total,
            "filtros": {
                chave: str(valor)
                for chave, valor in criterios_kwargs.items()
                if valor is not None
            },
        },
        force_commit=True,
    )

    nome = f"audit-{corte:%Y%m%d-%H%M%S}.csv"
    return StreamingResponse(
        _gerar_csv(empresa_id, criterios_kwargs, corte),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{nome}"'},
    )


@router.get(
    "/{log_id}",
    response_model=AuditLogSchema,
    summary="Detalhe de um registo de auditoria",
    dependencies=[AdminOuAuditorDep],
)
def get_audit_log(
    log_id: uuid.UUID,
    db: SessionDep,
    utilizador_atual: Utilizador = Depends(get_current_user),
):
    """Devolve o detalhe completo de um registo de auditoria do tenant."""
    log = db.get(AuditLog, log_id)
    if not log or log.empresa_id != utilizador_atual.empresa_id:
        raise HTTPException(status_code=404, detail="Registo não encontrado.")

    utilizadores_map, anonimizados = _contexto_do_lote(db, [log])

    nome: str | None = None
    if log.utilizador_id:
        u = utilizadores_map.get(log.utilizador_id)
        if u:
            nome = decifrar_pii(u.nome)

    dados_ant, dados_nov = _dados_visiveis(log, anonimizados)
    d = definicao(log.acao, log.entidade_tipo)

    return AuditLogSchema(
        id=log.id,
        created_at=log.created_at,
        utilizador_id=log.utilizador_id,
        utilizador_nome=nome,
        acao=log.acao,
        acao_canonica=d.codigo,
        familia=d.familia,
        severidade=d.severidade,
        entidade_tipo=entidade_apresentacao(log.entidade_tipo),
        entidade_id=log.entidade_id,
        alteracoes=_calcular_alteracoes(dados_ant, dados_nov),
        dados_anteriores=dados_ant,
        dados_novos=dados_nov,
        ip_address=decifrar_pii(log.ip_address),
        user_agent=decifrar_pii(log.user_agent),
        resultado=log.resultado,
    )
