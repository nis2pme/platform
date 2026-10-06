"""
Catálogo das ações de auditoria — vocabulário controlado da trilha.

A `class Acao` diz que códigos existem. Este catálogo diz o que cada código
SIGNIFICA para quem lê o registo: a que família pertence (é por aí que se
filtra), que entidade toca, e que peso tem.

Porque é um ficheiro à parte e não mais campos na `Acao`: a `Acao` é referenciada
em mais de uma centena de pontos de escrita e tem de continuar a ser uma
constante de texto simples. O catálogo é lido só por quem apresenta ou conta.

Três garantias que sustentam o resto:

1. **Cobertura total.** Há um teste que percorre a `Acao` e falha se um código
   não tiver entrada aqui, e ao contrário. Uma ação nova sem definição parte os
   testes — é isso que impede o vocabulário de voltar a dispersar.
2. **Nunca levanta.** Um código desconhecido (linha antiga, instalação com
   versão diferente) resolve para uma definição de recurso. A auditoria não pode
   fazer falhar a leitura do que já está gravado.
3. **Só para a frente.** A trilha nunca sofre UPDATE. Um código gravado com o
   significado errado corrige-se na ESCRITA e mapeia-se na LEITURA, através do
   `ALIAS_LEGADO` — as linhas antigas ficam exatamente como foram escritas.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.shared.audit import Acao


# ---------------------------------------------------------------------------
# Vocabulário
# ---------------------------------------------------------------------------

class Familia:
    """Agrupadores de filtro. É esta lista que povoa o seletor de família."""

    AUTENTICACAO = "autenticacao"
    UTILIZADORES = "utilizadores"
    EMPRESA = "empresa"
    CONTROLOS = "controlos"
    EVIDENCIAS = "evidencias"
    RELATORIOS = "relatorios"
    INCIDENTES = "incidentes"
    TAREFAS = "tarefas"
    FORMACAO = "formacao"
    ATIVOS = "ativos"
    RISCO = "risco"
    FORNECEDORES = "fornecedores"
    DOSSIE = "dossie"
    CONETORES = "conetores"
    IMPORTACAO = "importacao"
    BACKUPS = "backups"
    ASSISTENTE_IA = "assistente_ia"
    SISTEMA = "sistema"
    # Ações do plano de controlo da plataforma. Partilham a tabela com as do
    # tenant mas ficam sem empresa associada, por isso nunca aparecem no ecrã
    # de auditoria de um cliente. Estão aqui porque a coluna as contém.
    PLATAFORMA = "plataforma"
    # Só para códigos que não estão no catálogo — nunca atribuída à mão.
    DESCONHECIDA = "desconhecida"


class Severidade:
    """
    Peso do que aconteceu, para ordenar a atenção de quem lê.

    `CRITICO` não quer dizer "correu mal": quer dizer que a linha muda a postura
    de segurança da instalação ou tira dados de dentro dela. Alterar o papel de
    um utilizador é crítico e é um sucesso.
    """

    INFO = "info"
    AVISO = "aviso"
    CRITICO = "critico"


@dataclass(frozen=True)
class DefinicaoAcao:
    codigo: str
    familia: str
    entidade: str
    severidade: str
    # Conta para o indicador de falhas de autenticação. Separado da severidade
    # de propósito: a severidade é apresentação, isto é uma regra de contagem.
    falha_seguranca: bool = False


# ---------------------------------------------------------------------------
# Definições
# ---------------------------------------------------------------------------

_I = Severidade.INFO
_A = Severidade.AVISO
_C = Severidade.CRITICO
_F = Familia

_DEFINICOES: tuple[tuple[str, str, str, str, bool], ...] = (
    # (código, família, entidade, severidade, conta como falha de autenticação)

    # --- Autenticação -------------------------------------------------------
    (Acao.LOGIN_SUCESSO, _F.AUTENTICACAO, "Utilizador", _I, False),
    (Acao.LOGIN_FALHA, _F.AUTENTICACAO, "Utilizador", _A, True),
    (Acao.CONTA_BLOQUEADA, _F.AUTENTICACAO, "Utilizador", _C, True),
    (Acao.IP_BLOQUEADO, _F.AUTENTICACAO, "Login", _C, True),
    # Aviso, não crítico: uma recusa isolada é rotina (um ecrã aberto por
    # engano). O que interessa é o padrão, e para isso conta como falha de
    # segurança — assim entra nas contagens que revelam sondagem repetida.
    (Acao.ACESSO_NEGADO, _F.AUTENTICACAO, "Utilizador", _A, True),
    (Acao.LOGOUT, _F.AUTENTICACAO, "Utilizador", _I, False),
    (Acao.REFRESH_TOKEN, _F.AUTENTICACAO, "Utilizador", _I, False),
    (Acao.FA2_VERIFICADO, _F.AUTENTICACAO, "Utilizador", _I, False),
    (Acao.FA2_FALHOU, _F.AUTENTICACAO, "Utilizador", _A, True),
    (Acao.FA2_ATIVADO, _F.AUTENTICACAO, "Utilizador", _I, False),
    # Desligar o segundo fator baixa a proteção da conta — é decisão, não rotina.
    (Acao.FA2_DESATIVADO, _F.AUTENTICACAO, "Utilizador", _C, False),
    (Acao.BACKUP_CODE_USADO, _F.AUTENTICACAO, "Utilizador", _A, False),
    (Acao.PASSWORD_ALTERADA, _F.AUTENTICACAO, "Utilizador", _I, False),
    (Acao.PASSWORD_RESET_PEDIDO, _F.AUTENTICACAO, "Utilizador", _I, False),
    (Acao.PASSWORD_RESET_CONFIRMADO, _F.AUTENTICACAO, "Utilizador", _I, False),
    # Reposições feitas por um administrador dão acesso a uma conta que não é a
    # dele. É a linha que um auditor procura primeiro numa suspeita de abuso.
    (Acao.PASSWORD_RESET_ADMIN, _F.AUTENTICACAO, "Utilizador", _C, False),
    (Acao.MFA_RESET_ADMIN, _F.AUTENTICACAO, "Utilizador", _C, False),
    # Com acesso à máquina, sem passar pela aplicação: dá a mesma entrada numa
    # conta que o administrador não fez, e é a linha que distingue as duas origens.
    (Acao.PASSWORD_RESET_CONSOLA, _F.AUTENTICACAO, "Utilizador", _C, False),
    (Acao.FA2_RESET_CONSOLA, _F.AUTENTICACAO, "Utilizador", _C, False),

    # --- Gestão de utilizadores --------------------------------------------
    (Acao.UTILIZADOR_CRIADO, _F.UTILIZADORES, "Utilizador", _I, False),
    (Acao.UTILIZADOR_NOME_ALTERADO, _F.UTILIZADORES, "Utilizador", _I, False),
    (Acao.UTILIZADOR_DESATIVADO, _F.UTILIZADORES, "Utilizador", _A, False),
    (Acao.UTILIZADOR_REATIVADO, _F.UTILIZADORES, "Utilizador", _A, False),
    (Acao.UTILIZADOR_ROLE_ALTERADO, _F.UTILIZADORES, "Utilizador", _C, False),
    (Acao.UTILIZADOR_DELEGACAO_ATRIBUIDA, _F.UTILIZADORES, "Utilizador", _I, False),
    (Acao.UTILIZADOR_DELEGACAO_REMOVIDA, _F.UTILIZADORES, "Utilizador", _I, False),
    (Acao.UTILIZADOR_ANONIMIZADO, _F.UTILIZADORES, "Utilizador", _C, False),

    # --- Empresa ------------------------------------------------------------
    (Acao.EMPRESA_REGISTADA, _F.EMPRESA, "Empresa", _I, False),
    (Acao.EMPRESA_DADOS_ATUALIZADOS, _F.EMPRESA, "Empresa", _I, False),
    (Acao.EMPRESA_DADOS_EXPORTADOS, _F.EMPRESA, "Empresa", _A, False),
    (Acao.EMPRESA_ELIMINACAO_PEDIDA, _F.EMPRESA, "Empresa", _C, False),
    (Acao.EMPRESA_SUSPENSA, _F.EMPRESA, "Empresa", _C, False),
    (Acao.EMPRESA_REATIVADA, _F.EMPRESA, "Empresa", _A, False),

    # --- Controlos ----------------------------------------------------------
    (Acao.CONTROLO_ESTADO_ALTERADO, _F.CONTROLOS, "Controlo", _I, False),
    (Acao.CONTROLO_NIVEL_ALTERADO, _F.CONTROLOS, "Controlo", _I, False),
    (Acao.CONTROLO_CHECK_CONCLUIDO, _F.CONTROLOS, "Controlo", _I, False),
    (Acao.CONTROLO_CHECK_REVERTIDO, _F.CONTROLOS, "Controlo", _A, False),
    (Acao.CONTROLO_APROVADO, _F.CONTROLOS, "Controlo", _I, False),
    (Acao.CONTROLO_NAO_APROVADO, _F.CONTROLOS, "Controlo", _A, False),
    # Excluir um controlo do âmbito muda o que a conformidade mede. Fica sempre
    # com justificação, e um auditor tem de a conseguir encontrar.
    (Acao.CONTROLO_NAO_APLICAVEL, _F.CONTROLOS, "Controlo", _A, False),
    (Acao.CONTROLO_REAPLICADO, _F.CONTROLOS, "Controlo", _I, False),

    # --- Evidências ---------------------------------------------------------
    (Acao.EVIDENCIA_UPLOAD, _F.EVIDENCIAS, "Evidencia", _I, False),
    (Acao.EVIDENCIA_ELIMINADA, _F.EVIDENCIAS, "Evidencia", _A, False),
    (Acao.EVIDENCIA_RECONCILIADA, _F.EVIDENCIAS, "Evidencia", _A, False),
    (Acao.EVIDENCIA_LIGADA, _F.EVIDENCIAS, "Evidencia", _I, False),
    # Desligar tira prova a um controlo sem apagar nada — quem lê a auditoria à
    # procura de uma quebra de conformidade tem de dar por isto.
    (Acao.EVIDENCIA_DESLIGADA, _F.EVIDENCIAS, "Evidencia", _A, False),
    (Acao.EVIDENCIA_AMBITO_ALTERADO, _F.EVIDENCIAS, "Evidencia", _I, False),
    (Acao.EVIDENCIA_VERSAO_CRIADA, _F.EVIDENCIAS, "Evidencia", _I, False),
    (Acao.EVIDENCIA_METADADOS_ALTERADOS, _F.EVIDENCIAS, "Evidencia", _I, False),
    (Acao.EVIDENCIA_RESTAURADA, _F.EVIDENCIAS, "Evidencia", _I, False),
    # A reciclagem apaga conteúdo sem ninguém carregar num botão. É a única ação
    # deste módulo sem autor humano, e por isso a que mais precisa de rasto.
    (Acao.EVIDENCIA_RECICLADA, _F.EVIDENCIAS, "Evidencia", _A, False),
    # Apagamento irreversível a pedido do titular: sai conteúdo da instalação.
    # Legado — mantém-se para as linhas antigas se continuarem a ler.
    (Acao.EVIDENCIA_APAGADA_RGPD, _F.EVIDENCIAS, "Evidencia", _C, False),
    # Apagar de vez uma órfã que nunca foi prova: irreversível, com razão.
    (Acao.EVIDENCIA_APAGADA_DEFINITIVAMENTE, _F.EVIDENCIAS, "Evidencia", _A, False),
    # Apagar prova com fundamento: atravessa a retenção e deixa lápide.
    (Acao.EVIDENCIA_APAGADA_COM_LAPIDE, _F.EVIDENCIAS, "Evidencia", _C, False),
    (Acao.EVIDENCIA_REAPAGADA_POS_RESTAURO, _F.EVIDENCIAS, "Evidencia", _A, False),
    # Quem viu o conteúdo de uma prova. Ver fica na instalação; descarregar leva
    # uma cópia para fora dela, como um relatório exportado.
    (Acao.EVIDENCIA_VISUALIZADA, _F.EVIDENCIAS, "Evidencia", _I, False),
    (Acao.EVIDENCIA_DESCARREGADA, _F.EVIDENCIAS, "Evidencia", _A, False),

    # --- Relatórios ---------------------------------------------------------
    # Um relatório exportado é conteúdo de conformidade que sai da aplicação.
    (Acao.RELATORIO_EXPORTADO, _F.RELATORIOS, "Relatorio", _A, False),
    (Acao.RELATORIO_AUDITORIA_CRIADO, _F.RELATORIOS, "Relatorio", _I, False),

    # --- Assistente (premium) ----------------------------------------------
    (Acao.ANALISE_IA_SOLICITADA, _F.ASSISTENTE_IA, "AnaliseIA", _I, False),
    (Acao.ANALISE_IA_CONCLUIDA, _F.ASSISTENTE_IA, "AnaliseIA", _I, False),
    (Acao.ANALISE_IA_ERRO, _F.ASSISTENTE_IA, "AnaliseIA", _A, False),

    # --- Inventário de ativos (premium) ------------------------------------
    (Acao.ATIVO_CRIADO, _F.ATIVOS, "Ativo", _I, False),
    (Acao.ATIVO_ATUALIZADO, _F.ATIVOS, "Ativo", _I, False),
    (Acao.ATIVO_ELIMINADO, _F.ATIVOS, "Ativo", _A, False),
    (Acao.ATIVO_CRITICIDADE_CLASSIFICADA, _F.ATIVOS, "Ativo", _I, False),
    (Acao.ATIVO_DEPENDENCIAS_DEFINIDAS, _F.ATIVOS, "Ativo", _I, False),
    (Acao.ATIVO_REVISAO_REGISTADA, _F.ATIVOS, "Ativo", _I, False),
    (Acao.ATIVO_RESPONSAVEL_ATRIBUIDO, _F.ATIVOS, "Ativo", _I, False),
    # Abate com sanitização: o ativo deixou de existir e os dados que continha
    # foram destruídos. Sem esta linha não há prova de que o foram.
    (Acao.ATIVO_SANITIZADO, _F.ATIVOS, "Ativo", _A, False),

    # --- Risco (premium) ----------------------------------------------------
    (Acao.RISCO_CRIADO, _F.RISCO, "Risco", _I, False),
    (Acao.RISCO_ATUALIZADO, _F.RISCO, "Risco", _I, False),
    (Acao.RISCO_ELIMINADO, _F.RISCO, "Risco", _A, False),
    (Acao.RISCO_REAVALIADO, _F.RISCO, "Risco", _I, False),
    # O apetite ao risco é decisão do órgão de gestão e move o limiar de tudo
    # o resto — uma alteração aqui reclassifica riscos sem lhes tocar.
    (Acao.RISCO_APETITE_DEFINIDO, _F.RISCO, "Risco", _C, False),
    (Acao.RISCO_DONO_ATRIBUIDO, _F.RISCO, "Risco", _I, False),
    (Acao.TRATAMENTO_CRIADO, _F.RISCO, "Tratamento", _I, False),
    (Acao.TRATAMENTO_ATUALIZADO, _F.RISCO, "Tratamento", _I, False),
    (Acao.TRATAMENTO_ELIMINADO, _F.RISCO, "Tratamento", _A, False),

    # --- Incidentes ---------------------------------------------------------
    (Acao.INCIDENTE_CRIADO, _F.INCIDENTES, "Incidente", _A, False),
    (Acao.INCIDENTE_ATUALIZADO, _F.INCIDENTES, "Incidente", _I, False),
    (Acao.INCIDENTE_ELIMINADO, _F.INCIDENTES, "Incidente", _A, False),
    (Acao.INCIDENTE_ESTADO_ALTERADO, _F.INCIDENTES, "Incidente", _I, False),
    (Acao.INCIDENTE_MARCO_REGISTADO, _F.INCIDENTES, "Incidente", _I, False),
    (Acao.INCIDENTE_NOTIFICACAO_REGISTADA, _F.INCIDENTES, "Incidente", _A, False),
    (Acao.INCIDENTE_EVENTO_ADICIONADO, _F.INCIDENTES, "Incidente", _I, False),

    # --- Tarefas recorrentes ------------------------------------------------
    (Acao.TAREFA_CRIADA, _F.TAREFAS, "Tarefa", _I, False),
    (Acao.TAREFA_ATUALIZADA, _F.TAREFAS, "Tarefa", _I, False),
    (Acao.TAREFA_ELIMINADA, _F.TAREFAS, "Tarefa", _A, False),
    (Acao.TAREFA_CONCLUIDA, _F.TAREFAS, "Tarefa", _I, False),

    # --- Fornecedores (premium) --------------------------------------------
    (Acao.FORNECEDOR_CRIADO, _F.FORNECEDORES, "Fornecedor", _I, False),
    (Acao.FORNECEDOR_ATUALIZADO, _F.FORNECEDORES, "Fornecedor", _I, False),
    (Acao.FORNECEDOR_ELIMINADO, _F.FORNECEDORES, "Fornecedor", _A, False),
    (Acao.FORNECEDOR_AVALIADO, _F.FORNECEDORES, "Fornecedor", _I, False),
    (Acao.FORNECEDOR_RESPONSAVEL_ATRIBUIDO, _F.FORNECEDORES, "Fornecedor", _I, False),

    # --- Formação -----------------------------------------------------------
    (Acao.FORMACAO_CRIADA, _F.FORMACAO, "Formacao", _I, False),
    (Acao.FORMACAO_ATUALIZADA, _F.FORMACAO, "Formacao", _I, False),
    (Acao.FORMACAO_ELIMINADA, _F.FORMACAO, "Formacao", _A, False),
    (Acao.FORMACAO_ESTADO_ALTERADO, _F.FORMACAO, "Formacao", _I, False),
    (Acao.FORMACAO_PARTICIPANTE_ADICIONADO, _F.FORMACAO, "Formacao", _I, False),
    (Acao.FORMACAO_PARTICIPANTE_REMOVIDO, _F.FORMACAO, "Formacao", _I, False),
    (Acao.FORMACAO_PRESENCA_MARCADA, _F.FORMACAO, "Formacao", _I, False),

    # --- Dossiê para o auditor ---------------------------------------------
    (Acao.DOSSIE_GERADO, _F.DOSSIE, "Dossie", _A, False),
    (Acao.PARECER_IMPORTADO, _F.DOSSIE, "Dossie", _I, False),
    # Define em quem a instalação confia para assinar o que dela sai.
    (Acao.DOSSIE_ATESTACAO_DEFINIDA, _F.DOSSIE, "Dossie", _C, False),

    # --- Conetores (premium) ------------------------------------------------
    (Acao.CONETOR_CONFIGURADO, _F.CONETORES, "Conetor", _A, False),
    (Acao.CONETOR_VERIFICACAO, _F.CONETORES, "Conetor", _I, False),
    (Acao.CONETOR_DRIFT_DETETADO, _F.CONETORES, "Conetor", _A, False),
    (Acao.CONETOR_EVENTO_RESOLVIDO, _F.CONETORES, "Conetor", _I, False),
    (Acao.CONETOR_REMOVIDO, _F.CONETORES, "Conetor", _A, False),
    # Descartar um alerta grave é decidir que não houve incidente: fica à vista.
    (Acao.CONETOR_ALERTA_DECIDIDO, _F.CONETORES, "Conetor", _A, False),

    # --- Importação (premium) ----------------------------------------------
    # Analisar e simular não escrevem nada; aplicar e reverter escrevem.
    (Acao.IMPORTACAO_ANALISADA, _F.IMPORTACAO, "Importacao", _I, False),
    (Acao.IMPORTACAO_SIMULADA, _F.IMPORTACAO, "Importacao", _I, False),
    (Acao.IMPORTACAO_APLICADA, _F.IMPORTACAO, "Importacao", _A, False),
    (Acao.IMPORTACAO_REVERTIDA, _F.IMPORTACAO, "Importacao", _A, False),
    (Acao.IMPORTACAO_DESCOBERTA_DECIDIDA, _F.IMPORTACAO, "Importacao", _A, False),

    # --- Cópias de segurança ------------------------------------------------
    (Acao.BACKUP_CRIADO, _F.BACKUPS, "Backup", _I, False),
    (Acao.BACKUP_ELIMINADO, _F.BACKUPS, "Backup", _A, False),
    # Um backup é a instalação inteira num ficheiro. Descarregar, importar,
    # restaurar ou mudar a frase-passe são as ações de maior alcance que existem.
    (Acao.BACKUP_DESCARREGADO, _F.BACKUPS, "Backup", _C, False),
    (Acao.BACKUP_PASSPHRASE_DEFINIDA, _F.BACKUPS, "Backup", _C, False),
    (Acao.BACKUP_AGENDADO_ALTERADO, _F.BACKUPS, "Backup", _A, False),
    (Acao.BACKUP_IMPORTADO, _F.BACKUPS, "Backup", _C, False),
    (Acao.RESTAURO_EXECUTADO, _F.BACKUPS, "Backup", _C, False),

    # --- Sistema ------------------------------------------------------------
    (Acao.POLITICA_ALTERADA, _F.SISTEMA, "Politica", _C, False),
    # Quem muda o servidor de saída passa a decidir por onde saem as
    # recuperações de password e os avisos de prazo legal.
    (Acao.SISTEMA_EMAIL_CONFIGURADO, _F.SISTEMA, "Sistema", _C, False),
    # Descer o TLS para HTTP ou trocar o certificado muda o que protege as sessões.
    (Acao.SISTEMA_HTTPS_CONFIGURADO, _F.SISTEMA, "Sistema", _C, False),
    (Acao.SISTEMA_ATUALIZACOES_CONFIGURADAS, _F.SISTEMA, "Sistema", _A, False),
    # Código novo a correr como root no anfitrião, pedido de dentro da app.
    (Acao.SISTEMA_ATUALIZACAO_PEDIDA, _F.SISTEMA, "Sistema", _C, False),
    (Acao.SISTEMA_ATUALIZACAO_CONCLUIDA, _F.SISTEMA, "Sistema", _C, False),
    (Acao.SISTEMA_ATUALIZACAO_FALHADA, _F.SISTEMA, "Sistema", _C, False),
    (Acao.LICENCA_INSTALADA, _F.SISTEMA, "Sistema", _C, False),
    # A própria trilha: um mês saiu da janela, ou alguém a levou para fora.
    (Acao.AUDIT_PURGADO, _F.SISTEMA, "AuditLog", _A, False),
    (Acao.AUDIT_EXPORTADO, _F.SISTEMA, "AuditLog", _A, False),
    (Acao.AUDIT_CADEIA_PARTIDA, _F.SISTEMA, "AuditLog", _C, False),
    # Linhas escritas depois do início da cadeia e fora dela (escrita direta, ou
    # uma sessão própria que não teve o head a tempo): não partem nada, mas não
    # se podem dar por verdadeiras.
    (Acao.AUDIT_CADEIA_SEM_ELO, _F.SISTEMA, "AuditLog", _A, False),
    # Uma cabeça que o fornecedor testemunhou desapareceu da trilha.
    (Acao.AUDIT_TESTEMUNHO_DIVERGENTE, _F.SISTEMA, "AuditLog", _A, False),
    # Lembretes lidos que saíram da janela de retenção.
    (Acao.NOTIFICACOES_PURGADAS, _F.SISTEMA, "Notificacao", _I, False),
)

# Vocabulário do plano de controlo da plataforma. Esses códigos não estão na
# classe `Acao` — vivem no container que os escreve — mas passam pelo mesmo
# helper e acabam na mesma coluna. Sem definição aqui, cada operação de
# plataforma deixaria um aviso no log e apareceria sem família a quem a lê.
_DEFINICOES_PLATAFORMA: tuple[tuple[str, str, str, str, bool], ...] = (
    ("superadmin.login_sucesso", _F.PLATAFORMA, "SuperAdmin", _A, False),
    ("superadmin.login_falha", _F.PLATAFORMA, "SuperAdmin", _C, True),
    ("superadmin.logout", _F.PLATAFORMA, "SuperAdmin", _I, False),
    ("superadmin.2fa_verificado", _F.PLATAFORMA, "SuperAdmin", _I, False),
    ("superadmin.2fa_ativado", _F.PLATAFORMA, "SuperAdmin", _I, False),
    ("superadmin.2fa_falhou", _F.PLATAFORMA, "SuperAdmin", _C, True),
    ("superadmin.empresa_criada", _F.PLATAFORMA, "Empresa", _A, False),
    ("superadmin.empresa_atualizada", _F.PLATAFORMA, "Empresa", _A, False),
    ("superadmin.empresa_suspensa", _F.PLATAFORMA, "Empresa", _C, False),
    ("superadmin.empresa_ativada", _F.PLATAFORMA, "Empresa", _A, False),
    # Eliminação em cascata, sem retorno.
    ("superadmin.empresa_eliminada", _F.PLATAFORMA, "Empresa", _C, False),
    ("superadmin.empresa_purgada", _F.PLATAFORMA, "Empresa", _C, False),
    ("superadmin.admin_password_reset", _F.PLATAFORMA, "Utilizador", _C, False),
    ("superadmin.admin_mfa_reset", _F.PLATAFORMA, "Utilizador", _C, False),
    # Palavra-passe e segundo fator repostos de uma vez.
    ("superadmin.admin_reset_tudo", _F.PLATAFORMA, "Utilizador", _C, False),
    # Plano de um tenant escrito no gateway (trial/standard/nenhum) e estado de um
    # selo de auditor no diretório (publicado/suspenso/pendente).
    ("superadmin.empresa_plano", _F.PLATAFORMA, "Empresa", _A, False),
    # Planos de todas as empresas escritos de novo no gateway (base dos direitos reposta).
    ("superadmin.planos_repostos", _F.PLATAFORMA, "Empresa", _A, False),
    ("superadmin.auditor_estado", _F.PLATAFORMA, "Auditor", _A, False),
    # Catálogo de frameworks (de todos os tenants): importar, reimportar, mudar o
    # predefinido (migra as empresas ativas) e eliminar.
    ("superadmin.framework_importado", _F.PLATAFORMA, "Framework", _A, False),
    ("superadmin.framework_reimportado", _F.PLATAFORMA, "Framework", _A, False),
    ("superadmin.framework_predefinido", _F.PLATAFORMA, "Framework", _C, False),
    ("superadmin.framework_eliminado", _F.PLATAFORMA, "Framework", _C, False),
    # Recuperação da conta de um operador pelo script local (password, 2FA, bloqueio).
    ("superadmin.recuperacao_local", _F.PLATAFORMA, "SuperAdmin", _C, False),
)

CODIGOS_PLATAFORMA: frozenset[str] = frozenset(
    codigo for codigo, *_resto in _DEFINICOES_PLATAFORMA
)

CATALOGO: dict[str, DefinicaoAcao] = {
    codigo: DefinicaoAcao(codigo, familia, entidade, severidade, falha)
    for codigo, familia, entidade, severidade, falha in (
        *_DEFINICOES, *_DEFINICOES_PLATAFORMA
    )
}

# Ordem de apresentação das famílias. É esta sequência que o seletor mostra —
# as que aparecem mais na trilha vêm primeiro.
FAMILIAS: tuple[str, ...] = (
    _F.AUTENTICACAO, _F.UTILIZADORES, _F.CONTROLOS, _F.EVIDENCIAS,
    _F.INCIDENTES, _F.TAREFAS, _F.RISCO, _F.ATIVOS, _F.FORNECEDORES,
    _F.FORMACAO, _F.RELATORIOS, _F.DOSSIE, _F.CONETORES, _F.IMPORTACAO,
    _F.ASSISTENTE_IA, _F.BACKUPS, _F.EMPRESA, _F.SISTEMA, _F.PLATAFORMA,
)

# Códigos que contam para o indicador de falhas de autenticação. Deriva do
# catálogo em vez de ser uma lista escrita à mão — era assim que o indicador
# antigo contava a password errada e esquecia a conta e o endereço bloqueados.
CODIGOS_FALHA_SEGURANCA: tuple[str, ...] = tuple(
    definicao.codigo for definicao in CATALOGO.values() if definicao.falha_seguranca
)


# ---------------------------------------------------------------------------
# Correções de leitura (a trilha nunca é reescrita)
# ---------------------------------------------------------------------------

# (código gravado, entidade_tipo gravado) -> código correto.
#
# A entidade faz parte da chave porque é ela que desfaz a ambiguidade: o mesmo
# código foi usado para duas coisas diferentes, e só o tipo da entidade afetada
# distingue uma da outra. As linhas continuam a devolver o `acao` original; o
# código corrigido vem noutro campo.
ALIAS_LEGADO: dict[tuple[str, str], str] = {
    # Alterar o nome de um utilizador ficava registado como atualização de dados
    # da empresa. A escrita já foi corrigida; isto trata do que ficou gravado.
    (Acao.EMPRESA_DADOS_ATUALIZADOS, "Utilizador"): Acao.UTILIZADOR_NOME_ALTERADO,
}

# Nome interno da tabela -> nome que se mostra. O valor gravado NÃO muda: quem
# filtra por entidade continua a poder usar o nome interno.
ENTIDADE_APRESENTACAO: dict[str, str] = {
    "ControloEmpresaV2": "Controlo",
    "ControloEmpresaCheckV2": "Controlo",
    "AcaoFormacao": "Formacao",
    "PoliticaCapacidade": "Politica",
    "DefinicoesRisco": "Risco",
}

# Caminho inverso, para o filtro por entidade aceitar o nome apresentado.
_ENTIDADE_ARMAZENADA: dict[str, list[str]] = {}
for _interno, _visivel in ENTIDADE_APRESENTACAO.items():
    _ENTIDADE_ARMAZENADA.setdefault(_visivel, []).append(_interno)


# ---------------------------------------------------------------------------
# Consulta
# ---------------------------------------------------------------------------

def _entidade_provavel(codigo: str) -> str:
    """Entidade deduzida do prefixo, para códigos que o catálogo não conhece."""
    prefixo = codigo.split(".", 1)[0]
    return prefixo.capitalize() if prefixo else "—"


def resolver_codigo(codigo: str, entidade_tipo: str | None = None) -> str:
    """Devolve o código canónico de uma linha já gravada."""
    if entidade_tipo:
        alias = ALIAS_LEGADO.get((codigo, entidade_tipo))
        if alias:
            return alias
    return codigo


def definicao(codigo: str, entidade_tipo: str | None = None) -> DefinicaoAcao:
    """
    Definição de um código gravado, com os alias de leitura já aplicados.

    Nunca levanta: um código que o catálogo não conhece devolve uma definição
    construída na hora. A alternativa seria uma listagem que rebenta por causa
    de uma linha escrita por outra versão da aplicação.
    """
    canonico = resolver_codigo(codigo, entidade_tipo)
    encontrada = CATALOGO.get(canonico)
    if encontrada is not None:
        return encontrada
    return DefinicaoAcao(
        codigo=canonico,
        familia=Familia.DESCONHECIDA,
        entidade=_entidade_provavel(canonico),
        severidade=Severidade.INFO,
    )


def codigos_da_familia(familia: str) -> list[str]:
    """
    Códigos a procurar para filtrar por família.

    Inclui os códigos legados que resolvem para esta família: sem isso, filtrar
    por «utilizadores» perderia as linhas antigas de alteração de nome, que
    estão gravadas com o código da família «empresa».
    """
    codigos = [d.codigo for d in CATALOGO.values() if d.familia == familia]
    for (gravado, _entidade), canonico in ALIAS_LEGADO.items():
        alvo = CATALOGO.get(canonico)
        if alvo is not None and alvo.familia == familia and gravado not in codigos:
            codigos.append(gravado)
    return codigos


def entidade_apresentacao(entidade_tipo: str | None) -> str | None:
    """Nome de entidade tal como se mostra a quem lê."""
    if not entidade_tipo:
        return None
    return ENTIDADE_APRESENTACAO.get(entidade_tipo, entidade_tipo)


def entidades_armazenadas(entidade_tipo: str) -> list[str]:
    """
    Valores gravados que correspondem a um nome de entidade.

    Aceita tanto o nome interno como o apresentado, para o filtro funcionar com
    aquilo que a listagem mostra — dois nomes internos podem partilhar o mesmo
    nome visível.
    """
    return _ENTIDADE_ARMAZENADA.get(entidade_tipo, [entidade_tipo])
