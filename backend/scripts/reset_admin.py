#!/usr/bin/env python3
"""
NIS2PME — Reset de credenciais do administrador / Administrator credentials reset.

Uso (dentro do container Docker) / Usage (inside the Docker container):
    docker exec -it nis2pme_backend python scripts/reset_admin.py

Uso (fora do container) / Usage (outside the container):
    DATABASE_URL=... python scripts/reset_admin.py

O script (sem autenticação — acesso direto à BD) / The script (no auth — direct DB access):
  - Lista os utilizadores ADMIN e SUBADMIN e permite escolher qual recuperar
  - Oferece reset de password, desativação de 2FA, ou ambos
  - Usa as MESMAS regras de password do resto da plataforma (validar_forca_password)
  - Regista a ação no AuditLog (best-effort)
  - Bilingue (Português / English)
"""
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

# ── Carregar variáveis de ambiente / Load environment variables ───────────────
# 1. Secrets auto-gerados do volume (Docker)
_secrets_file = Path("/app/data/auto-secrets.env")
if _secrets_file.exists():
    for _line in _secrets_file.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _key, _, _val = _line.partition("=")
            os.environ.setdefault(_key.strip(), _val.strip())

# 2. .env local (desenvolvimento) — best-effort: em produção o DATABASE_URL já
# chega via env_file/environment do compose, por isso um .env ilegível não deve
# impedir o script de correr.
_env_file = Path(__file__).resolve().parents[1] / ".env"
try:
    if _env_file.exists():
        for _line in _env_file.read_text(encoding="utf-8").splitlines():
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _key, _, _val = _line.partition("=")
                os.environ.setdefault(_key.strip(), _val.strip())
except OSError:
    pass

# Forçar on-prem (defensivo, caso algum import futuro carregue a config)
os.environ.setdefault("DEPLOYMENT_MODE", "onprem")

# ── Verificar variáveis obrigatórias / Check required variables ───────────────
# Só DATABASE_URL é necessária: o reset usa Argon2 (sem Fernet) e não decifra TOTP.
if not os.environ.get("DATABASE_URL"):
    print("\nERRO / ERROR: DATABASE_URL não definida / is not set.")
    print("  Dentro do Docker / Inside Docker:")
    print("    docker exec -it nis2pme_backend python scripts/reset_admin.py")
    sys.exit(1)

# ── Imports após env pronto / Imports once env is ready ───────────────────────
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    from argon2 import PasswordHasher
    from sqlalchemy.exc import OperationalError
    from sqlmodel import Session, create_engine, select

    from app.empresas.models import Empresa  # noqa: F401 — regista o mapper antes do Utilizador
    from app.auth.models import CodigoBackup2FA, RoleUtilizador, TokenRefresh, Utilizador
    from app.shared.audit import AuditLog, ResultadoAcao
    from app.shared.utils import validar_forca_password
except ImportError as e:
    print(f"\nERRO / ERROR: dependência ou módulo em falta / missing dependency or module: {e}")
    print("  Corre dentro do container / Run inside the container.")
    sys.exit(1)

_ph = PasswordHasher()


# ── Mensagens bilingues / Bilingual messages ──────────────────────────────────
MSGS = {
    "pt": {
        "title": "NIS2PME — Reset de credenciais do administrador",
        "no_admins": "ERRO: Não foi encontrado nenhum administrador/subadministrador.",
        "not_configured": "  O sistema ainda não foi configurado? Corre primeiro o assistente de setup.",
        "users_title": "  Utilizadores recuperáveis:",
        "twofa_on": "2FA ativo",
        "twofa_off": "2FA inativo",
        "choose_user": "  Escolhe o utilizador a recuperar (número): ",
        "invalid_number": "  Número inválido.",
        "op_title": "  O que pretendes fazer?",
        "op1": "    1. Redefinir password",
        "op2": "    2. Desativar 2FA (MFA)",
        "op3": "    3. Redefinir password E desativar 2FA",
        "op0": "    0. Cancelar",
        "op_prompt": "  Opção: ",
        "op_invalid": "  Introduz 0, 1, 2 ou 3.",
        "cancelled": "  Cancelado. Nenhuma alteração foi feita.",
        "irreversible": "  ATENÇÃO: Esta operação é irreversível.",
        "confirm_email": "  Para confirmar, escreve o email do utilizador ({email}): ",
        "email_mismatch": "  Email não coincide. Cancelado.",
        "pwd_rules": "  Nova password (mín. 8 caracteres: maiúscula, minúscula, dígito e especial):",
        "pwd_prompt": "  Nova password: ",
        "pwd_confirm": "  Confirmar password: ",
        "pwd_invalid": "  Password inválida. Requisitos: mín. 8 caracteres, com maiúscula, minúscula, dígito e caráter especial.",
        "pwd_mismatch": "  As passwords não coincidem.",
        "applying": "  A aplicar alterações...",
        "done_pwd": "  ✓ Password redefinida",
        "done_sessions": "  ✓ Todas as sessões ativas revogadas",
        "done_2fa": "  ✓ 2FA desativado e backup codes apagados",
        "success": "  Concluído com sucesso!",
        "can_login": "  → O utilizador pode agora iniciar sessão com a nova password.",
        "twofa_note": "  → O 2FA foi desativado. Pode reativá-lo nas definições de conta após iniciar sessão.",
        "security": "  SEGURANÇA: guarda este terminal em segurança e fecha a sessão.",
        "db_error": "  ERRO: não foi possível ligar à base de dados. Confirma que o container está a correr e tenta de novo.",
        "unexpected": "  ERRO inesperado: {err}",
    },
    "en": {
        "title": "NIS2PME — Administrator credentials reset",
        "no_admins": "ERROR: No administrator/sub-administrator was found.",
        "not_configured": "  Has the system been set up yet? Run the setup wizard first.",
        "users_title": "  Recoverable users:",
        "twofa_on": "2FA enabled",
        "twofa_off": "2FA disabled",
        "choose_user": "  Choose the user to recover (number): ",
        "invalid_number": "  Invalid number.",
        "op_title": "  What do you want to do?",
        "op1": "    1. Reset password",
        "op2": "    2. Disable 2FA (MFA)",
        "op3": "    3. Reset password AND disable 2FA",
        "op0": "    0. Cancel",
        "op_prompt": "  Option: ",
        "op_invalid": "  Enter 0, 1, 2 or 3.",
        "cancelled": "  Cancelled. No changes were made.",
        "irreversible": "  WARNING: This operation is irreversible.",
        "confirm_email": "  To confirm, type the user's email ({email}): ",
        "email_mismatch": "  Email does not match. Cancelled.",
        "pwd_rules": "  New password (min. 8 characters: upper-case, lower-case, digit and special):",
        "pwd_prompt": "  New password: ",
        "pwd_confirm": "  Confirm password: ",
        "pwd_invalid": "  Invalid password. Requirements: at least 8 characters, with upper-case, lower-case, a digit and a special character.",
        "pwd_mismatch": "  The passwords do not match.",
        "applying": "  Applying changes...",
        "done_pwd": "  ✓ Password reset",
        "done_sessions": "  ✓ All active sessions revoked",
        "done_2fa": "  ✓ 2FA disabled and backup codes deleted",
        "success": "  Completed successfully!",
        "can_login": "  → The user can now sign in with the new password.",
        "twofa_note": "  → 2FA has been disabled. It can be re-enabled in the account settings after signing in.",
        "security": "  SECURITY: keep this terminal safe and close the session.",
        "db_error": "  ERROR: could not connect to the database. Check that the container is running and try again.",
        "unexpected": "  Unexpected error: {err}",
    },
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _escolher_idioma() -> dict:
    """Pergunta o idioma (default PT). Returns the chosen message dict."""
    print()
    print("  Idioma / Language:   [1] Português   [2] English")
    escolha = input("  > ").strip()
    return MSGS["en"] if escolha == "2" else MSGS["pt"]


def _input_seguro(prompt: str) -> str:
    """Lê input sem echo no terminal."""
    import getpass
    return getpass.getpass(prompt)


def _listar_recuperaveis(db: Session) -> list[Utilizador]:
    """ADMIN + SUBADMIN não eliminados, ordem determinística (ADMIN antes, depois email)."""
    return db.exec(
        select(Utilizador)
        .where(
            Utilizador.role.in_([RoleUtilizador.ADMIN, RoleUtilizador.SUBADMIN]),  # type: ignore[attr-defined]
            Utilizador.deleted_at.is_(None),  # type: ignore[union-attr]
        )
        .order_by(Utilizador.role, Utilizador.email)
    ).all()


def _apagar_refresh_tokens(db: Session, utilizador_id: uuid.UUID) -> None:
    for t in db.exec(
        select(TokenRefresh).where(TokenRefresh.utilizador_id == utilizador_id)
    ).all():
        db.delete(t)


def _apagar_codigos_backup(db: Session, utilizador_id: uuid.UUID) -> None:
    for c in db.exec(
        select(CodigoBackup2FA).where(CodigoBackup2FA.utilizador_id == utilizador_id)
    ).all():
        db.delete(c)


def _registar_auditlog(db: Session, utilizador: Utilizador, acao: str, dados: dict) -> None:
    """Best-effort — não interrompe o reset se a tabela/coluna não existir."""
    try:
        db.add(AuditLog(
            empresa_id=utilizador.empresa_id,
            utilizador_id=utilizador.id,
            acao=acao,
            entidade_tipo="Utilizador",
            entidade_id=utilizador.id,
            dados_anteriores=None,
            dados_novos=str(dados),
            ip_address="127.0.0.1 (reset-script)",
            user_agent="reset_admin.py",
            resultado=ResultadoAcao.SUCESSO,
        ))
        db.commit()
    except Exception:
        db.rollback()


# ── Fluxo principal ───────────────────────────────────────────────────────────

def _fluxo(db: Session, M: dict) -> None:
    users = _listar_recuperaveis(db)
    if not users:
        print()
        print(M["no_admins"])
        print(M["not_configured"])
        sys.exit(1)

    # 1. Escolher o utilizador (auto se só houver 1)
    if len(users) == 1:
        utilizador = users[0]
    else:
        print()
        print(M["users_title"])
        for i, u in enumerate(users, 1):
            twofa = M["twofa_on"] if u.totp_ativo else M["twofa_off"]
            print(f"    {i}. {u.email}  [{u.role.value}]  ({twofa})")
        print()
        while True:
            try:
                idx = int(input(M["choose_user"]))
                if 1 <= idx <= len(users):
                    utilizador = users[idx - 1]
                    break
            except ValueError:
                pass
            print(M["invalid_number"])

    twofa = M["twofa_on"] if utilizador.totp_ativo else M["twofa_off"]
    print()
    print(f"  {utilizador.email}  [{utilizador.role.value}]  ({twofa})")

    # 2. Escolher operação
    print()
    print(M["op_title"])
    print(M["op1"])
    print(M["op2"])
    print(M["op3"])
    print(M["op0"])
    print()
    while True:
        try:
            opcao = int(input(M["op_prompt"]))
            if opcao in (0, 1, 2, 3):
                break
        except ValueError:
            pass
        print(M["op_invalid"])

    if opcao == 0:
        print()
        print(M["cancelled"])
        sys.exit(0)

    fazer_pwd = opcao in (1, 3)
    fazer_mfa = opcao in (2, 3)

    # 3. Confirmar por email
    print()
    print(M["irreversible"])
    conf = input(M["confirm_email"].format(email=utilizador.email)).strip()
    if conf.lower() != utilizador.email.lower():
        print()
        print(M["email_mismatch"])
        sys.exit(1)

    # 4. Nova password (regras centralizadas)
    nova_hash = None
    if fazer_pwd:
        print()
        print(M["pwd_rules"])
        while True:
            pw = _input_seguro(M["pwd_prompt"])
            valida, _ = validar_forca_password(pw)
            if not valida:
                print(M["pwd_invalid"])
                continue
            if pw != _input_seguro(M["pwd_confirm"]):
                print(M["pwd_mismatch"])
                continue
            break
        nova_hash = _ph.hash(pw)

    # 5. Aplicar
    print()
    print(M["applying"])
    if fazer_pwd and nova_hash:
        utilizador.password_hash = nova_hash
        utilizador.password_temporaria_ativa = False
        _apagar_refresh_tokens(db, utilizador.id)
        print(M["done_pwd"])
        print(M["done_sessions"])
    if fazer_mfa:
        utilizador.totp_secret_cifrado = None
        utilizador.totp_ativo = False
        _apagar_codigos_backup(db, utilizador.id)
        print(M["done_2fa"])

    utilizador.updated_at = datetime.now(timezone.utc)
    db.add(utilizador)
    db.commit()
    db.refresh(utilizador)

    # 6. AuditLog
    if fazer_pwd:
        _registar_auditlog(db, utilizador, "utilizador.password_reset_manual",
                           {"metodo": "reset_admin_script", "sessoes_revogadas": True})
    if fazer_mfa:
        _registar_auditlog(db, utilizador, "utilizador.2fa_reset_manual",
                           {"metodo": "reset_admin_script"})

    # 7. Sumário
    print()
    print("=" * 60)
    print(M["success"])
    print()
    if fazer_pwd:
        print(M["can_login"])
    if fazer_mfa:
        print(M["twofa_note"])
    print()
    print(M["security"])
    print("=" * 60)
    print()


def main() -> None:
    M = _escolher_idioma()
    print()
    print("=" * 60)
    print(f"  {M['title']}")
    print("=" * 60)

    try:
        engine = create_engine(os.environ["DATABASE_URL"], echo=False)
        with Session(engine) as db:
            _fluxo(db, M)
    except OperationalError:
        print()
        print(M["db_error"])
        sys.exit(1)
    except SystemExit:
        raise
    except KeyboardInterrupt:
        print()
        print(M["cancelled"])
        sys.exit(1)
    except Exception as e:  # noqa: BLE001
        print()
        print(M["unexpected"].format(err=e))
        sys.exit(1)


if __name__ == "__main__":
    main()
