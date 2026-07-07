"""
PremiumClient — cliente fino (no core, ABERTO) para o contrato premium.v1.

O core fala SEMPRE com o sidecar premium através deste cliente. O transporte é
plugável e escolhido por configuração:

  - NullTransport  (default): premium DESLIGADO. Toda a feature = não-autorizada.
                   Mantém o open-core funcional sem qualquer sidecar a correr.
  - GrpcTransport  (PREMIUM_ENABLED=true): liga ao sidecar via gRPC (premium.v1).

A AUTORIDADE dos direitos é sempre do sidecar; e o sidecar é DONO do ciclo de vida
do job de análise IA. Este cliente apenas pergunta o entitlement (com cache curta),
submete o contexto e lê o estado do job.
"""
from __future__ import annotations

import json
import threading
import time
from functools import lru_cache

from app.config import get_settings
from app.premium.schemas import Entitlement

# Janela do rate-limit do gateway → sufixo curto que o frontend traduz (janela.*).
_JANELA_CURTA = {"req_hora": "hora", "req_dia": "dia", "req_mes": "mes"}


class AnaliseLimiteError(RuntimeError):
    """Limite atingido (rate-limit por janela/metering do gateway, ou já-em-curso).

    Carrega o `detalhe` já pronto para o corpo do 402/429 (o frontend traduz por
    i18n a partir de `detalhe["codigo"]`).
    """

    def __init__(self, detalhe: dict) -> None:
        super().__init__(detalhe.get("codigo", "limite"))
        self.detalhe = detalhe


def _detalhe_429(details: str) -> dict:
    """Mapeia o corpo de um RESOURCE_EXHAUSTED do sidecar para o detalhe do 429.

    Aceita:
      - `{"detail": {"codigo": ...}}`  → já final (ex.: "limite_em_curso"); passa.
      - `{"detail": {"janela"/"reason"/"limite"/"reset_em"}}` → rate-limit do gateway.
      - `{"detail": "<texto>"}` ou parse falhado → limite genérico.
    """
    try:
        corpo = json.loads(details)
    except (ValueError, TypeError):
        return {"codigo": "limite"}

    det = corpo.get("detail") if isinstance(corpo, dict) else corpo
    if isinstance(det, dict):
        if "codigo" in det:
            return det
        janela = det.get("janela")
        if janela:
            return {
                "codigo": "limite_janela",
                "janela": _JANELA_CURTA.get(janela, janela),
                "limite": det.get("limite"),
                "reset_em": det.get("reset_em"),
            }
        if det.get("reason") == "limit_reached":
            return {"codigo": "limite_tokens"}
    return {"codigo": "limite"}


# Estado do job (enum gerado: PENDENTE=0, PROCESSANDO=1, CONCLUIDO=2, ERRO=3).
_ESTADO_MAP = {0: "pendente", 1: "processando", 2: "concluido", 3: "erro"}


def _job_pb_to_dict(pb) -> dict | None:
    """Converte o AnaliseJob (protobuf) num dict simples. `job_id` vazio = sem job."""
    if not pb.job_id:
        return None
    estado = _ESTADO_MAP.get(pb.estado, "erro")
    relatorio = None
    if pb.estado == 2:  # CONCLUIDO
        r = pb.relatorio
        relatorio = {
            "resumo_executivo": r.resumo_executivo,
            "pontos_positivos": list(r.pontos_positivos),
            "lacunas_identificadas": list(r.lacunas_identificadas),
            "recomendacoes": list(r.recomendacoes),
            "score_qualidade_documentacao": r.score_qualidade_documentacao,
            "score_robustez_implementacao": r.score_robustez_implementacao,
            "nivel_confianca": r.nivel_confianca,
            "gerado_em": r.gerado_em,
        }
    return {
        "job_id": pb.job_id,
        "controlo_empresa_id": pb.controlo_empresa_id,
        "estado": estado,
        "relatorio": relatorio,
        "erro_codigo": pb.erro_codigo or None,
        "erro_categoria": pb.erro_categoria or None,
        "created_at": pb.created_at,
        "updated_at": pb.updated_at,
        "auditoria_pendente": pb.auditoria_pendente,
    }


class PremiumTransport:
    """Interface de transporte do contrato premium.v1."""

    def check_entitlement(self, tenant_id: str, feature: str) -> Entitlement:
        raise NotImplementedError

    def criar_analise_gaps(self, meta: dict, evidencias: bytes) -> dict:
        """Submete uma análise IA (client-streaming) e devolve o job (dict)."""
        raise NotImplementedError

    def obter_analise_por_controlo(
        self, tenant_id: str, controlo_empresa_id: str, reclamar_auditoria: bool
    ) -> dict | None:
        """Estado/resultado do job mais recente de um controlo (ou None se não há)."""
        raise NotImplementedError


class NullTransport(PremiumTransport):
    """Premium desligado — tudo não-autorizado. É o default do open-core."""

    def check_entitlement(self, tenant_id: str, feature: str) -> Entitlement:
        return Entitlement.disabled(feature, reason="premium_disabled")

    def criar_analise_gaps(self, meta: dict, evidencias: bytes) -> dict:
        raise RuntimeError("Premium desligado — análise IA indisponível.")

    def obter_analise_por_controlo(
        self, tenant_id: str, controlo_empresa_id: str, reclamar_auditoria: bool
    ) -> dict | None:
        raise RuntimeError("Premium desligado — análise IA indisponível.")


class GrpcTransport(PremiumTransport):
    """
    Liga ao sidecar premium via gRPC (premium.v1).

    Requer `grpcio` instalado e os stubs gerados a partir de
    app/premium/proto/premium.proto (`premium_pb2` / `premium_pb2_grpc`).

    Transporte: canal mTLS com pinning da CA quando PREMIUM_TLS_CA/CLIENT_CERT/
    CLIENT_KEY estão definidos (ver _criar_canal); senão canal inseguro (dev).
    """

    def __init__(self, addr: str) -> None:
        self._addr = addr
        self._lock = threading.Lock()
        self._stub = None

    def _ensure_stub(self):
        if self._stub is not None:
            return self._stub
        with self._lock:
            if self._stub is None:
                try:
                    import grpc  # type: ignore
                    from app.premium.proto import premium_pb2_grpc  # type: ignore
                except ImportError as exc:
                    raise RuntimeError(
                        "PREMIUM_ENABLED=true mas o transporte gRPC não está disponível. "
                        "Instalar `grpcio` e gerar os stubs de premium.proto."
                    ) from exc
                channel = self._criar_canal(grpc)
                self._stub = premium_pb2_grpc.PremiumProviderStub(channel)
        return self._stub

    def _criar_canal(self, grpc):
        """Canal mTLS para o sidecar se os certificados estiverem configurados; senão
        canal inseguro (dev). O core apresenta o seu cert de cliente e verifica o cert
        do sidecar contra a CA dada (pinning). `PREMIUM_TLS_SERVER_NAME` alinha o nome
        verificado (SAN do sidecar) com o host do endereço."""
        import os

        ca = os.getenv("PREMIUM_TLS_CA")
        cert = os.getenv("PREMIUM_TLS_CLIENT_CERT")
        key = os.getenv("PREMIUM_TLS_CLIENT_KEY")
        if ca and cert and key:
            creds = grpc.ssl_channel_credentials(
                root_certificates=open(ca, "rb").read(),
                private_key=open(key, "rb").read(),
                certificate_chain=open(cert, "rb").read(),
            )
            options = []
            server_name = os.getenv("PREMIUM_TLS_SERVER_NAME")
            if server_name:
                options.append(("grpc.ssl_target_name_override", server_name))
            return grpc.secure_channel(self._addr, creds, options=options)
        return grpc.insecure_channel(self._addr)  # dev sem mTLS

    def check_entitlement(self, tenant_id: str, feature: str) -> Entitlement:
        from app.premium.proto import premium_pb2  # type: ignore

        stub = self._ensure_stub()
        resp = stub.CheckEntitlement(
            premium_pb2.EntitlementQuery(tenant_id=tenant_id, feature=feature)
        )
        return Entitlement(
            feature=feature,
            enabled=resp.enabled,
            limits=dict(resp.limits),
            expires_at=resp.expires_at or None,
            reason=resp.reason,
        )

    # --- Assistente IA (o sidecar é dono do job) ---

    # Chunk do payload de evidências (256 KiB) — contorna o limite de 4 MB do gRPC.
    _CHUNK_BYTES = 256 * 1024

    def criar_analise_gaps(self, meta: dict, evidencias: bytes) -> dict:
        import grpc  # type: ignore
        from app.premium.proto import premium_pb2  # type: ignore

        stub = self._ensure_stub()

        def _gen():
            # 1ª mensagem: metadata. Seguintes: chunks de evidências (cifradas).
            yield premium_pb2.ChunkContexto(
                meta=premium_pb2.MetaContexto(
                    tenant_id=meta["tenant_id"],
                    controlo_empresa_id=meta["controlo_empresa_id"],
                    framework_id=meta["framework_id"],
                    controlo_codigo=meta["controlo_codigo"],
                    nivel_minimo=meta["nivel_minimo"],
                    locale=meta["locale"],
                    idempotency_key=meta.get("idempotency_key", ""),
                )
            )
            for i in range(0, len(evidencias), self._CHUNK_BYTES):
                yield premium_pb2.ChunkContexto(
                    evidencia=evidencias[i : i + self._CHUNK_BYTES]
                )

        try:
            resp = stub.CriarAnaliseGaps(_gen())
        except grpc.RpcError as exc:
            if exc.code() == grpc.StatusCode.RESOURCE_EXHAUSTED:
                raise AnaliseLimiteError(_detalhe_429(exc.details() or "")) from exc
            raise
        job = _job_pb_to_dict(resp)
        if job is None:
            raise RuntimeError("sidecar devolveu job vazio na submissão")
        return job

    def obter_analise_por_controlo(
        self, tenant_id: str, controlo_empresa_id: str, reclamar_auditoria: bool
    ) -> dict | None:
        from app.premium.proto import premium_pb2  # type: ignore

        stub = self._ensure_stub()
        resp = stub.ObterAnalisePorControlo(
            premium_pb2.AnaliseControloRef(
                tenant_id=tenant_id,
                controlo_empresa_id=controlo_empresa_id,
                reclamar_auditoria=reclamar_auditoria,
            )
        )
        return _job_pb_to_dict(resp)


class PremiumClient:
    """
    Fachada do core para o premium. Resolve o transporte por config e faz cache
    curta (TTL) das verificações de entitlement.
    """

    def __init__(self, transport: PremiumTransport, cache_ttl: int) -> None:
        self._transport = transport
        self._cache_ttl = max(0, cache_ttl)
        self._cache: dict[tuple[str, str], tuple[float, Entitlement]] = {}
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        """True se há um transporte real (sidecar configurado)."""
        return not isinstance(self._transport, NullTransport)

    def check_entitlement(self, tenant_id: str, feature: str) -> Entitlement:
        key = (tenant_id, feature)
        now = time.monotonic()
        if self._cache_ttl:
            with self._lock:
                hit = self._cache.get(key)
                if hit and hit[0] > now:
                    return hit[1]
        ent = self._transport.check_entitlement(tenant_id, feature)
        if self._cache_ttl:
            with self._lock:
                self._cache[key] = (now + self._cache_ttl, ent)
        return ent

    def has_feature(self, tenant_id: str, feature: str) -> bool:
        return self.check_entitlement(tenant_id, feature).enabled

    # --- Assistente IA — pass-through ao transporte (sem cache; é estado de job) ---

    def criar_analise_gaps(self, meta: dict, evidencias: bytes) -> dict:
        return self._transport.criar_analise_gaps(meta, evidencias)

    def obter_analise_por_controlo(
        self, tenant_id: str, controlo_empresa_id: str, reclamar_auditoria: bool = False
    ) -> dict | None:
        return self._transport.obter_analise_por_controlo(
            tenant_id, controlo_empresa_id, reclamar_auditoria
        )


def _build_transport() -> PremiumTransport:
    settings = get_settings()
    if not settings.PREMIUM_ENABLED:
        return NullTransport()
    if not settings.PREMIUM_SIDECAR_ADDR:
        raise RuntimeError(
            "PREMIUM_ENABLED=true mas PREMIUM_SIDECAR_ADDR está vazio. "
            "Definir o endereço do sidecar premium (ex.: premium-sidecar:50051)."
        )
    return GrpcTransport(settings.PREMIUM_SIDECAR_ADDR)


@lru_cache
def get_premium_client() -> PremiumClient:
    """Singleton do PremiumClient. Usar como dependência FastAPI: Depends(get_premium_client)."""
    settings = get_settings()
    return PremiumClient(_build_transport(), settings.PREMIUM_ENTITLEMENT_CACHE_TTL)
