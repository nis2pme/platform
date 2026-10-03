"""
Assistente IA (premium) — camada FINA do open-core.

O sidecar é dono do job (admissão, submissão, polling e persistência). Aqui o core
apenas: faz o gate da feature, reúne e sela o contexto do controlo (custódia de
dados), submete-o ao sidecar, lê o estado e regista a auditoria no core-db.

Auditoria: a submissão é auditada de imediato (síncrona). A CONCLUSÃO acontece do
lado do sidecar; o core regista-a a 1.ª vez que a observa num GET e só DEPOIS de a
gravar reclama a marca ao sidecar — se a gravação falhar, o GET seguinte volta a
tentar (antes, a marca ia primeiro e o registo perdia-se). Um registo já feito não
se repete: confere-se pelo id do job.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import HTTPException, Request
from sqlmodel import Session

from app.auth.models import Utilizador
from app.empresas.models import Empresa
from app.premium.client import (AnaliseLimiteError, PremiumClient,
                                PremiumIndisponivelError, e_indisponibilidade)
from app.premium.context import construir_contexto_controlo, get_ce_or_404
from app.premium.schemas import AnaliseIASchema, EstadoAnaliseIA, RelatorioGapsSchema
from app.premium.sealing import CifraPorConfigurarError
from app.shared.audit import Acao, ResultadoAcao, registar_acao
from app.shared.capacidades import ClasseAcao, exigir_ambito
from app.shared.concorrencia import LIMITE_OPERACOES_PESADAS


def _parse_dt(valor: str | None) -> datetime:
    """RFC3339 → datetime (aware). Tolera 'Z' e valores vazios."""
    if not valor:
        return datetime.now(timezone.utc)
    try:
        return datetime.fromisoformat(valor.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)


def _traduzir_erro(exc: Exception) -> HTTPException | None:
    """Erros do sidecar que têm resposta própria (e não um 500 genérico)."""
    if isinstance(exc, PremiumIndisponivelError) or e_indisponibilidade(exc):
        return HTTPException(status_code=503, detail={"codigo": "premium_indisponivel"})
    try:
        import grpc  # type: ignore
    except ImportError:
        return None
    if isinstance(exc, grpc.RpcError):
        codigo = exc.code()
        if codigo == grpc.StatusCode.PERMISSION_DENIED:
            # A guarda de licença do sidecar recusou (o gate do core já tinha
            # passado com a cache): o módulo não está ativo.
            return HTTPException(status_code=402, detail={"codigo": "premium_inativo", "feature": "ai_assistant"})
        if codigo == grpc.StatusCode.FAILED_PRECONDITION and "licenca_so_leitura" in (exc.details() or ""):
            return HTTPException(status_code=403, detail={"codigo": "licenca_so_leitura", "feature": "ai_assistant"})
        return HTTPException(status_code=502, detail={"codigo": "premium_erro"})
    return None


def _job_to_schema(job: dict) -> AnaliseIASchema:
    relatorio = None
    if job.get("relatorio"):
        try:
            relatorio = RelatorioGapsSchema.model_validate(job["relatorio"])
        except Exception:  # noqa: BLE001 — relatório inválido não deve rebentar o GET
            relatorio = None
    return AnaliseIASchema(
        id=uuid.UUID(job["job_id"]),
        controlo_empresa_id=uuid.UUID(job["controlo_empresa_id"]),
        estado=EstadoAnaliseIA(job["estado"]),
        relatorio=relatorio,
        erro_codigo=job.get("erro_codigo"),
        created_at=_parse_dt(job.get("created_at")),
        updated_at=_parse_dt(job.get("updated_at")),
    )


def solicitar_analise(
    db: Session,
    controlo_empresa_id: uuid.UUID,
    empresa: Empresa,
    utilizador: Utilizador,
    premium: PremiumClient,
    request: Request | None = None,
) -> AnaliseIASchema:
    """Monta o contexto, submete-o ao sidecar e devolve o estado do job."""
    # Uma análise consome quota do tenant: quem só alcança os controlos que lhe
    # estão delegados não a gasta nos dos colegas.
    ce = get_ce_or_404(db, controlo_empresa_id, empresa.id)
    exigir_ambito(utilizador, "controlos", ClasseAcao.OPERAR, ce.implementador_id)

    # As evidências decifradas e seladas vivem em memória desde a montagem até o
    # sidecar as receber (no pior caso ~4 vezes o payload, que o teto do
    # envelope limita a ~24 MiB): tudo isso ocupa a vaga das operações pesadas,
    # partilhada com o dossiê e o scrypt dos backups.
    with LIMITE_OPERACOES_PESADAS.ocupar():
        job, meta = _montar_e_submeter(db, controlo_empresa_id, empresa, premium)

    resultado = (
        ResultadoAcao.SUCESSO
        if job["estado"] != EstadoAnaliseIA.ERRO.value
        else ResultadoAcao.FALHA
    )
    registar_acao(
        db,
        acao=Acao.ANALISE_IA_SOLICITADA,
        resultado=resultado,
        empresa_id=empresa.id,
        utilizador_id=utilizador.id,
        entidade_tipo="AnaliseIA",
        entidade_id=uuid.UUID(job["job_id"]),
        dados_novos={
            "controlo_empresa_id": str(controlo_empresa_id),
            "controlo_codigo": meta["controlo_codigo"],
            "estado": job["estado"],
        },
        request=request,
    )
    return _job_to_schema(job)


def _montar_e_submeter(
    db: Session, controlo_empresa_id: uuid.UUID, empresa: Empresa, premium: PremiumClient
) -> tuple[dict, dict]:
    """Monta o contexto (evidências decifradas e seladas) e entrega-o ao sidecar.
    Devolve (job, meta)."""
    try:
        meta, evidencias_blob = construir_contexto_controlo(db, controlo_empresa_id, empresa)
    except CifraPorConfigurarError:
        # Configuração da instalação (a chave chega com a licença), não avaria:
        # o administrador tem de saber o que falta em vez de ler «erro interno».
        raise HTTPException(status_code=503, detail={"codigo": "cifra_por_configurar"})

    try:
        job = premium.criar_analise_gaps(meta, evidencias_blob)
    except AnaliseLimiteError as exc:
        # Limite (rate-limit por janela/metering, ou já-em-curso): 429 com CÓDIGO+params
        # (o frontend traduz). Sem persistir nem auditar — igual ao comportamento local.
        raise HTTPException(status_code=429, detail=exc.detalhe)
    except Exception as exc:  # noqa: BLE001 — traduzido abaixo; o resto sobe
        # O sidecar não está utilizável (503), recusou pela licença (402/403) ou
        # respondeu mal (502). Não é avaria da plataforma, e o cliente precisa da
        # diferença. Sem persistir nem auditar: não houve análise.
        traduzido = _traduzir_erro(exc)
        if traduzido is None:
            raise
        raise traduzido from exc
    return job, meta


def get_analise_por_controlo(
    db: Session,
    controlo_empresa_id: uuid.UUID,
    empresa: Empresa,
    utilizador: Utilizador,
    premium: PremiumClient,
    request: Request | None = None,
) -> AnaliseIASchema | None:
    """Devolve o estado/resultado do job do controlo; regista a auditoria de conclusão
    a 1.ª vez que observa o estado terminal (reclamando a marca ao sidecar)."""
    ce = get_ce_or_404(db, controlo_empresa_id, empresa.id)
    exigir_ambito(utilizador, "controlos", ClasseAcao.OPERAR, ce.implementador_id)

    try:
        job = premium.obter_analise_por_controlo(
            str(empresa.id), str(controlo_empresa_id), reclamar_auditoria=False
        )
    except Exception as exc:  # noqa: BLE001 — traduzido abaixo; o resto sobe
        traduzido = _traduzir_erro(exc)
        if traduzido is None:
            raise
        raise traduzido from exc
    if job is None:
        return None

    if job.get("auditoria_pendente") and not _conclusao_ja_auditada(db, job["job_id"]):
        if job["estado"] == EstadoAnaliseIA.CONCLUIDO.value:
            registar_acao(
                db,
                acao=Acao.ANALISE_IA_CONCLUIDA,
                resultado=ResultadoAcao.SUCESSO,
                empresa_id=empresa.id,
                utilizador_id=utilizador.id,
                entidade_tipo="AnaliseIA",
                entidade_id=uuid.UUID(job["job_id"]),
                dados_novos={"controlo_empresa_id": str(controlo_empresa_id)},
                request=request,
            )
        elif job["estado"] == EstadoAnaliseIA.ERRO.value:
            dados = {"controlo_empresa_id": str(controlo_empresa_id)}
            if job.get("erro_categoria"):
                dados["tipo_erro"] = job["erro_categoria"]
            registar_acao(
                db,
                acao=Acao.ANALISE_IA_ERRO,
                resultado=ResultadoAcao.FALHA,
                empresa_id=empresa.id,
                utilizador_id=utilizador.id,
                entidade_tipo="AnaliseIA",
                entidade_id=uuid.UUID(job["job_id"]),
                dados_novos=dados,
                request=request,
            )
    if job.get("auditoria_pendente"):
        # Gravado o registo, reclama-se a marca. Se isto falhar, o GET seguinte
        # vê a marca por reclamar mas o registo já feito (e não o repete).
        db.commit()
        try:
            premium.obter_analise_por_controlo(
                str(empresa.id), str(controlo_empresa_id), reclamar_auditoria=True
            )
        except Exception:  # noqa: BLE001 — a marca fica para o GET seguinte
            pass

    return _job_to_schema(job)


def _conclusao_ja_auditada(db: Session, job_id: str) -> bool:
    from sqlmodel import select

    from app.shared.audit import AuditLog

    return db.exec(
        select(AuditLog.id).where(
            AuditLog.entidade_id == uuid.UUID(job_id),
            AuditLog.acao.in_((Acao.ANALISE_IA_CONCLUIDA, Acao.ANALISE_IA_ERRO)),  # type: ignore[attr-defined]
        ).limit(1)
    ).first() is not None
