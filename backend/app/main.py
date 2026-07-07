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
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from app.config import get_settings
from app.database import engine
from app.shared.utils import host_e_dominio, obter_ip_cliente, pedido_e_seguro

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

limiter = Limiter(key_func=obter_ip_cliente, default_limits=["200/minute"])


# ---------------------------------------------------------------------------
# Lifespan (startup / shutdown)
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Seed automático do framework (carrega framework se não existir na DB).
    # Necessário em saas/saas-trial também: não há wizard de setup nesses modos
    # para o fazer manualmente, e o FRAMEWORKS_DIR vem sempre cozido na imagem.
    from sqlmodel import Session
    from app.setup.seed_framework import seed_framework_se_necessario
    with Session(engine) as _seed_db:
        seed_framework_se_necessario(_seed_db)

    if settings.DEPLOYMENT_MODE == "onprem":
        # Verificação de atualizações: arranque + cada 24h (respeita VERIFY_UPDATES).
        import asyncio
        from app.updates.service import verificar_updates_sync

        async def _loop_updates():
            while True:
                try:
                    await asyncio.to_thread(verificar_updates_sync)
                except Exception:  # noqa: BLE001
                    pass
                await asyncio.sleep(24 * 3600)

        asyncio.create_task(_loop_updates())

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
    title=settings.APP_NAME,
    version="0.2.0",
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
from app.setup.router import router as setup_router  # noqa: E402
from app.plano_prioritario.router import router as plano_prioritario_router  # noqa: E402
from app.documentos.router import router as documentos_router  # noqa: E402
from app.updates.router import router as updates_router  # noqa: E402
from app.premium.router import router as premium_router  # noqa: E402
from app.premium.analise_router import router as premium_analise_router  # noqa: E402

app.include_router(auth_router, prefix="/api")
app.include_router(audit_logs_router, prefix="/api")
app.include_router(controlos_router, prefix="/api")
app.include_router(empresas_router, prefix="/api")
app.include_router(evidencias_router, prefix="/api")
app.include_router(utilizadores_router, prefix="/api")
app.include_router(relatorios_router, prefix="/api")
app.include_router(notificacoes_router, prefix="/api")
app.include_router(setup_router, prefix="/api")
app.include_router(plano_prioritario_router, prefix="/api")
app.include_router(documentos_router, prefix="/api")
app.include_router(updates_router, prefix="/api")
app.include_router(premium_router, prefix="/api")
app.include_router(premium_analise_router, prefix="/api")

# Router interno de gestão privilegiada de tenants (suspender/reativar). Mecanismo
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

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """
    Formata erros do Pydantic (422) num array de dicionários legíveis 
    ou numa string amigável para o frontend, melhorando a UX.
    Em vez de [{loc: ..., msg: ..., type: ...}], devolvemos um detalhe limpo.
    """
    erros = []
    for error in exc.errors():
        mensagem = error["msg"]
        
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
    
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": detail_msg},
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
        content={"detail": "Erro interno do servidor. Tente novamente mais tarde."},
    )
