"""
Cliente gRPC do módulo Inventário de Ativos (premium.v1 / InventarioService).

Isolado do cliente da IA para ser fácil de manter, mas reutiliza o MESMO canal
mTLS ao sidecar (`criar_canal_sidecar`). O core é passthrough: valida a forma
(schemas do router), delega no sidecar (que é a autoridade — valida os atributos
contra o catálogo, faz o tenant-scoping e é dono da premium-data-db) e converte
entre protobuf e dicts. Nenhuma lógica de domínio vive aqui.
"""
from __future__ import annotations

from functools import lru_cache

from app.config import get_settings
from app.premium.client import ClienteSidecar
from app.premium.conversao import ator_pb, documento_to_dict


# ── Conversões protobuf → dict (o router devolve dicts; o frontend consome JSON) ──


def _opcao_to_dict(pb) -> dict:
    return {"valor": pb.valor, "label": pb.label}


def _campo_to_dict(pb) -> dict:
    return {
        "chave": pb.chave,
        "tipo_dado": pb.tipo_dado,
        "label": pb.label,
        "ajuda": pb.ajuda,
        "opcoes": [_opcao_to_dict(o) for o in pb.opcoes],
    }


def _tipo_to_dict(pb) -> dict:
    return {
        "chave": pb.chave,
        "icone": pb.icone,
        "nome": pb.nome,
        "descricao": pb.descricao,
        "campos": [_campo_to_dict(c) for c in pb.campos],
    }


def _ativo_to_dict(pb) -> dict:
    return {
        "id": pb.id,
        "tipo": pb.tipo,
        "nome": pb.nome,
        "descricao": pb.descricao,
        "responsavel_id": pb.responsavel_id,
        "responsavel_nome": pb.responsavel_nome,
        "localizacao": pb.localizacao,
        "estado": pb.estado,
        "confidencialidade": pb.confidencialidade,
        "integridade": pb.integridade,
        "disponibilidade": pb.disponibilidade,
        "valor_negocio": pb.valor_negocio,
        "criticidade": pb.criticidade,
        "criticidade_origem": pb.criticidade_origem,
        "criticidade_explicacao": pb.criticidade_explicacao,
        "criticidade_justificacao": pb.criticidade_justificacao,
        "atributos": dict(pb.atributos),
        "depende_de": list(pb.depende_de),
        "controlos": list(pb.controlos),
        "ultima_revisao": pb.ultima_revisao or None,
        "created_at": pb.created_at,
        "updated_at": pb.updated_at,
        # Sanitização no abate (ID.GA-8) — só preenchido no ObterAtivo.
        "sanitizacao_metodo": pb.sanitizacao_metodo or None,
        "sanitizacao_data": pb.sanitizacao_data or None,
        "sanitizacao_responsavel_nome": pb.sanitizacao_responsavel_nome or None,
        "sanitizacao_nota": pb.sanitizacao_nota or None,
        # Procedência: de onde veio a linha e quando entrou pela última vez. É o
        # que distingue "alguém escreveu isto aqui" de "o sistema do cliente diz
        # isto" — a diferença que o auditor procura.
        "origem": pb.origem or "manual",
        "fonte_tipo": pb.fonte_tipo or None,
        "sincronizado_em": pb.sincronizado_em or None,
        "estado_origem": pb.estado_origem or "presente",
        "tipo_por_confirmar": pb.tipo_por_confirmar,
    }


def _painel_to_dict(pb) -> dict:
    return {
        "total": pb.total,
        "criticos": pb.criticos,
        "por_rever": pb.por_rever,
        "alertas": pb.alertas,
        "por_tipo": [
            {"tipo": c.tipo, "label": c.label, "total": c.total} for c in pb.por_tipo
        ],
    }


class InventarioClient(ClienteSidecar):
    """Fala com o InventarioService do sidecar. Stub criado de forma lazy."""

    _NOME_STUB = "InventarioServiceStub"

    def listar_tipos(self, tenant_id: str, locale: str) -> list[dict]:
        from app.premium.proto import premium_pb2  # type: ignore

        resp = self._ensure_stub().ListarTiposAtivo(
            premium_pb2.TiposAtivoReq(tenant_id=tenant_id, locale=locale)
        )
        return [_tipo_to_dict(t) for t in resp.tipos]

    def listar_ativos(
        self, tenant_id: str, tipo: str, locale: str, limite: int, offset: int
    ) -> dict:
        from app.premium.proto import premium_pb2  # type: ignore

        resp = self._ensure_stub().ListarAtivos(
            premium_pb2.ListarAtivosReq(
                tenant_id=tenant_id, tipo=tipo, locale=locale, limite=limite, offset=offset
            )
        )
        return {"ativos": [_ativo_to_dict(a) for a in resp.ativos], "total": resp.total}

    def obter_ativo(self, tenant_id: str, ativo_id: str, locale: str) -> dict:
        from app.premium.proto import premium_pb2  # type: ignore

        resp = self._ensure_stub().ObterAtivo(
            premium_pb2.AtivoRef(tenant_id=tenant_id, id=ativo_id, locale=locale)
        )
        return _ativo_to_dict(resp)

    def guardar_ativo(
        self, tenant_id: str, dados: dict, ativo_id: str = "", ator: dict | None = None
    ) -> dict:
        from app.premium.proto import premium_pb2  # type: ignore

        pb = premium_pb2.Ativo(
            id=ativo_id,
            tenant_id=tenant_id,
            tipo=dados.get("tipo", ""),
            nome=dados.get("nome", ""),
            descricao=dados.get("descricao", ""),
            responsavel_id=dados.get("responsavel_id", ""),
            responsavel_nome=dados.get("responsavel_nome", ""),
            localizacao=dados.get("localizacao", ""),
            estado=dados.get("estado", "em_uso"),
            atributos=dados.get("atributos") or {},
            ator=ator_pb(premium_pb2, ator),
        )
        return _ativo_to_dict(self._ensure_stub().GuardarAtivo(pb))

    def eliminar_ativo(
        self, tenant_id: str, ativo_id: str, ator: dict | None = None
    ) -> None:
        from app.premium.proto import premium_pb2  # type: ignore

        self._ensure_stub().EliminarAtivo(
            premium_pb2.AtivoRef(
                tenant_id=tenant_id, id=ativo_id, ator=ator_pb(premium_pb2, ator)
            )
        )

    def classificar_criticidade(
        self, tenant_id: str, ativo_id: str, dados: dict, locale: str, ator: dict | None = None
    ) -> dict:
        from app.premium.proto import premium_pb2  # type: ignore

        pb = premium_pb2.ClassificarReq(
            tenant_id=tenant_id,
            ativo_id=ativo_id,
            locale=locale,
            modo=dados.get("modo", ""),
            confidencialidade=int(dados.get("confidencialidade", 0)),
            integridade=int(dados.get("integridade", 0)),
            disponibilidade=int(dados.get("disponibilidade", 0)),
            valor_negocio=int(dados.get("valor_negocio", 0)),
            criticidade_manual=dados.get("criticidade_manual", ""),
            justificacao=dados.get("justificacao", ""),
            ator=ator_pb(premium_pb2, ator),
        )
        return _ativo_to_dict(self._ensure_stub().ClassificarCriticidade(pb))

    def definir_dependencias(
        self, tenant_id: str, ativo_id: str, depende_de: list[str], ator: dict | None = None
    ) -> None:
        from app.premium.proto import premium_pb2  # type: ignore

        self._ensure_stub().DefinirDependencias(
            premium_pb2.DependenciasReq(
                tenant_id=tenant_id,
                ativo_id=ativo_id,
                depende_de=depende_de,
                ator=ator_pb(premium_pb2, ator),
            )
        )

    def registar_revisao(
        self,
        tenant_id: str,
        ativo_ids: list[str],
        por_id: str,
        por_nome: str,
        ator: dict | None = None,
    ) -> None:
        from app.premium.proto import premium_pb2  # type: ignore

        self._ensure_stub().RegistarRevisao(
            premium_pb2.RevisaoReq(
                tenant_id=tenant_id,
                ativo_ids=ativo_ids,
                revisto_por_id=por_id,
                revisto_por_nome=por_nome,
                ator=ator_pb(premium_pb2, ator),
            )
        )

    def registar_sanitizacao(
        self,
        tenant_id: str,
        ativo_id: str,
        metodo: str,
        por_id: str,
        por_nome: str,
        data: str = "",
        nota: str = "",
        ator: dict | None = None,
    ) -> None:
        from app.premium.proto import premium_pb2  # type: ignore

        self._ensure_stub().RegistarSanitizacao(
            premium_pb2.SanitizacaoReq(
                tenant_id=tenant_id,
                ativo_id=ativo_id,
                metodo=metodo,
                responsavel_id=por_id,
                responsavel_nome=por_nome,
                data=data,
                nota=nota,
                ator=ator_pb(premium_pb2, ator),
            )
        )

    def obter_painel(self, tenant_id: str, locale: str) -> dict:
        from app.premium.proto import premium_pb2  # type: ignore

        resp = self._ensure_stub().ObterPainel(
            premium_pb2.PainelReq(tenant_id=tenant_id, locale=locale)
        )
        return _painel_to_dict(resp)

    def gerar_documento(self, tenant_id: str, tipo: str, locale: str) -> dict:
        from app.premium.proto import premium_pb2  # type: ignore

        resp = self._ensure_stub().GerarDocumento(
            premium_pb2.DocumentoReq(tenant_id=tenant_id, tipo=tipo, locale=locale)
        )
        return documento_to_dict(resp)


@lru_cache
def get_inventario_client() -> InventarioClient | None:
    """Singleton do cliente de inventário, ou None se o premium estiver desligado.
    (O gate real é o `require_feature`; None só acontece com premium off.)"""
    settings = get_settings()
    if not settings.PREMIUM_ENABLED or not settings.PREMIUM_SIDECAR_ADDR:
        return None
    return InventarioClient(settings.PREMIUM_SIDECAR_ADDR)
