"""
Leitura e alteração da política de capacidades de uma empresa.

Um interruptor não guarda um valor próprio: guarda o efeito que tem nas células
da matriz. O estado que o ecrã mostra é, por isso, DERIVADO — comparam-se as
células com os dois valores que o interruptor pode escrever. Daí existir um
terceiro estado, `personalizado`: as células podem ficar a meio caminho se uma
versão futura mudar o defeito de uma delas. Um ecrã que só soubesse dizer
"ligado" ou "desligado" teria de mentir.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import HTTPException, Request, status
from sqlmodel import Session, select

from app.auth.models import RoleUtilizador
from app.politica import store
from app.politica.interruptores import (
    CELULAS_DA_GRELHA,
    INTERRUPTORES,
    POR_CHAVE,
    Celula,
    Interruptor,
    ambitos_possiveis,
    motivo_de_bloqueio,
    motivo_do_modulo,
    validar_celula,
    validar_matriz,
    validar_valor,
)
from app.politica.models import PoliticaCapacidade
from app.shared.audit import Acao, registar_acao
from app.shared.capacidades import (
    AMBITO_NENHUM,
    MATRIZ,
    ORDEM_CLASSES,
    ORDEM_PAPEIS,
    Ambito,
    ClasseAcao,
    celula_efetiva,
    matriz_efetiva,
)

LIGADO = "ligado"
DESLIGADO = "desligado"
PERSONALIZADO = "personalizado"

# Por que caminho um desvio foi decidido. Não muda o efeito da célula — muda o
# que o documento de funções e responsabilidades pode afirmar sobre a
# organização, e é isso que um auditor foi ali procurar.
ORIGEM_INTERRUPTOR = "interruptor"
ORIGEM_GRELHA = "grelha"


def _valor(papeis: dict[RoleUtilizador, Ambito], papel: RoleUtilizador) -> str:
    """Âmbito de um papel numa célula, como string — sem papel = "nenhum"."""
    ambito = papeis.get(papel)
    return AMBITO_NENHUM if ambito is None else ambito.value


def _valor_efetivo(empresa_id, celula: Celula) -> str:
    modulo, classe, papel = celula
    return _valor(celula_efetiva(empresa_id, modulo, classe), papel)


def _valor_de_origem(celula: Celula) -> str:
    modulo, classe, papel = celula
    return _valor(MATRIZ.get(modulo, {}).get(classe, {}), papel)


def estado_de(empresa_id, interruptor: Interruptor) -> str:
    valores = {_valor_efetivo(empresa_id, c) for c in interruptor.celulas}
    if valores == {interruptor.ligado}:
        return LIGADO
    if valores == {interruptor.desligado}:
        return DESLIGADO
    return PERSONALIZADO


def estado_da_politica(empresa_id) -> list[dict]:
    """Estado de cada interruptor para esta empresa, na ordem em que se mostram."""
    return [
        {
            "chave": i.chave,
            "estado": estado_de(empresa_id, i),
            "defeito": i.defeito,
        }
        for i in INTERRUPTORES
    ]


def _matriz_projetada(empresa_id, alteracoes: dict[Celula, str]) -> dict:
    """A matriz como ficaria se estas alterações fossem gravadas.

    Projetada em memória de propósito: os invariantes são verificados ANTES de
    escrever, e não dependem de a transação já estar visível a quem lê.
    """
    projetada = {
        modulo: {classe: dict(papeis) for classe, papeis in classes.items()}
        for modulo, classes in matriz_efetiva(empresa_id).items()
    }
    for (modulo, classe, papel), valor in alteracoes.items():
        celula = projetada.setdefault(modulo, {}).setdefault(classe, {})
        if valor == AMBITO_NENHUM:
            celula.pop(papel, None)
        else:
            celula[papel] = Ambito(valor)
    return projetada


def _gravar_celula(
    db: Session, empresa_id, celula: Celula, valor: str, utilizador_id, origem: str
) -> None:
    """Guarda o desvio — ou apaga-o, se o valor voltar a ser o de origem.

    A tabela é esparsa: nada se guarda enquanto a empresa concordar com a
    plataforma. É isso que faz um defeito alterado numa versão futura chegar a
    quem nunca tocou na célula, sem tocar em quem tocou.
    """
    modulo, classe, papel = celula
    linha = db.exec(
        select(PoliticaCapacidade).where(
            PoliticaCapacidade.empresa_id == empresa_id,
            PoliticaCapacidade.modulo == modulo,
            PoliticaCapacidade.classe == classe.value,
            PoliticaCapacidade.papel == papel.value,
        )
    ).first()

    if valor == _valor_de_origem(celula):
        if linha is not None:
            db.delete(linha)
        return

    if linha is None:
        linha = PoliticaCapacidade(
            empresa_id=empresa_id,
            modulo=modulo,
            classe=classe.value,
            papel=papel.value,
            ambito=valor,
        )
    linha.ambito = valor
    linha.origem = origem
    linha.alterado_por = utilizador_id
    linha.alterado_em = datetime.now(timezone.utc)
    db.add(linha)


def aplicar_interruptor(
    db: Session,
    utilizador,
    chave: str,
    ligar: bool,
    request: Request | None = None,
) -> list[dict]:
    """
    Liga ou desliga um interruptor, reescrevendo todas as células do pacote.

    Reescreve o pacote INTEIRO: é a única forma de o estado voltar a ser
    inequívoco depois de uma célula ter ficado fora de sítio.
    """
    interruptor = POR_CHAVE.get(chave)
    if interruptor is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Interruptor desconhecido.",
        )

    empresa_id = utilizador.empresa_id
    estado_anterior = estado_de(empresa_id, interruptor)
    alvo = interruptor.valor(ligar)
    alteracoes = {celula: alvo for celula in interruptor.celulas}

    for celula in interruptor.celulas:
        validar_celula(celula)
    validar_matriz(_matriz_projetada(empresa_id, alteracoes))

    for celula, valor in alteracoes.items():
        _gravar_celula(db, empresa_id, celula, valor, utilizador.id, ORIGEM_INTERRUPTOR)

    registar_acao(
        db=db,
        acao=Acao.POLITICA_ALTERADA,
        empresa_id=empresa_id,
        utilizador_id=utilizador.id,
        entidade_tipo="PoliticaCapacidade",
        dados_anteriores={"interruptor": chave, "estado": estado_anterior},
        dados_novos={"interruptor": chave, "estado": LIGADO if ligar else DESLIGADO},
        request=request,
    )

    # Commit explícito antes de esquecer o cache: pela ordem inversa, um pedido
    # em paralelo podia voltar a encher o cache com a política antiga.
    db.commit()
    store.invalidar(empresa_id)
    return estado_da_politica(empresa_id)


# ── Edição célula a célula ──────────────────────────────────────────────────────


def matriz_para_edicao(empresa_id) -> list[dict]:
    """
    A matriz como o ecrã de edição precisa dela: o que vale hoje, o que a
    plataforma trazia, e o que cada célula aceita.

    Devolve a matriz INTEIRA, incluindo o que não é editável. Um ecrã que só
    mostrasse as células mexíveis deixaria de ser um retrato da política — e é o
    retrato que permite perceber uma decisão antes de a mudar. O que não se pode
    mexer vai marcado, para o ecrã o poder mostrar fechado e dizer porquê.
    """
    efetiva = matriz_efetiva(empresa_id)
    modulos = []
    for modulo, classes in efetiva.items():
        linhas = []
        for classe in ORDEM_CLASSES:
            if classe not in classes:
                continue
            celulas = [
                {
                    "papel": papel.value,
                    "valor": _valor(classes[classe], papel),
                    "origem": _valor_de_origem((modulo, classe, papel)),
                    "editavel": (modulo, classe, papel) in CELULAS_DA_GRELHA,
                    # Não basta dizer que está fechada: "configura-se noutro
                    # sítio" e "não se configura de todo" são coisas diferentes,
                    # e quem lê o ecrã tem de as distinguir.
                    "motivo": motivo_de_bloqueio((modulo, classe, papel)),
                }
                for papel in ORDEM_PAPEIS
            ]
            linhas.append(
                {
                    "classe": classe.value,
                    "ambitos": list(ambitos_possiveis(modulo, classe)),
                    "celulas": celulas,
                }
            )
        modulos.append(
            {
                "modulo": modulo,
                "editavel": any(
                    c["editavel"] for linha in linhas for c in linha["celulas"]
                ),
                "motivo": motivo_do_modulo(modulo),
                "classes": linhas,
            }
        )
    return modulos


def _celula_de(modulo: str, classe: str, papel: str) -> Celula:
    """Converte a célula recebida do cliente. Nome desconhecido = 422."""
    try:
        return (modulo, ClasseAcao(classe), RoleUtilizador(papel))
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"codigo": "celula_desconhecida", "invariante": f"{modulo}.{classe}"},
        )


def aplicar_celulas(
    db: Session,
    utilizador,
    alteracoes: list,
    request: Request | None = None,
) -> list[dict]:
    """
    Grava um conjunto de células editadas à mão e devolve a matriz atualizada.

    Em lote e não uma a uma: os invariantes são propriedades da matriz INTEIRA, e
    uma alteração que só é válida acompanhada de outra seria recusada se as duas
    chegassem separadas. Tudo passa, ou nada é gravado.

    A grelha não alcança a governação da instalação — essa só se toca pelos
    interruptores nomeados, e é o que impede que daqui saia um caminho para
    alguém alargar as suas próprias permissões.
    """
    empresa_id = utilizador.empresa_id

    pedidas: dict[Celula, str] = {}
    for alteracao in alteracoes:
        celula = _celula_de(alteracao.modulo, alteracao.classe, alteracao.papel)
        if celula not in CELULAS_DA_GRELHA:
            modulo, classe, papel = celula
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "codigo": "celula_nao_configuravel",
                    "invariante": f"{modulo}.{classe.value}[{papel.value}]",
                },
            )
        validar_valor(celula, alteracao.valor)
        pedidas[celula] = alteracao.valor

    # Só o que muda mesmo: reescrever uma célula com o valor que já tem sujaria a
    # auditoria com decisões que ninguém tomou.
    efetivas = {celula: _valor_efetivo(empresa_id, celula) for celula in pedidas}
    mudadas = {c: v for c, v in pedidas.items() if v != efetivas[c]}
    if not mudadas:
        return matriz_para_edicao(empresa_id)

    validar_matriz(_matriz_projetada(empresa_id, mudadas))

    for celula, valor in mudadas.items():
        _gravar_celula(db, empresa_id, celula, valor, utilizador.id, ORIGEM_GRELHA)
        modulo, classe, papel = celula
        registar_acao(
            db=db,
            acao=Acao.POLITICA_ALTERADA,
            empresa_id=empresa_id,
            utilizador_id=utilizador.id,
            entidade_tipo="PoliticaCapacidade",
            dados_anteriores={
                "celula": f"{modulo}.{classe.value}[{papel.value}]",
                "valor": efetivas[celula],
            },
            dados_novos={
                "celula": f"{modulo}.{classe.value}[{papel.value}]",
                "valor": valor,
                "origem": ORIGEM_GRELHA,
            },
            request=request,
        )

    # Mesma ordem do caminho dos interruptores: gravar, e só depois esquecer o
    # cache — ao contrário, um pedido em paralelo reenchia-o com o estado antigo.
    db.commit()
    store.invalidar(empresa_id)
    return matriz_para_edicao(empresa_id)


__all__ = [
    "LIGADO",
    "DESLIGADO",
    "PERSONALIZADO",
    "ORIGEM_GRELHA",
    "ORIGEM_INTERRUPTOR",
    "aplicar_celulas",
    "aplicar_interruptor",
    "estado_da_politica",
    "estado_de",
    "matriz_para_edicao",
]
