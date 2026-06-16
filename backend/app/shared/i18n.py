"""
Mensagens de erro e texto da aplicação — PT-PT.

Centraliza todas as strings do backend para facilitar manutenção e futura
internacionalização. As mensagens são usadas nos HTTPException detail,
respostas de serviço e logs.

Uso:
    from app.shared.i18n import Msgs
    raise HTTPException(status_code=404, detail=Msgs.CONTROLO_NAO_ENCONTRADO)
"""


class Msgs:
    """Mensagens de erro e informação do backend (PT-PT)."""

    # ──────────────────────────────────────────────────────────────────────
    # Autenticação e sessões
    # ──────────────────────────────────────────────────────────────────────
    TOKEN_INVALIDO = "Token inválido."
    TOKEN_INVALIDO_EXPIRADO = "Token inválido ou expirado."
    TOKEN_INVALIDO_ENDPOINT = "Token inválido para este endpoint."
    TOKEN_TEMP_INVALIDO = "Token temporário inválido ou expirado."
    TOKEN_TEMP_INVALIDO_OP = "Token temporário inválido para esta operação."
    TOKEN_TEMP_OBRIGATORIO = "Token temporário obrigatório."
    TOKEN_NAUTH_ENDPOINT = "Token não autorizado para este endpoint."

    SESSAO_NAO_ENCONTRADA = "Sessão não encontrada. Por favor, faça login novamente."
    SESSAO_EXPIRADA = "Sessão expirada. Por favor, faça login novamente."
    SESSAO_EXPIRADA_ADMIN = "Sessão expirada. Faça login novamente."

    CREDENCIAIS_INVALIDAS = "Credenciais inválidas."
    CONTA_DESATIVADA = "Conta desativada. Contacte o administrador."
    CONTA_SUSPENSA = "A sua conta está temporariamente suspensa. Contacte o suporte."
    CONTA_REMOVIDA = "Conta removida."
    COOKIE_NAO_ENCONTRADO = "Cookie de sessão não encontrado."

    # ──────────────────────────────────────────────────────────────────────
    # 2FA / TOTP
    # ──────────────────────────────────────────────────────────────────────
    TOTP_INVALIDO = "Código 2FA inválido."
    TOTP_INVALIDO_HORA = "Código TOTP inválido. Verifique a hora do seu dispositivo e tente novamente."
    TOTP_CONFIGURACAO_INVALIDA = "Configuração 2FA inválida."
    TOTP_NAO_CONFIGURADO = "Configure primeiro o 2FA antes de o ativar."
    TOTP_CONTA_INVALIDA_2FA = "Conta inválida ou 2FA não configurado."
    TOTP_SETUP_NAO_INICIADO = "Conta inválida ou setup 2FA não iniciado."

    # ──────────────────────────────────────────────────────────────────────
    # Registo e utilizadores
    # ──────────────────────────────────────────────────────────────────────
    REGISTO_MODO_ONPREM = "Registo público não disponível neste modo de instalação."
    EMAIL_JA_REGISTADO = "Este email já está registado."

    UTILIZADOR_NAO_ENCONTRADO = "Utilizador não encontrado."
    UTILIZADOR_SEM_PERMISSAO = "Sem permissão."
    UTILIZADOR_SEM_PERMISSAO_ACAO = "Sem permissão para realizar esta ação."
    UTILIZADOR_APENAS_ADMINS = "Apenas admins podem gerir outros utilizadores."
    UTILIZADOR_JA_DESATIVADO = "Utilizador já está desativado."
    UTILIZADOR_JA_ATIVO = "Utilizador já está ativo."
    UTILIZADOR_JA_ANONIMIZADO = "Utilizador já foi anonimizado."
    UTILIZADOR_ANONIMIZADO_NO_REATIVAR = "Conta anonimizada não pode ser reativada."
    UTILIZADOR_NAO_PODE_DESATIVAR_PROPRIA = "Não pode desativar a sua própria conta."
    UTILIZADOR_NAO_PODE_ANONIMIZAR_PROPRIA = "Não pode anonimizar a sua própria conta."
    UTILIZADOR_NAO_PODE_ALTERAR_PROPRIO_ROLE = "Não pode alterar o seu próprio role."
    UTILIZADOR_NAO_PODE_ALTERAR_ROLE_ADMIN = "Não é possível alterar o role de um administrador."
    UTILIZADOR_SEM_PERMISSAO_GERIR = "Sem permissão para gerir este utilizador."
    UTILIZADOR_SUBADMIN_NAO_CRIA_ADMIN = "Sub-administradores não podem criar utilizadores com este perfil."
    UTILIZADOR_SUBADMIN_NAO_PROMOVE = "Sub-administradores não podem atribuir este perfil."

    PASSWORD_INCORRETA = "Password atual incorreta."
    PASSWORD_ATUAL_INCORRETA = "Password incorreta."
    PASSWORD_IGUAL_ATUAL = "A nova password não pode ser igual à atual."

    # ──────────────────────────────────────────────────────────────────────
    # Empresas
    # ──────────────────────────────────────────────────────────────────────
    EMPRESA_NAO_ENCONTRADA = "Empresa não encontrada."
    EMPRESA_JA_SUSPENSA = "Empresa já está suspensa."
    EMPRESA_NAO_SUSPENSA = "Empresa não está suspensa."

    # ──────────────────────────────────────────────────────────────────────
    # Controlos
    # ──────────────────────────────────────────────────────────────────────
    CONTROLO_NAO_ENCONTRADO = "Controlo não encontrado."
    CONTROLO_NAO_DISPONIVEL = "Controlo não disponível para esta empresa."
    CONTROLO_SEM_ACESSO = "Sem acesso a este controlo."
    CONTROLO_CEO_SEM_ALTERACAO = "Perfil CEO não pode alterar controlos."
    CONTROLO_USAR_APROVAR_REPROVAR = "Use os endpoints /aprovar ou /reprovar para aprovação."
    CONTROLO_IMPLEMENTADOR_ESTADOS = "Implementador só pode mudar estado para 'em_progresso' ou 'implementado'."
    CONTROLO_APENAS_AUDITORES_APROVAM = "Apenas auditores podem aprovar controlos."
    CONTROLO_APENAS_APROVADO_DE_IMPLEMENTADO = "Apenas controlos no estado 'implementado' podem ser aprovados."
    CONTROLO_APENAS_AUDITORES_REPROVAM = "Apenas auditores podem reprovar controlos."
    CONTROLO_APENAS_REPROVAR_IMPL_APROV = "Apenas controlos 'implementado' ou 'aprovado' podem ser reprovados."
    CONTROLO_APENAS_ADMINS_DELEGAM = "Apenas administradores podem delegar controlos."

    CHECK_NAO_ENCONTRADO = "Check não encontrado."
    CHECK_INVALIDO_CONTROLO = "Check inválido para este controlo."

    IMPLEMENTADOR_NAO_ENCONTRADO = "Implementador não encontrado nesta empresa."
    IMPLEMENTADOR_ROLE_INCORRETO = "O utilizador selecionado não tem o perfil de implementador."

    # ──────────────────────────────────────────────────────────────────────
    # Evidências
    # ──────────────────────────────────────────────────────────────────────
    EVIDENCIA_SEM_ACESSO_LER = "Sem acesso às evidências deste controlo."
    EVIDENCIA_SEM_ACESSO_ADICIONAR = "Sem acesso para adicionar evidências a este controlo."
    EVIDENCIA_SEM_PERMISSAO = "Sem permissão para adicionar evidências."
    EVIDENCIA_CONTEUDO_VAZIO = "A evidência deve ter pelo menos uma nota de texto ou um ficheiro."
    EVIDENCIA_FICHEIRO_VAZIO = "O ficheiro não pode estar vazio."
    EVIDENCIA_NAO_ENCONTRADA = "Evidência não encontrada."
    EVIDENCIA_NAO_E_FICHEIRO = "Esta evidência não é um ficheiro."
    EVIDENCIA_FICHEIRO_SERVIDOR = "Ficheiro não encontrado no servidor."
    EVIDENCIA_APENAS_ADMINS_ELIMINAM = "Apenas administradores podem eliminar evidências."

    # ──────────────────────────────────────────────────────────────────────
    # Documentos / Templates
    # ──────────────────────────────────────────────────────────────────────
    DOCUMENTO_NAO_ENCONTRADO = "Template de documento não encontrado."
    DOCUMENTO_FICHEIRO_INDISPONIVEL = "Ficheiro temporariamente indisponível. Contacte o administrador."

    # ──────────────────────────────────────────────────────────────────────
    # Superadmin
    # ──────────────────────────────────────────────────────────────────────
    SUPERADMIN_CONTA_INVALIDA = "Conta inválida."
    SUPERADMIN_INATIVO = "Conta de superadmin inativa."
    SUPERADMIN_INATIVO_INEXISTENTE = "Conta de superadmin inativa ou inexistente."
    SUPERADMIN_DESATIVADO = "Conta de superadmin desativada."
