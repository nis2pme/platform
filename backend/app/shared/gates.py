"""
Nenhuma rota da API fica sem quem a autorize.

A autorização é declarada rota a rota, e por isso uma rota nova nasce ABERTA até
alguém se lembrar de a fechar. Foi assim que a pesquisa global passou a devolver
o que a matriz negava: estava no meio de uma dúzia de rotas sem gate, e não
havia lista nenhuma onde isso saltasse à vista.

Este módulo inverte esse defeito uma vez, num sítio: percorre as rotas montadas
e recusa o arranque se alguma exigir autorização nenhuma sem estar declarada em
`ROTAS_ABERTAS`, com a razão escrita. Uma rota deliberadamente pública continua a
ser possível — o que deixa de ser possível é uma esquecida esconder-se entre elas.

Corre no import da aplicação, logo falha primeiro em desenvolvimento e em CI (a
suíte importa `app.main`), muito antes de qualquer instalação. É a mesma postura
que a configuração já tem: uma instalação mal configurada não arranca.

O que este módulo NÃO faz, de propósito:
  - não sabe QUAL a célula certa para cada rota — isso é julgamento humano, e a
    coerência entre as células declaradas e as impostas tem teste próprio;
  - não olha para a política de nenhuma empresa: pergunta se a rota declara
    ALGUMA coisa, o que é propriedade do código e vale para todos os tenants ao
    mesmo tempo. Se lesse a matriz efetiva precisaria da base de dados no
    arranque e poderia falhar por causa da configuração de um cliente;
  - não substitui a verificação em cada pedido, que continua a ser dos gates.
"""
from __future__ import annotations

# Prefixo das rotas da aplicação. O plano de controlo do superadministrador é
# montado à parte e protege-se por outra via (rede e token próprio).
PREFIXO_API = "/api"

# Rotas que respondem sem exigir autorização, e a razão de cada uma. Uma entrada
# aqui é uma afirmação — "isto é deliberado" —, e sem razão escrita é o mesmo que
# não haver lista nenhuma.
#
# O `require_feature` NÃO conta como gate: responde "este tenant pagou?" com 402,
# não "esta pessoa pode?". Uma rota que só o tenha aparece aqui na mesma.
ROTAS_ABERTAS: dict[tuple[str, str], str] = {
    ("GET", "/api/health"): (
        "Sonda de saúde. Não toca em dados de nenhum tenant."
    ),
    # ── Antes de haver sessão ──────────────────────────────────────────────────
    # Autenticar é o que PRODUZ a identidade: exigir aqui uma capacidade seria
    # exigi-la a quem ainda não tem nenhuma. Estas rotas protegem-se por outros
    # meios — limitação de tentativas por IP, bloqueio por conta, segredos de uso
    # único e comparação em tempo constante.
    ("POST", "/api/auth/login"): "Autenticação. Limitada por IP e por conta.",
    ("POST", "/api/auth/login/verificar-2fa"): "Segundo fator, com token temporário.",
    ("POST", "/api/auth/login/setup-2fa/iniciar"): (
        "Configuração do segundo fator quando ele é obrigatório e ainda não existe."
    ),
    ("POST", "/api/auth/login/setup-2fa/confirmar"): "Idem, confirmação.",
    ("POST", "/api/auth/login/alterar-password-temporaria"): (
        "Troca obrigatória da credencial temporária, antes de haver acesso."
    ),
    ("POST", "/api/auth/refresh"): "Renovação da sessão pelo cookie de refresh.",
    ("POST", "/api/auth/logout"): "Terminar a sessão é sempre permitido.",
    ("GET", "/api/auth/me"): (
        "Quem sou eu e o que posso — é daqui que o cliente recebe as suas "
        "capacidades. Exigir uma capacidade para as ir buscar era circular."
    ),
    ("POST", "/api/auth/register"): (
        "Registo de empresa nova (só em modo SaaS). Não há ainda tenant."
    ),
    ("POST", "/api/auth/password-reset/solicitar"): (
        "Recuperação de acesso: por definição, de quem não consegue autenticar-se."
    ),
    ("POST", "/api/auth/password-reset/regras"): (
        "Idem: o mínimo de password que o reset vai exigir, só a quem traz um token válido."
    ),
    ("POST", "/api/auth/password-reset/confirmar"): "Idem, com segredo de uso único.",
    ("POST", "/api/auth/2fa/configurar"): "Configurar o próprio segundo fator.",
    ("POST", "/api/auth/2fa/ativar"): "Ativar o próprio segundo fator.",
    # ── Arranque da instalação ─────────────────────────────────────────────────
    # Correm antes de existir administrador; o que as protege é a própria
    # instalação ainda não estar configurada (e o serviço recusa repetir o
    # arranque depois de o estar).
    ("GET", "/api/setup/status"): "Diz se a instalação já foi configurada.",
    ("GET", "/api/setup/tls"): "Modo de TLS escolhido no primeiro arranque.",
    ("POST", "/api/setup/iniciar"): "Primeiro passo do arranque da instalação.",
    ("POST", "/api/setup/configurar"): (
        "Cria a empresa e o primeiro administrador. Só antes de existir um."
    ),
    # ── O próprio utilizador ───────────────────────────────────────────────────
    ("GET", "/api/utilizadores/me"): (
        "Ver o próprio perfil não é capacidade: é de toda a gente, e por isso "
        "não consta da matriz."
    ),
    ("POST", "/api/utilizadores/me/password"): (
        "Alterar a própria password. Exige a password atual no corpo."
    ),
    ("PATCH", "/api/utilizadores/{utilizador_id}"): (
        "Editar o próprio perfil. O serviço restringe: a administração alcança "
        "qualquer pessoa, os restantes apenas a si próprios."
    ),
    ("GET", "/api/utilizadores"): (
        "Listagem restringida no serviço: quem administra vê a equipa, os "
        "restantes veem-se apenas a si."
    ),
    ("GET", "/api/utilizadores/{utilizador_id}"): (
        "Mesma restrição da listagem, aplicada a um registo."
    ),
    # ── Contexto que o ecrã precisa para funcionar ─────────────────────────────
    ("GET", "/api/empresas/me"): (
        "A matriz não declara leitura em `empresa` de propósito: os dados "
        "básicos (nome, classificação) são lidos por toda a gente para o "
        "próprio ecrã funcionar. O que é de administração é ALTERÁ-LOS."
    ),
    ("GET", "/api/documentos"): (
        "Catálogo de modelos de documentos da plataforma — não são dados do "
        "tenant."
    ),
    ("GET", "/api/documentos/{doc_id}/download"): (
        "Descarregar um modelo em branco. Idem: não é conteúdo da empresa."
    ),
    ("GET", "/api/dossie/selo"): (
        "Selo de auditoria externa do painel (\"dossiê de {data} revisto por "
        "{auditor}\"). Está num router à parte precisamente para ficar fora do "
        "gate de leitura do dossiê e alimentar o painel de toda a equipa."
    ),
    # ── Notificações: são sempre do próprio ────────────────────────────────────
    # O âmbito sai do token, nunca de um parâmetro: o único conjunto acessível é
    # o de quem está a pedir, e por isso não há nada que uma célula da matriz
    # pudesse decidir aqui.
    ("GET", "/api/notificacoes"): "As notificações do próprio utilizador.",
    ("GET", "/api/notificacoes/resumo"): (
        "Contadores das notificações do próprio."
    ),
    ("GET", "/api/notificacoes/catalogo"): (
        "Vocabulário de categorias e severidades — não são dados do tenant."
    ),
    ("PUT", "/api/notificacoes/{notificacao_id}/lida"): (
        "Marcar como lida uma notificação do próprio."
    ),
    ("PUT", "/api/notificacoes/{notificacao_id}/nao-lida"): (
        "Repor como não lida uma notificação do próprio."
    ),
    ("POST", "/api/notificacoes/marcar-lidas"): (
        "Marcar em lote as notificações do próprio que correspondem aos filtros."
    ),
    ("PUT", "/api/notificacoes/entidade/{entidade_tipo}/{entidade_id}/lidas"): (
        "Idem, em lote pelos avisos de uma entidade visitada."
    ),
    ("PUT", "/api/notificacoes/controlo/{controlo_empresa_id}/lidas"): (
        "Forma abreviada da anterior, para controlos."
    ),
    # ── Autoriza por resultado, não por rota ───────────────────────────────────
    ("GET", "/api/pesquisa"): (
        "A pesquisa atravessa oito módulos e não tem uma célula que a descreva: "
        "cada TIPO de resultado é autorizado pela célula que a tabela "
        "`PESQUISAVEIS` lhe associa, e o âmbito é aplicado na consulta. "
        "Autorizar aqui à entrada seria autorizar tudo ou nada."
    ),
}


def gates_da_rota(dependant) -> set:
    """Os gates que correm antes deste handler, incluindo os herdados do router.

    O FastAPI guarda a árvore de dependências resolvida em `route.dependant`;
    percorrê-la apanha tanto o que a rota declara como o que herda do
    `APIRouter`, que é onde a maioria dos módulos a declara.
    """
    encontrados = set()
    pilha = [dependant]
    vistos = set()
    while pilha:
        d = pilha.pop()
        if id(d) in vistos:
            continue
        vistos.add(id(d))
        marca = getattr(getattr(d, "call", None), "_gate", None)
        if marca is not None:
            encontrados.add(marca)
        pilha.extend(getattr(d, "dependencies", []))
    return encontrados


def _rotas_da_api(app):
    """(métodos, caminho, gates) de cada rota da API, ignorando o resto."""
    for rota in app.routes:
        dependant = getattr(rota, "dependant", None)
        metodos = getattr(rota, "methods", None)
        caminho = str(getattr(rota, "path", ""))
        if dependant is None or not metodos or not caminho.startswith(PREFIXO_API):
            continue
        for metodo in sorted(metodos - {"HEAD", "OPTIONS"}):
            yield metodo, caminho, gates_da_rota(dependant)


def rotas_sem_gate(app) -> list[tuple[str, str]]:
    """As rotas que não exigem autorização nenhuma e não estão declaradas."""
    return [
        (metodo, caminho)
        for metodo, caminho, gates in _rotas_da_api(app)
        if not gates and (metodo, caminho) not in ROTAS_ABERTAS
    ]


def declaracoes_obsoletas(app) -> list[tuple[str, str]]:
    """Entradas de `ROTAS_ABERTAS` que já não correspondem a rota aberta nenhuma.

    Uma isenção que deixou de fazer falta tem de sair, senão volta a esconder o
    defeito que a lista existe para tornar visível — e se a rota passou a ter
    gate, a entrada passou a afirmar uma coisa falsa sobre a aplicação.
    """
    abertas = {
        (metodo, caminho)
        for metodo, caminho, gates in _rotas_da_api(app)
        if not gates
    }
    return sorted(set(ROTAS_ABERTAS) - abertas)


def verificar_gates(app) -> None:
    """Recusa o arranque se alguma rota da API ficou sem quem a autorize."""
    faltam = rotas_sem_gate(app)
    if faltam:
        lista = "\n".join(f"  {metodo:6} {caminho}" for metodo, caminho in sorted(faltam))
        raise RuntimeError(
            "Rotas da API sem autorização declarada:\n"
            f"{lista}\n"
            "Declare um gate na rota (ou no router), ou — se for mesmo para "
            "responder a qualquer utilizador autenticado — acrescente-a a "
            "`ROTAS_ABERTAS` em app/shared/gates.py, com a razão escrita."
        )
