"""
Modelo AuditLog e helper registar_acao().

A tabela nunca sofre UPDATE: um registo escrito não volta a ser tocado. Há DELETE,
mas apenas por retenção e apenas depois de as linhas estarem em arquivo — ver
`app/auditoria/arquivo.py`. Quem ler isto não deve assumir que a tabela contém tudo
o que alguma vez aconteceu: contém a janela de `AUDIT_RETENCAO_DIAS`, e o resto está
nos `.jsonl.gz` em /app/data/audit-archive.
"""
import logging
import uuid
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any

from fastapi import Request
from sqlalchemy import event
from sqlalchemy.orm import Session as SessionSQLAlchemy
from sqlmodel import Field, Session, SQLModel

logger = logging.getLogger(__name__)


class ResultadoAcao(str, Enum):
    SUCESSO = "sucesso"
    FALHA = "falha"
    # A tentativa chegou ao servidor, foi compreendida, e o controlo de acesso
    # recusou-a. Distinto de FALHA, que é a ação a correr mal — aqui a ação nem
    # chegou a começar. Sem esta distinção o registo conta o que foi feito e
    # cala o que foi tentado, e é no que foi tentado que se vê o reconhecimento.
    NEGADO = "negado"


class AuditLog(SQLModel, table=True):
    """
    Registo imutável de todas as ações relevantes na plataforma.
    Sem updated_at — registos nunca são alterados após criação.
    """

    __tablename__ = "audit_logs"

    id: uuid.UUID = Field(
        default_factory=uuid.uuid4,
        primary_key=True,
        index=True,
    )

    # Contexto do tenant (nullable para ações de plataforma)
    empresa_id: uuid.UUID | None = Field(default=None, index=True)
    # Utilizador responsável pela ação (nullable para ações de sistema)
    utilizador_id: uuid.UUID | None = Field(default=None, index=True)

    # Ação estruturada: "entidade.verbo", ex: "utilizador.login_sucesso"
    acao: str = Field(max_length=100, index=True)

    # Entidade afetada (opcional)
    entidade_tipo: str | None = Field(default=None, max_length=50)
    entidade_id: uuid.UUID | None = Field(default=None)

    # Estado antes/depois (JSON serializado como string) — sem dados pessoais sensíveis
    dados_anteriores: str | None = Field(default=None)  # JSON string
    dados_novos: str | None = Field(default=None)       # JSON string

    # Contexto HTTP (cifrado em repouso com PII_ENCRYPTION_KEY)
    ip_address: str | None = Field(default=None, max_length=200)  # cifrado
    # Sem teto de comprimento: o criptograma de um User-Agent longo não cabia no
    # limite anterior e fazia o INSERT falhar. O corte é feito no texto limpo.
    user_agent: str | None = Field(default=None)  # cifrado

    # Impressão determinística do IP. O Fernet não é determinístico, logo o mesmo
    # IP dá criptogramas diferentes em cada linha e não há forma de o contar ou
    # filtrar em SQL. Fica NULL nas linhas anteriores a esta coluna existir — quem
    # conta tem de dizer quantas são, em vez de as tratar como se não tivessem IP.
    ip_hash: str | None = Field(default=None, max_length=64, index=True)

    resultado: ResultadoAcao = Field(default=ResultadoAcao.SUCESSO)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), index=True)

    # Encadeamento por hash — ver app/shared/audit_cadeia.py. `hash_anterior`
    # aponta para a linha anterior DA MESMA EMPRESA; `hash_registo` é o SHA-256
    # deste registo já encadeado. Alterar uma linha antiga obriga a recalcular
    # daí até ao fim, e o *head* exportado no dossiê e no backup denuncia-o.
    # Nullable: as linhas escritas antes de a cadeia existir ficam sem hash e a
    # verificação salta-as em vez de as acusar — a cadeia começa onde começou.
    hash_anterior: str | None = Field(default=None, max_length=64)
    hash_registo: str | None = Field(default=None, max_length=64, index=True)

    # Compromisso com o conteúdo de `dados_anteriores`/`dados_novos`. É este
    # digest — e não o texto — que entra no `hash_registo`, porque o arquivo
    # mascara os dois campos ao purgar: assinar o texto faria a cadeia partir no
    # momento da purga, em toda a história arquivada de uma vez.
    dados_hash: str | None = Field(default=None, max_length=64)


# ---------------------------------------------------------------------------
# Strings de ação padronizadas — única fonte de verdade
# ---------------------------------------------------------------------------

class Acao:
    """Constantes para o campo `acao` do AuditLog."""

    # Autenticação
    LOGIN_SUCESSO = "utilizador.login_sucesso"
    LOGIN_FALHA = "utilizador.login_falha"
    CONTA_BLOQUEADA = "utilizador.conta_bloqueada"
    IP_BLOQUEADO = "utilizador.ip_bloqueado"
    # Sessão válida, ação recusada pelo controlo de acesso. Fica na família da
    # autenticação por ser onde vivem os restantes eventos de controlo de
    # acesso (conta bloqueada, IP bloqueado) e por o seletor de famílias já a
    # ter traduzida; uma família própria obrigaria a mexer no frontend.
    ACESSO_NEGADO = "utilizador.acesso_negado"
    LOGOUT = "utilizador.logout"
    REFRESH_TOKEN = "utilizador.token_renovado"
    FA2_VERIFICADO = "utilizador.2fa_verificado"
    FA2_FALHOU = "utilizador.2fa_falhou"
    FA2_ATIVADO = "utilizador.2fa_ativado"
    FA2_DESATIVADO = "utilizador.2fa_desativado"
    BACKUP_CODE_USADO = "utilizador.backup_code_usado"
    PASSWORD_ALTERADA = "utilizador.password_alterada"
    PASSWORD_RESET_PEDIDO = "utilizador.password_reset_pedido"
    PASSWORD_RESET_CONFIRMADO = "utilizador.password_reset_confirmado"
    PASSWORD_RESET_ADMIN = "utilizador.password_reset_admin"
    MFA_RESET_ADMIN = "utilizador.mfa_reset_admin"
    # Reposição feita na consola do servidor, sem sessão na aplicação: quem a fez
    # tinha acesso à máquina, não a um perfil de administrador. Códigos próprios
    # para a trilha distinguir as duas origens.
    PASSWORD_RESET_CONSOLA = "utilizador.password_reset_manual"
    FA2_RESET_CONSOLA = "utilizador.2fa_reset_manual"

    # Gestão de utilizadores (apenas admin)
    UTILIZADOR_CRIADO = "utilizador.criado"
    # O nome é PII cifrada: o registo diz que mudou, nunca para quê.
    UTILIZADOR_NOME_ALTERADO = "utilizador.nome_alterado"
    UTILIZADOR_DESATIVADO = "utilizador.desativado"
    UTILIZADOR_REATIVADO = "utilizador.reativado"
    UTILIZADOR_ROLE_ALTERADO = "utilizador.role_alterado"
    UTILIZADOR_DELEGACAO_ATRIBUIDA = "utilizador.delegacao_atribuida"
    UTILIZADOR_DELEGACAO_REMOVIDA = "utilizador.delegacao_removida"
    UTILIZADOR_ANONIMIZADO = "utilizador.anonimizado"

    # Registo de empresa
    EMPRESA_REGISTADA = "empresa.registada"
    EMPRESA_DADOS_ATUALIZADOS = "empresa.dados_atualizados"
    EMPRESA_DADOS_EXPORTADOS = "empresa.dados_exportados"
    EMPRESA_ELIMINACAO_PEDIDA = "empresa.conta_eliminacao_pedida"
    # Gestão privilegiada de tenants (mecanismo interno; ator no campo dados_novos)
    EMPRESA_SUSPENSA = "empresa.suspensa"
    EMPRESA_REATIVADA = "empresa.reativada"

    # Controlos
    CONTROLO_ESTADO_ALTERADO = "controlo.estado_alterado"
    CONTROLO_NIVEL_ALTERADO = "controlo.nivel_maturidade_alterado"
    CONTROLO_CHECK_CONCLUIDO = "controlo.check_concluido"
    CONTROLO_CHECK_REVERTIDO = "controlo.check_revertido"
    CONTROLO_APROVADO = "controlo.aprovado"
    CONTROLO_NAO_APROVADO = "controlo.nao_aprovado"
    # Scoping de exclusão: marcar/repor "não aplicável" é decisão de âmbito
    # com peso de conformidade — fica sempre com justificação na auditoria.
    CONTROLO_NAO_APLICAVEL = "controlo.nao_aplicavel"
    CONTROLO_REAPLICADO = "controlo.reaplicado"

    # Evidências
    EVIDENCIA_UPLOAD = "evidencia.upload"
    EVIDENCIA_ELIMINADA = "evidencia.eliminada"
    EVIDENCIA_RECONCILIADA = "evidencia.reconciliada_pos_restauro"
    # A mesma prova serve vários controlos: ligar e desligar são atos próprios,
    # e não variantes de carregar/eliminar. Quem lê o registo precisa de
    # distinguir "saiu deste controlo" de "deixou de existir".
    EVIDENCIA_LIGADA = "evidencia.ligada"
    EVIDENCIA_DESLIGADA = "evidencia.desligada"
    EVIDENCIA_AMBITO_ALTERADO = "evidencia.ambito_alterado"
    # Substituir conteúdo cria uma versão; corrigir o título não.
    EVIDENCIA_VERSAO_CRIADA = "evidencia.versao_criada"
    EVIDENCIA_METADADOS_ALTERADOS = "evidencia.metadados_alterados"
    # Tirada da reciclagem de volta aos controlos de onde saiu.
    EVIDENCIA_RESTAURADA = "evidencia.restaurada"
    # Fim da reciclagem: sem ligação a controlo nenhum durante a janela inteira.
    EVIDENCIA_RECICLADA = "evidencia.reciclada"
    # Apagamento definitivo a pedido do titular dos dados. Deixa lápide.
    # Legado: as linhas novas usam EVIDENCIA_APAGADA_COM_LAPIDE.
    EVIDENCIA_APAGADA_RGPD = "evidencia.apagada_rgpd"
    # Apagar de vez, da reciclagem, uma órfã que nunca foi prova. Sem lápide.
    EVIDENCIA_APAGADA_DEFINITIVAMENTE = "evidencia.apagada_definitivamente"
    # Apagar prova (retida ou viva) com fundamento registado. Deixa lápide.
    EVIDENCIA_APAGADA_COM_LAPIDE = "evidencia.apagada_com_lapide"
    # Um restauro trouxe de volta uma evidência apagada depois do backup, e o
    # registo de apagamentos voltou a apagá-la.
    EVIDENCIA_REAPAGADA_POS_RESTAURO = "evidencia.reapagada_pos_restauro"
    # Acesso ao CONTEÚDO (não às listagens): abrir a nota ou pré-visualizar o
    # ficheiro no ecrã é ver; descarregar o ficheiro é levá-lo para fora.
    EVIDENCIA_VISUALIZADA = "evidencia.visualizada"
    EVIDENCIA_DESCARREGADA = "evidencia.descarregada"

    # Relatórios
    RELATORIO_EXPORTADO = "relatorio.exportado"

    # Relatórios de auditoria
    RELATORIO_AUDITORIA_CRIADO = "relatorio_auditoria.criado"

    # Assistente IA (premium) — auditoria registada pelo seam (app/premium)
    ANALISE_IA_SOLICITADA = "analise_ia.solicitada"
    ANALISE_IA_CONCLUIDA = "analise_ia.concluida"
    ANALISE_IA_ERRO = "analise_ia.erro"

    # Inventário de Ativos (premium) — auditado nos routers do core após sucesso gRPC
    ATIVO_CRIADO = "ativo.criado"
    ATIVO_ATUALIZADO = "ativo.atualizado"
    ATIVO_ELIMINADO = "ativo.eliminado"
    ATIVO_CRITICIDADE_CLASSIFICADA = "ativo.criticidade_classificada"
    ATIVO_DEPENDENCIAS_DEFINIDAS = "ativo.dependencias_definidas"
    ATIVO_REVISAO_REGISTADA = "ativo.revisao_registada"
    ATIVO_RESPONSAVEL_ATRIBUIDO = "ativo.responsavel_atribuido"
    ATIVO_SANITIZADO = "ativo.sanitizado"

    # Análise de Risco (premium)
    RISCO_CRIADO = "risco.criado"
    RISCO_ATUALIZADO = "risco.atualizado"
    RISCO_ELIMINADO = "risco.eliminado"
    RISCO_REAVALIADO = "risco.reavaliado"
    RISCO_APETITE_DEFINIDO = "risco.apetite_definido"
    RISCO_DONO_ATRIBUIDO = "risco.dono_atribuido"
    TRATAMENTO_CRIADO = "tratamento.criado"
    TRATAMENTO_ATUALIZADO = "tratamento.atualizado"
    TRATAMENTO_ELIMINADO = "tratamento.eliminado"

    # Incidentes (core) — notificações do Regime Jurídico da Cibersegurança (RJC, arts. 40.º a 45.º)
    INCIDENTE_CRIADO = "incidente.criado"
    INCIDENTE_ATUALIZADO = "incidente.atualizado"
    INCIDENTE_ELIMINADO = "incidente.eliminado"
    INCIDENTE_ESTADO_ALTERADO = "incidente.estado_alterado"
    # Já não se escreve (os marcos passaram a registar-se como notificações
    # enviadas); fica para as linhas antigas da trilha terem significado.
    INCIDENTE_MARCO_REGISTADO = "incidente.marco_registado"
    INCIDENTE_NOTIFICACAO_REGISTADA = "incidente.notificacao_registada"
    INCIDENTE_EVENTO_ADICIONADO = "incidente.evento_adicionado"

    # Tarefas recorrentes (core) — obrigações periódicas do QNRCS
    TAREFA_CRIADA = "tarefa.criada"
    TAREFA_ATUALIZADA = "tarefa.atualizada"
    TAREFA_ELIMINADA = "tarefa.eliminada"
    TAREFA_CONCLUIDA = "tarefa.concluida"

    # Fornecedores / cadeia de abastecimento (premium) — auditado no core após sucesso gRPC
    FORNECEDOR_CRIADO = "fornecedor.criado"
    FORNECEDOR_ATUALIZADO = "fornecedor.atualizado"
    FORNECEDOR_ELIMINADO = "fornecedor.eliminado"
    FORNECEDOR_AVALIADO = "fornecedor.avaliado"

    # Formação (core) — PR.FC-1/2 (formação do órgão de gestão: RJC, arts. 25.º, n.º 1, al. d), e 27.º, n.º 1, al. f))
    FORMACAO_CRIADA = "formacao.criada"
    FORMACAO_ATUALIZADA = "formacao.atualizada"
    FORMACAO_ELIMINADA = "formacao.eliminada"
    FORMACAO_ESTADO_ALTERADO = "formacao.estado_alterado"
    FORMACAO_PARTICIPANTE_ADICIONADO = "formacao.participante_adicionado"
    FORMACAO_PARTICIPANTE_REMOVIDO = "formacao.participante_removido"
    FORMACAO_PRESENCA_MARCADA = "formacao.presenca_marcada"

    # Dossiê de auditoria — exportação cifrada de todos os dados de conformidade
    DOSSIE_GERADO = "dossie.gerado"
    # Parecer de auditor externo importado (round-trip do dossiê)
    PARECER_IMPORTADO = "dossie.parecer_importado"
    # Atestação da chave de instância definida (cadeia de confiança NIS2PME)
    DOSSIE_ATESTACAO_DEFINIDA = "dossie.atestacao_definida"

    # Conetores (premium) — verificação técnica em serviços externos
    CONETOR_CONFIGURADO = "conetor.configurado"
    CONETOR_VERIFICACAO = "conetor.verificacao"
    CONETOR_DRIFT_DETETADO = "conetor.drift_detetado"
    CONETOR_EVENTO_RESOLVIDO = "conetor.evento_resolvido"
    CONETOR_REMOVIDO = "conetor.removido"
    # Um alerta grave da monitorização decidido: passou a incidente ou foi
    # descartado (com motivo, cifrado no sidecar).
    CONETOR_ALERTA_DECIDIDO = "conetor.alerta_decidido"

    # Importação (premium) — dados vindos de ferramentas que a empresa já usa.
    # Quatro ações e não uma: analisar não escreve nada, simular também não, e
    # quem lê o registo tem de conseguir distinguir o ensaio do ato.
    IMPORTACAO_ANALISADA = "importacao.analisada"
    IMPORTACAO_SIMULADA = "importacao.simulada"
    IMPORTACAO_APLICADA = "importacao.aplicada"
    IMPORTACAO_REVERTIDA = "importacao.revertida"
    # Aceitar uma máquina descoberta por um relatório CRIA um ativo; dispensá-la
    # é uma decisão sobre o âmbito do inventário. As duas ficam registadas — a
    # segunda mais importante do que parece: "porque é que isto não está no
    # inventário?" tem de ter resposta.
    IMPORTACAO_DESCOBERTA_DECIDIDA = "importacao.descoberta_decidida"

    # Backups (on-prem) — cópias de segurança da instalação
    BACKUP_CRIADO = "backup.criado"
    BACKUP_ELIMINADO = "backup.eliminado"
    BACKUP_DESCARREGADO = "backup.descarregado"
    BACKUP_PASSPHRASE_DEFINIDA = "backup.passphrase_definida"
    BACKUP_AGENDADO_ALTERADO = "backup.agendado_alterado"
    BACKUP_IMPORTADO = "backup.importado"
    RESTAURO_EXECUTADO = "backup.restauro_executado"

    # Servidor de saída de correio (on-prem). Quem o muda passa a decidir por
    # onde saem as recuperações de password e os avisos de prazo legal — e a
    # credencial usada para isso. A password nunca entra no registo: está na
    # lista de chaves sensíveis, que a substitui por [REDACTED].
    SISTEMA_EMAIL_CONFIGURADO = "sistema.email_configurado"
    # O HTTPS do nginx da instalação (modo, e o esquema do endereço público). Descer
    # para HTTP, ou trocar o certificado, não pode passar sem rasto.
    SISTEMA_HTTPS_CONFIGURADO = "sistema.https_configurado"
    # Ligar/desligar a verificação de atualizações (o aviso de versões de segurança).
    SISTEMA_ATUALIZACOES_CONFIGURADAS = "sistema.atualizacoes_configuradas"
    # Ficheiro de licença premium instalado pela UI (on-prem). Muda o que a
    # instalação pode fazer; fica com o identificador, o plano e o termo.
    LICENCA_INSTALADA = "sistema.licenca_instalada"

    # Política de permissões — quem pode o quê. Uma alteração aqui muda a
    # postura de segurança da instalação e entra no registo de funções e
    # responsabilidades que o auditor lê.
    POLITICA_ALTERADA = "politica.alterada"

    # Retenção da própria trilha: um mês saiu da janela e foi para arquivo
    AUDIT_PURGADO = "audit.purgado"
    # A trilha foi extraída para um ficheiro. Quem leva a auditoria para fora
    # da aplicação deixa também rasto dentro dela.
    AUDIT_EXPORTADO = "audit.exportado"
    # A verificação diária encontrou um elo partido ou um registo alterado.
    AUDIT_CADEIA_PARTIDA = "audit.cadeia_partida"
    # A verificação encontrou linhas escritas depois do início da cadeia sem
    # fazerem parte dela (escritas fora da aplicação, ou sem o head a tempo).
    AUDIT_CADEIA_SEM_ELO = "audit.cadeia_sem_elo"
    # Uma cabeça da cadeia testemunhada pelo fornecedor (heartbeat da licença)
    # já não está na trilha: a história anterior foi reescrita.
    AUDIT_TESTEMUNHO_DIVERGENTE = "audit.testemunho_divergente"

    # Retenção das notificações: lembretes já lidos saíram da janela. Não são
    # registo probatório — o facto que os originou fica na trilha e no módulo de
    # onde veio — mas o apagamento não se faz em silêncio.
    NOTIFICACOES_PURGADAS = "notificacoes.purgadas"


# ---------------------------------------------------------------------------
# Helper principal
# ---------------------------------------------------------------------------

# Chaves que NUNCA devem aparecer serializada nos audit logs (CWE-532)
_CHAVES_SENSIVEIS = frozenset({
    "password", "password_hash", "nova_password", "password_antiga",
    "totp_secret", "totp_secret_cifrado", "backup_codes",
    "token", "refresh_token", "access_token", "token_hash",
    "secret", "key", "api_key", "smtp_password",
    "passphrase", "private_key", "chave_privada", "client_secret",
    "authorization", "cookie",
})

# Trava de profundidade: um payload aninhado ao infinito não pode fazer a
# sanitização entrar em recursão sem fim antes de chegar ao json.dumps.
_PROFUNDIDADE_MAX = 6


def _sanitize_valor(valor: Any, profundidade: int = 0) -> Any:
    """
    Percorre a estrutura e substitui por '[REDACTED]' o valor de qualquer chave
    sensível, a qualquer nível — dentro de dicionários e também dentro de listas.

    A versão anterior descia em dicionários mas não em listas, portanto uma chave
    sensível guardada dentro de uma coleção passava em claro para a base.
    """
    if profundidade > _PROFUNDIDADE_MAX:
        return "[...]"
    if isinstance(valor, dict):
        return {
            chave: (
                "[REDACTED]"
                if chave.lower() in _CHAVES_SENSIVEIS
                else _sanitize_valor(item, profundidade + 1)
            )
            for chave, item in valor.items()
        }
    if isinstance(valor, (list, tuple)):
        return [_sanitize_valor(item, profundidade + 1) for item in valor]
    # O registo vai para JSON: datas, identificadores e enumerados passam a texto.
    # Sem isto, corrigir uma data (formação, tarefa, incidente) rebentava com 500
    # no `json.dumps`, e a alteração pedida perdia-se com a transação.
    if isinstance(valor, (datetime, date)):
        return valor.isoformat()
    if isinstance(valor, uuid.UUID):
        return str(valor)
    if isinstance(valor, Enum):
        return valor.value
    return valor


def _sanitize_dados(dados: dict[str, Any] | None) -> dict[str, Any] | None:
    """Sanitiza o dicionário de topo antes de o serializar para o AuditLog."""
    if dados is None:
        return None
    return _sanitize_valor(dados)


# ---------------------------------------------------------------------------
# Encadeamento no commit
# ---------------------------------------------------------------------------

# Chave em `Session.info` onde ficam as entradas ainda por encadear.
_POR_ENCADEAR = "_audit_por_encadear"


def _por_encadear(session: Session) -> list[AuditLog]:
    """Lista de espera da sessão, garantindo que há transação aberta.

    O `begin()` não é decorativo. Uma entrada em espera não toca na base, por
    isso um pedido que só tenha auditado não chega a abrir transação nenhuma — e
    o `rollback()` que se segue a um erro não dispara evento nenhum, deixando a
    entrada a bordo para ser escrita no commit seguinte. Medido: a auditoria
    passava a afirmar uma ação que a base nunca teve. Abrir a transação aqui põe
    o registo debaixo das mesmas regras da operação que ele audita.
    """
    if not session.in_transaction():
        session.begin()
    return session.info.setdefault(_POR_ENCADEAR, [])


def _encadear_antes_do_commit(session: SessionSQLAlchemy) -> None:
    """Encadeia e escreve as entradas acumuladas, à porta do commit.

    É aqui — e não onde o negócio chamou `registar_acao` — que a linha de *head*
    da cadeia é trancada. O bloqueio só é largado no commit, por isso trancá-la
    no fim faz com que dure o que dura esta função, em vez de durar o resto do
    pedido (escrita de ficheiros, chamadas ao sidecar, geração de documentos).

    O gancho é **global**, e não uma chamada dentro do `get_session`, de
    propósito: há código que faz `commit()` por sua conta — tarefas de fundo,
    scripts de operação, restauro —, e uma entrada que não passasse por aqui não
    ficaria só sem elo: ficaria por escrever, e a auditoria perdia-se em
    silêncio.
    """
    # No SQLAlchemy 2.0 o `before_commit` dispara também ao fechar um savepoint
    # (`begin_nested`). Encadear aí trancava a cabeça no primeiro savepoint do
    # pedido e segurava-a até ao fim dele — a retenção que isto quer evitar. Só
    # o commit de topo encadeia; o que estiver pendente espera por ele.
    if session.in_nested_transaction():
        return
    pendentes = session.info.pop(_POR_ENCADEAR, None)
    if not pendentes:
        return
    from app.shared import audit_cadeia

    try:
        with session.no_autoflush:
            audit_cadeia.encadear_lote(
                session, pendentes, espera_ms=audit_cadeia.ESPERA_PEDIDO_MS
            )
    except Exception:
        # A auditoria nunca pode fazer falhar a operação que audita, e menos
        # ainda pode fazê-la falhar no commit — o utilizador já teve a resposta.
        # Sem elo, mas escrita.
        logger.exception(
            "Falha a encadear a auditoria — os registos são gravados sem elo."
        )
    session.add_all(pendentes)


def _descartar_por_encadear(session: SessionSQLAlchemy, transacao_anterior=None) -> None:
    """Um rollback desfaz a operação; os registos dela não têm o que provar.

    O savepoint é a exceção que obriga a olhar para a transação: o próprio
    encadeamento usa savepoints, e a evidência também, portanto descartar em
    qualquer `after_soft_rollback` apagaria a auditoria de operações que
    seguiram em frente. Só o desfazer da transação de topo limpa a lista.
    """
    if getattr(transacao_anterior, "nested", False):
        return
    session.info.pop(_POR_ENCADEAR, None)


event.listen(SessionSQLAlchemy, "before_commit", _encadear_antes_do_commit)
# `after_soft_rollback` e não `after_rollback`: medido, o segundo dispara também
# ao desfazer um savepoint e não recebe a transação, portanto não há como
# distinguir lá dentro o savepoint do rollback verdadeiro.
event.listen(SessionSQLAlchemy, "after_soft_rollback", _descartar_por_encadear)


def registar_acao(
    db: Session | None,
    *,
    acao: str,
    resultado: ResultadoAcao = ResultadoAcao.SUCESSO,
    empresa_id: uuid.UUID | None = None,
    utilizador_id: uuid.UUID | None = None,
    entidade_tipo: str | None = None,
    entidade_id: uuid.UUID | None = None,
    dados_anteriores: dict[str, Any] | None = None,
    dados_novos: dict[str, Any] | None = None,
    request: Request | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
    force_commit: bool = False,
) -> AuditLog:
    """
    Regista uma ação no AuditLog de forma atómica.
    Nunca loga passwords, tokens ou dados pessoais sensíveis.

    Args:
        db: Sessão de base de dados ativa. Pode ser None quando
            `force_commit=True`, porque nesse caso o registo é escrito numa
            sessão própria e esta não chega a ser tocada — é o que permite
            auditar a partir de sítios que não têm sessão em mão, como os gates
            de autorização.
        acao: String estruturada da ação (usar constantes Acao.*).
        resultado: sucesso ou falha.
        empresa_id: UUID do tenant, se aplicável.
        utilizador_id: UUID do utilizador, se aplicável.
        entidade_tipo: Tipo da entidade afetada (ex: "Controlo").
        entidade_id: UUID da entidade afetada.
        dados_anteriores: Dict com estado anterior (sem dados sensíveis).
        dados_novos: Dict com estado novo (sem dados sensíveis).
        request: Objeto Request do FastAPI para extrair IP e User-Agent.
        ip_address: IP override (se request não disponível).
        user_agent: User-Agent override (se request não disponível).
        force_commit: Se True, usa sessão independente e faz commit imediato.
            Usar em casos de falha de autenticação, onde o caller lança
            HTTPException logo a seguir e a transação principal sofre rollback.
    """
    import json
    from app.shared import audit_cadeia
    from app.shared.audit_catalogo import CATALOGO
    from app.shared.hashes import hash_ip_auditoria
    from app.shared.pii import cifrar_pii, truncar_para_cifra

    # Um código fora do catálogo continua a ser gravado: a auditoria nunca pode
    # fazer falhar a operação que está a auditar. Mas fica um aviso, porque na
    # leitura esse código não tem família nem tradução e aparece cru ao
    # utilizador. Os testes apanham isto antes de chegar aqui.
    if acao not in CATALOGO:
        logger.warning(
            "Ação '%s' não está no catálogo de auditoria — será gravada sem "
            "família nem severidade.", acao,
        )

    # Extrai IP e User-Agent do request se disponível
    if request is not None:
        ip = _extrair_ip(request)
        ua = request.headers.get("user-agent", "")
    else:
        ip = ip_address
        ua = user_agent

    # O criptograma tem de caber na coluna. Um User-Agent longo — que o cliente
    # escolhe — chegava a estourar o INSERT e, nos caminhos de autenticação, a
    # devolver 500 em vez da resposta esperada.
    ip = truncar_para_cifra(ip, limite_coluna=200)
    ua = truncar_para_cifra(ua, limite_coluna=700)

    entrada = AuditLog(
        empresa_id=empresa_id,
        utilizador_id=utilizador_id,
        acao=acao,
        entidade_tipo=entidade_tipo,
        entidade_id=entidade_id,
        dados_anteriores=json.dumps(_sanitize_dados(dados_anteriores)) if dados_anteriores else None,
        dados_novos=json.dumps(_sanitize_dados(dados_novos)) if dados_novos else None,
        ip_address=cifrar_pii(ip),
        ip_hash=hash_ip_auditoria(ip),
        user_agent=cifrar_pii(ua),
        resultado=resultado,
    )

    if force_commit:
        # Sessão independente: persiste o log mesmo que a transação principal
        # sofra rollback (ex: HTTPException lançada logo após esta chamada).
        #
        # A cadeia é encadeada AQUI, na sessão própria, e não na de fora: é esta
        # que vai escrever a linha, e o *head* tem de avançar na mesma transação
        # em que a linha nasce. Encadear na sessão principal deixaria o *head*
        # avançada e a linha por escrever se a principal sofresse o rollback que
        # motivou o `force_commit`.
        from app.database import engine as _engine
        with Session(_engine) as audit_session:
            # Com limite de espera: esta ligação é OUTRA. Se a transação
            # principal deste mesmo pedido já tiver o *head* bloqueado, esperar
            # por ela seria esperar por quem está à espera desta escrita — o
            # pedido ficaria pendurado, sem erro, com a ligação retida. Não
            # encadear é uma degradação pequena e ruidosa; pendurar não é.
            if not audit_cadeia.encadear(
                audit_session,
                entrada,
                espera_ms=audit_cadeia.ESPERA_SESSAO_PROPRIA_MS,
            ):
                # No Postgres, o tempo esgotado num bloqueio **aborta a
                # transação**: sem este rollback o INSERT seguinte morre com
                # "current transaction is aborted" e o registo perde-se — que é
                # precisamente o que este caminho existe para impedir. Medido
                # contra Postgres real; em SQLite não acontece, e por isso os
                # testes da suite normal nunca o mostrariam.
                audit_session.rollback()
            audit_session.add(entrada)
            audit_session.commit()
    elif db is None:
        raise ValueError(
            "registar_acao sem sessão exige force_commit=True — sem um dos dois "
            "o registo não teria onde ser escrito e perder-se-ia em silêncio."
        )
    else:
        # A entrada **não** é adicionada à sessão aqui, e isso é deliberado: fica
        # em espera e só é encadeada e escrita à porta do commit, para que a
        # tranca da linha de *head* não seja retida durante o resto do pedido.
        #
        # Adicioná-la já e encadear no fim não serviria: qualquer `db.flush()`
        # pelo meio escreveria a linha com os hashes a NULL, e corrigi-los depois
        # obrigaria a um UPDATE — numa tabela cujo desenho é não o aceitar (nas
        # instalações onde os grants foram apertados, o papel da aplicação nem
        # sequer tem UPDATE sobre ela).
        #
        # Continua a não haver commit aqui: o registo faz parte da mesma
        # transação que a ação que audita, e cai com ela se ela cair.
        _por_encadear(db).append(entrada)

    return entrada


def registar_negacao(
    utilizador,
    *,
    modulo: str,
    acao: str,
    codigo: str = "sem_permissao",
    request: Request | None = None,
) -> None:
    """Deixa rasto de uma tentativa recusada pelo controlo de acesso.

    Escreve em sessão própria (`force_commit`): quem chama isto lança a seguir
    um 403, e o rollback da transação principal levaria o registo com ele.

    A auditoria nunca faz falhar a recusa. Se o registo não puder ser escrito
    fica um aviso no log e o 403 segue na mesma — recusar e depois rebentar a
    meio trocaria uma negação limpa por um 500.

    Só chega aqui quem já está autenticado: uma sessão inválida leva 401 antes
    de qualquer gate. Quem pode gerar estas linhas é, portanto, quem tem
    credenciais válidas — e uma sequência delas vinda da mesma conta é
    exatamente o sinal que se quer poder ver.
    """
    try:
        registar_acao(
            None,
            acao=Acao.ACESSO_NEGADO,
            resultado=ResultadoAcao.NEGADO,
            empresa_id=getattr(utilizador, "empresa_id", None),
            utilizador_id=getattr(utilizador, "id", None),
            entidade_tipo="Utilizador",
            entidade_id=getattr(utilizador, "id", None),
            dados_novos={"modulo": modulo, "acao": acao, "codigo": codigo},
            request=request,
            force_commit=True,
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "Não foi possível registar a recusa de acesso (%s/%s)",
            modulo, acao, exc_info=True,
        )


def _extrair_ip(request: Request) -> str:
    """Resolve o IP real do cliente, resistente a spoofing (CWE-348).

    Delega na fonte única partilhada (app.shared.utils.obter_ip_cliente) para
    manter a mesma lógica na auditoria e no rate limiting.
    """
    from app.shared.utils import obter_ip_cliente

    return obter_ip_cliente(request)
