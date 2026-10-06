"""
Ponto de entrada da aplicação FastAPI — NIS2PME Backend.
Configura CORS, rate limiting (slowapi) e inclui os routers.
"""
import logging
from contextlib import asynccontextmanager
from urllib.parse import urlparse

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.shared.mensagens import traduzir as traduzir_mensagem
from app.shared.resposta_json import UtcJSONResponse
from app.shared.utils import parse_accept_language
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from app.config import get_settings
from app.database import engine
from app.shared.manutencao import em_manutencao
from app.shared.utils import host_e_dominio, obter_chave_limite, pedido_e_seguro

settings = get_settings()

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.DEBUG if settings.DEBUG else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Rate limiter global (partilhado com os routers)
# ---------------------------------------------------------------------------

# A chave é o IP do cliente reduzido ao /64 em IPv6 (ver `chave_de_limite_ip`).
limiter = Limiter(key_func=obter_chave_limite, default_limits=["200/minute"])


# ---------------------------------------------------------------------------
# Lifespan (startup / shutdown)
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Estado da cifra de PII, registado logo à cabeça. Sem esta linha o estado é
    # inobservável: os campos são lidos e escritos na mesma, e a única diferença é
    # se ficam legíveis a quem leia a base de dados.
    from app.shared.pii import cifra_ativa
    if cifra_ativa():
        logger.info("Cifra de PII ATIVA (campos pessoais cifrados em repouso).")
    else:
        logger.warning(
            "[SEGURANCA] Cifra de PII INATIVA — PII_DEV_PLAINTEXT em uso. "
            "Os campos pessoais NÃO estão cifrados. Nunca usar assim em produção."
        )

    # Seed automático do framework (carrega framework se não existir na DB).
    # Necessário em saas/saas-trial também: não há wizard de setup nesses modos
    # para o fazer manualmente, e o FRAMEWORKS_DIR vem sempre cozido na imagem.
    from sqlmodel import Session
    from app.setup.seed_framework import seed_framework_se_necessario
    with Session(engine) as _seed_db:
        seed_framework_se_necessario(_seed_db)

    # Finalização de um restauro de backup (on-prem): o entrypoint já migrou a
    # base — reconciliar evidências, auditar e limpar o modo manutenção. Sem
    # restauro pendente limpa apenas uma flag de manutenção órfã (auto-cura).
    if settings.DEPLOYMENT_MODE == "onprem":
        from app.backup.restaurar import finalizar_restauro_no_arranque
        try:
            finalizar_restauro_no_arranque()
        except Exception:  # noqa: BLE001 — a app tem de arrancar; o relatório fica nos logs
            logger.exception("Finalização do restauro falhou — verifique data/restauro-pendente.json.")

        # Um backup morto a meio (falta de memória) deixa o testemunho: limpar o
        # que ficou e não o repetir hoje, antes de o tick do agendado arrancar.
        from app.backup.service import recuperar_backup_interrompido
        try:
            with Session(engine) as _bk_db:
                recuperar_backup_interrompido(_bk_db)
        except Exception:  # noqa: BLE001 — a app tem de arrancar
            logger.exception("Recuperação de um backup interrompido falhou.")

    # Prazos das notificações de incidentes (Regime Jurídico da Cibersegurança,
    # arts. 42.º-44.º; RGPD, art. 33.º): o mais curto é de 24 h, por isso o tick
    # corre de hora a hora e põe os avisos in-app (e email) de marcos em risco ou
    # em atraso de acordo com a realidade. Aplica-se a ambos os modos de deployment.
    # As referências das tasks ficam em app.state: sem referência forte, o event
    # loop pode deixar o garbage collector matar a task a meio.
    import asyncio
    from app.incidentes.service import verificar_prazos_incidentes

    async def _loop_prazos_incidentes():
        while True:
            try:
                if not em_manutencao():
                    def _run():
                        with Session(engine) as _db:
                            verificar_prazos_incidentes(_db)
                    await asyncio.to_thread(_run)
            except Exception:  # noqa: BLE001 — o tick vigia prazos legais: nunca morre, mas fica registado
                logger.exception("Tick de prazos de incidentes falhou; nova tentativa no próximo ciclo.")
            await asyncio.sleep(3600)

    app.state.ticks = [asyncio.create_task(_loop_prazos_incidentes())]

    # Tarefas recorrentes de conformidade (rever acessos, testar backups, formação
    # anual, ...): tick diário que gera notificações in-app para prazos a vencer ou
    # em atraso. Aplica-se a ambos os modos de deployment.
    from app.tarefas.service import verificar_prazos_tarefas

    async def _loop_prazos_tarefas():
        while True:
            try:
                if not em_manutencao():
                    def _run():
                        with Session(engine) as _db:
                            verificar_prazos_tarefas(_db)
                    await asyncio.to_thread(_run)
            except Exception:  # noqa: BLE001
                logger.exception("Tick de prazos de tarefas falhou; nova tentativa no próximo ciclo.")
            await asyncio.sleep(24 * 3600)

    app.state.ticks.append(asyncio.create_task(_loop_prazos_tarefas()))

    # Conetores (premium): consome os eventos do sidecar (drift/contradição) e
    # produz notificações, evidência técnica automática e auditoria. Tick de 6h;
    # sem premium configurado (ou sem a feature) não faz nada — fail-soft.
    from app.premium.conetor_tick import processar_conetores

    async def _loop_conetores():
        while True:
            try:
                if not em_manutencao():
                    def _run():
                        with Session(engine) as _db:
                            processar_conetores(_db)
                    await asyncio.to_thread(_run)
            except Exception:  # noqa: BLE001
                logger.exception("Tick dos conetores falhou; nova tentativa no próximo ciclo.")
            await asyncio.sleep(6 * 3600)

    app.state.ticks.append(asyncio.create_task(_loop_conetores()))

    # Resumo semanal por email ("a precisar de atenção"): tick horário que só age
    # à segunda-feira de manhã (>= 07:00 UTC). O gate (EMAIL_NOTIFICACOES no .env +
    # email configurado) e o dedup por semana vivem em enviar_digest_semanal —
    # com o email desligado o tick não faz nada.
    from app.notificacoes.email import enviar_digest_semanal

    async def _loop_digest_email():
        while True:
            try:
                from datetime import datetime, timezone
                agora = datetime.now(timezone.utc)
                if agora.weekday() == 0 and agora.hour >= 7 and not em_manutencao():
                    def _run():
                        with Session(engine) as _db:
                            enviar_digest_semanal(_db)
                    await asyncio.to_thread(_run)
            except Exception:  # noqa: BLE001
                logger.exception("Tick do digest semanal falhou; nova tentativa no próximo ciclo.")
            await asyncio.sleep(3600)

    app.state.ticks.append(asyncio.create_task(_loop_digest_email()))

    # Retenção das notificações: apaga os lembretes JÁ LIDOS que saíram da janela
    # de NOTIF_RETENCAO_DIAS. Nunca toca numa notificação por ler. Sem isto a
    # tabela só cresce, e é ela que serve o contador do sininho em cada pedido.
    from app.notificacoes.service import purgar_lidas_antigas

    async def _loop_retencao_notificacoes():
        while True:
            try:
                if not em_manutencao():
                    def _run():
                        with Session(engine) as _db:
                            apagadas = purgar_lidas_antigas(
                                _db, dias=settings.NOTIF_RETENCAO_DIAS
                            )
                            if apagadas:
                                _db.commit()
                                logger.info(
                                    "Retenção de notificações: %d linha(s) apagada(s).",
                                    apagadas,
                                )
                    await asyncio.to_thread(_run)
            except Exception:  # noqa: BLE001 — a higiene da tabela não pode derrubar a app
                logger.exception("Tick de retenção de notificações falhou; nova tentativa no próximo ciclo.")
            await asyncio.sleep(24 * 3600)

    app.state.ticks.append(asyncio.create_task(_loop_retencao_notificacoes()))

    # Retenção do audit log: arquiva em /app/data/audit-archive os meses civis que
    # saíram inteiros da janela de AUDIT_RETENCAO_DIAS e só depois os apaga da tabela.
    # Na esmagadora maioria dos dias não faz nada — só há trabalho quando vira um mês,
    # e numa instalação recente não há trabalho nenhum durante cerca de um ano.
    from app.auditoria.arquivo import arquivar_e_purgar

    async def _loop_retencao_auditoria():
        while True:
            try:
                if not em_manutencao():
                    def _run():
                        with Session(engine) as _db:
                            arquivar_e_purgar(_db)
                    await asyncio.to_thread(_run)
            except Exception:  # noqa: BLE001 — falhar aqui não pode impedir o resto; a trilha fica intacta
                logger.exception("Tick de retenção do audit log falhou; nova tentativa no próximo ciclo.")
            await asyncio.sleep(24 * 3600)

    app.state.ticks.append(asyncio.create_task(_loop_retencao_auditoria()))

    # Higiene diária: confere a cadeia de hashes da auditoria (uma adulteração
    # fica no log e no próprio registo) e apaga tokens de sessão/reset que já
    # não valem há mais de um mês — nunca eram apagados.
    from app.auditoria.verificar_cadeia import verificar_e_registar
    from app.auth.service import purgar_tokens_expirados

    async def _loop_higiene_diaria():
        while True:
            try:
                if not em_manutencao():
                    def _run():
                        with Session(engine) as _db:
                            verificar_e_registar(_db)
                            apagados = purgar_tokens_expirados(_db)
                            if apagados["refresh"] or apagados["reset"]:
                                logger.info("Tokens purgados: %s", apagados)
                    await asyncio.to_thread(_run)
            except Exception:  # noqa: BLE001 — a higiene não pode derrubar a app
                logger.exception("Tick de higiene diária falhou; nova tentativa no próximo ciclo.")
            await asyncio.sleep(24 * 3600)

    app.state.ticks.append(asyncio.create_task(_loop_higiene_diaria()))

    # Testemunho externo da trilha (on-prem com premium): de hora a hora, as
    # cabeças das cadeias seguem para o fornecedor no heartbeat da licença e o que
    # ele testemunhou da última vez confere-se com a trilha.
    if settings.DEPLOYMENT_MODE == "onprem" and settings.PREMIUM_ENABLED:
        from app.auditoria.testemunho import testemunhar_e_conferir

        async def _loop_testemunho_trilha():
            while True:
                try:
                    if not em_manutencao():
                        def _run():
                            with Session(engine) as _db:
                                testemunhar_e_conferir(_db)
                        await asyncio.to_thread(_run)
                except Exception:  # noqa: BLE001 — o testemunho não pode derrubar a app
                    logger.exception("Tick do testemunho da trilha falhou; nova tentativa na próxima hora.")
                await asyncio.sleep(3600)

        app.state.ticks.append(asyncio.create_task(_loop_testemunho_trilha()))

    # Reciclagem das evidências sem controlo associado. Contar referências no
    # momento de desligar seria uma corrida entre dois utilizadores; ficar órfã
    # é um estado, e é aqui que se trata de quem lá está há demasiado tempo.
    # Na esmagadora maioria dos dias não faz nada.
    from app.evidencias.reciclagem import varrer as varrer_orfas

    async def _loop_reciclagem_evidencias():
        while True:
            try:
                if not em_manutencao() and settings.EVIDENCIA_ORFA_DIAS > 0:
                    def _run():
                        with Session(engine) as _db:
                            varrer_orfas(_db, settings.EVIDENCIA_ORFA_DIAS)
                    await asyncio.to_thread(_run)
            except Exception:  # noqa: BLE001 — a higiene não pode derrubar a app
                logger.exception(
                    "Tick de reciclagem de evidências falhou; nova tentativa no próximo ciclo."
                )
            await asyncio.sleep(24 * 3600)

    app.state.ticks.append(asyncio.create_task(_loop_reciclagem_evidencias()))

    # Fim do acesso (trial no SaaS, licença no on-prem): tick diário que avisa os
    # administradores no centro de notificações e por email, uma vez por marco.
    from app.premium.prazo_acesso import avisar_fim_de_acesso

    async def _loop_fim_de_acesso():
        await asyncio.sleep(120)  # deixa o sidecar arrancar primeiro
        while True:
            try:
                if not em_manutencao():
                    def _run():
                        with Session(engine) as _db:
                            avisar_fim_de_acesso(_db)
                    await asyncio.to_thread(_run)
            except Exception:  # noqa: BLE001 — o tick nunca morre; fica registado
                logger.exception("Aviso de fim de acesso falhou; nova tentativa no próximo ciclo.")
            await asyncio.sleep(24 * 3600)

    app.state.ticks.append(asyncio.create_task(_loop_fim_de_acesso()))

    # SaaS: reconciliação do provisionamento de planos. O registo pede o plano ao
    # gateway em best-effort; o que falhou fica marcado na empresa e volta a
    # pedir-se aqui, a cada 15 min (o upsert do gateway é idempotente).
    if settings.DEPLOYMENT_MODE == "saas":
        from app.premium.provisioning import reconciliar_provisionamento

        async def _loop_reconciliar_provisionamento():
            while True:
                await asyncio.sleep(15 * 60)
                try:
                    if not em_manutencao():
                        def _run():
                            with Session(engine) as _db:
                                reconciliar_provisionamento(_db)
                        await asyncio.to_thread(_run)
                except Exception:  # noqa: BLE001 — o tick nunca morre; fica registado
                    logger.exception("Reconciliação de planos falhou; nova tentativa no próximo ciclo.")

        app.state.ticks.append(asyncio.create_task(_loop_reconciliar_provisionamento()))

    if settings.DEPLOYMENT_MODE == "onprem":
        # Verificação de atualizações: arranque + cada 24h (respeita VERIFY_UPDATES).
        import asyncio
        from app.updates.service import verificar_updates_sync

        async def _loop_updates():
            while True:
                try:
                    if not em_manutencao():
                        await asyncio.to_thread(verificar_updates_sync)
                except Exception:  # noqa: BLE001
                    logger.exception("Verificação de atualizações falhou; nova tentativa no próximo ciclo.")
                await asyncio.sleep(24 * 3600)

        app.state.ticks.append(asyncio.create_task(_loop_updates()))

        # Backup diário agendado (ligado por defeito): tick horário que age quando
        # a hora local chega à configurada e ainda não há backup do dia. O gate
        # (ativo + passphrase definida) e o dedup diário vivem no serviço — uma
        # falha (ex.: disco cheio) fica no log e o ciclo seguinte tenta de novo.
        from app.backup.service import executar_backup_agendado

        async def _loop_backup_agendado():
            while True:
                try:
                    if not em_manutencao():
                        def _run():
                            with Session(engine) as _db:
                                executar_backup_agendado(_db)
                        await asyncio.to_thread(_run)
                except Exception:  # noqa: BLE001
                    logger.exception("Tick do backup agendado falhou; nova tentativa no próximo ciclo.")
                await asyncio.sleep(3600)

        app.state.ticks.append(asyncio.create_task(_loop_backup_agendado()))

    # Aviso de segurança: cookie de refresh não-seguro em modo de produção
    if not settings.COOKIE_SECURE and settings.DEPLOYMENT_MODE in ("saas", "onprem"):
        logger.warning(
            "[SECURITY] COOKIE_SECURE=False em DEPLOYMENT_MODE=%s. "
            "Definir COOKIE_SECURE=true no .env para produção (HTTPS).",
            settings.DEPLOYMENT_MODE,
        )
    logger.info(
        "NIS2PME backend iniciado [modo=%s, debug=%s]",
        settings.DEPLOYMENT_MODE,
        settings.DEBUG,
    )
    yield


# ---------------------------------------------------------------------------
# Aplicação FastAPI
# ---------------------------------------------------------------------------

app = FastAPI(
    # As datas-hora saem marcadas como UTC (ver `shared/resposta_json.py`).
    default_response_class=UtcJSONResponse,
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="Backend API — Plataforma NIS2PME de conformidade para PME portuguesas.",
    docs_url="/api/docs" if settings.ENABLE_API_DOCS else None,
    redoc_url="/api/redoc" if settings.ENABLE_API_DOCS else None,
    openapi_url="/api/openapi.json" if settings.ENABLE_API_DOCS else None,
    lifespan=lifespan,
)

# Expor o limiter no state para os routers o usarem
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)

# ---------------------------------------------------------------------------
# CORS — apenas origens explícitas, nunca wildcard em produção
# ---------------------------------------------------------------------------

# CORS — apenas origens explícitas, nunca wildcard em produção
_cors_origins = list(settings.CORS_ORIGINS)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,  # necessário para cookies httpOnly
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Accept"],
)


# ---------------------------------------------------------------------------
# Security headers — defense in depth (CWE-693)
# Adicionados mesmo atrás do Cloudflare Tunnel, por defense in depth.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Modo manutenção — durante um restauro de backup a API recusa tudo exceto o
# healthcheck (a flag vive no volume de dados e é limpa no arranque seguinte).
# ---------------------------------------------------------------------------

@app.middleware("http")
async def manutencao_middleware(request: Request, call_next):
    if request.url.path != "/api/health" and em_manutencao():
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"detail": "Em manutenção: restauro de backup em curso. Aguarde."},
        )
    return await call_next(request)


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    # CSP para respostas da API (JSON puro — sem recursos externos necessários)
    response.headers["Content-Security-Policy"] = "default-src 'none'"
    # HSTS só quando o pedido é realmente HTTPS E o endereço é um domínio (não IP).
    # Em deployments por IP (forçosamente self-signed) o HSTS impediria o
    # click-through do aviso de certificado e poderia trancar o acesso (#5 / CWE-319).
    if pedido_e_seguro(request) and host_e_dominio(urlparse(get_settings().APP_URL).hostname):
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


# ---------------------------------------------------------------------------
# Teto ao corpo dos pedidos. Acrescentado depois dos outros middlewares, fica
# por fora de todos: um corpo acima do teto é recusado antes de alguém o ler.
# ---------------------------------------------------------------------------

from app.shared.limite_corpo import LimiteDeCorpo  # noqa: E402

_MiB = 1024 * 1024
# As rotas que recebem ficheiros grandes. As outras rotas das mesmas áreas só
# recebem JSON pequeno e ficam no teto geral.
_ROTA_UPLOAD_BACKUP = "/api/backups/upload"
_ROTAS_UPLOAD_PARECER = ("/api/dossie/parecer/inspecionar", "/api/dossie/parecer/confirmar")


def teto_do_corpo(caminho: str) -> int:
    """Máximo, em bytes, do corpo de um pedido a `caminho`."""
    if caminho == _ROTA_UPLOAD_BACKUP:
        return settings.CORPO_MAX_BACKUP_MB * _MiB
    if caminho in _ROTAS_UPLOAD_PARECER:
        return settings.CORPO_MAX_PARECER_MB * _MiB
    return settings.CORPO_MAX_MB * _MiB


def _detalhe_corpo_grande(scope) -> str:
    """Uma frase, e não um código: é o que os ecrãs mostram tal como vem. Sai na
    língua do pedido, como as mensagens do tratador de erros."""
    cabecalhos = dict(scope.get("headers") or [])
    locale = parse_accept_language(cabecalhos.get(b"accept-language", b"").decode("latin-1"))
    return traduzir_mensagem("O pedido é demasiado grande.", locale)


def teto_do_json(caminho: str) -> int:
    """Máximo, em bytes, de um corpo JSON para `caminho`. As rotas de autenticação
    são anónimas e esperam pela vaga do argon2 com o corpo já analisado em
    memória: o teto delas é o mais baixo."""
    if caminho.startswith("/api/auth/"):
        return settings.CORPO_MAX_JSON_AUTH_KB * 1024
    return settings.CORPO_MAX_JSON_KB * 1024


from app.shared.guarda_json import GuardaJson  # noqa: E402
from app.shared.dependencies import access_token_so_expirado, payload_de_access_token  # noqa: E402
from app.shared.multipart import GuardaMultipart  # noqa: E402


def _detalhe_sem_sessao(scope, motivo: str) -> str:
    cabecalhos = dict(scope.get("headers") or [])
    locale = parse_accept_language(cabecalhos.get(b"accept-language", b"").decode("latin-1"))
    return traduzir_mensagem(motivo, locale)


# Um upload (multipart) sem sessão válida é recusado antes de o corpo ser guardado:
# o FastAPI escreveria o ficheiro num temporário em disco antes da autenticação.
# Até ao teto geral (o que o nginx já tem inteiro), e sempre para uma sessão só
# expirada, o corpo lê-se e deita-se fora: o nginx devolve o 401 e o browser renova.
app.add_middleware(
    GuardaMultipart,
    validar=payload_de_access_token,
    detalhe=_detalhe_sem_sessao,
    drenar_ate=settings.CORPO_MAX_MB * _MiB,
    so_expirado=access_token_so_expirado,
)

# Acrescentada antes do teto geral, fica por dentro dele: o `LimiteDeCorpo`
# continua a ser o primeiro a ver o pedido.
app.add_middleware(
    GuardaJson,
    teto=teto_do_json,
    estrutura_max=settings.CORPO_MAX_JSON_ESTRUTURA,
    detalhe=_detalhe_corpo_grande,
)
app.add_middleware(LimiteDeCorpo, teto=teto_do_corpo, detalhe=_detalhe_corpo_grande)

# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------

from app.auth.router import router as auth_router  # noqa: E402
from app.audit_logs.router import router as audit_logs_router  # noqa: E402
from app.controlos.router import router as controlos_router  # noqa: E402
from app.empresas.router import router as empresas_router  # noqa: E402
from app.evidencias.router import router as evidencias_router  # noqa: E402
from app.utilizadores.router import router as utilizadores_router  # noqa: E402
from app.relatorios.router import router as relatorios_router  # noqa: E402
from app.notificacoes.router import router as notificacoes_router  # noqa: E402
from app.incidentes.router import router as incidentes_router  # noqa: E402
from app.tarefas.router import router as tarefas_router  # noqa: E402
from app.formacao.router import router as formacao_router  # noqa: E402
from app.setup.router import router as setup_router  # noqa: E402
from app.plano_prioritario.router import router as plano_prioritario_router  # noqa: E402
from app.politica.router import router as politica_router  # noqa: E402
from app.documentos.router import router as documentos_router  # noqa: E402
from app.dossie.router import router as dossie_router  # noqa: E402
from app.dossie.router import router_selo as dossie_selo_router  # noqa: E402
from app.updates.router import router as updates_router  # noqa: E402
from app.pesquisa.router import router as pesquisa_router  # noqa: E402
from app.premium.router import router as premium_router  # noqa: E402
from app.premium.analise_router import router as premium_analise_router  # noqa: E402
from app.premium.inventario_router import router as premium_inventario_router  # noqa: E402
from app.premium.risco_router import router as premium_risco_router  # noqa: E402
from app.premium.fornecedor_router import router as premium_fornecedor_router  # noqa: E402
from app.premium.conetor_router import router as premium_conetor_router  # noqa: E402
from app.premium.conetor_router import router_remocao as premium_conetor_remocao_router  # noqa: E402
from app.premium.verificacoes_router import router as premium_verificacoes_router  # noqa: E402
from app.premium.importacao_router import router as premium_importacao_router  # noqa: E402

app.include_router(auth_router, prefix="/api")
app.include_router(audit_logs_router, prefix="/api")
app.include_router(controlos_router, prefix="/api")
app.include_router(empresas_router, prefix="/api")
app.include_router(evidencias_router, prefix="/api")
app.include_router(utilizadores_router, prefix="/api")
app.include_router(relatorios_router, prefix="/api")
app.include_router(notificacoes_router, prefix="/api")
app.include_router(incidentes_router, prefix="/api")
app.include_router(tarefas_router, prefix="/api")
app.include_router(formacao_router, prefix="/api")
app.include_router(setup_router, prefix="/api")
app.include_router(plano_prioritario_router, prefix="/api")
app.include_router(politica_router, prefix="/api")
app.include_router(documentos_router, prefix="/api")
app.include_router(dossie_router, prefix="/api")
app.include_router(dossie_selo_router, prefix="/api")
app.include_router(updates_router, prefix="/api")
app.include_router(pesquisa_router, prefix="/api")
app.include_router(premium_router, prefix="/api")
app.include_router(premium_analise_router, prefix="/api")
app.include_router(premium_inventario_router, prefix="/api")
app.include_router(premium_risco_router, prefix="/api")
app.include_router(premium_fornecedor_router, prefix="/api")
app.include_router(premium_conetor_router, prefix="/api")
app.include_router(premium_conetor_remocao_router, prefix="/api")
app.include_router(premium_verificacoes_router, prefix="/api")
app.include_router(premium_importacao_router, prefix="/api")

# Saúde do sistema e backups: operação da instalação — só fazem sentido (e só
# são montados) em on-prem; no SaaS a operação é do operador da plataforma.
if settings.DEPLOYMENT_MODE == "onprem":
    from app.sistema.router import router as sistema_router  # noqa: E402
    from app.backup.router import router as backup_router  # noqa: E402
    app.include_router(sistema_router, prefix="/api")
    app.include_router(backup_router, prefix="/api")

# Router interno de gestão privilegiada de tenants (suspender trials). Mecanismo
# máquina-a-máquina: montado SÓ em saas e com token presente. Em on-prem nem existe —
# não há rota nem caminho de código para o atingir.
if settings.DEPLOYMENT_MODE == "saas" and settings.CORE_SUSPEND_TOKEN:
    from app.internal_admin.router import router as internal_admin_router  # noqa: E402

    app.include_router(internal_admin_router)
    logger.info("Router interno de gestão de tenants montado (saas + token).")

# ---------------------------------------------------------------------------
# Healthcheck público
# ---------------------------------------------------------------------------

@app.get("/api/health", tags=["Sistema"], include_in_schema=False)
async def healthcheck():
    """Endpoint de healthcheck para Cloudflare / Docker compose."""
    return {"status": "ok", "app": settings.APP_NAME}


# ---------------------------------------------------------------------------
# Handler global de erros não tratados
# ---------------------------------------------------------------------------

def _campo_acima_do_teto(error: dict) -> dict | None:
    """O campo do pedido que passou do teto, sem nunca devolver o valor enviado.

    Um texto acima do teto (`string_too_long`) diz `{campo, max}` em caracteres;
    uma lista ou um dicionário com entradas a mais (`too_long`) diz também
    `unidade: "itens"`. O campo é sempre o de topo do corpo: um texto dentro dos
    atributos de um ativo é o campo `atributos`, e dentro de uma lista o da lista.
    """
    loc = error["loc"]
    max_length = (error.get("ctx") or {}).get("max_length")
    if len(loc) < 2 or not isinstance(max_length, int):
        return None
    if error["type"] == "string_too_long":
        return {"campo": str(loc[1]), "max": max_length}
    if error["type"] == "too_long":
        return {"campo": str(loc[1]), "max": max_length, "unidade": "itens"}
    return None


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """
    Formata erros do Pydantic (422) num array de dicionários legíveis 
    ou numa string amigável para o frontend, melhorando a UX.
    Em vez de [{loc: ..., msg: ..., type: ...}], devolvemos um detalhe limpo.
    """
    erros = []
    # Os textos e as listas acima do teto vão também por campo, para o ecrã dizer qual encurtar:
    # o `detail` é uma frase, e dela já não se tira o campo.
    campos_acima_do_teto = []
    for error in exc.errors():
        mensagem = error["msg"]

        acima = _campo_acima_do_teto(error)
        if acima:
            campos_acima_do_teto.append(acima)

        # Remover o clássico prefixo "Value error, " ou "Assertion failed, "
        if mensagem.startswith("Value error, "):
            mensagem = mensagem.replace("Value error, ", "", 1)
        elif mensagem.startswith("Assertion failed, "):
            mensagem = mensagem.replace("Assertion failed, ", "", 1)
        
        if error["type"] == "missing":
            campo = str(error["loc"][-1]) if error["loc"] else "Desconhecido"
            mensagem = f"O campo '{campo}' é obrigatório."
        
        # Capitalizar 1ª letra
        if mensagem and len(mensagem) > 0:
            mensagem = mensagem[0].upper() + mensagem[1:]
            
        if mensagem not in erros:
            erros.append(mensagem)
    
    # Se houver apenas um erro, envia logo a string. Se vários, junta todos com separador
    detail_msg = " | ".join(erros) if erros else "Erro de validação (dados inválidos)."

    conteudo: dict = {"detail": traduzir_mensagem(detail_msg, _locale_do_pedido(request))}
    if campos_acima_do_teto:
        conteudo["campos_acima_do_teto"] = campos_acima_do_teto
    return JSONResponse(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, content=conteudo)


def _locale_do_pedido(request: Request) -> str | None:
    return parse_accept_language(request.headers.get("accept-language"))


@app.exception_handler(StarletteHTTPException)
async def handler_http_exception(request: Request, exc: StarletteHTTPException):
    """As mensagens nascem em português nos serviços; quem usa a aplicação em
    inglês recebe-as traduzidas à saída, pelo Accept-Language do pedido.
    Os pormenores de um 409 (o impacto de um apagamento) levam datas: saem
    com fuso, como as respostas normais."""
    return UtcJSONResponse(
        status_code=exc.status_code,
        content={"detail": traduzir_mensagem(exc.detail, _locale_do_pedido(request))},
        headers=getattr(exc, "headers", None),
    )


@app.exception_handler(Exception)
async def handler_erro_generico(request: Request, exc: Exception):
    # Em produção evita expor stack traces nos logs externos (CWE-209).
    # Em DEBUG mantém o exception completo para facilitar desenvolvimento.
    if settings.DEBUG:
        logger.exception("Erro não tratado: %s", exc)
    else:
        logger.error("Erro não tratado [%s]: %s", type(exc).__name__, str(exc)[:200])
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "detail": traduzir_mensagem(
                "Erro interno do servidor. Tente novamente mais tarde.", _locale_do_pedido(request)
            )
        },
    )


# ---------------------------------------------------------------------------
# Nenhuma rota da API sem quem a autorize
# ---------------------------------------------------------------------------
# A seguir a TODAS as rotas montadas — incluindo as condicionais ao modo de
# implantação e o healthcheck. Recusa o arranque se alguma rota da API não
# declarar autorização e não estiver na lista das deliberadamente abertas, pela
# mesma razão que a configuração recusa arrancar com segredos por preencher.
from app.shared.gates import verificar_gates  # noqa: E402

verificar_gates(app)
