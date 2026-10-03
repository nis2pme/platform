"""
require_feature(...) — dependência FastAPI que faz o *gate* de funcionalidades premium.

Espelha o `require_role(...)` do RBAC (app/shared/dependencies.py), mas em vez de
um papel verifica um ENTITLEMENT do tenant junto do sidecar premium (via
PremiumClient). O tenant é o `empresa_id` do utilizador autenticado.
"""
from typing import Annotated

from fastapi import Depends, HTTPException, status, Request

from app.premium.client import (PremiumClient, e_indisponibilidade,
                                get_premium_client)
from app.shared.dependencies import get_current_user

# Alias tipado para injeção limpa nos routers.
PremiumClientDep = Annotated[PremiumClient, Depends(get_premium_client)]

# Porque é que a licença está só de leitura (códigos estáveis do sidecar, que o
# frontend traduz). Só estes seguem para o cliente, nunca texto livre.
MOTIVOS_SO_LEITURA = frozenset({"expirada", "revogada", "nao_validada", "sem_validacao"})


def recusa_so_leitura(feature: str, limits: dict) -> HTTPException:
    """403 `licenca_so_leitura`, com o motivo quando o sidecar o deu: quem lê a
    mensagem sabe o que fazer (renovar, validar online, falar com o fornecedor)."""
    detalhe = {"codigo": "licenca_so_leitura", "feature": feature}
    motivo = (limits or {}).get("motivo")
    if motivo in MOTIVOS_SO_LEITURA:
        detalhe["motivo"] = motivo
    return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detalhe)


def require_feature(*features: str):
    """
    Factory de dependência: exige que o tenant do utilizador autenticado tenha
    TODAS as features premium indicadas. Caso contrário → 402 Payment Required.

    Uso nos routers:
        @router.get("/x", dependencies=[Depends(require_feature("ai_assistant"))])
        async def rota(...):

    Ou como parâmetro tipado:
        async def rota(utilizador = Depends(require_feature("ai_assistant"))):

    ORDEM: quando uma rota acumula este gate com o de autorização, o de
    autorização vem SEMPRE primeiro na lista de dependências — o FastAPI
    avalia-as por ordem, e quem não pode a ação deve levar 403, não 402.
    Ao contrário, o 402 conta a quem não tem permissão que a teria se o tenant
    pagasse: é o modelo de autorização a escapar-se pela mensagem de erro.
    Primeiro decide-se se a pessoa pode; só depois se o tenant comprou.
    """
    def verificador(
        utilizador=Depends(get_current_user),
        premium: PremiumClient = Depends(get_premium_client),
        request: Request = None,  # type: ignore[assignment] — o FastAPI injeta-o; chamadas diretas (testes) podem omiti-lo
    ):
        tenant_id = str(utilizador.empresa_id)
        for feature in features:
            try:
                tem_feature = premium.has_feature(tenant_id, feature)
            except Exception as exc:  # noqa: BLE001 — reenviado se não for transporte
                if not e_indisponibilidade(exc):
                    # Não é «não se chegou ao sidecar»: é defeito nosso, ou o
                    # sidecar a recusar a pergunta. Sai 500, como deve — apanhar
                    # tudo aqui esconderia o defeito atrás de uma mensagem
                    # tranquila que ainda por cima convida a repetir o pedido.
                    raise
                # Não se conseguiu PERGUNTAR se o tenant tem a feature. São coisas
                # diferentes de não ter, e a resposta tem de as separar:
                #   - negar o acesso, porque um gate que não consegue verificar
                #     não pode deixar passar;
                #   - dizer que o módulo está indisponível, e não que a plataforma
                #     avariou (500) nem que falta pagar (402). Só o 503 leva quem
                #     recebe a agir no sítio certo — a rede, o sidecar — e faz o
                #     cliente HTTP tentar outra vez em vez de desistir.
                #
                # Este gate corre ANTES do corpo da rota, como dependência, por
                # isso o tradutor de erros das rotas nunca chega a ver a falha.
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail={"codigo": "premium_indisponivel", "feature": feature},
                ) from exc
            if not tem_feature:
                # Código estável (não texto cravado) → o frontend traduz por i18n.
                raise HTTPException(
                    status_code=status.HTTP_402_PAYMENT_REQUIRED,
                    detail={"codigo": "premium_inativo", "feature": feature},
                )
            # Licença só de leitura (on-prem: revogada, fora do termo, sem
            # validação provada, ou sem contacto com o serviço há demasiado
            # tempo): os dados são do cliente, por isso consultar continua e
            # escrever pára, sem limite de tempo. É 403 e não 402 — os dados
            # continuam a ser do cliente. Só se consulta o modo em escritas (o
            # direito vem da cache do cliente).
            if request is not None and request.method not in ("GET", "HEAD", "OPTIONS"):
                try:
                    direito = premium.check_entitlement(tenant_id, feature)
                except Exception as exc:  # noqa: BLE001 — o mesmo tratamento de cima
                    if not e_indisponibilidade(exc):
                        raise
                    raise HTTPException(
                        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                        detail={"codigo": "premium_indisponivel", "feature": feature},
                    ) from exc
                if direito.limits.get("modo") == "so_leitura":
                    raise recusa_so_leitura(feature, direito.limits)
        return utilizador

    return verificador


def require_alguma_feature(*features: str):
    """
    Irmã de `require_feature`: exige que o tenant tenha PELO MENOS UMA das
    features. Serve módulos feitos de partes compradas à parte (os conetores:
    Entra, AD, GVM, Wazuh) — o ecrã abre-se com qualquer uma, e cada operação
    exige depois a sua com `require_feature`.

    Mesmas respostas: 402 se não tiver nenhuma, 503 se não se conseguir
    perguntar. Não verifica o modo só-leitura da licença: isso é da feature da
    operação concreta, que a rota exige à parte.
    """
    def verificador(
        utilizador=Depends(get_current_user),
        premium: PremiumClient = Depends(get_premium_client),
    ):
        tenant_id = str(utilizador.empresa_id)
        indisponivel = None
        for feature in features:
            try:
                if premium.has_feature(tenant_id, feature):
                    return utilizador
            except Exception as exc:  # noqa: BLE001 — reenviado se não for transporte
                if not e_indisponibilidade(exc):
                    raise
                indisponivel = exc
        if indisponivel is not None:
            # Uma das perguntas não chegou ao sidecar: não se sabe se o tenant
            # tem a feature, e não se pode dizer que não tem.
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"codigo": "premium_indisponivel", "feature": features[0]},
            ) from indisponivel
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail={"codigo": "premium_inativo", "feature": features[0]},
        )

    return verificador


def recusar_escrita_em_so_leitura(*features: str):
    """
    Para escritas em módulos feitos de várias partes (as verificações juntam as
    fontes de vários conetores): se a licença estiver em só-leitura, recusa (403
    `licenca_so_leitura`), como o `require_feature` faz nas escritas dos outros
    módulos. Pergunta pelas features que o tenant tem; sem nenhuma, o portão do
    router já recusou antes.
    """
    def verificador(
        utilizador=Depends(get_current_user),
        premium: PremiumClient = Depends(get_premium_client),
    ):
        tenant_id = str(utilizador.empresa_id)
        for feature in features:
            try:
                direito = premium.check_entitlement(tenant_id, feature)
            except Exception as exc:  # noqa: BLE001 — reenviado se não for transporte
                if not e_indisponibilidade(exc):
                    raise
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail={"codigo": "premium_indisponivel", "feature": feature},
                ) from exc
            if direito.enabled and direito.limits.get("modo") == "so_leitura":
                raise recusa_so_leitura(feature, direito.limits)
        return utilizador

    return verificador

