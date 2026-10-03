"""
Matriz de capacidades — autorização transversal por MÓDULO × CLASSE DE AÇÃO.

Fonte única de verdade sobre "que papel pode fazer o quê, e sobre que registos".
Lê-se como uma tabela: cada módulo mapeia classes de ação para os papéis
autorizados, e cada papel para o seu ÂMBITO. Acrescentar um módulo novo é
acrescentar uma entrada de dados — nenhum código de autorização novo.

Duas perguntas, duas camadas:
  - "quem pode?"          → require_capability(...) no router, declarativo
  - "sobre que registos?" → exigir_ambito(...) no service, que precisa do registo
                            em mão mas lê a decisão desta mesma tabela

A tabela abaixo é o DEFEITO. Cada empresa pode afastar-se dele em células
autorizadas (ver `app/politica`), e o que vale é sempre o resultado dos dois:
defeito do código, sobreposto pelos desvios da empresa. Por isso as funções
públicas recebem o UTILIZADOR — dele saem o papel e a empresa, e não há forma
de esquecer um dos dois.

Gates ortogonais nos routers (acumulam-se):
  - require_role(...)        → papel bruto (app/shared/dependencies.py)
  - require_feature(...)     → entitlement premium do tenant, 402 (app/premium/dependencies.py)
  - require_capability(...)  → esta matriz, 403 com código estável para i18n

O frontend recebe as capacidades efetivas do utilizador no payload de
autenticação — nunca duplica a matriz.
"""
import logging
from enum import Enum

from fastapi import Depends, HTTPException, Request, status

from app.auth.models import RoleUtilizador
from app.shared.dependencies import get_current_user

logger = logging.getLogger(__name__)


class ClasseAcao(str, Enum):
    """Classes de ação — separam operação, governação e ações destrutivas."""

    VER = "ver"              # leitura de ecrãs, painéis e documentos
    OPERAR = "operar"        # criar/editar registos do dia-a-dia
    GOVERNAR = "governar"    # decisões de gestão (apetite ao risco, aceitação de risco)
    ELIMINAR = "eliminar"    # remoção definitiva de registos
    DELEGAR = "delegar"      # atribuir dono/responsável a outra pessoa
    APROVAR = "aprovar"      # validação independente (auditor)
    EXPORTAR = "exportar"    # extração estruturada de dados para fora da app


class Ambito(str, Enum):
    """
    Sobre que registos a capacidade se aplica.

    Mesmos valores que o sidecar usa na message `Ator` do contrato gRPC — core e
    sidecar partilham o vocabulário, e o âmbito enviado ao sidecar é lido daqui.
    """

    TOTAL = "total"            # qualquer registo do tenant
    ATRIBUIDO = "atribuido"    # só registos de que é dono/responsável


# Terceiro valor que só existe na CONFIGURAÇÃO, nunca numa célula resolvida:
# a empresa retirou a capacidade ao papel, e a célula deixa de o incluir.
AMBITO_NENHUM = "nenhum"

_R = RoleUtilizador
_T = Ambito.TOTAL
_A = Ambito.ATRIBUIDO


def _total(*papeis: RoleUtilizador) -> dict[RoleUtilizador, Ambito]:
    """Célula em que todos os papéis indicados alcançam qualquer registo."""
    return {p: _T for p in papeis}


_TODOS = _total(_R.ADMIN, _R.SUBADMIN, _R.IMPLEMENTADOR, _R.AUDITOR, _R.CEO)
_OPERADORES = _total(_R.ADMIN, _R.SUBADMIN, _R.IMPLEMENTADOR)
# Órgão de gestão: o CEO governa (define apetite, aceita riscos) mas não opera.
_GESTAO = _total(_R.ADMIN, _R.SUBADMIN, _R.CEO)
_ADMINS = _total(_R.ADMIN, _R.SUBADMIN)
# Quem lê relatórios de conformidade: gestão + auditor. O implementador vê o seu
# trabalho nos módulos, não os relatórios da empresa.
_LEITORES_RELATORIOS = _total(_R.ADMIN, _R.SUBADMIN, _R.AUDITOR, _R.CEO)

# O implementador escreve só no que lhe está atribuído; a gestão alcança tudo.
_OPERADORES_ATRIBUIDO = {_R.ADMIN: _T, _R.SUBADMIN: _T, _R.IMPLEMENTADOR: _A}

# Linha-padrão dos módulos cujo dono-do-recurso é imposto (sidecar premium: o
# handler compara o responsável do registo com o ator antes de escrever).
#
# GOVERNAR fica de fora de propósito: destes módulos só o risco tem uma decisão
# de gestão a tomar. Declará-la na linha-padrão atribuiria aos restantes uma
# responsabilidade que nenhum código exerce, e esta tabela é lida por um auditor.
_DEFEITO_ATRIBUIDO: dict[ClasseAcao, dict[RoleUtilizador, Ambito]] = {
    ClasseAcao.VER: _TODOS,
    ClasseAcao.OPERAR: _OPERADORES_ATRIBUIDO,
    ClasseAcao.ELIMINAR: _ADMINS,
    ClasseAcao.DELEGAR: _ADMINS,
}

# MÓDULO → CLASSE → papel → âmbito.
#
# Esta tabela não serve só os gates: é também a fonte do documento-evidência de
# funções e responsabilidades (ver `documento_matriz_capacidades`), que vai para
# as mãos de um auditor. Por isso uma classe só aparece na linha de um módulo se
# esse módulo tiver mesmo a ação — declarar uma classe inexistente seria afirmar
# uma responsabilidade que o sistema não tem.
MATRIZ: dict[str, dict[ClasseAcao, dict[RoleUtilizador, Ambito]]] = {
    # Controlos: o implementador só alcança os que lhe foram delegados, na
    # leitura e na escrita. Marcar "não aplicável" é a decisão de âmbito e está
    # reservada à administração — o CEO não a toma. Não existe eliminação de
    # controlos, por isso a classe não consta.
    "controlos": {
        ClasseAcao.VER: {**_ADMINS, _R.IMPLEMENTADOR: _A, _R.AUDITOR: _T, _R.CEO: _T},
        ClasseAcao.OPERAR: _OPERADORES_ATRIBUIDO,
        ClasseAcao.GOVERNAR: _ADMINS,
        ClasseAcao.DELEGAR: _ADMINS,
        ClasseAcao.APROVAR: _total(_R.AUDITOR),
    },
    "inventario": dict(_DEFEITO_ATRIBUIDO),
    # Definir o apetite ao risco e aceitar um risco residual são atos do órgão de
    # gestão — e este é o único módulo desta família onde existe decisão dessas.
    "risco": {**_DEFEITO_ATRIBUIDO, ClasseAcao.GOVERNAR: _GESTAO},
    "fornecedores": dict(_DEFEITO_ATRIBUIDO),
    "incidentes": dict(_DEFEITO_ATRIBUIDO),
    "tarefas": dict(_DEFEITO_ATRIBUIDO),
    "formacao": dict(_DEFEITO_ATRIBUIDO),
    # Evidências: seguem o dono do controlo a que pertencem — não se governam nem
    # se delegam por si.
    #
    # Eliminar só existe sobre órfãs que nunca foram prova (apagar de vez, da
    # reciclagem). Aí não há controlo por quem decidir, e o dono é quem a
    # carregou: o implementador apaga as suas sem esperar pela janela nem pedir à
    # administração. Apagar prova é outra coisa, e fica só à administração.
    "evidencias": {
        ClasseAcao.VER: {**_ADMINS, _R.IMPLEMENTADOR: _A, _R.AUDITOR: _T, _R.CEO: _T},
        ClasseAcao.OPERAR: _OPERADORES_ATRIBUIDO,
        ClasseAcao.ELIMINAR: {**_ADMINS, _R.IMPLEMENTADOR: _A},
    },
    # Relatórios: só se leem. A exportação estruturada (RGPD) é mais restrita que
    # a leitura e tem classe própria.
    "relatorios": {
        ClasseAcao.VER: _LEITORES_RELATORIOS,
        ClasseAcao.EXPORTAR: _ADMINS,
    },
    # Conetores: as LIGAÇÕES a sistemas externos (credenciais, servidor, coletor).
    # Configurar uma é gestão, e é aqui que vivem os segredos — por isso fica na
    # administração. O que as ligações provam está em `verificacoes`.
    "conetores": {
        ClasseAcao.VER: _ADMINS,
        ClasseAcao.OPERAR: _ADMINS,
    },
    # Verificações técnicas: o que as ligações e os relatórios importados provam.
    # Ver é de quem tem de corrigir — o implementador também — e de quem lê a
    # conformidade (auditor, gestão): são resultados, não segredos. OPERAR é
    # marcar um desvio como visto e decidir um alerta grave (incidente ou
    # descartado). GOVERNAR é decidir as metas da empresa para os sinais (limites
    # mais apertados do que o mínimo, sinais desligados, controlos ligados).
    "verificacoes": {
        ClasseAcao.VER: _TODOS,
        ClasseAcao.OPERAR: _OPERADORES,
        ClasseAcao.GOVERNAR: _ADMINS,
    },
    # Importação de dados de ferramentas externas. Substituir em massa o
    # inventário ou o registo de risco a partir de um ficheiro é uma decisão de
    # gestão, com o mesmo alcance de uma reconfiguração — não é trabalho de quem
    # implementa um controlo. VER é consultar de onde se pode importar; OPERAR é
    # submeter o ficheiro, simular, aplicar e desfazer. Desfazer fica em OPERAR e
    # não numa classe de eliminação: repõe o que a própria importação escreveu,
    # salta tudo o que uma pessoa tocou desde então, e nunca alcança registos que
    # ela não criou.
    "importacao": {
        ClasseAcao.VER: _ADMINS,
        ClasseAcao.OPERAR: _ADMINS,
    },
    # ── Governação da plataforma ────────────────────────────────────────────────
    # Estes módulos não guardam trabalho de conformidade: gerem a instalação. Estão
    # aqui pelas mesmas duas razões que os outros — um único sítio a decidir, e um
    # registo de funções e responsabilidades que descreve a aplicação inteira.
    #
    # VER de `utilizadores` é ver a EQUIPA — serve os seletores de responsável.
    # Ver o próprio perfil não é capacidade: é de toda a gente, e não consta.
    # O órgão de gestão fica de fora: não opera nenhum módulo, logo nunca preenche
    # um seletor, e a lista de pessoas não tem de lhe passar pelas mãos.
    "utilizadores": {
        ClasseAcao.VER: _total(_R.ADMIN, _R.SUBADMIN, _R.IMPLEMENTADOR),
        ClasseAcao.OPERAR: _ADMINS,
    },
    # Anonimização RGPD: módulo próprio por ser irreversível. Só o admin — uma
    # conta de subadministração comprometida não deve poder apagar uma identidade
    # sem retorno.
    "rgpd": {
        ClasseAcao.VER: _total(_R.ADMIN),
        ClasseAcao.OPERAR: _total(_R.ADMIN),
    },
    "auditoria": {
        ClasseAcao.VER: _total(_R.ADMIN, _R.SUBADMIN, _R.AUDITOR),
    },
    "dossie": {
        ClasseAcao.VER: _ADMINS,
        ClasseAcao.OPERAR: _ADMINS,
    },
    # Cópias de segurança: só o admin. Um backup leva a instalação inteira num
    # ficheiro — é a ação com maior alcance da aplicação, e não é trabalho diário
    # de quem administra o dia-a-dia.
    "backup": {
        ClasseAcao.VER: _total(_R.ADMIN),
        ClasseAcao.OPERAR: _total(_R.ADMIN),
    },
    # Definições da empresa. Não tem VER: os dados básicos da empresa (nome,
    # classificação) são lidos por toda a gente para o próprio ecrã funcionar, e
    # declarar aqui uma leitura restrita afirmaria no documento GR.FR-3 algo que
    # a aplicação não impõe. O que é de administração é ALTERÁ-LOS.
    "empresa": {
        ClasseAcao.GOVERNAR: _ADMINS,
    },
    "sistema": {
        ClasseAcao.VER: _ADMINS,
        ClasseAcao.OPERAR: _total(_R.ADMIN),
    },
    # Plano de ação prioritário. A leitura é de toda a equipa: é o que o painel
    # mostra a quem entra, e quem só alcança o que lhe está atribuído vê nele
    # apenas os seus controlos — o alcance vem da linha de `controlos`, que é
    # onde essa decisão é tomada, e não se repete aqui. De administração é
    # responder ao questionário que gera o plano e mandar regerá-lo.
    "plano": {
        ClasseAcao.VER: _TODOS,
        ClasseAcao.GOVERNAR: _ADMINS,
    },
    # A própria política de permissões. Só o admin — se o subadministrador
    # pudesse editá-la, promovia-se a admin em dois cliques.
    "politica": {
        ClasseAcao.VER: _total(_R.ADMIN),
        ClasseAcao.GOVERNAR: _total(_R.ADMIN),
    },
}


# ── Resolução: defeito do código + desvios da empresa ───────────────────────────

_PAPEL_POR_VALOR = {p.value: p for p in RoleUtilizador}
_AMBITO_POR_VALOR = {a.value: a for a in Ambito}

# Células órfãs já assinaladas. A resolução corre em todos os pedidos e várias
# vezes dentro do mesmo — sem isto, uma linha órfã enchia o registo. O conjunto
# é limitado pelo número de células da matriz.
_ORFAS_AVISADAS: set[tuple] = set()


def _avisar_orfa(empresa_id, modulo: str, classe: ClasseAcao, papel) -> None:
    """Assinala UMA vez que existe um desvio guardado que já não é configurável.

    Não se apaga a linha: pode voltar a ser válida noutra versão, e apagá-la
    tiraria a uma cópia de segurança a possibilidade de a reler.
    """
    chave = (str(empresa_id), modulo, classe.value, papel.value)
    if chave in _ORFAS_AVISADAS:
        return
    _ORFAS_AVISADAS.add(chave)
    logger.warning(
        "Desvio de política ignorado: %s.%s[%s] não é configurável nesta versão.",
        modulo, classe.value, papel.value,
    )


def celula_efetiva(
    empresa_id, modulo: str, classe: ClasseAcao
) -> dict[RoleUtilizador, Ambito]:
    """
    A célula tal como vale NESTA empresa: o defeito acima, sobreposto pelos
    desvios que a empresa guardou.

    Valores que esta versão não reconheça (papel ou âmbito de uma versão mais
    recente) são ignorados e a célula fica com o defeito — nunca com um estado
    inventado.

    O mesmo vale para desvios sobre células que esta versão NÃO deixa configurar.
    Os limites são verificados quando a política se grava, mas uma linha pode
    chegar por outro caminho — o restauro de uma cópia de segurança antiga, ou
    uma atualização que aperte a lista do que é configurável. Um limite que só
    existisse na escrita seria propriedade de um caminho de código, e não da
    aplicação; aqui é honrado também na leitura, que é onde decide.
    """
    from app.politica import store
    from app.politica.interruptores import CELULAS_PERMITIDAS

    base = MATRIZ.get(modulo, {}).get(classe, {})
    desvios = store.desvios(empresa_id).get((modulo, classe.value))
    if not desvios:
        return base

    efetiva = dict(base)
    for papel_valor, ambito_valor in desvios.items():
        papel = _PAPEL_POR_VALOR.get(papel_valor)
        if papel is None:
            continue
        if (modulo, classe, papel) not in CELULAS_PERMITIDAS:
            _avisar_orfa(empresa_id, modulo, classe, papel)
            continue
        if ambito_valor == AMBITO_NENHUM:
            efetiva.pop(papel, None)
        elif ambito_valor in _AMBITO_POR_VALOR:
            efetiva[papel] = _AMBITO_POR_VALOR[ambito_valor]
    return efetiva


def matriz_efetiva(empresa_id) -> dict[str, dict[ClasseAcao, dict[RoleUtilizador, Ambito]]]:
    """A matriz inteira desta empresa. Serve o documento de funções e responsabilidades."""
    return {
        modulo: {
            classe: celula_efetiva(empresa_id, modulo, classe) for classe in classes
        }
        for modulo, classes in MATRIZ.items()
    }


def tem_capacidade(utilizador, modulo: str, classe: ClasseAcao) -> bool:
    """True se o utilizador tem a capacidade. Fail-closed: módulo ou classe fora da matriz = negado."""
    return utilizador.role in celula_efetiva(utilizador.empresa_id, modulo, classe)


def papel_contido_em(empresa_id, papel_alvo: RoleUtilizador, papel_ator: RoleUtilizador) -> bool:
    """True se tudo o que `papel_alvo` alcança nesta empresa cabe no que `papel_ator`
    alcança — em cada célula, o mesmo âmbito ou um mais largo.

    Serve a gestão de contas: criar, promover, repor ou desativar uma conta cujo
    papel faça algo que o ator não pode seria uma escalada por um caminho lateral
    (entrar na conta, ou reservar-lhe uma decisão que o ator não tem). A pergunta é
    sempre à luz da matriz efetiva da empresa, a mesma que autoriza os pedidos.

    Papéis iguais estão contidos (a hierarquia entre pares decide-se à parte). Esta
    função não isenta ninguém: o administrador é tratado por quem chama, porque tem
    capacidades — a validação independente do auditor (`aprovar`) — que por desenho
    nem ele possui, e ainda assim gere a conta do auditor.
    """
    if papel_alvo == papel_ator:
        return True
    for modulo, classes in MATRIZ.items():
        for classe in classes:
            celula = celula_efetiva(empresa_id, modulo, classe)
            ambito_alvo = celula.get(papel_alvo)
            if ambito_alvo is None:
                continue
            ambito_ator = celula.get(papel_ator)
            if ambito_ator is None:
                return False
            if ambito_ator is Ambito.ATRIBUIDO and ambito_alvo is Ambito.TOTAL:
                return False
    return True


def ambito_de(utilizador, modulo: str, classe: ClasseAcao) -> Ambito | None:
    """Âmbito do utilizador nesta célula, ou None se não tem a capacidade de todo."""
    return celula_efetiva(utilizador.empresa_id, modulo, classe).get(utilizador.role)


def so_atribuidos(utilizador, modulo: str, classe: ClasseAcao) -> bool:
    """
    True se este utilizador só alcança os registos de que é dono.

    Para as LISTAGENS, que filtram em SQL em vez de verificarem registo a registo.
    Uma listagem que não use o mesmo critério do gate mostra linhas que o
    utilizador não consegue abrir — ou esconde-lhe o próprio trabalho.
    """
    return ambito_de(utilizador, modulo, classe) is Ambito.ATRIBUIDO


def capacidades_do_papel(role: RoleUtilizador, empresa_id) -> list[str]:
    """Lista ordenada "modulo.classe" com todas as capacidades do papel nesta empresa."""
    return sorted(
        f"{modulo}.{classe.value}"
        for modulo, classes in MATRIZ.items()
        for classe in classes
        if role in celula_efetiva(empresa_id, modulo, classe)
    )


def ambitos_do_papel(role: RoleUtilizador, empresa_id) -> dict[str, str]:
    """Âmbitos do papel nesta empresa, só nas células que NÃO são "total"."""
    resultado = {}
    for modulo, classes in MATRIZ.items():
        for classe in classes:
            ambito = celula_efetiva(empresa_id, modulo, classe).get(role)
            if ambito is not None and ambito is not Ambito.TOTAL:
                resultado[f"{modulo}.{classe.value}"] = ambito.value
    return resultado


def capacidades_de(utilizador) -> list[str]:
    """
    Capacidades do utilizador autenticado.
    Enviada ao frontend no payload de autenticação (login/refresh/me).
    """
    return capacidades_do_papel(utilizador.role, utilizador.empresa_id)


def ambitos_de(utilizador) -> dict[str, str]:
    """
    Âmbitos do utilizador, só onde estão limitados — na prática meia dúzia de
    entradas. Acompanha `capacidades_de` no payload de autenticação: a lista diz
    o que ele pode, este mapa diz onde é que isso está limitado.

    Campo à parte de propósito: um frontend que o ignore continua a funcionar,
    porque quem decide é o servidor.
    """
    return ambitos_do_papel(utilizador.role, utilizador.empresa_id)


# ── Documento "Funções e Responsabilidades" (GR.FR-3) ───────────────────────────
# Rótulos localizados só para a exportação. A matriz em si (acima) é a fonte de
# verdade; aqui traduz-se para linguagem de auditor. Acrescentar um idioma = 1 mapa.
ORDEM_CLASSES = [
    ClasseAcao.VER, ClasseAcao.OPERAR, ClasseAcao.GOVERNAR,
    ClasseAcao.ELIMINAR, ClasseAcao.DELEGAR, ClasseAcao.APROVAR,
    ClasseAcao.EXPORTAR,
]
ORDEM_PAPEIS = [_R.ADMIN, _R.SUBADMIN, _R.IMPLEMENTADOR, _R.AUDITOR, _R.CEO]

_TEXTOS_MATRIZ = {
    "pt": {
        "titulo": "Funções e Responsabilidades — Matriz de Capacidades",
        "subtitulo": "Quem pode fazer o quê em cada módulo da plataforma.",
        "intro": (
            "Este documento regista as funções e responsabilidades de cibersegurança "
            "atribuídas na plataforma, por módulo e por tipo de ação. Serve de registo "
            "documentado dos papéis e da segregação de funções."
        ),
        "sim": "Sim", "nao": "—", "sim_atribuido": "Sim, no que lhe é atribuído",
        "nota_ambito": (
            "\"Sim, no que lhe é atribuído\" significa que a pessoa só alcança os "
            "registos de que é responsável, não os da restante equipa."
        ),
        "modulos": {
            "controlos": "Controlos", "inventario": "Inventário de Ativos",
            "risco": "Análise de Risco", "incidentes": "Incidentes",
            "tarefas": "Tarefas Recorrentes",
            "fornecedores": "Fornecedores", "formacao": "Formação",
            "evidencias": "Evidências", "relatorios": "Relatórios",
            "conetores": "Ligações a Sistemas Externos",
            "verificacoes": "Verificações Técnicas",
            "importacao": "Importação de Dados Externos",
            "utilizadores": "Gestão de Utilizadores",
            "rgpd": "Anonimização de Dados Pessoais (RGPD)",
            "auditoria": "Registos de Auditoria",
            "dossie": "Dossiê para o Auditor",
            "backup": "Cópias de Segurança",
            "empresa": "Definições da Empresa",
            "sistema": "Sistema e Atualizações",
            "plano": "Plano de Ação Prioritário",
            "politica": "Política de Permissões",
        },
        "classes": {
            "ver": "Ver", "operar": "Operar (criar/editar)",
            "governar": "Governar (decisões de gestão)", "eliminar": "Eliminar",
            "delegar": "Delegar (atribuir responsável)", "aprovar": "Aprovar",
            "exportar": "Exportar dados",
        },
        "papeis": {
            "admin": "Administrador", "subadmin": "Subadministrador",
            "implementador": "Implementador", "auditor": "Auditor", "ceo": "Gerência (CEO)",
        },
        "col_acao": "Ação",
        "desvios_titulo": "Alterações a esta matriz feitas pela organização",
        "desvios_intro": (
            "A tabela acima já reflete a configuração em vigor. Esta secção "
            "identifica os pontos em que a organização se afastou da "
            "configuração de origem da plataforma."
        ),
        "sem_desvios": (
            "A organização não alterou nenhuma permissão: a matriz acima é a "
            "configuração de origem da plataforma."
        ),
        "editada_a_mao": (
            "Parte destas alterações foi decidida célula a célula, e não através "
            "das opções nomeadas que a plataforma oferece. A organização definiu "
            "assim a sua própria segregação de funções, dentro dos limites que a "
            "plataforma não deixa alterar."
        ),
        "col_modulo": "Módulo", "col_papel": "Papel",
        "col_origem": "De origem", "col_atual": "Nesta organização",
        "retirado": "Não atribuído",
    },
    "en": {
        "titulo": "Roles and Responsibilities — Capability Matrix",
        "subtitulo": "Who can do what in each platform module.",
        "intro": (
            "This document records the cybersecurity roles and responsibilities assigned "
            "in the platform, by module and action type. It serves as a documented record "
            "of roles and segregation of duties."
        ),
        "sim": "Yes", "nao": "—", "sim_atribuido": "Yes, for assigned records",
        "nota_ambito": (
            "\"Yes, for assigned records\" means the person only reaches records "
            "they are responsible for, not those of the rest of the team."
        ),
        "modulos": {
            "controlos": "Controls", "inventario": "Asset Inventory",
            "risco": "Risk Analysis", "incidentes": "Incidents",
            "tarefas": "Recurring Tasks",
            "fornecedores": "Suppliers", "formacao": "Training",
            "evidencias": "Evidence", "relatorios": "Reports",
            "conetores": "External System Connections",
            "verificacoes": "Technical Checks",
            "importacao": "External Data Import",
            "utilizadores": "User Management",
            "rgpd": "Personal Data Anonymisation (GDPR)",
            "auditoria": "Audit Logs",
            "dossie": "Auditor Dossier",
            "backup": "Backups",
            "empresa": "Company Settings",
            "sistema": "System and Updates",
            "plano": "Priority Action Plan",
            "politica": "Permissions Policy",
        },
        "classes": {
            "ver": "View", "operar": "Operate (create/edit)",
            "governar": "Govern (management decisions)", "eliminar": "Delete",
            "delegar": "Delegate (assign owner)", "aprovar": "Approve",
            "exportar": "Export data",
        },
        "papeis": {
            "admin": "Administrator", "subadmin": "Sub-administrator",
            "implementador": "Implementer", "auditor": "Auditor", "ceo": "Management (CEO)",
        },
        "col_acao": "Action",
        "desvios_titulo": "Changes to this matrix made by the organisation",
        "desvios_intro": (
            "The table above already reflects the configuration in force. This "
            "section identifies the points where the organisation departed from "
            "the platform's original configuration."
        ),
        "sem_desvios": (
            "The organisation has not changed any permission: the matrix above "
            "is the platform's original configuration."
        ),
        "editada_a_mao": (
            "Some of these changes were decided cell by cell, rather than through "
            "the named options the platform offers. The organisation has therefore "
            "defined its own segregation of duties, within the limits the platform "
            "does not allow to be changed."
        ),
        "col_modulo": "Module", "col_papel": "Role",
        "col_origem": "Original", "col_atual": "In this organisation",
        "retirado": "Not granted",
    },
}


def documento_matriz_capacidades(locale: str | None, empresa_id) -> dict:
    """Payload do documento-evidência da matriz de capacidades (GR.FR-3).

    Mesma forma dos documentos dos módulos premium ({titulo, subtitulo, secoes}) —
    o frontend gera o PDF com o renderizador já existente. Uma secção por módulo,
    cada uma com a tabela Ação × Papéis, e uma secção final com o que esta
    organização alterou. Um auditor precisa das duas coisas: o que vale hoje, e
    onde é que isso deixou de ser o que a plataforma traz de origem.
    """
    from datetime import datetime, timezone

    t = _TEXTOS_MATRIZ.get("en" if (locale or "").startswith("en") else "pt")
    cabecalho = [t["col_acao"]] + [t["papeis"][p.value] for p in ORDEM_PAPEIS]
    efetiva = matriz_efetiva(empresa_id)

    def rotulo(ambito: Ambito | None) -> str:
        if ambito is None:
            return t["nao"]
        return t["sim"] if ambito is Ambito.TOTAL else t["sim_atribuido"]

    intro = f"{t['intro']}\n\n{t['nota_ambito']}"
    secoes = [{"titulo": "", "texto": intro, "cabecalho": [], "linhas": []}]
    for modulo, classes in efetiva.items():
        linhas = []
        for classe in ORDEM_CLASSES:
            papeis = classes.get(classe)
            if papeis is None:
                continue
            linhas.append(
                [t["classes"][classe.value]]
                + [rotulo(papeis.get(p)) for p in ORDEM_PAPEIS]
            )
        if linhas:
            secoes.append(
                {"titulo": t["modulos"].get(modulo, modulo), "texto": "",
                 "cabecalho": cabecalho, "linhas": linhas}
            )

    def rotulo_desvio(ambito: Ambito | None) -> str:
        return t["retirado"] if ambito is None else rotulo(ambito)

    desvios = []
    for modulo, classes in efetiva.items():
        for classe, papeis in classes.items():
            defeito = MATRIZ[modulo][classe]
            for papel in ORDEM_PAPEIS:
                if papeis.get(papel) is defeito.get(papel):
                    continue
                desvios.append([
                    t["modulos"].get(modulo, modulo),
                    t["classes"][classe.value],
                    t["papeis"][papel.value],
                    rotulo_desvio(defeito.get(papel)),
                    rotulo_desvio(papeis.get(papel)),
                ])

    # Como a organização decidiu, e não só o quê: editar a matriz célula a célula
    # é um facto que um auditor tem de conseguir ler no documento.
    from app.politica import store

    texto_desvios = t["desvios_intro"] if desvios else t["sem_desvios"]
    if desvios and store.editada_a_mao(empresa_id):
        texto_desvios = texto_desvios + "\n\n" + t["editada_a_mao"]

    secoes.append({
        "titulo": t["desvios_titulo"],
        "texto": texto_desvios,
        "cabecalho": (
            [t["col_modulo"], t["col_acao"], t["col_papel"],
             t["col_origem"], t["col_atual"]]
            if desvios else []
        ),
        "linhas": desvios,
    })

    return {
        "titulo": t["titulo"],
        "subtitulo": t["subtitulo"],
        "data_geracao": datetime.now(timezone.utc).isoformat(),
        "secoes": secoes,
    }


def _negar(
    modulo: str, classe: ClasseAcao, codigo: str = "sem_permissao"
) -> HTTPException:
    """403 com código estável (não texto cravado) → o frontend traduz por i18n."""
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={"codigo": codigo, "modulo": modulo, "acao": classe.value},
    )


def _registar_negacao(
    utilizador,
    modulo: str,
    classe: ClasseAcao,
    codigo: str = "sem_permissao",
    request=None,
) -> None:
    """Atalho local: a recusa desta matriz, no vocabulário da auditoria."""
    from app.shared.audit import registar_negacao

    registar_negacao(
        utilizador, modulo=modulo, acao=classe.value, codigo=codigo, request=request
    )


def require_capability(modulo: str, classe: ClasseAcao):
    """
    Factory de dependência FastAPI — irmã de require_role/require_feature.

    Responde a "este papel pode esta ação?". Não responde a "sobre este registo?"
    — para isso é o `exigir_ambito`, que corre no service com o registo em mão.

    Uso nos routers:
        @router.post("/x", dependencies=[Depends(require_capability("risco", ClasseAcao.OPERAR))])

    Ou como parâmetro injetado (para obter o utilizador):
        utilizador: Utilizador = Depends(require_capability("risco", ClasseAcao.OPERAR))
    """

    def verificador(request: Request, utilizador=Depends(get_current_user)):
        if not tem_capacidade(utilizador, modulo, classe):
            _registar_negacao(utilizador, modulo, classe, request=request)
            raise _negar(modulo, classe)
        return utilizador

    # Marca lida por `app/shared/gates.py` ao percorrer a árvore de dependências
    # de cada rota, para saber quais é que declaram quem as autoriza. Explícita,
    # e não deduzida do nome dos argumentos: assim renomear um parâmetro não
    # desliga em silêncio a verificação que corre no arranque.
    verificador._gate = ("capacidade", modulo, classe)
    return verificador


def exigir_ambito(
    utilizador,
    modulo: str,
    classe: ClasseAcao,
    dono_id,
) -> None:
    """
    Verifica se o utilizador pode agir SOBRE ESTE REGISTO.

    Complemento do `require_capability`: aquele decide se o papel tem a ação,
    este decide se a tem sobre este registo em concreto. Corre no service porque
    precisa do registo carregado, mas a decisão vem da matriz — não daqui.

    `dono_id` é o responsável/dono do registo (UUID, str ou None).

    Fail-closed em três pontos:
      - papel sem a capacidade → 403 (o gate do router pode ter sido esquecido)
      - âmbito desconhecido    → 403
      - âmbito atribuído sem dono definido → 403; um registo órfão não é de
        ninguém, e tratá-lo como sendo de quem o pediu abria-o a toda a gente

    Dois códigos distintos, como no caminho premium: quem tem a ação mas não o
    registo ouve "não lhe está atribuído" — dentro da mesma empresa isso não
    revela nada que a listagem já não mostre, e poupa uma chamada ao suporte.
    """
    ambito = ambito_de(utilizador, modulo, classe)
    if ambito is Ambito.TOTAL:
        return
    if ambito is Ambito.ATRIBUIDO:
        if dono_id and str(dono_id) == str(utilizador.id):
            return
        # Alcançar o registo de outra pessoa é a tentativa que mais interessa
        # ver repetida: sem registo, uma varredura registo a registo passa
        # despercebida. Corre no service, que não tem o request em mão — fica
        # sem IP, mas com quem, o quê e quando.
        _registar_negacao(utilizador, modulo, classe, "sem_permissao_recurso")
        raise _negar(modulo, classe, "sem_permissao_recurso")
    _registar_negacao(utilizador, modulo, classe)
    raise _negar(modulo, classe)


def dono_na_criacao(utilizador, modulo: str, dono_pedido):
    """
    Quem fica responsável por um registo NOVO, e se a atribuição é legítima.

    Quem alcança tudo atribui a quem quiser (incluindo a ninguém). Quem só alcança
    o que lhe está atribuído tem de ficar com o registo: criar já atribuído a
    outra pessoa seria delegar, e delegar é outra capacidade.

    Devolve o dono final; levanta 403 se a atribuição pedida não for permitida.
    """
    if not so_atribuidos(utilizador, modulo, ClasseAcao.OPERAR):
        return dono_pedido
    if dono_pedido and str(dono_pedido) != str(utilizador.id):
        _registar_negacao(utilizador, modulo, ClasseAcao.DELEGAR)
        raise _negar(modulo, ClasseAcao.DELEGAR)
    return utilizador.id


def exigir_delegacao_se_muda_dono(utilizador, modulo: str, dono_atual, dono_novo):
    """
    Passar um registo a outra pessoa é delegar — mesmo quando acontece como efeito
    de uma edição normal. Sem esta verificação, quem só alcança o que lhe está
    atribuído livrava-se do registo (ou tirava-o a um colega) por uma edição.
    """
    if str(dono_atual or "") == str(dono_novo or ""):
        return
    if not tem_capacidade(utilizador, modulo, ClasseAcao.DELEGAR):
        _registar_negacao(utilizador, modulo, ClasseAcao.DELEGAR)
        raise _negar(modulo, ClasseAcao.DELEGAR)
