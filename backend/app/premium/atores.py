"""
Identidade e atribuição de pessoas nos módulos premium (inventário/risco).

Duas responsabilidades, ambas no core (a autoridade de identidade):
  - ator_de(...)         → identidade de quem age, enviada ao sidecar para o
                           enforcement de âmbito (message Ator do contrato gRPC).
  - resolver_pessoa(...) → valida o responsável/dono submetido no corpo do pedido
                           contra os utilizadores reais do tenant; o nome vem
                           SEMPRE da base de dados, nunca do cliente.
"""
import uuid

from fastapi import HTTPException, status
from sqlmodel import Session

from app.auth.models import Utilizador
from app.shared.capacidades import Ambito, ClasseAcao, ambito_de
from app.shared.pii import decifrar_pii


def negar_capacidade(
    utilizador, modulo: str, classe: ClasseAcao, request=None
) -> HTTPException:
    """403 de capacidade num router premium, com o mesmo rasto que os gates do núcleo.

    Os routers premium decidem algumas capacidades em linha (a classe depende do
    corpo — delegar, ou governar num tratamento de aceitação). Sem isto, essas
    recusas não deixavam rasto, ao contrário das que passam por `require_capability`.
    Devolve a exceção para o chamador a levantar.
    """
    from app.shared.audit import registar_negacao

    registar_negacao(utilizador, modulo=modulo, acao=classe.value, codigo="sem_permissao", request=request)
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={"codigo": "sem_permissao", "modulo": modulo, "acao": classe.value},
    )


def registar_recusa_de_recurso(utilizador, modulo: str) -> None:
    """Rasto da recusa de âmbito que veio do sidecar (o registo não lhe está
    atribuído). É o par do que o `exigir_ambito` do núcleo já regista; corre sem
    o pedido em mão, por isso fica sem IP, mas com quem, o quê e quando."""
    from app.shared.audit import registar_negacao

    registar_negacao(utilizador, modulo=modulo, acao="operar", codigo="sem_permissao_recurso")


def ator_de(
    utilizador: Utilizador,
    modulo: str,
    classe: ClasseAcao = ClasseAcao.OPERAR,
) -> dict:
    """
    Identidade do ator para os RPCs de escrita do sidecar.

    O sidecar não conhece papéis, só âmbitos ("total" / "atribuido"). O âmbito
    deste papel neste módulo é lido da matriz de capacidades — a mesma tabela que
    autoriza o pedido no router — para que o núcleo e o sidecar não possam
    discordar sobre quem alcança o quê.

    `classe` tem de ser a da ação que está a ser executada, e não um valor fixo.
    O âmbito de um papel muda de classe para classe: há quem governe sem operar
    (o órgão de gestão define o apetite ao risco e aceita riscos, mas não trata
    do dia-a-dia). Descrever esse ator pela classe de operação dá-lhe o âmbito
    mais restrito — ou nenhum, e então o fail-closed abaixo transforma-o em
    "atribuido" — e o sidecar, que recusa governação em âmbito atribuído, nega a
    ação a quem a matriz acabou de autorizar. Autorizar por uma classe e
    descrever o ator por outra é o mesmo que as duas camadas discordarem.

    Fail-closed: papel sem âmbito nesta classe cai no mais restrito.
    """
    ambito = ambito_de(utilizador, modulo, classe)
    return {
        "id": str(utilizador.id),
        "nome": decifrar_pii(utilizador.nome) or "",
        "ambito": (ambito or Ambito.ATRIBUIDO).value,
    }


def resolver_pessoa(
    db: Session,
    utilizador: Utilizador,
    pessoa_id: str,
    pessoa_nome: str,
    atual_id: str | None = None,
    atual_nome: str = "",
) -> tuple[str, str, bool]:
    """
    Resolve o responsável/dono submetido num pedido de escrita.

    Devolve (id, nome, mudou):
      - pessoa_id preenchido → tem de ser um utilizador ATIVO do MESMO tenant;
        o nome é lido da base de dados (o valor do cliente é ignorado).
      - pessoa_id vazio + nome preenchido → pessoa externa sem conta na app
        (caso legítimo em PME); o nome livre é aceite.
      - ambos vazios → na criação (atual_id is None) atribui o próprio utilizador;
        na atualização significa "sem responsável".
      - mudou → a atribuição difere da atual; o caller exige a capacidade
        DELEGAR e regista a auditoria de atribuição.
    """
    if pessoa_id:
        try:
            alvo_uuid = uuid.UUID(pessoa_id)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"codigo": "pessoa_invalida"},
            )
        alvo = db.get(Utilizador, alvo_uuid)
        if (
            not alvo
            or alvo.empresa_id != utilizador.empresa_id
            or not alvo.ativo
            or alvo.deleted_at is not None
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"codigo": "pessoa_invalida"},
            )
        id_final = str(alvo.id)
        nome_final = decifrar_pii(alvo.nome) or ""
    elif pessoa_nome:
        # Pessoa externa: sem conta, nome livre. Nunca satisfaz o âmbito
        # "atribuido" de um implementador (não há id para coincidir).
        id_final = ""
        nome_final = pessoa_nome.strip()[:200]
    elif atual_id is None:
        # Criação sem indicação → o próprio utilizador fica responsável.
        id_final = str(utilizador.id)
        nome_final = decifrar_pii(utilizador.nome) or ""
    else:
        # Atualização com campos vazios → fica sem responsável.
        id_final = ""
        nome_final = ""

    if atual_id is None:
        # Criação: "mudou" = atribuiu a alguém que não o próprio (delegação).
        mudou = id_final != str(utilizador.id)
    else:
        # Atualização: "mudou" = a atribuição difere da atual (pessoas externas
        # não têm id — compara-se o nome).
        mudou = id_final != atual_id or (
            id_final == "" and nome_final != (atual_nome or "")
        )
    return id_final, nome_final, mudou
