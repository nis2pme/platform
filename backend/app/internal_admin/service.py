"""
Lógica do mecanismo de gestão privilegiada de tenants.

Uma única função de mudança de estado (`definir_suspensao`), idempotente e
auditada, para qualquer chamador interno partilhar o mesmo caminho. O `actor`
(quem ordenou) é registado na auditoria do core.
"""
from __future__ import annotations

import uuid

from fastapi import HTTPException, Request, status
from sqlmodel import Session

from app.empresas.models import Empresa
from app.shared.audit import Acao, registar_acao


def definir_suspensao(
    db: Session,
    empresa_id: uuid.UUID,
    suspenso: bool,
    actor: str,
    request: Request | None = None,
) -> Empresa:
    """Suspende (True) ou reativa (False) uma empresa. Idempotente.

    Devolve a empresa. 404 se não existir (mensagem genérica — não confirma a
    existência de ids a quem sonde a API interna).
    """
    empresa = db.get(Empresa, empresa_id)
    if empresa is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")

    anterior = empresa.suspenso
    if anterior == suspenso:
        return empresa  # no-op idempotente: não duplica auditoria

    empresa.suspenso = suspenso
    db.add(empresa)
    registar_acao(
        db,
        acao=Acao.EMPRESA_SUSPENSA if suspenso else Acao.EMPRESA_REATIVADA,
        empresa_id=empresa.id,
        entidade_tipo="Empresa",
        entidade_id=empresa.id,
        dados_anteriores={"suspenso": anterior},
        dados_novos={"suspenso": suspenso, "actor": actor},
        request=request,
    )
    db.commit()
    return empresa
