"""Tradução das mensagens de erro da API.

As mensagens de erro são escritas em português nos serviços — é a língua em
que o produto nasceu e a que a maioria dos clientes lê. Um cliente que use a
aplicação em inglês recebe-as traduzidas aqui, no momento em que a resposta
sai, escolhidas pelo `Accept-Language` do pedido. Não se mudam os 160 pontos
de emissão: muda-se a saída.

Duas regras mantêm isto honesto:
  - um `detail` que não seja texto (um dicionário com `codigo`) passa intacto —
    esses o frontend já traduz pelo código;
  - uma mensagem sem tradução sai em português em vez de sair vazia, e o teste
    anti-deriva acusa-a para não ficar assim.
"""
from __future__ import annotations

from typing import Any

# Português → inglês. As chaves são as mensagens EXATAS emitidas pelo código.
_EN: dict[str, str] = {
    "A data final não pode ser anterior à data inicial.": "The end date cannot be earlier than the start date.",
    "A evidência deve ter pelo menos uma nota de texto ou um ficheiro.": "The evidence must have at least a text note or a file.",
    "A nova password não pode ser igual à atual.": "The new password cannot be the same as the current one.",
    "A nova password não pode ser igual à password atual.": "The new password cannot be the same as the current password.",
    "A nova password não pode ser igual à password temporária.": "The new password cannot be the same as the temporary password.",
    "A password deve conter pelo menos uma letra minúscula.": "The password must contain at least one lowercase letter.",
    "A password deve conter pelo menos uma letra maiúscula.": "The password must contain at least one uppercase letter.",
    "A password deve conter pelo menos um dígito.": "The password must contain at least one digit.",
    "A password deve conter pelo menos um carácter especial.": "The password must contain at least one special character.",
    "A password deve conter pelo menos um número.": "The password must contain at least one number.",
    "A password deve conter pelo menos uma maiúscula.": "The password must contain at least one uppercase letter.",
    "A política de segurança é fixa nesta modalidade.": "The security policy is fixed in this deployment mode.",
    "A sua conta está temporariamente suspensa. Contacte o suporte.": "Your account is temporarily suspended. Contact support.",
    "Apenas administradores podem alterar o estado da conta.": "Only administrators can change the account status.",
    "Apenas administradores podem alterar o papel.": "Only administrators can change the role.",
    "Apenas admins podem gerir outros utilizadores.": "Only administrators can manage other users.",
    "Apenas controlos 'implementado' ou 'aprovado' podem ser reprovados.": "Only controls in the 'implemented' or 'approved' state can be rejected.",
    "Apenas controlos no estado 'implementado' podem ser aprovados.": "Only controls in the 'implemented' state can be approved.",
    "As passwords não coincidem.": "The passwords do not match.",
    "Ação de formação não encontrada.": "Training session not found.",
    "Caminho de destino inválido.": "Invalid destination path.",
    "Categoria inválida.": "Invalid category.",
    "Certificado PEM inválido. Verifique que o ficheiro está no formato correcto (-----BEGIN CERTIFICATE-----).": "Invalid PEM certificate. Check that the file is in the correct format (-----BEGIN CERTIFICATE-----).",
    "Chave privada PEM inválida. Verifique que o ficheiro está no formato correcto (-----BEGIN PRIVATE KEY----- ou -----BEGIN RSA PRIVATE KEY-----).": "Invalid PEM private key. Check that the file is in the correct format (-----BEGIN PRIVATE KEY----- or -----BEGIN RSA PRIVATE KEY-----).",
    "Chave privada protegida por password. Remova a password antes de fazer upload (openssl rsa -in key.pem -out key_sem_password.pem).": "The private key is password-protected. Remove the password before uploading (openssl rsa -in key.pem -out key_without_password.pem).",
    "Check inválido para este controlo.": "Invalid check for this control.",
    "Configuração 2FA inválida.": "Invalid 2FA configuration.",
    "Configuração HTTPS só disponível em modo on-prem.": "HTTPS configuration is only available in on-premises mode.",
    "Configuração SMTP inválida. Verifique host, porta e credenciais.": "Invalid SMTP configuration. Check host, port and credentials.",
    "Configuração de email só disponível em modo on-prem.": "Email configuration is only available in on-premises mode.",
    "Configuração inválida: carateres de controlo não permitidos.": "Invalid configuration: control characters are not allowed.",
    "Configure primeiro o 2FA antes de o ativar.": "Set up 2FA before enabling it.",
    "Confirme que não se aplica uma obrigação legal de conservar esta prova nem ela é necessária num litígio.": "Confirm that no legal obligation to retain this proof applies and that it is not needed in legal proceedings.",
    "Conta anonimizada não pode ser reativada.": "An anonymised account cannot be reactivated.",
    "Conta desativada. Contacte o administrador.": "Account deactivated. Contact the administrator.",
    "Conta removida.": "Account removed.",
    "Controlo marcado como não aplicável — reponha a aplicabilidade primeiro.": "Control marked as not applicable — restore applicability first.",
    "Controlo não disponível para esta empresa.": "Control not available for this organisation.",
    "Controlo não encontrado.": "Control not found.",
    "Credenciais inválidas.": "Invalid credentials.",
    "Código 2FA inválido.": "Invalid 2FA code.",
    "Código TOTP inválido. Verifique a hora do seu dispositivo e tente novamente.": "Invalid TOTP code. Check your device's clock and try again.",
    "Demasiados pedidos. Tente novamente mais tarde.": "Too many requests. Try again later.",
    "Descreva a razão do apagamento (mínimo 10 caracteres).": "Describe the reason for the erasure (at least 10 characters).",
    "Diretório de configuração nginx não encontrado. Verifique que o volume 'nis2pme_nginx' está montado correctamente no docker-compose.": "Nginx configuration directory not found. Check that the 'nis2pme_nginx' volume is correctly mounted in docker-compose.",
    "Empresa não encontrada.": "Organisation not found.",
    "Empresa sem framework V2 associado.": "Organisation has no framework assigned.",
    "Esta evidência não está ligada a este controlo.": "This evidence is not linked to this control.",
    "Esta evidência não é um ficheiro.": "This evidence is not a file.",
    "Esta evidência tem demasiadas versões e cópias para apagar de uma vez.": "This evidence has too many versions and copies to erase at once.",
    "Esta instalação já foi configurada. Use o login normal.": "This installation has already been configured. Use the normal login.",
    "Estado TLS só disponível em modo on-prem.": "TLS status is only available in on-premises mode.",
    "Este email já está registado.": "This email is already registered.",
    "Este endereço de email já está registado.": "This email address is already registered.",
    "Este endpoint só está disponível em modo on-prem.": "This endpoint is only available in on-premises mode.",
    "Este utilizador já não tem password temporária ativa.": "This user no longer has an active temporary password.",
    "Evidência não encontrada.": "Evidence not found.",
    "Ficheiro não encontrado no servidor.": "File not found on the server.",
    "Formato inválido.": "Invalid format.",
    "Framework da empresa não encontrado.": "The organisation's framework was not found.",
    "Framework não encontrado.": "Framework not found.",
    "Fundamento de apagamento inválido.": "Invalid basis for the erasure.",
    "Implementador não encontrado nesta empresa.": "Implementer not found in this organisation.",
    "Implementador só pode mudar estado para 'em_progresso' ou 'implementado'.": "An implementer can only change the state to 'in progress' or 'implemented'.",
    "Incidente não encontrado.": "Incident not found.",
    "Notificação não encontrada.": "Notification not found.",
    "Indique o intervalo em dias.": "Provide the interval in days.",
    "Indique o motivo do apagamento (mínimo 10 caracteres).": "Provide the reason for deletion (at least 10 characters).",
    "Indique um participante.": "Provide a participant.",
    "Interruptor desconhecido.": "Unknown switch.",
    "Lista de controlos inválida.": "Invalid list of controls.",
    "Marco inválido.": "Invalid milestone.",
    "Marco já registado.": "Milestone already recorded.",
    "Modelo de tarefa inválido.": "Invalid task template.",
    "Nenhum framework ativo disponível.": "No active framework available.",
    "Not Found": "Not Found",
    "Não foi possível registar o apagamento. Nada foi apagado.": "The erasure could not be recorded. Nothing was erased.",
    "Não pode alterar o seu próprio role.": "You cannot change your own role.",
    "Não pode anonimizar a sua própria conta.": "You cannot anonymise your own account.",
    "Não pode desativar a sua própria conta.": "You cannot deactivate your own account.",
    "Não é possível promover um utilizador a admin por este endpoint.": "A user cannot be promoted to admin through this endpoint.",
    "Não é possível resetar password de utilizador desativado.": "The password of a deactivated user cannot be reset.",
    "O certificado e a chave privada não correspondem. Certifique-se de que fazem parte do mesmo par.": "The certificate and the private key do not match. Make sure they belong to the same pair.",
    "O conteúdo do ficheiro não corresponde ao tipo declarado. Verifique se o ficheiro não está corrompido ou adulterado.": "The file content does not match the declared type. Check that the file is not corrupted or tampered with.",
    "O controlo já está marcado como não aplicável.": "The control is already marked as not applicable.",
    "O controlo não está marcado como não aplicável.": "The control is not marked as not applicable.",
    "O ficheiro não pode estar vazio.": "The file cannot be empty.",
    "O ficheiro não tem a estrutura interna esperada para o tipo declarado.": "The file does not have the internal structure expected for the declared type.",
    "O pedido é demasiado grande.": "The request is too large.",
    "O servidor está ocupado. Tente de novo dentro de momentos.": "The server is busy. Please try again in a moment.",
    "O texto é obrigatório.": "The text is required.",
    "O título da evidência é obrigatório.": "The evidence title is required.",
    "O título da evidência não pode ter mais de 255 caracteres.": "The evidence title cannot be longer than 255 characters.",
    "O título é obrigatório.": "The title is required.",
    "O utilizador selecionado não tem o perfil de implementador.": "The selected user does not have the implementer role.",
    "Participante não encontrado.": "Participant not found.",
    "Password atual incorreta.": "Current password is incorrect.",
    "Password incorreta.": "Incorrect password.",
    "Pessoa inválida.": "Invalid person.",
    "Política de permissões indisponível.": "Permission policy unavailable.",
    "Razão de apagamento inválida.": "Invalid reason for the erasure.",
    "Registo não encontrado.": "Record not found.",
    "Registo público não disponível neste modo de instalação.": "Public registration is not available in this installation mode.",
    "Responsável inválido.": "Invalid owner.",
    "Sem permissão para alterar esta evidência.": "No permission to change this evidence.",
    "Sem permissão para gerir este utilizador.": "No permission to manage this user.",
    "Sem permissão para propagar a revisão a todos os controlos indicados.": "No permission to propagate the review to all the indicated controls.",
    "Sem permissão para realizar esta ação.": "No permission to perform this action.",
    "Sem permissão.": "No permission.",
    "Sessão de setup inválida ou expirada. Recarregue a página para reiniciar o assistente.": "Setup session invalid or expired. Reload the page to restart the wizard.",
    "Sessão expirada. Por favor, faça login novamente.": "Session expired. Please sign in again.",
    "Sessão não encontrada. Por favor, faça login novamente.": "Session not found. Please sign in again.",
    "Sub-administradores não podem atribuir este perfil.": "Sub-administrators cannot assign this role.",
    "Sub-administradores não podem criar utilizadores com este perfil.": "Sub-administrators cannot create users with this role.",
    "Só a administração pode apagar uma evidência com lápide.": "Only administrators can erase evidence with a tombstone.",
    "Só é possível propagar para controlos que esta evidência já sustenta.": "Propagation is only possible to controls this evidence already supports.",
    "Tarefa não encontrada.": "Task not found.",
    "Token inválido ou expirado.": "Invalid or expired token.",
    "Token inválido para este endpoint.": "Invalid token for this endpoint.",
    "Token inválido.": "Invalid token.",
    "Não autenticado.": "Not authenticated.",
    "Token temporário inválido ou expirado.": "Temporary token invalid or expired.",
    "Token temporário inválido para esta operação.": "Temporary token not valid for this operation.",
    "Token temporário obrigatório.": "Temporary token required.",
    "Uma ação realizada não pode ter data futura.": "A completed session cannot have a future date.",
    "Uma conclusão não pode ter data futura.": "A completion cannot have a future date.",
    "Uma evidência sem controlo associado não pode ser revista; ligue-a primeiro.": "Evidence without an associated control cannot be reviewed; link it first.",
    "Use o ecrã de perfil para alterar a sua própria password.": "Use the profile screen to change your own password.",
    "Use o seu perfil para gerir o seu próprio MFA.": "Use your profile to manage your own MFA.",
    "Utilizador inválido para esta operação.": "Invalid user for this operation.",
    "Utilizador já está ativo.": "User is already active.",
    "Utilizador já está desativado.": "User is already deactivated.",
    "Utilizador já foi anonimizado.": "User has already been anonymised.",
    "Utilizador não encontrado.": "User not found.",
    "cert_pem e key_pem são obrigatórios para modo=custom.": "cert_pem and key_pem are required for mode=custom.",
    "Erro interno do servidor. Tente novamente mais tarde.": "Internal server error. Try again later.",
    "Erro de validação (dados inválidos).": "Validation error (invalid data).",
    "O 2FA já está ativo.": "Two-factor authentication is already enabled.",
    "O 2FA não pode ser validado: a chave de cifra desta instalação não abre o segredo guardado. Contacte o administrador.": "Two-factor authentication cannot be checked: this installation's encryption key does not open the stored secret. Contact the administrator.",
    "O ficheiro desta evidência não abre com a chave de cifra desta instalação. Contacte o administrador.": "This evidence file does not open with this installation's encryption key. Contact the administrator.",
}

# Mensagens com um número ou nome no meio: traduz-se pelo prefixo fixo e
# mantém-se o resto (o valor) tal como veio.
_EN_PREFIXOS: tuple[tuple[str, str], ...] = (
    ("A justificação é obrigatória (mínimo ", "A justification is required (minimum "),
    ("O campo '", "The field '"),
    # O mínimo vem da política da empresa: o número muda de instalação para instalação.
    ("A password deve ter pelo menos ", "The password must be at least "),
)
_EN_SUFIXOS: tuple[tuple[str, str], ...] = (
    ("' é obrigatório.", "' is required."),
    (" caracteres.", " characters long."),
)


def traduzir(detail: Any, locale: str | None) -> Any:
    """Devolve o `detail` na língua pedida. Só toca em texto; o resto passa."""
    if locale != "en" or not isinstance(detail, str):
        return detail
    if detail in _EN:
        return _EN[detail]
    for pt, en in _EN_PREFIXOS:
        if detail.startswith(pt):
            resto = detail[len(pt):]
            for pt_s, en_s in _EN_SUFIXOS:
                if resto.endswith(pt_s):
                    resto = resto[: -len(pt_s)] + en_s
            return en + resto
    # Várias mensagens de validação juntas com " | ".
    if " | " in detail:
        return " | ".join(traduzir(parte, locale) for parte in detail.split(" | "))
    return detail


def tem_traducao(mensagem: str) -> bool:
    return mensagem in _EN or any(mensagem.startswith(pt) for pt, _ in _EN_PREFIXOS)
