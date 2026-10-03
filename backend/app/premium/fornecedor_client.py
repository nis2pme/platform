"""
Cliente gRPC do módulo Cadeia de Abastecimento / Fornecedores
(premium.v1 / FornecedorService).

Isolado (fácil de manter), reutiliza o MESMO canal mTLS ao sidecar
(`criar_canal_sidecar`). O core é passthrough: o sidecar é a autoridade (deriva o
score/classe, valida, faz o tenant-scoping e é dono da premium-data-db). Nenhuma
lógica de domínio aqui.
"""
from __future__ import annotations

from functools import lru_cache

from app.config import get_settings
from app.premium.client import ClienteSidecar


# ── Conversões protobuf → dict ────────────────────────────────────────────────

def _ator_pb(premium_pb2, ator: dict | None):
    """Constrói a message Ator (identidade de quem age) para os RPCs de escrita.
    None → campo ausente (o sidecar trata como âmbito total)."""
    if not ator:
        return None
    return premium_pb2.Ator(
        id=ator.get("id", ""),
        nome=ator.get("nome", ""),
        ambito=ator.get("ambito", ""),
    )


def _fornecedor_to_dict(pb) -> dict:
    return {
        "id": pb.id,
        "ativo_id": pb.ativo_id or None,
        "ativo_nome": pb.ativo_nome or None,
        "nome": pb.nome,
        "servico": pb.servico,
        "contacto": pb.contacto,
        "criticidade": pb.criticidade,
        "estado": pb.estado,
        "due_diligence": pb.due_diligence,
        "due_diligence_nota": pb.due_diligence_nota,
        "due_diligence_data": pb.due_diligence_data or None,
        "requisitos": dict(pb.requisitos),
        "pessoal_chave": pb.pessoal_chave,
        "termino_nota": pb.termino_nota,
        "acessos_revogados": pb.acessos_revogados,
        "dados_destino": pb.dados_destino,
        "encerrado_em": pb.encerrado_em or None,
        "risco_score": pb.risco_score,
        "risco_classe": pb.risco_classe,
        "ultima_avaliacao": pb.ultima_avaliacao or None,
        "proxima_avaliacao": pb.proxima_avaliacao or None,
        "responsavel_id": pb.responsavel_id,
        "responsavel_nome": pb.responsavel_nome,
        "created_at": pb.created_at,
        "updated_at": pb.updated_at,
    }


def _avaliacao_to_dict(pb) -> dict:
    return {
        "id": pb.id,
        "score": pb.score,
        "classe": pb.classe,
        "nota": pb.nota,
        "avaliador_nome": pb.avaliador_nome,
        "data": pb.data,
    }


def _painel_to_dict(pb) -> dict:
    return {
        "total": pb.total,
        "criticos": pb.criticos,
        "risco_alto": pb.risco_alto,
        "por_avaliar": pb.por_avaliar,
        "sem_due_diligence": pb.sem_due_diligence,
    }


def _documento_to_dict(pb) -> dict:
    return {
        "titulo": pb.titulo,
        "subtitulo": pb.subtitulo,
        "data_geracao": pb.data_geracao,
        "controlos": list(pb.controlos),
        "secoes": [
            {
                "titulo": s.titulo,
                "texto": s.texto,
                "cabecalho": list(s.cabecalho),
                "linhas": [list(l.celulas) for l in s.linhas],
            }
            for s in pb.secoes
        ],
    }


class FornecedorClient(ClienteSidecar):
    """Fala com o FornecedorService do sidecar. Stub criado de forma lazy."""

    _NOME_STUB = "FornecedorServiceStub"

    def listar(self, tenant_id: str, estado: str, locale: str, limite: int, offset: int) -> dict:
        from app.premium.proto import premium_pb2  # type: ignore

        resp = self._ensure_stub().ListarFornecedores(
            premium_pb2.ListarFornecedoresReq(
                tenant_id=tenant_id, estado=estado, locale=locale, limite=limite, offset=offset
            )
        )
        return {
            "fornecedores": [_fornecedor_to_dict(f) for f in resp.fornecedores],
            "total": resp.total,
        }

    def obter(self, tenant_id: str, fornecedor_id: str) -> dict:
        from app.premium.proto import premium_pb2  # type: ignore

        resp = self._ensure_stub().ObterFornecedor(
            premium_pb2.FornecedorRef(tenant_id=tenant_id, id=fornecedor_id)
        )
        return _fornecedor_to_dict(resp)

    def guardar(
        self, tenant_id: str, dados: dict, fornecedor_id: str = "", ator: dict | None = None
    ) -> dict:
        from app.premium.proto import premium_pb2  # type: ignore

        pb = premium_pb2.Fornecedor(
            id=fornecedor_id,
            tenant_id=tenant_id,
            ativo_id=dados.get("ativo_id") or "",
            nome=dados.get("nome", ""),
            servico=dados.get("servico", ""),
            contacto=dados.get("contacto", ""),
            criticidade=dados.get("criticidade", ""),
            estado=dados.get("estado", "ativo"),
            due_diligence=bool(dados.get("due_diligence", False)),
            due_diligence_nota=dados.get("due_diligence_nota", ""),
            due_diligence_data=dados.get("due_diligence_data", "") or "",
            requisitos={k: str(v) for k, v in (dados.get("requisitos") or {}).items()},
            pessoal_chave=dados.get("pessoal_chave", ""),
            termino_nota=dados.get("termino_nota", ""),
            acessos_revogados=bool(dados.get("acessos_revogados", False)),
            dados_destino=dados.get("dados_destino", ""),
            encerrado_em=dados.get("encerrado_em", "") or "",
            responsavel_id=dados.get("responsavel_id", ""),
            responsavel_nome=dados.get("responsavel_nome", ""),
            ator=_ator_pb(premium_pb2, ator),
        )
        return _fornecedor_to_dict(self._ensure_stub().GuardarFornecedor(pb))

    def eliminar(self, tenant_id: str, fornecedor_id: str, ator: dict | None = None) -> None:
        from app.premium.proto import premium_pb2  # type: ignore

        self._ensure_stub().EliminarFornecedor(
            premium_pb2.FornecedorRef(
                tenant_id=tenant_id, id=fornecedor_id, ator=_ator_pb(premium_pb2, ator)
            )
        )

    def listar_questionario(self, tenant_id: str, locale: str) -> list[dict]:
        from app.premium.proto import premium_pb2  # type: ignore

        resp = self._ensure_stub().ListarQuestionario(
            premium_pb2.QuestionarioReq(tenant_id=tenant_id, locale=locale)
        )
        return [
            {"chave": p.chave, "texto": p.texto, "ajuda": p.ajuda} for p in resp.perguntas
        ]

    def avaliar(
        self,
        tenant_id: str,
        fornecedor_id: str,
        respostas: dict,
        nota: str,
        avaliador_id: str,
        avaliador_nome: str,
        ator: dict | None = None,
    ) -> dict:
        from app.premium.proto import premium_pb2  # type: ignore

        resp = self._ensure_stub().AvaliarFornecedor(
            premium_pb2.AvaliacaoFornecedorReq(
                tenant_id=tenant_id,
                fornecedor_id=fornecedor_id,
                respostas={k: int(v) for k, v in (respostas or {}).items()},
                nota=nota,
                avaliador_id=avaliador_id,
                avaliador_nome=avaliador_nome,
                ator=_ator_pb(premium_pb2, ator),
            )
        )
        return _fornecedor_to_dict(resp)

    def listar_avaliacoes(self, tenant_id: str, fornecedor_id: str) -> list[dict]:
        from app.premium.proto import premium_pb2  # type: ignore

        resp = self._ensure_stub().ListarAvaliacoesFornecedor(
            premium_pb2.FornecedorRef(tenant_id=tenant_id, id=fornecedor_id)
        )
        return [_avaliacao_to_dict(a) for a in resp.avaliacoes]

    def obter_painel(self, tenant_id: str) -> dict:
        from app.premium.proto import premium_pb2  # type: ignore

        resp = self._ensure_stub().ObterPainelFornecedor(
            premium_pb2.PainelFornecedorReq(tenant_id=tenant_id)
        )
        return _painel_to_dict(resp)

    def gerar_documento(self, tenant_id: str, tipo: str, locale: str) -> dict:
        from app.premium.proto import premium_pb2  # type: ignore

        resp = self._ensure_stub().GerarDocumento(
            premium_pb2.DocumentoReq(tenant_id=tenant_id, tipo=tipo, locale=locale)
        )
        return _documento_to_dict(resp)


@lru_cache
def get_fornecedor_client() -> FornecedorClient | None:
    """Singleton do cliente de fornecedores, ou None se o premium estiver desligado."""
    settings = get_settings()
    if not settings.PREMIUM_ENABLED or not settings.PREMIUM_SIDECAR_ADDR:
        return None
    return FornecedorClient(settings.PREMIUM_SIDECAR_ADDR)
