"""
Pedido dos módulos premium pelo interface (on-prem): o cartão de Definições →
Sistema. Só o administrador pede, cancela e ativa; a ativação pede a password de
quem a faz, como a atualização da instalação (reinicia o backend).
"""
from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field, field_validator
from slowapi import Limiter

from app.auth.models import RoleUtilizador, Utilizador
from app.config import get_settings
from app.premium import pedido_premium as servico
from app.shared.audit import Acao, ResultadoAcao, registar_acao
from app.shared.dependencies import SessionDep, require_role
from app.shared.utils import obter_chave_limite

router = APIRouter(prefix="/premium/pedido", tags=["Premium"])
_Admin = Depends(require_role(RoleUtilizador.ADMIN))
_limiter = Limiter(key_func=obter_chave_limite)

_EMAIL = re.compile(r"^[^@\s]{1,64}@[A-Za-z0-9.-]{1,185}\.[A-Za-z]{2,24}$")


def _texto(v: str) -> str:
    v = " ".join("".join(c for c in v if c.isprintable()).split())
    if not v:
        raise ValueError("vazio")
    return v


class FaturacaoIn(BaseModel):
    nome: str = Field(max_length=200)
    nif: str = Field(max_length=9)
    morada: str = Field(max_length=500)
    email: str = Field(max_length=254)

    @field_validator("nome", "morada")
    @classmethod
    def _limpo(cls, v: str) -> str:
        return _texto(v)

    @field_validator("nif")
    @classmethod
    def _nif(cls, v: str) -> str:
        if not servico.nif_pt_valido(v):
            raise ValueError("NIF inválido")
        return v

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        if not _EMAIL.match(v.strip()):
            raise ValueError("email inválido")
        return v.strip()


class ContactoIn(BaseModel):
    nome: str = Field(max_length=200)
    email: str = Field(max_length=254)

    @field_validator("nome")
    @classmethod
    def _limpo(cls, v: str) -> str:
        return _texto(v)

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        if not _EMAIL.match(v.strip()):
            raise ValueError("email inválido")
        return v.strip()


class PedirIn(BaseModel):
    plano: str = Field(max_length=32)
    faturacao: FaturacaoIn
    contacto: ContactoIn
    termos_versao: str = Field(max_length=32)
    aceito_termos: bool
    # Substitui outra instalação da mesma empresa que já tinha os módulos premium.
    transferencia: bool = False


class AtivarIn(BaseModel):
    password: str = Field(max_length=256)


def _so_onprem() -> None:
    if get_settings().DEPLOYMENT_MODE != "onprem":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"codigo": "so_onprem"})


def _erro(exc: servico.PedidoErro) -> HTTPException:
    http = {
        "servico_indisponivel": status.HTTP_503_SERVICE_UNAVAILABLE,
        "servico_mal_configurado": status.HTTP_503_SERVICE_UNAVAILABLE,
        "campo_invalido": status.HTTP_422_UNPROCESSABLE_ENTITY,
        "demasiados_pedidos": status.HTTP_429_TOO_MANY_REQUESTS,
    }.get(exc.codigo, status.HTTP_409_CONFLICT)
    detalhe = {"codigo": exc.codigo, **({"campo": exc.campo} if exc.campo else {})}
    return HTTPException(status_code=http, detail=detalhe)


@router.get("", summary="Estado do pedido dos módulos premium")
def ver(db: SessionDep, utilizador: Utilizador = _Admin):
    _so_onprem()
    # O que o agente acabou de fazer conta já (sem ir à rede).
    servico.consultar_e_avisar(db, rede=False)
    return servico.vista(db, utilizador.empresa_id)


@router.post("", status_code=status.HTTP_201_CREATED, summary="Pedir os módulos premium")
@_limiter.limit("5/minute")
def pedir(dados: PedirIn, request: Request, db: SessionDep, utilizador: Utilizador = _Admin):
    _so_onprem()
    if not dados.aceito_termos:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail={"codigo": "termos_por_aceitar"})
    try:
        estado = servico.criar(
            db, utilizador=utilizador, faturacao=dados.faturacao.model_dump(),
            contacto=dados.contacto.model_dump(), plano=dados.plano, termos_versao=dados.termos_versao,
            transferencia=dados.transferencia,
        )
    except servico.PedidoErro as exc:
        raise _erro(exc) from None
    registar_acao(
        db, acao=Acao.SISTEMA_PREMIUM_PEDIDO, empresa_id=utilizador.empresa_id, utilizador_id=utilizador.id,
        dados_novos={"pedido_id": estado["pedido_id"], "plano": dados.plano, "termos_versao": dados.termos_versao,
                     "transferencia": dados.transferencia},
        request=request,
    )
    db.commit()
    return servico.vista(db, utilizador.empresa_id)


@router.delete("", summary="Cancelar o pedido dos módulos premium")
@_limiter.limit("5/minute")
def cancelar(request: Request, db: SessionDep, utilizador: Utilizador = _Admin):
    _so_onprem()
    try:
        fechado = servico.cancelar()
    except servico.PedidoErro as exc:
        raise _erro(exc) from None
    registar_acao(
        db, acao=Acao.SISTEMA_PREMIUM_PEDIDO_CANCELADO, empresa_id=utilizador.empresa_id,
        utilizador_id=utilizador.id, dados_novos={"pedido_id": fechado.get("pedido_id")}, request=request,
    )
    db.commit()
    return servico.vista(db, utilizador.empresa_id)


@router.post("/verificar", summary="Perguntar já ao fornecedor pelo estado do pedido")
@_limiter.limit("6/minute")
def verificar(request: Request, db: SessionDep, utilizador: Utilizador = _Admin):
    _so_onprem()
    try:
        servico.consultar_e_avisar(db)
    except servico.PedidoErro as exc:
        raise _erro(exc) from None
    return servico.vista(db, utilizador.empresa_id)


@router.post("/tentar-de-novo", summary="Repetir a instalação depois de uma falha")
@_limiter.limit("5/minute")
def tentar_de_novo(request: Request, db: SessionDep, utilizador: Utilizador = _Admin):
    _so_onprem()
    try:
        servico.tentar_de_novo()
    except servico.PedidoErro as exc:
        raise _erro(exc) from None
    estado = servico.ler_estado() or {}
    registar_acao(
        db, acao=Acao.SISTEMA_PREMIUM_INSTALACAO, empresa_id=utilizador.empresa_id, utilizador_id=utilizador.id,
        dados_novos={"pedido_id": estado.get("pedido_id")}, request=request,
    )
    db.commit()
    return servico.vista(db, utilizador.empresa_id)


@router.post("/aviso/dispensar", summary="Dispensar o aviso de um pedido fechado")
def dispensar(db: SessionDep, utilizador: Utilizador = _Admin):
    _so_onprem()
    servico.dispensar_aviso()
    return servico.vista(db, utilizador.empresa_id)


@router.post("/ativar", status_code=status.HTTP_202_ACCEPTED, summary="Ativar os módulos premium")
@_limiter.limit("5/minute")
def ativar(dados: AtivarIn, request: Request, db: SessionDep, utilizador: Utilizador = _Admin):
    """Deixa ao agente o pedido de ativação. Exige a password de quem pede: o que
    corre a seguir reinicia a aplicação."""
    from app.auth.service import verify_password

    _so_onprem()
    estado = servico.ler_estado() or {}

    def _recusar(codigo: str, http: int) -> HTTPException:
        registar_acao(
            None, acao=Acao.SISTEMA_PREMIUM_ATIVACAO_PEDIDA, resultado=ResultadoAcao.FALHA,
            empresa_id=utilizador.empresa_id, utilizador_id=utilizador.id,
            dados_novos={"pedido_id": estado.get("pedido_id"), "motivo": codigo}, request=request,
            force_commit=True,
        )
        return HTTPException(status_code=http, detail={"codigo": codigo})

    if not verify_password(dados.password, utilizador.password_hash):
        raise _recusar("password_incorreta", status.HTTP_400_BAD_REQUEST)
    try:
        servico.ativar()
    except servico.PedidoErro as exc:
        raise _recusar(exc.codigo, status.HTTP_409_CONFLICT) from None
    registar_acao(
        db, acao=Acao.SISTEMA_PREMIUM_ATIVACAO_PEDIDA, empresa_id=utilizador.empresa_id,
        utilizador_id=utilizador.id, dados_novos={"pedido_id": estado.get("pedido_id")}, request=request,
    )
    db.commit()
    return servico.vista(db, utilizador.empresa_id)
