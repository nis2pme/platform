"""
Router da pesquisa global.

Endpoint fino: junta os resultados do core com os do sidecar (quando existe e
responde). Definido como função SÍNCRONA de propósito — o FastAPI corre-a numa
worker thread, mantendo a chamada gRPC bloqueante fora do event loop.
"""
from fastapi import APIRouter, Depends, Query
from sqlmodel import Session

from app.database import get_session
from app.pesquisa import service
from app.pesquisa.schemas import RespostaPesquisaSchema
from app.premium import pesquisa_client
from app.shared.dependencies import CurrentUserDep

router = APIRouter(prefix="/pesquisa", tags=["Pesquisa"])


@router.get(
    "",
    response_model=RespostaPesquisaSchema,
    summary="Pesquisa global (controlos, incidentes, tarefas, evidências, formação + premium)",
)
def pesquisar(
    utilizador: CurrentUserDep,
    q: str = Query(default="", max_length=200, description="Expressão a procurar"),
    limite: int = Query(default=0, ge=0, le=25, description="Resultados por tipo (0 = default)"),
    locale: str = Query(default="pt", max_length=10),
    db: Session = Depends(get_session, scope="function"),
):
    """
    Devolve resultados agrupáveis por tipo, sempre restritos à empresa do
    utilizador autenticado. Expressões com menos de 2 caracteres devolvem vazio.

    Cada tipo de resultado é autorizado pela célula que a tabela `PESQUISAVEIS`
    lhe associa — os do core e os do sidecar pelo mesmo critério, lido uma só
    vez. Esconder um tipo no ecrã não serviria: os dados já tinham saído daqui.

    Se o sidecar premium existir mas não responder, os resultados do core vêm na
    mesma e `premium_indisponivel=true` sinaliza a degradação.
    """
    resultados = service.pesquisar_core(db, utilizador, q, locale, limite)

    permitidos = service.tipos_permitidos(utilizador)
    remotos = {t for t, p in permitidos.items() if p.remoto}

    premium_indisponivel = False
    # Sem nenhum dos módulos do sidecar, não se chega a chamá-lo: poupa-se a
    # ida à rede e não sai da instalação o que ninguém ali podia ver.
    if remotos and not service.query_curta(q):
        premium, indisponivel = pesquisa_client.pesquisar_premium(
            str(utilizador.empresa_id), q.strip(), locale, limite
        )
        resultados.extend(r for r in premium if r.tipo in remotos)
        premium_indisponivel = indisponivel

    return RespostaPesquisaSchema(
        resultados=resultados, premium_indisponivel=premium_indisponivel
    )
