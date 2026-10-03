"""
Os interruptores que uma empresa pode ligar e desligar.

Regra estruturante: um interruptor é um CONJUNTO DE CÉLULAS da matriz, nunca um
caso especial no código. Enquanto isto se mantiver, a mesma tabela e o mesmo
leitor servem tanto esta lista curta como, um dia, a edição célula a célula —
sem migração e sem reescrever a autorização. Um interruptor que virasse um `if`
dedicado deixaria de ser exprimível na matriz e partia essa propriedade.

Consequência prática: o que se quiser poder ligar e desligar tem de ter célula
própria. Foi por isso que a anonimização de dados pessoais ganhou módulo
próprio em vez de viver dentro da gestão de utilizadores.
"""
from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException, status

from app.auth.models import RoleUtilizador
from app.shared.capacidades import AMBITO_NENHUM, Ambito, ClasseAcao

_R = RoleUtilizador
_TOTAL = Ambito.TOTAL.value
_ATRIBUIDO = Ambito.ATRIBUIDO.value

# Célula da matriz: módulo, classe de ação, papel.
Celula = tuple[str, ClasseAcao, RoleUtilizador]

# Módulos onde o trabalho tem responsável e pode, por isso, ser limitado a ele.
_MODULOS_COM_DONO = (
    "inventario", "risco", "fornecedores", "incidentes", "tarefas", "formacao",
)
# Módulos com eliminação de registos.
_MODULOS_COM_ELIMINACAO = ("evidencias",) + _MODULOS_COM_DONO


@dataclass(frozen=True)
class Interruptor:
    """Um interruptor e as células que escreve nos dois estados."""

    chave: str
    defeito: bool
    ligado: str      # âmbito que as células tomam quando está ligado
    desligado: str   # âmbito que as células tomam quando está desligado
    celulas: tuple[Celula, ...]

    def valor(self, ligado: bool) -> str:
        return self.ligado if ligado else self.desligado


INTERRUPTORES: tuple[Interruptor, ...] = (
    # O implementador escreve só no que lhe está atribuído. Ligado de origem:
    # quem quiser abrir carrega no interruptor; quem descobre tarde de mais que
    # toda a gente podia editar tudo não tem como voltar atrás.
    Interruptor(
        chave="implementador_so_atribuido",
        defeito=True,
        ligado=_ATRIBUIDO,
        desligado=_TOTAL,
        celulas=(
            ("controlos", ClasseAcao.VER, _R.IMPLEMENTADOR),
            ("controlos", ClasseAcao.OPERAR, _R.IMPLEMENTADOR),
            ("evidencias", ClasseAcao.VER, _R.IMPLEMENTADOR),
            ("evidencias", ClasseAcao.OPERAR, _R.IMPLEMENTADOR),
            *(
                (modulo, ClasseAcao.OPERAR, _R.IMPLEMENTADOR)
                for modulo in _MODULOS_COM_DONO
            ),
        ),
    ),
    # Uma cópia de segurança leva a instalação inteira num ficheiro.
    Interruptor(
        chave="subadmin_backups",
        defeito=False,
        ligado=_TOTAL,
        desligado=AMBITO_NENHUM,
        celulas=(
            ("backup", ClasseAcao.VER, _R.SUBADMIN),
            ("backup", ClasseAcao.OPERAR, _R.SUBADMIN),
        ),
    ),
    # Anonimizar uma pessoa é irreversível.
    Interruptor(
        chave="subadmin_rgpd",
        defeito=False,
        ligado=_TOTAL,
        desligado=AMBITO_NENHUM,
        celulas=(
            ("rgpd", ClasseAcao.VER, _R.SUBADMIN),
            ("rgpd", ClasseAcao.OPERAR, _R.SUBADMIN),
        ),
    ),
    # Marcar um controlo como não aplicável é decidir o âmbito da conformidade.
    Interruptor(
        chave="gestao_marca_nao_aplicavel",
        defeito=False,
        ligado=_TOTAL,
        desligado=AMBITO_NENHUM,
        celulas=(("controlos", ClasseAcao.GOVERNAR, _R.CEO),),
    ),
    Interruptor(
        chave="implementador_ve_relatorios",
        defeito=False,
        ligado=_TOTAL,
        desligado=AMBITO_NENHUM,
        celulas=(("relatorios", ClasseAcao.VER, _R.IMPLEMENTADOR),),
    ),
    # Ligado = eliminar passa a ser só do administrador.
    Interruptor(
        chave="eliminar_so_admin",
        defeito=False,
        ligado=AMBITO_NENHUM,
        desligado=_TOTAL,
        celulas=tuple(
            (modulo, ClasseAcao.ELIMINAR, _R.SUBADMIN)
            for modulo in _MODULOS_COM_ELIMINACAO
        ),
    ),
)

POR_CHAVE = {i.chave: i for i in INTERRUPTORES}

# As células que os interruptores escrevem. Mantidas como conjunto PRÓPRIO, e não
# fundidas com as da grelha, por duas razões: é delas que se deriva o estado de
# cada interruptor, e é a diferença entre os dois conjuntos que permite dizer se
# uma empresa editou a matriz à mão.
CELULAS_DOS_PACOTES: frozenset[Celula] = frozenset(
    celula for i in INTERRUPTORES for celula in i.celulas
)


# ── A grelha: edição célula a célula ────────────────────────────────────────────
#
# A matriz inteira são umas centenas de células. Oferecê-las todas a uma PME sem
# literacia em cibersegurança não é dar-lhe controlo — é dar-lhe um formulário
# que se atravessa a clicar, e o resultado é a postura de segurança da
# instalação. A grelha cobre por isso um recorte, com dois cortes:
#
# 1. Só os módulos de TRABALHO. Aqui o organigrama varia mesmo de empresa para
#    empresa — há quem separe quem trata de fornecedores de quem trata de
#    incidentes, e não há defeito que sirva as duas. Nos módulos que gerem a
#    INSTALAÇÃO a pergunta é sempre a mesma e é binária ("só a administração, ou
#    também a subadministração?"), e para essa há interruptor.
# 2. Só os papéis variáveis. Tirar uma célula à administração não tem caso de uso
#    e trancaria a empresa fora do seu trabalho; ao auditor sobra a leitura,
#    porque a escrita já lhe está fechada pelos invariantes.
#
# Aprovar não se oferece a ninguém: é a validação independente que o documento de
# funções e responsabilidades certifica, e um invariante recusa transferi-la. Uma
# célula que o validador nunca aceita seria um interruptor que não faz nada.
MODULOS_DA_GRELHA: tuple[str, ...] = (
    "controlos", "evidencias", "relatorios",
) + _MODULOS_COM_DONO

PAPEIS_DA_GRELHA: tuple[RoleUtilizador, ...] = (
    _R.SUBADMIN, _R.IMPLEMENTADOR, _R.CEO,
)


def _celulas_da_grelha() -> frozenset[Celula]:
    from app.shared.capacidades import MATRIZ

    celulas: set[Celula] = set()
    for modulo in MODULOS_DA_GRELHA:
        for classe in MATRIZ.get(modulo, {}):
            if classe is ClasseAcao.APROVAR:
                continue
            for papel in PAPEIS_DA_GRELHA:
                celulas.add((modulo, classe, papel))
        # Do auditor só se decide o que ele alcança para auditar.
        if ClasseAcao.VER in MATRIZ.get(modulo, {}):
            celulas.add((modulo, ClasseAcao.VER, _R.AUDITOR))
    return frozenset(celulas)


CELULAS_DA_GRELHA: frozenset[Celula] = _celulas_da_grelha()

# Só estas células podem ter opinião da empresa. Tudo o resto da matriz é do
# produto — nomeadamente a governação da instalação, que só se toca pelos
# interruptores.
CELULAS_PERMITIDAS: frozenset[Celula] = CELULAS_DOS_PACOTES | CELULAS_DA_GRELHA


# ── Que valores cada célula pode tomar ──────────────────────────────────────────
#
# "Só o que lhe é atribuído" só significa alguma coisa onde há código a comparar
# o dono do registo com quem o pede. Nas restantes células a matriz responde
# apenas "tem ou não tem", e oferecer ali um âmbito seria oferecer um valor que
# nada lê. Em algumas seria pior do que inútil: onde a verificação corre sem dono
# em mão, "atribuído" recusaria a toda a gente — um interruptor que parece
# apertar e afinal fecha.
CELULAS_COM_AMBITO: frozenset[tuple[str, ClasseAcao]] = frozenset(
    {
        # Controlos e evidências: a verificação recebe o implementador do controlo.
        ("controlos", ClasseAcao.VER),
        ("controlos", ClasseAcao.OPERAR),
        ("controlos", ClasseAcao.GOVERNAR),
        ("evidencias", ClasseAcao.VER),
        ("evidencias", ClasseAcao.OPERAR),
        # Escrita nos módulos com responsável — no núcleo pela verificação de
        # âmbito, nos premium pelo ator que segue no pedido ao sidecar.
        *((modulo, ClasseAcao.OPERAR) for modulo in _MODULOS_COM_DONO),
        # Eliminação onde o responsável do registo é conhecido no núcleo. Numa
        # evidência órfã é quem a carregou.
        ("evidencias", ClasseAcao.ELIMINAR),
        ("incidentes", ClasseAcao.ELIMINAR),
        ("tarefas", ClasseAcao.ELIMINAR),
        ("formacao", ClasseAcao.ELIMINAR),
        # Riscos: o sidecar confirma o dono do risco pelo ator antes de apagar.
        ("risco", ClasseAcao.ELIMINAR),
    }
)


def ambitos_possiveis(modulo: str, classe: ClasseAcao) -> tuple[str, ...]:
    """Os valores que esta célula pode tomar na configuração."""
    if (modulo, classe) in CELULAS_COM_AMBITO:
        return (_TOTAL, _ATRIBUIDO, AMBITO_NENHUM)
    return (_TOTAL, AMBITO_NENHUM)


# ── Porque é que uma célula não se mexe aqui ────────────────────────────────────
#
# "Não é editável" são cinco situações diferentes, e um ecrã que lhes chame a
# mesma coisa engana. A que engana mais é confundir *configura-se noutro sítio*
# com *não se configura de todo*: a primeira é uma indicação, a segunda é um
# facto sobre o produto. Quem decide é o servidor; o ecrã traduz o código.
MOTIVO_INTERRUPTOR = "interruptor"   # configurável, mas pelas opções nomeadas
MOTIVO_APROVACAO = "aprovacao"       # validação independente, não se transfere
MOTIVO_ADMIN = "admin"               # a administração não perde permissões
MOTIVO_AUDITOR = "auditor"           # o auditor não escreve no que audita
MOTIVO_PLATAFORMA = "plataforma"     # não é configurável, ponto


def motivo_de_bloqueio(celula: Celula) -> str | None:
    """Porque é que esta célula não se edita na grelha. None = edita-se."""
    modulo, classe, papel = celula
    if celula in CELULAS_DA_GRELHA:
        return None
    if celula in CELULAS_DOS_PACOTES:
        return MOTIVO_INTERRUPTOR
    if classe is ClasseAcao.APROVAR:
        return MOTIVO_APROVACAO
    if papel is _R.ADMIN:
        return MOTIVO_ADMIN
    if papel is _R.AUDITOR and classe in _CLASSES_DE_ESCRITA:
        return MOTIVO_AUDITOR
    return MOTIVO_PLATAFORMA


def motivo_do_modulo(modulo: str) -> str | None:
    """Porque é que um módulo inteiro não se edita aqui. None = tem células editáveis."""
    if any(m == modulo for m, _, _ in CELULAS_DA_GRELHA):
        return None
    if any(m == modulo for m, _, _ in CELULAS_DOS_PACOTES):
        return MOTIVO_INTERRUPTOR
    return MOTIVO_PLATAFORMA


# ── Invariantes: verdadeiros em qualquer configuração ───────────────────────────

# O auditor faz validação independente. Se pudesse escrever no que audita, a
# empresa desligava a segregação de funções e continuava a exportar um documento
# a dizer que a tem.
_CLASSES_DE_ESCRITA = (
    ClasseAcao.OPERAR,
    ClasseAcao.GOVERNAR,
    ClasseAcao.ELIMINAR,
    ClasseAcao.DELEGAR,
)

# Agir sobre o que não se alcança é uma incoerência que o documento de funções e
# responsabilidades não sabe explicar: sairia "Eliminar: Sim" ao lado de
# "Ver: —", e nenhum auditor aceita isso como segregação de funções. Vale para
# TODAS as classes, e não só para operar — apagar às cegas é pior do que editar
# às cegas.
_CLASSES_QUE_EXIGEM_VER = (
    ClasseAcao.OPERAR,
    ClasseAcao.GOVERNAR,
    ClasseAcao.ELIMINAR,
    ClasseAcao.DELEGAR,
    ClasseAcao.EXPORTAR,
    ClasseAcao.APROVAR,
)

# Duas classes têm recusa própria porque a frase que a pessoa lê tem de nomear o
# que ela acabou de tentar fazer; as restantes partilham a genérica.
_CODIGO_SEM_VER = {
    ClasseAcao.OPERAR: "opera_sem_ver",
    ClasseAcao.APROVAR: "aprova_sem_ver",
}

# Ninguém tira ao administrador a gestão de pessoas nem a edição da política —
# uma configuração que o fizesse trancava a empresa fora da sua instalação.
_INTOCAVEIS_DO_ADMIN = (
    ("utilizadores", ClasseAcao.VER),
    ("utilizadores", ClasseAcao.OPERAR),
    ("politica", ClasseAcao.VER),
    ("politica", ClasseAcao.GOVERNAR),
)

# O outro lado da mesma moeda: garantir que o administrador MANTÉM uma célula não
# impede que ela seja DADA a mais alguém, e há células em que isso é uma escalada.
# Aqui diz-se quem pode, no máximo, tê-las.
#
#   - a política: quem a edita reescreve as suas próprias permissões, e promove-se
#     em dois passos. Fica só com quem administra a instalação.
#   - a gestão de pessoas e a anonimização: o serviço que as executa distingue
#     apenas administração de subadministração ao decidir sobre quem se pode agir.
#     Dar estas capacidades a outro papel dá-lhe poder sobre contas que esse
#     serviço não sabe proteger — repor a credencial de quem administra, por
#     exemplo. O limite tem de ser dito onde a política se decide, e não só onde
#     ela se aplica.
_LIMITES_MAXIMOS = (
    ("politica", ClasseAcao.VER, frozenset({_R.ADMIN})),
    ("politica", ClasseAcao.GOVERNAR, frozenset({_R.ADMIN})),
    ("utilizadores", ClasseAcao.OPERAR, frozenset({_R.ADMIN, _R.SUBADMIN})),
    ("rgpd", ClasseAcao.OPERAR, frozenset({_R.ADMIN, _R.SUBADMIN})),
)


def _recusar(codigo: str, detalhe: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail={"codigo": codigo, "invariante": detalhe},
    )


def validar_celula(celula: Celula) -> None:
    """A empresa só pode opinar sobre as células que lhe são oferecidas."""
    if celula not in CELULAS_PERMITIDAS:
        modulo, classe, papel = celula
        raise _recusar(
            "celula_nao_configuravel", f"{modulo}.{classe.value}[{papel.value}]"
        )


def validar_valor(celula: Celula, valor: str) -> None:
    """E só os valores que essa célula sabe honrar."""
    modulo, classe, papel = celula
    if valor not in ambitos_possiveis(modulo, classe):
        raise _recusar(
            "ambito_nao_aplicavel", f"{modulo}.{classe.value}[{papel.value}]={valor}"
        )


def validar_matriz(efetiva: dict) -> None:
    """
    Verifica os invariantes sobre a matriz JÁ RESOLVIDA.

    Corre no servidor e no mesmo sítio para qualquer forma de edição: se um dia
    houver um segundo ecrã, não pode trazer validação própria.
    """
    for modulo, classes in efetiva.items():
        for classe in _CLASSES_DE_ESCRITA:
            if _R.AUDITOR in classes.get(classe, {}):
                raise _recusar(
                    "auditor_nao_escreve", f"{modulo}.{classe.value}"
                )
        # Um módulo sem leitura DECLARADA não tem termo de comparação: as
        # definições da empresa são lidas por toda a gente para o próprio ecrã
        # funcionar, e por isso a matriz não lhes declara `ver`. Não confundir
        # com uma leitura declarada e depois retirada a todos — essa fecha a
        # escrita a todos, que é precisamente o que se quer.
        if ClasseAcao.VER not in classes:
            continue
        ver = set(classes[ClasseAcao.VER])
        for classe in _CLASSES_QUE_EXIGEM_VER:
            # Quem valida tem de conseguir ler o que valida; quem apaga também.
            # Sem isto, uma configuração podia deixar o auditor a aprovar
            # registos que não alcança, ou a gerência a eliminar às cegas.
            if not set(classes.get(classe, {})) <= ver:
                raise _recusar(
                    _CODIGO_SEM_VER.get(classe, "escreve_sem_ver"),
                    f"{modulo}.{classe.value}",
                )

    for modulo, classe in _INTOCAVEIS_DO_ADMIN:
        if _R.ADMIN not in efetiva.get(modulo, {}).get(classe, {}):
            raise _recusar(
                "admin_perde_o_controlo", f"{modulo}.{classe.value}"
            )

    for modulo, classe, permitidos in _LIMITES_MAXIMOS:
        if set(efetiva.get(modulo, {}).get(classe, {})) - permitidos:
            raise _recusar(
                "capacidade_reservada", f"{modulo}.{classe.value}"
            )

    # Aprovar é validação independente: não se transfere para quem executa.
    aprovadores = set(efetiva.get("controlos", {}).get(ClasseAcao.APROVAR, {}))
    if aprovadores - {_R.AUDITOR}:
        raise _recusar("aprovacao_nao_se_transfere", "controlos.aprovar")
