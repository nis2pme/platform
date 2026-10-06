"""
Cliente gRPC do módulo Análise de Risco (premium.v1 / RiscoService).

Isolado (fácil de manter), reutiliza o MESMO canal mTLS ao sidecar
(`criar_canal_sidecar`). O core é passthrough: o sidecar é a autoridade (deriva o
nível/classe, valida, faz o tenant-scoping e é dono da premium-data-db). Nenhuma
lógica de domínio aqui.
"""
from __future__ import annotations

from functools import lru_cache

from app.config import get_settings
from app.premium.client import ClienteSidecar
from app.premium.conversao import ator_pb, documento_to_dict


def _mapa_de_maturidades(maturidades: dict | None) -> dict[str, str]:
    """O mapa {controlo → nível de maturidade} como o contrato o leva (texto).

    O sidecar nunca lê a base do núcleo: é por aqui que sabe a maturidade de cada
    controlo para calcular o risco residual.
    """
    return {k: str(v) for k, v in (maturidades or {}).items()}


# ── Conversões protobuf → dict ────────────────────────────────────────────────


def _tratamento_to_dict(pb) -> dict:
    return {
        "id": pb.id,
        "risco_id": pb.risco_id,
        "tipo": pb.tipo,
        "controlo_id": pb.controlo_id,
        "descricao": pb.descricao,
        "estado": pb.estado,
        "prioridade": pb.prioridade,
        "data_alvo": pb.data_alvo or None,
        "responsavel_id": pb.responsavel_id,
        "responsavel_nome": pb.responsavel_nome,
        "created_at": pb.created_at,
    }


def _risco_to_dict(pb) -> dict:
    return {
        "id": pb.id,
        "titulo": pb.titulo,
        "descricao": pb.descricao,
        "ativo_id": pb.ativo_id or None,
        "ativo_nome": pb.ativo_nome or None,
        "ameaca": pb.ameaca,
        "vulnerabilidade": pb.vulnerabilidade,
        "probabilidade": pb.probabilidade,
        "impacto": pb.impacto,
        "nivel": pb.nivel,
        "classe": pb.classe,
        "estado": pb.estado,
        "dono_id": pb.dono_id,
        "dono_nome": pb.dono_nome,
        "justificacao": pb.justificacao,
        "tratamentos": [_tratamento_to_dict(t) for t in pb.tratamentos],
        "created_at": pb.created_at,
        "updated_at": pb.updated_at,
        "cenario_chave": pb.cenario_chave,
        "nivel_residual": pb.nivel_residual,
        "classe_residual": pb.classe_residual,
        # Procedência: distingue um risco escrito aqui de um que veio do Monarc.
        "origem": pb.origem or "manual",
        "fonte_tipo": pb.fonte_tipo or None,
        "sincronizado_em": pb.sincronizado_em or None,
        "estado_origem": pb.estado_origem or "presente",
        # Só a listagem o preenche (a mesma regra do número do painel).
        "por_reavaliar": pb.por_reavaliar,
    }


def _cenario_to_dict(pb) -> dict:
    return {
        "chave": pb.chave,
        "titulo": pb.titulo,
        "porque": pb.porque,
        "ameaca": pb.ameaca,
        "vulnerabilidade": pb.vulnerabilidade,
        # Os códigos do catálogo: o router resolve-os em controlos da empresa e
        # não os devolve.
        "controlos_sugeridos": list(pb.controlos_sugeridos),
    }


def _avaliacao_to_dict(pb) -> dict:
    return {
        "id": pb.id,
        "probabilidade": pb.probabilidade,
        "impacto": pb.impacto,
        "nivel": pb.nivel,
        "classe": pb.classe,
        "justificacao": pb.justificacao,
        "avaliador_nome": pb.avaliador_nome,
        "data": pb.data,
    }


def _painel_to_dict(pb) -> dict:
    return {
        "total": pb.total,
        "altos": pb.altos,
        "em_tratamento": pb.em_tratamento,
        "abertos": pb.abertos,
        "acima_tolerado": pb.acima_tolerado,
        "por_reavaliar": pb.por_reavaliar,
        "acima_tolerado_residual": pb.acima_tolerado_residual,
        "matriz": [
            {"probabilidade": c.probabilidade, "impacto": c.impacto, "total": c.total}
            for c in pb.matriz
        ],
    }


def _definicoes_to_dict(pb) -> dict:
    return {
        "limiar_tratar": pb.limiar_tratar,
        "limiar_urgente": pb.limiar_urgente,
        "aprovador": pb.aprovador,
        "data_aprovacao": pb.data_aprovacao,
        "periodicidade_altos": pb.periodicidade_altos,
        "periodicidade_moderados": pb.periodicidade_moderados,
        "periodicidade_baixos": pb.periodicidade_baixos,
    }


_NUMEROS_DAS_DEFINICOES = (
    "limiar_tratar",
    "limiar_urgente",
    "periodicidade_altos",
    "periodicidade_moderados",
    "periodicidade_baixos",
)


class RiscoClient(ClienteSidecar):
    """Fala com o RiscoService do sidecar. Stub criado de forma lazy."""

    _NOME_STUB = "RiscoServiceStub"

    def listar(
        self,
        tenant_id: str,
        estado: str,
        ativo_id: str,
        limite: int,
        offset: int,
        maturidades: dict | None = None,
    ) -> dict:
        from app.premium.proto import premium_pb2  # type: ignore

        resp = self._ensure_stub().ListarRiscos(
            premium_pb2.ListarRiscosReq(
                tenant_id=tenant_id,
                estado=estado,
                ativo_id=ativo_id,
                limite=limite,
                offset=offset,
                maturidade_controlos=_mapa_de_maturidades(maturidades),
            )
        )
        return {"riscos": [_risco_to_dict(r) for r in resp.riscos], "total": resp.total}

    def obter(self, tenant_id: str, risco_id: str, maturidades: dict | None = None) -> dict:
        from app.premium.proto import premium_pb2  # type: ignore

        resp = self._ensure_stub().ObterRisco(
            premium_pb2.RiscoRef(
                tenant_id=tenant_id,
                id=risco_id,
                maturidade_controlos=_mapa_de_maturidades(maturidades),
            )
        )
        return _risco_to_dict(resp)

    def guardar(
        self, tenant_id: str, dados: dict, risco_id: str = "", ator: dict | None = None
    ) -> dict:
        from app.premium.proto import premium_pb2  # type: ignore

        pb = premium_pb2.Risco(
            id=risco_id,
            tenant_id=tenant_id,
            titulo=dados.get("titulo", ""),
            descricao=dados.get("descricao", ""),
            ativo_id=dados.get("ativo_id") or "",
            ameaca=dados.get("ameaca", ""),
            vulnerabilidade=dados.get("vulnerabilidade", ""),
            probabilidade=int(dados.get("probabilidade", 1)),
            impacto=int(dados.get("impacto", 1)),
            estado=dados.get("estado", "aberto"),
            dono_id=dados.get("dono_id", ""),
            dono_nome=dados.get("dono_nome", ""),
            justificacao=dados.get("justificacao", ""),
            cenario_chave=dados.get("cenario_chave", ""),
            ator=ator_pb(premium_pb2, ator),
        )
        return _risco_to_dict(self._ensure_stub().GuardarRisco(pb))

    def listar_cenarios(self, tenant_id: str, tipo: str, locale: str) -> list[dict]:
        from app.premium.proto import premium_pb2  # type: ignore

        resp = self._ensure_stub().ListarCenarios(
            premium_pb2.CenariosReq(tenant_id=tenant_id, tipo=tipo, locale=locale)
        )
        return [_cenario_to_dict(c) for c in resp.cenarios]

    def eliminar(self, tenant_id: str, risco_id: str, ator: dict | None = None) -> None:
        from app.premium.proto import premium_pb2  # type: ignore

        self._ensure_stub().EliminarRisco(
            premium_pb2.RiscoRef(
                tenant_id=tenant_id, id=risco_id, ator=ator_pb(premium_pb2, ator)
            )
        )

    def reavaliar(
        self,
        tenant_id: str,
        risco_id: str,
        dados: dict,
        por_id: str,
        por_nome: str,
        ator: dict | None = None,
    ) -> dict:
        from app.premium.proto import premium_pb2  # type: ignore

        resp = self._ensure_stub().Reavaliar(
            premium_pb2.ReavaliarReq(
                tenant_id=tenant_id,
                risco_id=risco_id,
                probabilidade=int(dados.get("probabilidade", 1)),
                impacto=int(dados.get("impacto", 1)),
                justificacao=dados.get("justificacao", ""),
                avaliador_id=por_id,
                avaliador_nome=por_nome,
                ator=ator_pb(premium_pb2, ator),
            )
        )
        return _risco_to_dict(resp)

    def listar_avaliacoes(self, tenant_id: str, risco_id: str) -> list[dict]:
        from app.premium.proto import premium_pb2  # type: ignore

        resp = self._ensure_stub().ListarAvaliacoes(
            premium_pb2.RiscoRef(tenant_id=tenant_id, id=risco_id)
        )
        return [_avaliacao_to_dict(a) for a in resp.avaliacoes]

    def guardar_tratamento(
        self,
        tenant_id: str,
        risco_id: str,
        dados: dict,
        trat_id: str = "",
        ator: dict | None = None,
    ) -> dict:
        from app.premium.proto import premium_pb2  # type: ignore

        pb = premium_pb2.Tratamento(
            id=trat_id,
            tenant_id=tenant_id,
            risco_id=risco_id,
            tipo=dados.get("tipo", "mitigar"),
            controlo_id=dados.get("controlo_id", ""),
            descricao=dados.get("descricao", ""),
            estado=dados.get("estado", "planeado"),
            prioridade=int(dados.get("prioridade", 0)),
            data_alvo=dados.get("data_alvo") or "",
            responsavel_id=dados.get("responsavel_id", ""),
            responsavel_nome=dados.get("responsavel_nome", ""),
            ator=ator_pb(premium_pb2, ator),
        )
        return _tratamento_to_dict(self._ensure_stub().GuardarTratamento(pb))

    def eliminar_tratamento(
        self, tenant_id: str, risco_id: str, trat_id: str, ator: dict | None = None
    ) -> None:
        from app.premium.proto import premium_pb2  # type: ignore

        self._ensure_stub().EliminarTratamento(
            premium_pb2.TratamentoRef(
                tenant_id=tenant_id,
                id=trat_id,
                risco_id=risco_id,
                ator=ator_pb(premium_pb2, ator),
            )
        )

    def obter_painel(self, tenant_id: str, maturidades: dict | None = None) -> dict:
        from app.premium.proto import premium_pb2  # type: ignore

        resp = self._ensure_stub().ObterPainel(
            premium_pb2.PainelRiscoReq(
                tenant_id=tenant_id, maturidade_controlos=_mapa_de_maturidades(maturidades)
            )
        )
        return _painel_to_dict(resp)

    def obter_definicoes(self, tenant_id: str) -> dict:
        from app.premium.proto import premium_pb2  # type: ignore

        resp = self._ensure_stub().ObterDefinicoes(premium_pb2.DefinicoesReq(tenant_id=tenant_id))
        return _definicoes_to_dict(resp)

    def guardar_definicoes(
        self, tenant_id: str, dados: dict, ator: dict | None = None
    ) -> dict:
        from app.premium.proto import premium_pb2  # type: ignore

        pb = premium_pb2.Definicoes(
            tenant_id=tenant_id,
            aprovador=dados.get("aprovador", ""),
            data_aprovacao=dados.get("data_aprovacao", "") or "",
            ator=ator_pb(premium_pb2, ator),
        )
        # Só o que veio: um número não enviado leva o valor por omissão do sidecar
        # (um 0 enviado é um valor, e o sidecar recusa-o).
        for campo in _NUMEROS_DAS_DEFINICOES:
            if dados.get(campo) is not None:
                setattr(pb, campo, int(dados[campo]))
        return _definicoes_to_dict(self._ensure_stub().GuardarDefinicoes(pb))

    def obter_atencao(self, tenant_id: str, locale: str) -> list[dict]:
        from app.premium.proto import premium_pb2  # type: ignore

        resp = self._ensure_stub().ObterAtencao(
            premium_pb2.AtencaoReq(tenant_id=tenant_id, locale=locale)
        )
        return [
            {"codigo": i.codigo, "total": i.total, "severidade": i.severidade}
            for i in resp.itens
        ]

    def gerar_documento(
        self, tenant_id: str, tipo: str, locale: str, maturidades: dict | None = None
    ) -> dict:
        from app.premium.proto import premium_pb2  # type: ignore

        resp = self._ensure_stub().GerarDocumento(
            premium_pb2.DocumentoReq(
                tenant_id=tenant_id,
                tipo=tipo,
                locale=locale,
                maturidade_controlos=_mapa_de_maturidades(maturidades),
            )
        )
        return documento_to_dict(resp)


@lru_cache
def get_risco_client() -> RiscoClient | None:
    """Singleton do cliente de risco, ou None se o premium estiver desligado."""
    settings = get_settings()
    if not settings.PREMIUM_ENABLED or not settings.PREMIUM_SIDECAR_ADDR:
        return None
    return RiscoClient(settings.PREMIUM_SIDECAR_ADDR)
