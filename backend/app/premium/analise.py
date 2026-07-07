"""
Assistente IA (premium) — camada FINA do open-core.

O sidecar é dono do job (admissão, submissão, polling e persistência). Aqui o core
apenas: faz o gate da feature, reúne e sela o contexto do controlo (custódia de
dados), submete-o ao sidecar, lê o estado e regista a auditoria no core-db.

Auditoria: a submissão é auditada de imediato (síncrona). A CONCLUSÃO acontece do
lado do sidecar; o core regista-a a 1.ª vez que a observa num GET, reclamando a
marca de forma atómica ao sidecar para não a duplicar entre pollings concorrentes.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import HTTPException, Request
from sqlmodel import Session

from app.auth.models import Utilizador
from app.empresas.models import Empresa
from app.premium.client import AnaliseLimiteError, PremiumClient
from app.premium.context import construir_contexto_controlo, get_ce_or_404
from app.premium.schemas import AnaliseIASchema, EstadoAnaliseIA, RelatorioGapsSchema
from app.shared.audit import Acao, ResultadoAcao, registar_acao


def _parse_dt(valor: str | None) -> datetime:
    """RFC3339 → datetime (aware). Tolera 'Z' e valores vazios."""
    if not valor:
        return datetime.now(timezone.utc)
    try:
        return datetime.fromisoformat(valor.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)


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
    meta, evidencias_blob = construir_contexto_controlo(db, controlo_empresa_id, empresa)

    try:
        job = premium.criar_analise_gaps(meta, evidencias_blob)
    except AnaliseLimiteError as exc:
        # Limite (rate-limit por janela/metering, ou já-em-curso): 429 com CÓDIGO+params
        # (o frontend traduz). Sem persistir nem auditar — igual ao comportamento local.
        raise HTTPException(status_code=429, detail=exc.detalhe)

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
    get_ce_or_404(db, controlo_empresa_id, empresa.id)

    job = premium.obter_analise_por_controlo(
        str(empresa.id), str(controlo_empresa_id), reclamar_auditoria=True
    )
    if job is None:
        return None

    if job.get("auditoria_pendente"):
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

    return _job_to_schema(job)
